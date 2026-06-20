from __future__ import annotations

import hashlib
import math
import os
import shutil
import uuid
from pathlib import Path
from typing import Any, Callable

from .operation_atoms import ATOM_BY_ID
from .svg_geometry import compile_svg_object


SCHEMA_VERSION = "ps-agent/v1"
WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = WORKSPACE_ROOT / "backend"
RUNTIME_ROOT = BACKEND_ROOT / "runtime"
ASSET_ROOT = RUNTIME_ROOT / "assets"
DEFAULT_DESIGN_ASSET_ROOT = WORKSPACE_ROOT / "design_assets" / "inbox"
DEFAULT_EXPORT_ROOT = RUNTIME_ROOT / "design_exports"
SUPPORTED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff", ".bmp", ".psd"}
DESIGN_TRIGGER_KEYWORDS = {
    "poster", "cover", "card", "panel", "collage", "layout", "magazine", "commercial", "banner",
    "海报", "封面", "卡片", "面板", "拼图", "排版", "加标题", "杂志", "商业物料", "素材编排", "标题遮挡",
}
PHOTO_ONLY_KEYWORDS = {"调色", "修肤", "修图", "白柔", "丁达尔", "去瑕疵", "肤色", "局部修"}
DESIGN_STAGE_POOL = [
    "asset_ingestion",
    "design_research",
    "design_lock",
    "canvas_setup",
    "mask_preparation",
    "layout_blockout",
    "asset_placement",
    "typography",
    "overlap_and_depth",
    "visual_unification",
    "final_review",
    "export_package",
]
DESIGN_NODE_TYPES = {
    "canvas",
    "group",
    "image",
    "cutout",
    "text",
    "shape",
    "path",
    "adjustment",
    "mask",
    "generated_asset",
    "vector_object",
}
DESIGN_EDGE_TYPES = {"parent", "above", "below", "clip_to", "mask_of", "align_to", "overlap_protect"}
GRAPH_LOWERABLE_EDGE_TYPES = {"above", "below", "clip_to", "mask_of"}
GRAPH_ONLY_EDGE_TYPES = {"parent", "align_to", "overlap_protect"}


def _error(code: str, message: str, details: Any | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "status": "error",
        "schema_version": SCHEMA_VERSION,
        "error": {"code": code, "message": message},
    }
    if details is not None:
        payload["error"]["details"] = details
    return payload


def _safe_part(value: str, default: str = "asset") -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in str(value))
    cleaned = cleaned.strip("._")
    return cleaned or default


def _safe_step_id(value: str, default: str = "step") -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in str(value))
    cleaned = cleaned.strip("._-")
    return (cleaned or default)[:64]


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _node_type(node: dict[str, Any]) -> str:
    return str(node.get("node_type") or node.get("type") or "")


def _edge_type(edge: dict[str, Any]) -> str:
    return str(edge.get("edge_type") or edge.get("type") or "")


def _edge_from(edge: dict[str, Any]) -> str:
    return str(edge.get("from") or edge.get("source") or edge.get("source_node_id") or "")


def _edge_to(edge: dict[str, Any]) -> str:
    return str(edge.get("to") or edge.get("target") or edge.get("target_node_id") or "")


def _bbox_from_node(node: dict[str, Any], fallback: dict[str, Any] | None = None) -> dict[str, float]:
    bbox = node.get("bbox") if isinstance(node.get("bbox"), dict) else fallback or {}
    return {
        "x": float(bbox.get("x", 0)),
        "y": float(bbox.get("y", 0)),
        "width": float(bbox.get("width", bbox.get("w", 0))),
        "height": float(bbox.get("height", bbox.get("h", 0))),
    }


def _looks_like_design(goal: str, design_type: str) -> bool:
    haystack = f"{goal} {design_type}".lower()
    if any(keyword.lower() in haystack for keyword in DESIGN_TRIGGER_KEYWORDS):
        return True
    if any(keyword.lower() in haystack for keyword in PHOTO_ONLY_KEYWORDS):
        return False
    return bool(design_type and design_type not in {"photo_grade", "photo_edit", "simple_image_adjust"})


def _design_overlay(payload: dict[str, Any], goal: str, design_type: str) -> dict[str, Any]:
    provided = payload.get("design_overlay")
    if isinstance(provided, dict):
        overlay = dict(provided)
    else:
        overlay = {}
    overlay.setdefault("enabled", _looks_like_design(goal, design_type))
    overlay.setdefault(
        "reason",
        "Triggered by design/composition intent" if overlay["enabled"] else "No design composition intent detected",
    )
    overlay.setdefault("role", "planning_overlay")
    overlay.setdefault("execution_policy", "shared_operation_and_selection_atoms_only")
    return overlay


def _default_design_brief(payload: dict[str, Any], goal: str, design_type: str) -> dict[str, Any]:
    provided = payload.get("design_brief")
    if isinstance(provided, dict):
        brief = dict(provided)
    else:
        brief = {}
    brief.setdefault("goal", goal)
    brief.setdefault("design_type", design_type)
    brief.setdefault("audience", payload.get("audience") or "")
    brief.setdefault("main_message", payload.get("main_message") or "")
    brief.setdefault("style_references", _as_list(payload.get("style_references")))
    brief.setdefault("forbidden", _as_list(payload.get("forbidden")))
    brief.setdefault("asset_roles", _as_list(payload.get("asset_roles")))
    brief.setdefault("output_use", payload.get("output_use") or "")
    brief.setdefault("notes", [])
    return brief


def _default_design_lock(payload: dict[str, Any], canvas: dict[str, Any], asset_manifest: list[Any]) -> dict[str, Any]:
    provided = payload.get("design_lock")
    if isinstance(provided, dict):
        lock = dict(provided)
    else:
        lock = {}
    width = int(canvas.get("width", 1080))
    height = int(canvas.get("height", 1350))
    margin_x = round(width * 0.06, 2)
    margin_y = round(height * 0.06, 2)
    lock.setdefault("canvas", canvas)
    lock.setdefault("safe_margins", {"top": margin_y, "right": margin_x, "bottom": margin_y, "left": margin_x})
    lock.setdefault(
        "main_visual_area",
        {"x": margin_x, "y": margin_y, "width": max(1, width - 2 * margin_x), "height": max(1, height - 2 * margin_y)},
    )
    lock.setdefault("typography", {"hierarchy": [], "min_font_size": 8, "overflow_policy": "review_and_revise"})
    lock.setdefault("palette", [])
    lock.setdefault("font_strategy", {"primary": "", "fallback": "Photoshop default; review required"})
    lock.setdefault("asset_manifest", asset_manifest)
    lock.setdefault("overlap_rules", [])
    lock.setdefault("export", {"format": payload.get("format", "png"), "max_side": payload.get("max_side", 4096), "quality": payload.get("quality", 10)})
    return lock


