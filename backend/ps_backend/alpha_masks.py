from __future__ import annotations

import importlib.util
import math
import time
import uuid
from pathlib import Path
from typing import Any, Callable


BACKEND_DIR = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = BACKEND_DIR.parent
RUNTIME_ROOT = BACKEND_DIR / "runtime"
ASSET_ROOT = RUNTIME_ROOT / "assets"

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
        raise ValueError("document_size is too large for the current alpha-mask implementation")
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
        raise ValueError("prompt.bbox is outside the asset crop")
    return [round(left, 3), round(top, 3), round(right, 3), round(bottom, 3)]


def normalize_mask_prompt(
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


def build_alpha_outputs(
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
        warnings.append("threshold is recorded for downstream tools; this alpha mask keeps soft edges")

    alpha_path = asset_dir / "alpha_mask.png"
    luma_path = asset_dir / "alpha_luma.png"
    raw_path = asset_dir / "alpha_mask.gray"
    mask_preview_path = asset_dir / "mask_preview.png"
    overlay_path = asset_dir / "overlay_preview.png"

    rgba = Image.new("RGBA", (doc_width, doc_height), (255, 255, 255, 0))
    rgba.putalpha(doc_mask)
    rgba.save(alpha_path)
    doc_mask.save(luma_path)
    raw_path.write_bytes(doc_mask.tobytes())
    crop_mask.save(mask_preview_path)
    overlay_image(source_image, crop_mask).save(overlay_path)

    return {
        "alpha_path": alpha_path,
        "luma_path": luma_path,
        "raw_path": raw_path,
        "mask_preview_path": mask_preview_path,
        "overlay_path": overlay_path,
        "relative": {
            "alpha": f"{relative_prefix}/alpha_mask.png",
            "luma": f"{relative_prefix}/alpha_luma.png",
            "raw": f"{relative_prefix}/alpha_mask.gray",
            "mask_preview": f"{relative_prefix}/mask_preview.png",
            "overlay": f"{relative_prefix}/overlay_preview.png",
        },
        "selected_pixels": selected_pixels,
        "area_ratio": round(area_ratio, 8),
        "mask_bbox": mask_document_bbox(doc_mask),
        "warnings": warnings,
    }


def compose_soft_masks(
    include_paths: list[Path],
    output_path: Path,
    merge_mode: str = "union",
    exclude_paths: list[Path] | None = None,
) -> dict[str, Any]:
    import numpy as np
    from PIL import Image

    if not include_paths:
        raise ValueError("at least one include mask is required")

    include_arrays = []
    size: tuple[int, int] | None = None
    for path in include_paths:
        with Image.open(path) as mask_file:
            mask = mask_file.convert("L")
            if size is None:
                size = mask.size
            elif mask.size != size:
                raise ValueError("all include masks must have the same size")
            include_arrays.append(np.asarray(mask, dtype=np.float32) / 255.0)

    if merge_mode == "intersect":
        merged = include_arrays[0]
        for array in include_arrays[1:]:
            merged = np.minimum(merged, array)
    else:
        merged = include_arrays[0]
        for array in include_arrays[1:]:
            merged = np.maximum(merged, array)

    exclude_count = 0
    if exclude_paths:
        exclude_arrays = []
        for path in exclude_paths:
            with Image.open(path) as mask_file:
                mask = mask_file.convert("L")
                if mask.size != size:
                    raise ValueError("exclude masks must match include mask size")
                exclude_arrays.append(np.asarray(mask, dtype=np.float32) / 255.0)
        if exclude_arrays:
            exclude_count = len(exclude_arrays)
            subtract = exclude_arrays[0]
            for array in exclude_arrays[1:]:
                subtract = np.maximum(subtract, array)
            merged = merged * (1.0 - subtract)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    result = Image.fromarray((np.clip(merged, 0.0, 1.0) * 255.0).astype("uint8"), mode="L")
    result.save(output_path)
    return {
        "output_path": str(output_path),
        "merge_mode": merge_mode,
        "include_count": len(include_paths),
        "exclude_count": exclude_count,
    }

def materialize_full_document_alpha_mask(
    raw_mask_path: Path,
    asset_dir: Path,
    relative_prefix: str,
    document_size: dict[str, int],
    threshold: float = 0.5,
    feather: float = 0.0,
    invert: bool = False,
) -> dict[str, Any]:
    from PIL import Image, ImageFilter, ImageOps

    doc_width = document_size["width"]
    doc_height = document_size["height"]
    expected_size = doc_width * doc_height
    payload = raw_mask_path.read_bytes()
    if len(payload) != expected_size:
        raise ValueError(
            f"raw alpha payload size mismatch: expected {expected_size} bytes for {doc_width}x{doc_height}, got {len(payload)}"
        )

    doc_mask = Image.frombytes("L", (doc_width, doc_height), payload)
    warnings: list[str] = []
    if invert:
        doc_mask = ImageOps.invert(doc_mask)
        warnings.append("mask_inverted")
    if feather > 0:
        doc_mask = doc_mask.filter(ImageFilter.GaussianBlur(radius=max(0.1, float(feather))))
        warnings.append("mask_feathered_in_backend")

    selected_pixels = sum(doc_mask.histogram()[1:])
    area_ratio = selected_pixels / float(doc_width * doc_height)
    if selected_pixels == 0:
        warnings.append("alpha_empty")
    if area_ratio < 0.0001:
        warnings.append("alpha_area_tiny")
    if area_ratio > 0.95:
        warnings.append("alpha_area_very_large")
    if threshold != 0.5:
        warnings.append("threshold is recorded for downstream tools; this alpha mask keeps soft edges")

    alpha_path = asset_dir / "alpha_mask.png"
    luma_path = asset_dir / "alpha_luma.png"
    normalized_raw_path = asset_dir / "alpha_mask.gray"
    rgba = Image.new("RGBA", (doc_width, doc_height), (255, 255, 255, 0))
    rgba.putalpha(doc_mask)
    rgba.save(alpha_path)
    doc_mask.save(luma_path)
    normalized_raw_path.write_bytes(doc_mask.tobytes())

    return {
        "alpha_path": alpha_path,
        "luma_path": luma_path,
        "raw_path": normalized_raw_path,
        "relative": {
            "alpha": f"{relative_prefix}/alpha_mask.png",
            "luma": f"{relative_prefix}/alpha_luma.png",
            "raw": f"{relative_prefix}/alpha_mask.gray",
        },
        "selected_pixels": selected_pixels,
        "area_ratio": round(area_ratio, 8),
        "mask_bbox": mask_document_bbox(doc_mask),
        "warnings": warnings,
    }

