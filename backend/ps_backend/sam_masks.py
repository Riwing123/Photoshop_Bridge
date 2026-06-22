from __future__ import annotations

import importlib.util
import json
import math
import os
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable


BACKEND_DIR = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = BACKEND_DIR.parent
RUNTIME_ROOT = BACKEND_DIR / "runtime"
ASSET_ROOT = RUNTIME_ROOT / "assets"

SAM_HOST = os.environ.get("PS_AGENT_SAM_HOST", "127.0.0.1")
SAM_PORT = int(os.environ.get("PS_AGENT_SAM_PORT", "17861"))
SAM_BASE_URL = f"http://{SAM_HOST}:{SAM_PORT}"
SAM_MODEL_DIR = BACKEND_DIR / "models" / "sam2"
SAM_MODEL_FILENAME = "sam2.1_hiera_base_plus.pt"
SAM_MODEL_PATH = SAM_MODEL_DIR / SAM_MODEL_FILENAME
SAM_CONFIG = os.environ.get("PS_AGENT_SAM_CONFIG", "configs/sam2.1/sam2.1_hiera_b+.yaml")
SAM_WORKER_SCRIPT = BACKEND_DIR / "sam_worker.py"
SAM_VENV_PYTHON = WORKSPACE_ROOT / ".venv-sam" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
SAM_PID_PATH = RUNTIME_ROOT / "sam-worker.pid"
SAM_LOG_PATH = RUNTIME_ROOT / "sam-worker.log"


AssetUrlBuilder = Callable[[str], str]


def api_error(code: str, message: str, details: Any | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "status": "error",
        "schema_version": "ps-agent/v1",
        "error": {"code": code, "message": message},
    }
    if details is not None:
        payload["error"]["details"] = details
    return payload


def safe_path_part(value: str, default: str = "asset") -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in value)
    cleaned = cleaned.strip("._")
    return cleaned or default


def new_asset_job_id() -> str:
    return f"job-{time.strftime('%Y%m%d%H%M%S', time.gmtime())}-{uuid.uuid4().hex[:8]}"


def is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(value, maximum))