def _default_layer_graph(payload: dict[str, Any], workflow_id: str, canvas: dict[str, Any], canvas_stage_id: str) -> dict[str, Any]:
    provided = payload.get("layer_graph")
    if isinstance(provided, dict):
        graph = dict(provided)
    else:
        graph = {}
    width = int(canvas.get("width", 1080))
    height = int(canvas.get("height", 1350))
    graph.setdefault("schema_version", SCHEMA_VERSION)
    graph.setdefault("graph_id", f"lg-{workflow_id}")
    graph.setdefault("allowed_node_types", sorted(DESIGN_NODE_TYPES))
    graph.setdefault("allowed_edge_types", sorted(DESIGN_EDGE_TYPES))
    graph.setdefault(
        "nodes",
        [
            {
                "node_id": "canvas",
                "node_type": "canvas",
                "role": "canvas",
                "bbox": {"x": 0, "y": 0, "width": width, "height": height},
                "z_order": 0,
                "stage_id": canvas_stage_id,
                "review_regions": ["global"],
            }
        ],
    )
    graph.setdefault("edges", [])
    graph.setdefault("notes", ["Codex owns visual layout decisions; backend only validates and lowers concrete nodes."])
    return graph


def _asset_root() -> Path:
    configured = os.environ.get("PS_AGENT_DESIGN_ASSET_ROOT")
    return Path(configured).expanduser().resolve() if configured else DEFAULT_DESIGN_ASSET_ROOT.resolve()


def _resolve_scan_root(payload: dict[str, Any]) -> Path:
    base = _asset_root()
    requested = payload.get("root_path") or payload.get("folder") or payload.get("asset_root")
    if requested:
        root = Path(str(requested)).expanduser().resolve()
    else:
        root = base
    try:
        root.relative_to(base)
    except ValueError as exc:
        raise ValueError(f"Design asset path must stay inside {base}") from exc
    return root


def _image_info(path: Path) -> dict[str, Any]:
    try:
        from PIL import Image, ImageStat
    except Exception as exc:  # pragma: no cover - depends on optional environment
        return {
            "readable": False,
            "error": {"code": "pillow_unavailable", "message": str(exc)},
        }

    try:
        with Image.open(path) as image:
            image.load()
            rgba = image.convert("RGBA")
            stat = ImageStat.Stat(rgba.resize((1, 1)))
            avg = [round(float(value), 2) for value in stat.mean[:4]]
            alpha_extrema = rgba.getchannel("A").getextrema()
            return {
                "readable": True,
                "width": int(image.width),
                "height": int(image.height),
                "mode": image.mode,
                "format": image.format,
                "has_alpha": bool(alpha_extrema[0] < 255),
                "aspect_ratio": round(float(image.width) / max(1.0, float(image.height)), 6),
                "average_rgba": avg,
            }
    except Exception as exc:
        return {
            "readable": False,
            "error": {"code": "image_open_failed", "message": str(exc)},
        }


def _dominant_colors(path: Path, max_colors: int = 6) -> list[dict[str, Any]]:
    try:
        from PIL import Image
    except Exception:
        return []
    try:
        with Image.open(path) as image:
            sample = image.convert("RGB")
            sample.thumbnail((96, 96))
            palette = sample.convert("P", palette=Image.Palette.ADAPTIVE, colors=max_colors)
            colors = palette.getcolors(max_colors * 96 * 96) or []
            total = sum(count for count, _ in colors) or 1
            rgb_palette = palette.getpalette() or []
            result = []
            for count, palette_index in sorted(colors, reverse=True)[:max_colors]:
                offset = int(palette_index) * 3
                rgb = rgb_palette[offset:offset + 3]
                if len(rgb) == 3:
                    result.append({
                        "rgb": rgb,
                        "hex": "#{:02x}{:02x}{:02x}".format(*rgb),
                        "ratio": round(count / total, 4),
                    })
            return result
    except Exception:
        return []


def _thumbnail(path: Path, scan_dir: Path, asset_id: str, max_side: int) -> dict[str, Any] | None:
    try:
        from PIL import Image
    except Exception:
        return None
    try:
        with Image.open(path) as image:
            preview = image.convert("RGBA") if image.mode in {"RGBA", "LA", "P"} else image.convert("RGB")
            preview.thumbnail((max_side, max_side))
            thumb_name = f"{asset_id}-thumb.png"
            thumb_path = scan_dir / thumb_name
            preview.save(thumb_path)
            return {
                "path": str(thumb_path),
                "relative": f"{scan_dir.name}/{thumb_name}",
                "width": preview.width,
                "height": preview.height,
                "mime_type": "image/png",
            }
    except Exception:
        return None


def _contact_sheet(thumbs: list[dict[str, Any]], scan_dir: Path, scan_id: str, columns: int) -> dict[str, Any] | None:
    if not thumbs:
        return None
    try:
        from PIL import Image, ImageDraw
    except Exception:
        return None
    cell_w = 220
    cell_h = 260
    columns = max(1, min(columns, 8))
    rows = math.ceil(len(thumbs) / columns)
    sheet = Image.new("RGB", (columns * cell_w, rows * cell_h), "white")
    draw = ImageDraw.Draw(sheet)
    for index, item in enumerate(thumbs):
        path = item.get("path")
        label = item.get("asset_id") or str(index + 1)
        if not path:
            continue
        try:
            with Image.open(path) as thumb:
                thumb = thumb.convert("RGB")
                x = (index % columns) * cell_w + (cell_w - thumb.width) // 2
                y = (index // columns) * cell_h + 12
                sheet.paste(thumb, (x, y))
                draw.text(((index % columns) * cell_w + 8, (index // columns) * cell_h + cell_h - 34), label, fill=(0, 0, 0))
        except Exception:
            continue
    name = f"{scan_id}-contact-sheet.jpg"
    out = scan_dir / name
    sheet.save(out, quality=88)
    return {
        "path": str(out),
        "relative": f"{scan_dir.name}/{name}",
        "width": sheet.width,
        "height": sheet.height,
        "mime_type": "image/jpeg",
    }


def _asset_payload(relative: str, path: Path, asset_url_builder: Callable[[str], str]) -> dict[str, Any]:
    return {
        "id": relative,
        "uri": asset_url_builder(relative),
        "path": str(path),
        "mime_type": "image/png" if path.suffix.lower() == ".png" else "image/jpeg",
        "size_bytes": path.stat().st_size if path.exists() else None,
    }


def scan_asset_library(payload: dict[str, Any], asset_url_builder: Callable[[str], str]) -> dict[str, Any]:
    try:
        root = _resolve_scan_root(payload)
    except ValueError as exc:
        return _error("design_asset_root_forbidden", str(exc))

    recursive = bool(payload.get("recursive", False))
    max_assets = max(1, min(int(payload.get("max_assets", 80)), 300))
    thumb_max_side = max(64, min(int(payload.get("thumbnail_max_side", 256)), 1024))
    columns = max(1, min(int(payload.get("contact_sheet_columns", 5)), 8))
    scan_id = "design-scan-" + uuid.uuid4().hex[:10]
    scan_dir = ASSET_ROOT / scan_id
    scan_dir.mkdir(parents=True, exist_ok=True)

    if not root.exists():
        root.mkdir(parents=True, exist_ok=True)

    pattern = "**/*" if recursive else "*"
    files = [
        path for path in root.glob(pattern)
        if path.is_file() and path.suffix.lower() in SUPPORTED_IMAGE_EXTENSIONS
    ][:max_assets]

    assets: list[dict[str, Any]] = []
    thumb_records: list[dict[str, Any]] = []
    for index, path in enumerate(files, start=1):
        rel = path.relative_to(_asset_root())
        digest = hashlib.sha1(str(rel).encode("utf-8", errors="replace")).hexdigest()[:10]
        asset_id = f"asset-{index:03d}-{digest}"
        info = _image_info(path)
        asset: dict[str, Any] = {
            "asset_id": asset_id,
            "filename": path.name,
            "path": str(path),
            "relative_path": str(rel),
            "extension": path.suffix.lower(),
            "size_bytes": path.stat().st_size,
            "mtime": path.stat().st_mtime,
            "readable": info.get("readable", False),
        }
        asset.update({key: value for key, value in info.items() if key not in {"readable"}})
        thumb = _thumbnail(path, scan_dir, asset_id, thumb_max_side) if info.get("readable") else None
        if thumb:
            thumb_payload = _asset_payload(thumb["relative"], Path(thumb["path"]), asset_url_builder)
            thumb_payload.update({"width": thumb["width"], "height": thumb["height"]})
            asset["thumbnail"] = thumb_payload
            thumb_records.append({"path": thumb["path"], "asset_id": asset_id})
        assets.append(asset)

    contact = _contact_sheet(thumb_records, scan_dir, scan_id, columns)
    contact_payload = None
    if contact:
        contact_payload = _asset_payload(contact["relative"], Path(contact["path"]), asset_url_builder)
        contact_payload.update({"width": contact["width"], "height": contact["height"]})

    return {
        "status": "ok",
        "schema_version": SCHEMA_VERSION,
        "scan_id": scan_id,
        "asset_root": str(_asset_root()),
        "scan_root": str(root),
        "asset_count": len(assets),
        "assets": assets,
        "contact_sheet": contact_payload,
        "warnings": [] if assets else [{"code": "no_assets_found", "message": "No supported image assets were found."}],
    }


def analyze_design_assets(payload: dict[str, Any], asset_url_builder: Callable[[str], str]) -> dict[str, Any]:
    scan = payload.get("scan_result")
    if not isinstance(scan, dict):
        scan = scan_asset_library(payload, asset_url_builder)
        if scan.get("status") != "ok":
            return scan
    assets = []
    for asset in scan.get("assets", []):
        if not isinstance(asset, dict):
            continue
        path = Path(str(asset.get("path") or ""))
        analysis = {
            "asset_id": asset.get("asset_id"),
            "filename": asset.get("filename"),
            "path": asset.get("path"),
            "dimensions": {"width": asset.get("width"), "height": asset.get("height")},
            "aspect_ratio": asset.get("aspect_ratio"),
            "has_alpha": asset.get("has_alpha"),
            "average_rgba": asset.get("average_rgba"),
            "dominant_colors": _dominant_colors(path, int(payload.get("max_colors", 6))) if path.exists() else [],
            "suggested_roles": [],
            "risk_flags": [],
        }
        ratio = asset.get("aspect_ratio")
        if isinstance(ratio, (int, float)):
            if ratio > 1.6:
                analysis["suggested_roles"].append("wide_background_or_banner")
            elif ratio < 0.75:
                analysis["suggested_roles"].append("portrait_main_visual")
            else:
                analysis["suggested_roles"].append("card_or_subject")
        if asset.get("has_alpha"):
            analysis["suggested_roles"].append("cutout_or_overlay")
        if not asset.get("readable", True):
            analysis["risk_flags"].append("unreadable")
        assets.append(analysis)
    return {
        "status": "ok",
        "schema_version": SCHEMA_VERSION,
        "scan_id": scan.get("scan_id"),
        "asset_count": len(assets),
        "asset_metrics": assets,
        "contact_sheet": scan.get("contact_sheet"),
    }


def prepare_asset_variant(payload: dict[str, Any], asset_url_builder: Callable[[str], str]) -> dict[str, Any]:
    try:
        root = _asset_root()
        path = Path(str(payload.get("asset_path") or payload.get("path") or "")).expanduser().resolve()
        path.relative_to(root)
    except Exception as exc:
        return _error("invalid_asset_path", "asset_path must point to an image inside the configured design asset root.", {"message": str(exc)})
    if not path.exists():
        return _error("asset_not_found", f"Asset file does not exist: {path}")

    variant_id = str(payload.get("variant_id") or f"design-variant-{uuid.uuid4().hex[:10]}")
    out_dir = ASSET_ROOT / _safe_part(variant_id, "design-variant")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_name = _safe_part(path.stem, "asset") + ".png"
    out_path = out_dir / out_name
    try:
        from PIL import Image
        with Image.open(path) as image:
            image.load()
            working = image.convert("RGBA")
            crop = payload.get("crop")
            if isinstance(crop, dict):
                x = int(crop.get("x", 0))
                y = int(crop.get("y", 0))
                w = int(crop.get("width", working.width))
                h = int(crop.get("height", working.height))
                working = working.crop((x, y, x + w, y + h))
            max_side = payload.get("max_side")
            if max_side:
                side = max(64, min(int(max_side), 8192))
                working.thumbnail((side, side))
            working.save(out_path)
    except Exception as exc:
        return _error("asset_variant_failed", str(exc), {"asset_path": str(path)})

    relative = f"{out_dir.name}/{out_name}"
    asset = _asset_payload(relative, out_path, asset_url_builder)
    try:
        from PIL import Image
        with Image.open(out_path) as image:
            asset["width"] = image.width
            asset["height"] = image.height
    except Exception:
        pass
    return {
        "status": "ok",
        "schema_version": SCHEMA_VERSION,
        "variant_id": out_dir.name,
        "source_path": str(path),
        "asset": asset,
    }


def create_design_plan(payload: dict[str, Any]) -> dict[str, Any]:
    goal = str(payload.get("goal") or payload.get("user_goal") or "").strip()
    design_type = str(payload.get("design_type") or "open_design").strip() or "open_design"
    workflow_id = str(payload.get("workflow_id") or f"wf-design-{uuid.uuid4().hex[:8]}")
    canvas = payload.get("canvas") if isinstance(payload.get("canvas"), dict) else {
        "width": 1080,
        "height": 1350,
        "resolution": 72,
        "background": {"rgb": [255, 255, 255]},
    }
    asset_manifest = payload.get("asset_manifest") or payload.get("assets") or []
    if not isinstance(asset_manifest, list):
        asset_manifest = []
    stage_ids = payload.get("stage_ids") if isinstance(payload.get("stage_ids"), list) else DESIGN_STAGE_POOL
    canvas_stage_id = "canvas_setup" if "canvas_setup" in stage_ids else str(stage_ids[0] if stage_ids else "canvas_setup")
    design_overlay = _design_overlay(payload, goal, design_type)
    design_brief = _default_design_brief(payload, goal, design_type)
    design_lock = _default_design_lock(payload, canvas, asset_manifest)
    layer_graph = _default_layer_graph(payload, workflow_id, canvas, canvas_stage_id)
    return {
        "status": "ok",
        "schema_version": SCHEMA_VERSION,
        "design_plan": {
            "schema_version": SCHEMA_VERSION,
            "workflow_id": workflow_id,
            "goal": goal,
            "design_type": design_type,
            "design_overlay": design_overlay,
            "design_brief": design_brief,
            "design_lock": design_lock,
            "canvas": canvas,
            "asset_manifest": asset_manifest,
            "asset_brief": payload.get("asset_brief") if isinstance(payload.get("asset_brief"), dict) else {},
            "research_findings": payload.get("research_findings") or [],
            "layer_graph": layer_graph,
            "stages": [
                {
                    "stage_id": stage,
                    "route": "design_overlay",
                    "objective": stage.replace("_", " "),
                    "expected_result": "Stage result must be visible in exported preview or recorded as structured design metadata.",
                    "operation_recipe": None,
                    "selection_recipe": None,
                    "review_regions": ["global"],
                    "pass_criteria": ["preview exported", "no missing required asset ids"],
                    "rollback_target": f"Codex Agent - {workflow_id} - {stage}",
                }
                for stage in stage_ids
            ],
        },
        "notes": [
            "Design overlay is a planning layer; execution must use shared operation/selection atoms.",
            "Codex must fill layer_graph and operation_recipe/selection_recipe for executable stages.",
            "Use ps_scan_asset_library and ps_analyze_design_assets before layout_composition.",
        ],
    }


def _validate_canvas(canvas: Any, errors: list[str], path: str) -> None:
    if not isinstance(canvas, dict):
        errors.append(f"{path} must be an object")
        return
    for key in ("width", "height"):
        value = canvas.get(key)
        if not isinstance(value, int) or not 16 <= value <= 30000:
            errors.append(f"{path}.{key} must be an integer 16..30000")


def _validate_bbox(bbox: Any, errors: list[str], path: str) -> None:
    if not isinstance(bbox, dict):
        errors.append(f"{path} must be an object")
        return
    for key in ("x", "y", "width", "height"):
        if not isinstance(bbox.get(key), (int, float)):
            errors.append(f"{path}.{key} must be a number")
    if isinstance(bbox.get("width"), (int, float)) and float(bbox["width"]) <= 0:
        errors.append(f"{path}.width must be > 0")
    if isinstance(bbox.get("height"), (int, float)) and float(bbox["height"]) <= 0:
        errors.append(f"{path}.height must be > 0")


def _validate_design_lock(lock: Any, errors: list[str], warnings: list[dict[str, str]]) -> None:
    required = {
        "canvas": dict,
        "safe_margins": dict,
        "main_visual_area": dict,
        "typography": dict,
        "palette": list,
        "font_strategy": dict,
        "asset_manifest": list,
        "overlap_rules": list,
        "export": dict,
    }
    if not isinstance(lock, dict):
        errors.append("design_plan.design_lock must be an object")
        return
    for key, expected_type in required.items():
        if key not in lock:
            errors.append(f"design_plan.design_lock.{key} is required")
        elif not isinstance(lock[key], expected_type):
            errors.append(f"design_plan.design_lock.{key} must be {expected_type.__name__}")
    if isinstance(lock.get("canvas"), dict):
        _validate_canvas(lock["canvas"], errors, "design_plan.design_lock.canvas")
    if isinstance(lock.get("main_visual_area"), dict):
        _validate_bbox(lock["main_visual_area"], errors, "design_plan.design_lock.main_visual_area")
    font_strategy = lock.get("font_strategy")
    if isinstance(font_strategy, dict) and not str(font_strategy.get("primary") or "").strip():
        warnings.append({"code": "font_primary_not_locked", "message": "design_lock.font_strategy.primary is empty; Photoshop may substitute fonts."})


def _validate_layer_graph(graph: Any, stage_ids: set[str], errors: list[str], warnings: list[dict[str, str]]) -> None:
    if not isinstance(graph, dict):
        errors.append("design_plan.layer_graph must be an object")
        return
    nodes = graph.get("nodes")
    edges = graph.get("edges")
    if not isinstance(nodes, list):
        errors.append("design_plan.layer_graph.nodes must be an array")
        return
    if not isinstance(edges, list):
        errors.append("design_plan.layer_graph.edges must be an array")
        return
    node_ids: set[str] = set()
    for index, node in enumerate(nodes):
        path = f"design_plan.layer_graph.nodes[{index}]"
        if not isinstance(node, dict):
            errors.append(f"{path} must be an object")
            continue
        node_id = str(node.get("node_id") or "")
        if not node_id:
            errors.append(f"{path}.node_id is required")
        if node_id in node_ids:
            errors.append(f"Duplicate layer_graph node_id: {node_id}")
        node_ids.add(node_id)
        node_type = _node_type(node)
        if node_type not in DESIGN_NODE_TYPES:
            errors.append(f"{path}.node_type is unsupported: {node_type}")
        if not str(node.get("role") or "").strip():
            errors.append(f"{path}.role is required")
        _validate_bbox(node.get("bbox"), errors, f"{path}.bbox")
        if not isinstance(node.get("z_order"), (int, float)):
            errors.append(f"{path}.z_order must be a number")
        stage_id = str(node.get("stage_id") or "")
        if not stage_id:
            errors.append(f"{path}.stage_id is required")
        elif stage_ids and stage_id not in stage_ids:
            errors.append(f"{path}.stage_id references unknown stage: {stage_id}")
        review_regions = node.get("review_regions")
        if not isinstance(review_regions, list) or not review_regions:
            errors.append(f"{path}.review_regions must be a non-empty array")
    for index, edge in enumerate(edges):
        path = f"design_plan.layer_graph.edges[{index}]"
        if not isinstance(edge, dict):
            errors.append(f"{path} must be an object")
            continue
        edge_type = _edge_type(edge)
        source = _edge_from(edge)
        target = _edge_to(edge)
        if edge_type not in DESIGN_EDGE_TYPES:
            errors.append(f"{path}.edge_type is unsupported: {edge_type}")
        if not source or source not in node_ids:
            errors.append(f"{path}.from/source references unknown node: {source}")
        if not target or target not in node_ids:
            errors.append(f"{path}.to/target references unknown node: {target}")
        if source and target and source == target:
            errors.append(f"{path} cannot reference the same node on both sides")
        if edge_type in GRAPH_ONLY_EDGE_TYPES:
            warnings.append({"code": "graph_only_edge", "message": f"{edge_type} is a planning constraint; it is not automatically lowered unless Codex emits concrete recipe steps."})


def _validate_stage_recipe_atoms(stage: dict[str, Any], path: str, errors: list[str]) -> None:
    recipe = stage.get("operation_recipe")
    if not isinstance(recipe, dict):
        recipe_or_plan = stage.get("recipe_or_plan")
        if isinstance(recipe_or_plan, dict) and isinstance(recipe_or_plan.get("steps"), list):
            recipe = recipe_or_plan
    if not isinstance(recipe, dict):
        return
    steps = recipe.get("steps")
    if not isinstance(steps, list):
        return
    for step_index, step in enumerate(steps):
        if not isinstance(step, dict):
            continue
        atom_id = str(step.get("atom_id") or "")
        atom = ATOM_BY_ID.get(atom_id)
        if not atom:
            errors.append(f"{path}.operation_recipe.steps[{step_index}].atom_id is unknown: {atom_id}")
        elif atom.get("status") == "planned":
            errors.append(f"{path}.operation_recipe.steps[{step_index}].atom_id is planned but not executable yet: {atom_id}")


def validate_design_plan(payload: dict[str, Any]) -> dict[str, Any]:
    plan = payload.get("design_plan") or payload.get("plan")
    errors: list[str] = []
    warnings: list[dict[str, str]] = []
    if not isinstance(plan, dict):
        errors.append("design_plan must be an object")
    else:
        if plan.get("schema_version") != SCHEMA_VERSION:
            errors.append("design_plan.schema_version must be ps-agent/v1")
        if not str(plan.get("workflow_id") or "").strip():
            errors.append("design_plan.workflow_id is required")
        _validate_canvas(plan.get("canvas"), errors, "design_plan.canvas")
        design_overlay = plan.get("design_overlay")
        if design_overlay is not None and not isinstance(design_overlay, dict):
            errors.append("design_plan.design_overlay must be an object when provided")
        design_brief = plan.get("design_brief")
        if not isinstance(design_brief, dict):
            errors.append("design_plan.design_brief must be an object")
        else:
            for key in ("goal", "design_type", "audience", "main_message", "style_references", "forbidden", "asset_roles", "output_use"):
                if key not in design_brief:
                    errors.append(f"design_plan.design_brief.{key} is required")
        _validate_design_lock(plan.get("design_lock"), errors, warnings)
        stages = plan.get("stages")
        stage_ids: set[str] = set()
        if not isinstance(stages, list) or not stages:
            errors.append("design_plan.stages must be a non-empty array")
        else:
            for index, stage in enumerate(stages):
                if not isinstance(stage, dict):
                    errors.append(f"design_plan.stages[{index}] must be an object")
                    continue
                stage_id = str(stage.get("stage_id") or "")
                if not stage_id:
                    errors.append(f"design_plan.stages[{index}].stage_id is required")
                if stage_id in stage_ids:
                    errors.append(f"Duplicate design stage_id: {stage_id}")
                stage_ids.add(stage_id)
                if not stage.get("rollback_target"):
                    errors.append(f"design_plan.stages[{index}].rollback_target is required")
                if stage.get("operation_recipe") is None and stage.get("selection_recipe") is None:
                    warnings.append({"code": "stage_not_executable", "message": f"Stage {stage_id} has no operation_recipe yet."})
                _validate_stage_recipe_atoms(stage, f"design_plan.stages[{index}]", errors)
        _validate_layer_graph(plan.get("layer_graph"), stage_ids, errors, warnings)
    return {
        "status": "ok",
        "schema_version": SCHEMA_VERSION,
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
    }


def _stage_from_plan(plan: dict[str, Any], stage_id: str) -> dict[str, Any] | None:
    for stage in plan.get("stages", []):
        if isinstance(stage, dict) and str(stage.get("stage_id") or "") == stage_id:
            return stage
    return None


def _recipe_from_stage(stage: dict[str, Any]) -> tuple[str | None, dict[str, Any] | None]:
    recipe = stage.get("operation_recipe") or stage.get("recipe")
    if isinstance(recipe, dict):
        return "operation_recipe", recipe
    selection_recipe = stage.get("selection_recipe")
    if isinstance(selection_recipe, dict):
        return "selection_recipe", selection_recipe
    recipe_or_plan = stage.get("recipe_or_plan")
    if isinstance(recipe_or_plan, dict):
        if isinstance(recipe_or_plan.get("operation_recipe"), dict):
            return "operation_recipe", recipe_or_plan["operation_recipe"]
        if isinstance(recipe_or_plan.get("selection_recipe"), dict):
            return "selection_recipe", recipe_or_plan["selection_recipe"]
        if isinstance(recipe_or_plan.get("steps"), list):
            return "operation_recipe", recipe_or_plan
        if isinstance(recipe_or_plan.get("candidates"), list) and isinstance(recipe_or_plan.get("merge_plan"), dict):
            return "selection_recipe", recipe_or_plan
    return None, None


def _layer_ref(step_id: str) -> str:
    return f"$steps.{step_id}.layer_id"


def _asset_params(node: dict[str, Any]) -> dict[str, Any]:
    source = node.get("asset") if isinstance(node.get("asset"), dict) else {}
    params: dict[str, Any] = {}
    for key in ("asset_uri", "uri"):
        if node.get(key) or source.get(key):
            params["asset_uri"] = node.get(key) or source.get(key)
            break
    for key in ("asset_path", "path"):
        if node.get(key) or source.get(key):
            params["asset_path"] = node.get(key) or source.get(key)
            break
    return params


def _node_to_steps(node: dict[str, Any], step_id: str, canvas: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    node_type = _node_type(node)
    bbox = _bbox_from_node(node, {"x": 0, "y": 0, "width": canvas.get("width", 1080), "height": canvas.get("height", 1350)})
    name = str(node.get("name") or node.get("role") or node.get("node_id") or step_id)
    steps: list[dict[str, Any]] = []
    warnings: list[str] = []
    errors: list[str] = []

    if node_type == "canvas":
        params = {
            "name": name,
            "width": int(round(bbox["width"] or canvas.get("width", 1080))),
            "height": int(round(bbox["height"] or canvas.get("height", 1350))),
            "resolution": node.get("resolution", canvas.get("resolution", 72)),
            "background": node.get("background", canvas.get("background", {"rgb": [255, 255, 255]})),
        }
        steps.append({"step_id": step_id, "atom_id": "document.create", "params": params})
    elif node_type == "group":
        steps.append({"step_id": step_id, "atom_id": "layer.create_group", "params": {"name": name}})
    elif node_type in {"image", "cutout", "generated_asset"}:
        params = _asset_params(node)
        if not (params.get("asset_uri") or params.get("asset_path")):
            errors.append(f"missing_asset: node {node.get('node_id')} requires asset_uri or asset_path")
        params.update({"name": name, "x": bbox["x"], "y": bbox["y"]})
        steps.append({"step_id": step_id, "atom_id": "asset.place_embedded", "params": params})
        transform_params = {
            "x": bbox["x"],
            "y": bbox["y"],
            "width": bbox["width"],
            "height": bbox["height"],
        }
        if node.get("rotation") is not None:
            transform_params["rotation"] = node.get("rotation")
        steps.append({"step_id": f"{step_id}_transform", "atom_id": "layer.transform", "target": _layer_ref(step_id), "params": transform_params})
        selection_mask = node.get("selection_mask") if isinstance(node.get("selection_mask"), dict) else None
        if node_type == "cutout" and selection_mask:
            steps.append({"step_id": f"{step_id}_mask", "atom_id": "mask.apply_alpha", "target": _layer_ref(step_id), "params": {"selection_mask": selection_mask}})
    elif node_type == "text":
        text = str(node.get("text") or node.get("content") or "")
        if not text:
            errors.append(f"unsupported_layer_node: text node {node.get('node_id')} requires text/content")
        params = {
            "text": text,
            "name": name,
            "x": bbox["x"],
            "y": bbox["y"],
            "font_size": node.get("font_size", node.get("size", 48)),
            "font": node.get("font", ""),
            "color": node.get("color", {"rgb": [0, 0, 0]}),
        }
        steps.append({"step_id": step_id, "atom_id": "text.create", "params": params})
    elif node_type == "shape":
        shape = str(node.get("shape") or node.get("shape_kind") or "rectangle")
        fill = node.get("fill", node.get("color", {"rgb": [0, 0, 0]}))
        if shape in {"rectangle", "rect"}:
            params = {"name": name, "x": bbox["x"], "y": bbox["y"], "width": bbox["width"], "height": bbox["height"], "fill": fill}
            steps.append({"step_id": step_id, "atom_id": "shape.rectangle", "params": params})
        elif shape == "ellipse":
            params = {"name": name, "x": bbox["x"], "y": bbox["y"], "width": bbox["width"], "height": bbox["height"], "fill": fill}
            steps.append({"step_id": step_id, "atom_id": "shape.ellipse", "params": params})
        elif shape == "polygon":
            points = node.get("points")
            if not isinstance(points, list):
                errors.append(f"unsupported_layer_node: polygon node {node.get('node_id')} requires points")
            steps.append({"step_id": step_id, "atom_id": "shape.polygon", "params": {"name": name, "points": points or [], "fill": fill}})
        elif shape == "star":
            params = {
                "name": name,
                "center_x": node.get("center_x", bbox["x"] + bbox["width"] / 2),
                "center_y": node.get("center_y", bbox["y"] + bbox["height"] / 2),
                "outer_radius": node.get("outer_radius", max(bbox["width"], bbox["height"]) / 2),
                "inner_radius": node.get("inner_radius"),
                "points": node.get("points", 5),
                "fill": fill,
            }
            steps.append({"step_id": step_id, "atom_id": "shape.star", "params": params})
        elif shape == "line":
            params = dict(node.get("params") or {})
            params.setdefault("name", name)
            params.setdefault("fill", fill)
            steps.append({"step_id": step_id, "atom_id": "shape.line", "params": params})
        elif shape == "polyline":
            points = node.get("points")
            if not isinstance(points, list):
                errors.append(f"unsupported_layer_node: polyline node {node.get('node_id')} requires points")
            steps.append({"step_id": step_id, "atom_id": "shape.polyline", "params": {"name": name, "points": points or [], "fill": fill, "width": node.get("width", node.get("stroke_width", 2))}})
        else:
            errors.append(f"unsupported_layer_node: unsupported shape kind {shape}")
    elif node_type == "path":
        points = node.get("points")
        subpaths = node.get("subpaths")
        if not isinstance(points, list) and not isinstance(subpaths, list):
            errors.append(f"unsupported_layer_node: path node {node.get('node_id')} requires points or subpaths")
        params = {"name": name, "points": points, "subpaths": subpaths, "closed": node.get("closed", True), "tolerance": node.get("tolerance")}
        steps.append({"step_id": step_id, "atom_id": "path.create_work_path", "params": {key: value for key, value in params.items() if value is not None}})
        if isinstance(node.get("fill"), dict):
            steps.append({"step_id": f"{step_id}_fill", "atom_id": "shape.path_fill", "params": {"fill": node["fill"], "name": f"{name} Fill"}})
        if isinstance(node.get("stroke"), dict):
            stroke = dict(node["stroke"])
            stroke.setdefault("name", f"{name} Stroke")
            steps.append({"step_id": f"{step_id}_stroke", "atom_id": "path.stroke", "params": stroke})
    elif node_type == "vector_object":
        vector_object = dict(node)
        vector_object.setdefault("object_id", node.get("node_id") or step_id)
        vector_object.setdefault("step_id", step_id)
        vector_object.setdefault("name", name)
        vector_object["bbox"] = bbox
        compiled = compile_svg_object({"vector_object": vector_object})
        if compiled.get("status") != "ok":
            error = compiled.get("error") if isinstance(compiled.get("error"), dict) else {}
            errors.append(f"svg_object_compile_failed: {error.get('message') or 'unknown SVG object error'}")
        else:
            fragment = compiled.get("operation_recipe_fragment") if isinstance(compiled.get("operation_recipe_fragment"), dict) else {}
            steps.extend(fragment.get("steps") if isinstance(fragment.get("steps"), list) else [])
            for warning in compiled.get("warnings") if isinstance(compiled.get("warnings"), list) else []:
                warnings.append(f"svg_object_warning: {warning}")
    elif node_type == "adjustment":
        op = str(node.get("op") or "")
        params = node.get("params") if isinstance(node.get("params"), dict) else {}
        if not op:
            errors.append(f"unsupported_layer_node: adjustment node {node.get('node_id')} requires op")
        steps.append({"step_id": step_id, "atom_id": "adjustment.create", "params": {"op": op, "params": params, "layer": {"name": name}}})
    elif node_type == "mask":
        selection_mask = node.get("selection_mask") if isinstance(node.get("selection_mask"), dict) else None
        target_layer_id = node.get("target_layer_id")
        if not selection_mask and not node.get("use_current_selection"):
            errors.append(f"unsupported_layer_node: mask node {node.get('node_id')} requires selection_mask or use_current_selection")
        atom_id = "mask.apply_current_selection" if node.get("use_current_selection") else "mask.apply_alpha"
        params = {"target_layer_id": target_layer_id}
        if selection_mask:
            params["selection_mask"] = selection_mask
        steps.append({"step_id": step_id, "atom_id": atom_id, "params": params})
    else:
        errors.append(f"unsupported_layer_node: unsupported node type {node_type}")

    return steps, warnings, errors


def _graph_stage_to_operation_recipe(plan: dict[str, Any], stage: dict[str, Any]) -> dict[str, Any]:
    stage_id = str(stage.get("stage_id") or "")
    graph = plan.get("layer_graph") if isinstance(plan.get("layer_graph"), dict) else {}
    canvas = plan.get("canvas") if isinstance(plan.get("canvas"), dict) else {"width": 1080, "height": 1350}
    nodes = [node for node in graph.get("nodes", []) if isinstance(node, dict) and str(node.get("stage_id") or "") == stage_id]
    if not nodes:
        return _error("design_stage_not_executable", f"Design stage {stage_id} has no operation_recipe, selection_recipe, or layer_graph nodes.")

    step_by_node: dict[str, str] = {}
    steps: list[dict[str, Any]] = []
    warnings: list[str] = []
    errors: list[str] = []
    for index, node in enumerate(sorted(nodes, key=lambda item: float(item.get("z_order", 0)))):
        node_id = str(node.get("node_id") or f"node_{index + 1}")
        step_id = _safe_step_id(node_id, f"node_{index + 1}")
        step_by_node[node_id] = step_id
        node_steps, node_warnings, node_errors = _node_to_steps(node, step_id, canvas)
        steps.extend(node_steps)
        warnings.extend(node_warnings)
        errors.extend(node_errors)

    for edge_index, edge in enumerate(_as_list(graph.get("edges"))):
        if not isinstance(edge, dict):
            continue
        edge_type = _edge_type(edge)
        source = _edge_from(edge)
        target = _edge_to(edge)
        if source not in step_by_node or target not in step_by_node:
            continue
        if edge_type == "above":
            steps.append({
                "step_id": _safe_step_id(f"edge_{edge_index}_above"),
                "atom_id": "layer.reorder",
                "params": {"layer_id": _layer_ref(step_by_node[source]), "position": "above", "reference_layer_id": _layer_ref(step_by_node[target])},
            })
        elif edge_type == "below":
            steps.append({
                "step_id": _safe_step_id(f"edge_{edge_index}_below"),
                "atom_id": "layer.reorder",
                "params": {"layer_id": _layer_ref(step_by_node[source]), "position": "below", "reference_layer_id": _layer_ref(step_by_node[target])},
            })
        elif edge_type == "clip_to":
            steps.append({
                "step_id": _safe_step_id(f"edge_{edge_index}_clip"),
                "atom_id": "layer.create_clipping_mask",
                "target": _layer_ref(step_by_node[source]),
                "params": {"layer_id": _layer_ref(step_by_node[source])},
            })
        elif edge_type == "mask_of":
            source_node = next((node for node in nodes if str(node.get("node_id") or "") == source), {})
            selection_mask = source_node.get("selection_mask") if isinstance(source_node, dict) and isinstance(source_node.get("selection_mask"), dict) else None
            if selection_mask:
                steps.append({
                    "step_id": _safe_step_id(f"edge_{edge_index}_mask"),
                    "atom_id": "mask.apply_alpha",
                    "target": _layer_ref(step_by_node[target]),
                    "params": {"selection_mask": selection_mask},
                })
            else:
                warnings.append(f"mask_of edge {source}->{target} has no selection_mask to lower.")
        elif edge_type in GRAPH_ONLY_EDGE_TYPES:
            warnings.append(f"{edge_type} edge {source}->{target} is recorded as graph metadata and requires explicit recipe steps if it must mutate Photoshop.")

    if errors:
        return _error("design_layer_graph_lower_failed", "Could not lower design layer_graph to an operation recipe.", {"errors": errors, "warnings": warnings})
    recipe_id = str(stage.get("recipe_id") or f"oprec-{_safe_part(plan.get('workflow_id', 'design'))}-{_safe_part(stage_id)}")
    return {
        "status": "ok",
        "schema_version": SCHEMA_VERSION,
        "recipe_type": "operation_recipe",
        "operation_recipe": {
            "schema_version": SCHEMA_VERSION,
            "recipe_id": recipe_id,
            "workflow_id": plan.get("workflow_id"),
            "stage_id": stage_id,
            "goal": stage.get("objective") or plan.get("goal") or "Apply design layer graph stage.",
            "steps": steps,
            "review": {"regions": stage.get("review_regions") or ["global"]},
            "safety": {"non_destructive": True, "allow_destructive": False, "create_history_state": True},
        },
        "lowered_from_layer_graph": {"node_count": len(nodes), "edge_count": len(_as_list(graph.get("edges")))},
        "warnings": warnings,
    }


def lower_design_stage_to_operation_recipe(payload: dict[str, Any]) -> dict[str, Any]:
    plan = payload.get("design_plan") or payload.get("plan")
    stage_id = str(payload.get("stage_id") or "")
    if not isinstance(plan, dict):
        return _error("invalid_design_plan", "design_plan must be an object.")
    validation = validate_design_plan({"design_plan": plan})
    if not validation.get("valid"):
        return _error("invalid_design_plan", "Design plan validation failed.", {"errors": validation.get("errors", []), "warnings": validation.get("warnings", [])})
    stage = _stage_from_plan(plan, stage_id)
    if not stage:
        return _error("design_stage_not_found", f"Unknown design stage_id: {stage_id}")

    recipe_type, recipe = _recipe_from_stage(stage)
    if isinstance(recipe, dict) and recipe_type:
        recipe = dict(recipe)
        recipe.setdefault("schema_version", SCHEMA_VERSION)
        recipe.setdefault("workflow_id", plan.get("workflow_id"))
        recipe.setdefault("stage_id", stage_id)
        recipe.setdefault("goal", stage.get("objective") or plan.get("goal") or "Apply design stage.")
        return {
            "status": "ok",
            "schema_version": SCHEMA_VERSION,
            "recipe_type": recipe_type,
            recipe_type: recipe,
            "warnings": validation.get("warnings", []),
        }

    lowered = _graph_stage_to_operation_recipe(plan, stage)
    if lowered.get("status") == "ok":
        lowered.setdefault("warnings", [])
        lowered["warnings"].extend([item.get("message", str(item)) for item in validation.get("warnings", []) if isinstance(item, dict)])
    return lowered


def review_design_stage(payload: dict[str, Any]) -> dict[str, Any]:
    stage_id = payload.get("stage_id")
    plan = payload.get("design_plan") if isinstance(payload.get("design_plan"), dict) else {}
    graph = plan.get("layer_graph") if isinstance(plan.get("layer_graph"), dict) else {}
    stage_nodes = [
        {
            "node_id": node.get("node_id"),
            "node_type": _node_type(node),
            "role": node.get("role"),
            "review_regions": node.get("review_regions", []),
        }
        for node in _as_list(graph.get("nodes"))
        if isinstance(node, dict) and str(node.get("stage_id") or "") == str(stage_id or "")
    ]
    return {
        "status": "ok",
        "schema_version": SCHEMA_VERSION,
        "stage_id": stage_id,
        "review": {
            "status": "needs_visual_review",
            "checklist": [
                "information hierarchy is clear at output size",
                "asset placement matches the requested hierarchy",
                "text remains readable at output size",
                "text does not overflow or collide with important subject regions",
                "important subjects are not cropped unexpectedly",
                "object-over-text or depth relationships match the layer graph",
                "safe margins are respected unless intentionally broken",
                "clipping masks and alpha masks have acceptable edge quality",
                "font substitution did not materially change the design direction",
                "layers are grouped under the stage rollback target",
                "exported preview matches requested aspect ratio and style direction",
            ],
            "feedback_mapping": {
                "workflow_id": payload.get("workflow_id"),
                "stage_id": stage_id,
                "block_id": payload.get("block_id"),
                "asset_id": payload.get("asset_id"),
                "node_id": payload.get("node_id"),
                "edge_id": payload.get("edge_id"),
            },
            "stage_nodes": stage_nodes,
        },
    }


def export_design_package(payload: dict[str, Any]) -> dict[str, Any]:
    DEFAULT_EXPORT_ROOT.mkdir(parents=True, exist_ok=True)
    return {
        "status": "ok",
        "schema_version": SCHEMA_VERSION,
        "export_root": str(DEFAULT_EXPORT_ROOT),
        "export_preview_payload": {
            "format": payload.get("format", "png"),
            "max_side": payload.get("max_side", 4096),
            "quality": payload.get("quality", 10),
            "wait": payload.get("wait", True),
            "timeout_ms": payload.get("timeout_ms", 120000),
        },
        "notes": ["Use ps_export_preview with export_preview_payload for the first implementation."],
    }