def normalize_size(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        raise ValueError("document_size must be an object with width and height")
    width = value.get("width")
    height = value.get("height")
    if not is_number(width) or not is_number(height):
        raise ValueError("document_size.width and document_size.height must be numbers")
    width_int = int(round(float(width)))
    height_int = int(round(float(height)))
    if width_int <= 0 or height_int <= 0:
        raise ValueError("document_size.width and document_size.height must be positive")
    if width_int > 20000 or height_int > 20000:
        raise ValueError("document_size is too large for the first SAM alpha-mask implementation")
    return {"width": width_int, "height": height_int}


def normalize_bbox(value: Any, field_name: str) -> dict[str, float]:
    if not isinstance(value, dict):
        raise ValueError(f"{field_name} must be an object with x, y, width, and height")
    missing = [key for key in ("x", "y", "width", "height") if key not in value]
    if missing:
        raise ValueError(f"{field_name} missing keys: {', '.join(missing)}")
    x = value.get("x")
    y = value.get("y")
    width = value.get("width")
    height = value.get("height")
    if not all(is_number(item) for item in (x, y, width, height)):
        raise ValueError(f"{field_name} values must be numbers")
    parsed = {"x": float(x), "y": float(y), "width": float(width), "height": float(height)}
    if parsed["x"] < 0 or parsed["y"] < 0 or parsed["width"] <= 0 or parsed["height"] <= 0:
        raise ValueError(f"{field_name} coordinates must be non-negative with positive width/height")
    return parsed


def normalize_point(value: Any, field_name: str) -> list[float]:
    if isinstance(value, dict):
        x = value.get("x")
        y = value.get("y")
    elif isinstance(value, (list, tuple)) and len(value) == 2:
        x, y = value
    else:
        raise ValueError(f"{field_name} must be [x, y] or {{x, y}}")
    if not is_number(x) or not is_number(y):
        raise ValueError(f"{field_name} coordinates must be numbers")
    if float(x) < 0 or float(y) < 0:
        raise ValueError(f"{field_name} coordinates must be non-negative")
    return [float(x), float(y)]


def resolve_workspace_asset_path(value: Any) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("asset_path must be a non-empty string")
    path = Path(value).expanduser().resolve()
    workspace = WORKSPACE_ROOT.resolve()
    try:
        path.relative_to(workspace)
    except ValueError as exc:
        raise ValueError(f"asset_path must be under {workspace}") from exc
    if not path.is_file():
        raise FileNotFoundError(f"asset_path does not exist: {path}")
    return path


def dependency_status(module_name: str) -> dict[str, Any]:
    return {"module": module_name, "available": importlib.util.find_spec(module_name) is not None}


def read_pid(path: Path) -> int | None:
    try:
        raw = path.read_text(encoding="utf-8").strip()
        return int(raw) if raw else None
    except (OSError, ValueError):
        return None


def request_worker_json(method: str, path: str, payload: dict[str, Any] | None = None, timeout: float = 5) -> dict[str, Any]:
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(SAM_BASE_URL + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return api_error("sam_worker_http_error", raw or str(exc))
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return api_error(
            "sam_worker_unreachable",
            f"Could not reach SAM worker at {SAM_BASE_URL}: {getattr(exc, 'reason', exc)}",
            {
                "base_url": SAM_BASE_URL,
                "start_command": "python backend/cli.py sam start",
                "status_command": "python backend/cli.py sam status",
                "log_path": str(SAM_LOG_PATH),
            },
        )


def sam_worker_status(timeout: float = 2) -> dict[str, Any]:
    health = request_worker_json("GET", "/health", timeout=timeout)
    return {
        "base_url": SAM_BASE_URL,
        "reachable": health.get("status") == "ok",
        "health": health,
        "pid": read_pid(SAM_PID_PATH),
        "paths": {
            "venv_python": {"path": str(SAM_VENV_PYTHON), "exists": SAM_VENV_PYTHON.is_file()},
            "worker_script": {"path": str(SAM_WORKER_SCRIPT), "exists": SAM_WORKER_SCRIPT.is_file()},
            "model_dir": {"path": str(SAM_MODEL_DIR), "exists": SAM_MODEL_DIR.is_dir()},
            "model": {
                "path": str(SAM_MODEL_PATH),
                "exists": SAM_MODEL_PATH.is_file(),
                "size_bytes": SAM_MODEL_PATH.stat().st_size if SAM_MODEL_PATH.is_file() else None,
            },
            "log": {"path": str(SAM_LOG_PATH), "exists": SAM_LOG_PATH.is_file()},
            "pid_file": {"path": str(SAM_PID_PATH), "exists": SAM_PID_PATH.is_file()},
        },
        "dependencies_in_main_env": {
            "pillow": dependency_status("PIL"),
            "numpy": dependency_status("numpy"),
            "torch": dependency_status("torch"),
            "sam2": dependency_status("sam2"),
        },
        "start_command": "python backend/cli.py sam start",
    }


def document_to_asset_point(
    point: list[float],
    document_bbox: dict[str, float],
    scale_factor: float,
    image_size: tuple[int, int],
    coord_space: str,
) -> list[float]:
    if coord_space == "asset":
        x, y = point
    else:
        x = (point[0] - document_bbox["x"]) * scale_factor
        y = (point[1] - document_bbox["y"]) * scale_factor
    return [
        round(clamp(float(x), 0.0, max(0.0, image_size[0] - 1)), 3),
        round(clamp(float(y), 0.0, max(0.0, image_size[1] - 1)), 3),
    ]


def document_to_asset_box(
    bbox: dict[str, float],
    document_bbox: dict[str, float],
    scale_factor: float,
    image_size: tuple[int, int],
    coord_space: str,
) -> list[float]:
    if coord_space == "asset":
        left = bbox["x"]
        top = bbox["y"]
        right = bbox["x"] + bbox["width"]
        bottom = bbox["y"] + bbox["height"]
    else:
        left = (bbox["x"] - document_bbox["x"]) * scale_factor
        top = (bbox["y"] - document_bbox["y"]) * scale_factor
        right = (bbox["x"] + bbox["width"] - document_bbox["x"]) * scale_factor
        bottom = (bbox["y"] + bbox["height"] - document_bbox["y"]) * scale_factor

    left = clamp(float(left), 0.0, max(0.0, image_size[0] - 1))
    top = clamp(float(top), 0.0, max(0.0, image_size[1] - 1))
    right = clamp(float(right), 0.0, float(image_size[0]))
    bottom = clamp(float(bottom), 0.0, float(image_size[1]))
    if right <= left or bottom <= top:
        raise ValueError("prompt.bbox is outside the SAM asset crop")
    return [round(left, 3), round(top, 3), round(right, 3), round(bottom, 3)]


def normalize_prompt(
    raw_prompt: Any,
    document_bbox: dict[str, float],
    scale_factor: float,
    image_size: tuple[int, int],
) -> dict[str, Any]:
    if not isinstance(raw_prompt, dict):
        raise ValueError("prompt must be an object")
    coord_space = str(raw_prompt.get("coord_space", "document"))
    if coord_space not in {"document", "asset"}:
        raise ValueError("prompt.coord_space must be 'document' or 'asset'")

    normalized: dict[str, Any] = {"coord_space": coord_space}
    if "bbox" in raw_prompt and raw_prompt["bbox"] is not None:
        bbox = normalize_bbox(raw_prompt["bbox"], "prompt.bbox")
        normalized["box"] = document_to_asset_box(bbox, document_bbox, scale_factor, image_size, coord_space)
        normalized["bbox"] = bbox

    point_coords: list[list[float]] = []
    point_labels: list[int] = []
    positive_points = raw_prompt.get("positive_points") or raw_prompt.get("points") or []
    negative_points = raw_prompt.get("negative_points") or []
    if positive_points is None:
        positive_points = []
    if negative_points is None:
        negative_points = []
    if not isinstance(positive_points, list) or not isinstance(negative_points, list):
        raise ValueError("prompt.positive_points and prompt.negative_points must be arrays")
    if len(positive_points) + len(negative_points) > 64:
        raise ValueError("prompt may contain at most 64 points")

    for index, point in enumerate(positive_points):
        point_coords.append(
            document_to_asset_point(
                normalize_point(point, f"prompt.positive_points[{index}]"),
                document_bbox,
                scale_factor,
                image_size,
                coord_space,
            )
        )
        point_labels.append(1)
    for index, point in enumerate(negative_points):
        point_coords.append(
            document_to_asset_point(
                normalize_point(point, f"prompt.negative_points[{index}]"),
                document_bbox,
                scale_factor,
                image_size,
                coord_space,
            )
        )
        point_labels.append(0)

    if point_coords:
        normalized["point_coords"] = point_coords
        normalized["point_labels"] = point_labels

    if "box" not in normalized and "point_coords" not in normalized:
        raise ValueError("prompt must include bbox, positive_points, or points")
    return normalized


def asset_payload(relative_path: str, path: Path, mime_type: str, asset_url_builder: AssetUrlBuilder | None) -> dict[str, Any]:
    payload = {
        "id": relative_path,
        "path": str(path),
        "mime_type": mime_type,
        "size_bytes": path.stat().st_size if path.is_file() else None,
    }
    if asset_url_builder is not None:
        payload["uri"] = asset_url_builder(relative_path)
    return payload


def mask_document_bbox(mask) -> dict[str, float] | None:
    bbox = mask.getbbox()
    if not bbox:
        return None
    left, top, right, bottom = bbox
    return {"x": float(left), "y": float(top), "width": float(right - left), "height": float(bottom - top)}


def overlay_image(image, mask):
    from PIL import Image

    base = image.convert("RGBA")
    color = Image.new("RGBA", base.size, (255, 80, 40, 130))
    alpha = mask.convert("L").point(lambda value: min(150, int(value * 0.58)))
    color.putalpha(alpha)
    return Image.alpha_composite(base, color)


def build_sam_outputs(
    asset_path: Path,
    crop_mask_path: Path,
    asset_dir: Path,
    relative_prefix: str,
    document_size: dict[str, int],
    document_bbox: dict[str, float],
    threshold: float,
) -> dict[str, Any]:
    from PIL import Image

    with Image.open(asset_path) as source_image_file:
        source_image = source_image_file.convert("RGB")
    with Image.open(crop_mask_path) as mask_file:
        crop_mask = mask_file.convert("L")

    if crop_mask.size != source_image.size:
        crop_mask = crop_mask.resize(source_image.size, Image.Resampling.BILINEAR)

    doc_width = document_size["width"]
    doc_height = document_size["height"]
    paste_width = max(1, int(round(document_bbox["width"])))
    paste_height = max(1, int(round(document_bbox["height"])))
    left = max(0, min(doc_width - 1, int(round(document_bbox["x"]))))
    top = max(0, min(doc_height - 1, int(round(document_bbox["y"]))))
    paste_width = min(paste_width, doc_width - left)
    paste_height = min(paste_height, doc_height - top)

    doc_mask = Image.new("L", (doc_width, doc_height), 0)
    resized_crop_mask = crop_mask.resize((paste_width, paste_height), Image.Resampling.BILINEAR)
    doc_mask.paste(resized_crop_mask, (left, top))

    selected_pixels = sum(doc_mask.histogram()[1:])
    area_ratio = selected_pixels / float(doc_width * doc_height)
    warnings: list[str] = []
    if selected_pixels == 0:
        warnings.append("alpha_empty")
    if area_ratio < 0.0001:
        warnings.append("alpha_area_tiny")
    if area_ratio > 0.95:
        warnings.append("alpha_area_very_large")
    if threshold != 0.5:
        warnings.append("threshold is recorded for downstream tools; this alpha mask keeps SAM soft edges")

    alpha_path = asset_dir / "alpha_mask.png"
    luma_path = asset_dir / "alpha_luma.png"
    mask_preview_path = asset_dir / "mask_preview.png"
    overlay_path = asset_dir / "overlay_preview.png"

    rgba = Image.new("RGBA", (doc_width, doc_height), (255, 255, 255, 0))
    rgba.putalpha(doc_mask)
    rgba.save(alpha_path)
    doc_mask.save(luma_path)
    crop_mask.save(mask_preview_path)
    overlay_image(source_image, crop_mask).save(overlay_path)

    return {
        "alpha_path": alpha_path,
        "luma_path": luma_path,
        "mask_preview_path": mask_preview_path,
        "overlay_path": overlay_path,
        "relative": {
            "alpha": f"{relative_prefix}/alpha_mask.png",
            "luma": f"{relative_prefix}/alpha_luma.png",
            "mask_preview": f"{relative_prefix}/mask_preview.png",
            "overlay": f"{relative_prefix}/overlay_preview.png",
        },
        "selected_pixels": selected_pixels,
        "area_ratio": round(area_ratio, 8),
        "mask_bbox": mask_document_bbox(doc_mask),
        "warnings": warnings,
    }


def generate_sam_mask(body: dict[str, Any], asset_url_builder: AssetUrlBuilder | None = None) -> dict[str, Any]:
    try:
        asset_path = resolve_workspace_asset_path(body.get("asset_path"))
        document_size = normalize_size(body.get("document_size"))
        document_bbox = normalize_bbox(body.get("document_bbox"), "document_bbox")
        scale_factor = float(body.get("scale_factor"))
        if not math.isfinite(scale_factor) or scale_factor <= 0:
            raise ValueError("scale_factor must be a positive number")
        threshold = float(body.get("threshold", 0.5))
        if not math.isfinite(threshold) or threshold < 0 or threshold > 1:
            raise ValueError("threshold must be between 0 and 1")
        feather = float(body.get("feather", 0) or 0)
        if not math.isfinite(feather) or feather < 0 or feather > 500:
            raise ValueError("feather must be between 0 and 500")
    except FileNotFoundError as exc:
        return api_error("asset_not_found", str(exc))
    except (TypeError, ValueError) as exc:
        return api_error("invalid_sam_mask_request", str(exc))

    try:
        from PIL import Image
    except ImportError:
        return api_error(
            "pillow_not_installed",
            "Pillow is required in the main backend to compose full-size alpha masks.",
        )

    with Image.open(asset_path) as image_file:
        image_size = image_file.size

    try:
        prompt = normalize_prompt(body.get("prompt"), document_bbox, scale_factor, image_size)
    except ValueError as exc:
        return api_error("invalid_sam_prompt", str(exc))

    worker = sam_worker_status(timeout=1.5)
    if not worker.get("reachable"):
        return api_error(
            "sam_worker_unreachable",
            "SAM worker is not reachable. Start it before generating masks.",
            worker,
        )

    job_id = safe_path_part(str(body.get("job_id") or new_asset_job_id()), "job")
    asset_dir = ASSET_ROOT / job_id
    asset_dir.mkdir(parents=True, exist_ok=True)
    crop_mask_path = asset_dir / "sam_crop_mask.png"

    worker_payload = {
        "image_path": str(asset_path),
        "prompt": {
            "box": prompt.get("box"),
            "point_coords": prompt.get("point_coords"),
            "point_labels": prompt.get("point_labels"),
        },
        "output_mask_path": str(crop_mask_path),
        "multimask_output": body.get("multimask_output", True) is not False,
    }
    worker_result = request_worker_json(
        "POST",
        "/predict",
        worker_payload,
        timeout=max(5.0, min(float(body.get("timeout_ms", 120000) or 120000) / 1000.0, 300.0)),
    )
    if worker_result.get("status") != "ok":
        return worker_result
    if not crop_mask_path.is_file():
        return api_error(
            "sam_mask_missing",
            "SAM worker returned ok but did not create the expected mask file.",
            {"expected_path": str(crop_mask_path), "worker_result": worker_result},
        )

    try:
        outputs = build_sam_outputs(
            asset_path,
            crop_mask_path,
            asset_dir,
            job_id,
            document_size,
            document_bbox,
            threshold,
        )
    except Exception as exc:
        return api_error("sam_alpha_compose_failed", str(exc))

    label = str(body.get("label") or "sam_alpha_mask")[:128]
    selection_mask = {
        "source": "alpha_mask",
        "asset_path": str(outputs["alpha_path"]),
        "asset_uri": asset_url_builder(outputs["relative"]["alpha"]) if asset_url_builder else None,
        "raw_asset_path": str(outputs["raw_path"]),
        "raw_asset_uri": asset_url_builder(outputs["relative"]["raw"]) if asset_url_builder else None,
        "mask_width": document_size["width"],
        "mask_height": document_size["height"],
        "threshold": threshold,
        "feather": feather,
        "invert": body.get("invert") is True,
        "show_marching_ants": body.get("show_marching_ants") is True,
        "label": label,
    }

    warnings = list(outputs["warnings"])
    if worker_result.get("warnings"):
        warnings.extend(worker_result["warnings"])

    return {
        "status": "ok",
        "schema_version": "ps-agent/v1",
        "provider": "sam2.1",
        "model": "sam2.1_hiera_base_plus",
        "job_id": job_id,
        "asset_path": str(asset_path),
        "document_size": document_size,
        "document_bbox": document_bbox,
        "scale_factor": scale_factor,
        "prompt": prompt,
        "confidence": worker_result.get("score"),
        "device": worker_result.get("device"),
        "alpha_mask": asset_payload(outputs["relative"]["alpha"], outputs["alpha_path"], "image/png", asset_url_builder),
        "alpha_luma": asset_payload(outputs["relative"]["luma"], outputs["luma_path"], "image/png", asset_url_builder),
        "alpha_raw": asset_payload(outputs["relative"]["raw"], outputs["raw_path"], "application/octet-stream", asset_url_builder),
        "mask_preview": asset_payload(outputs["relative"]["mask_preview"], outputs["mask_preview_path"], "image/png", asset_url_builder),
        "overlay_preview": asset_payload(outputs["relative"]["overlay"], outputs["overlay_path"], "image/png", asset_url_builder),
        "mask_bbox": outputs["mask_bbox"],
        "area_ratio": outputs["area_ratio"],
        "selected_pixels": outputs["selected_pixels"],
        "warnings": warnings,
        "selection_mask": selection_mask,
    }
