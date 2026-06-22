from __future__ import annotations

import argparse
import json
import math
import mimetypes
import os
import re
import secrets
import sys
import threading
import time
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


CURRENT_DIR = Path(__file__).resolve().parent
WORKSPACE_ROOT = CURRENT_DIR.parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

from ps_backend.job_queue import JobQueue, utc_now
from ps_backend.sam_masks import generate_sam_mask, sam_worker_status
from ps_backend.grounding_hq import (
    detect_grounding_boxes,
    generate_grounded_hq_mask,
    generate_hqsam_mask,
    grounding_hq_worker_status,
)
from ps_backend.alpha_masks import (
    asset_payload,
    compose_soft_masks,
    materialize_full_document_alpha_mask,
    normalize_size,
    resolve_workspace_asset_path,
)
from ps_backend.selection_strategy import create_selection_strategy, validate_selection_strategy
from ps_backend.capabilities import (
    CAPABILITY_BY_ID,
    dry_run_capability_call,
    list_capabilities,
    probe_capability,
    validate_capability_call,
)
from ps_backend.operation_atoms import (
    list_operation_atoms,
    probe_operation_atom,
    review_operation_recipe,
    validate_operation_recipe,
)
from ps_backend.selection_atoms import (
    list_selection_atoms,
    probe_selection_atom,
    review_selection_recipe,
    validate_selection_recipe,
)
from ps_backend.effect_primitives import (
    generate_layer_recipe,
    list_effect_primitives,
    lower_layer_recipe_to_plan,
    retrieve_effect_primitives,
    review_layer_recipe,
    validate_layer_recipe,
)
from ps_backend.workflow import (
    create_workflow_plan,
    finalize_workflow_review,
    find_stage,
    review_workflow_stage,
    stage_group_name,
    validate_workflow_plan,
)
from ps_backend.style_guidance import retrieve_style_guidance
from ps_backend.style_metrics import analyze_image_metrics
from ps_backend.style_scoring import score_grade_preview
from ps_backend.visual_review import compare_reference, detect_overflow, visual_score
from ps_backend.bezier_geometry import audit_bezier_handles
from ps_backend.svg_geometry import compile_svg_object
from ps_backend.design import (
    analyze_design_assets,
    create_design_plan,
    export_design_package,
    lower_design_stage_to_operation_recipe,
    prepare_asset_variant,
    review_design_stage,
    scan_asset_library,
    validate_design_plan,
)
from ps_backend.region_artifacts import (
    create_region_artifact,
    extract_region_contour,
    lower_region_to_path,
    lower_region_to_selection_recipe,
)


HOST = "127.0.0.1"
PORT = 17860
BACKEND_VERSION = "0.9.0"
STARTED_AT_EPOCH = time.time()
DEFAULT_WAIT_TIMEOUT_MS = 25000
DEFAULT_REGION_MAX_SIDE = 1536
DEFAULT_REGION_UPSCALE = True
MIN_REGION_MAX_SIDE = 128
MAX_REGION_MAX_SIDE = 2048
RUNTIME_ROOT = CURRENT_DIR / "runtime"
ASSET_ROOT = RUNTIME_ROOT / "assets"
MPL_CONFIG_ROOT = RUNTIME_ROOT / "matplotlib"
DB_PATH = RUNTIME_ROOT / "ps-agent.sqlite3"
LOG_PATH = RUNTIME_ROOT / "ps-agent-backend.log"
PID_PATH = RUNTIME_ROOT / "backend.pid"
LOCK_PATH = RUNTIME_ROOT / "backend.lock"
TOKEN_PATH = RUNTIME_ROOT / "backend.token"
JOB_ID_RE = re.compile(r"^job-[A-Za-z0-9_.:-]{1,120}$")
os.environ.setdefault("MPLCONFIGDIR", str(MPL_CONFIG_ROOT))
os.environ.setdefault("GLOG_minloglevel", "2")
os.environ.setdefault("ABSL_LOGGING_MIN_LOG_LEVEL", "2")
os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "2")
SUPPORTED_APPLY_OPS = {
    "adjust_exposure",
    "adjust_vibrance",
    "adjust_color_balance",
    "adjust_hue_saturation",
    "adjust_curves",
    "adjust_levels",
    "adjust_selective_color",
    "adjust_gradient_map",
    "adjust_color_lookup",
    "camera_raw_filter",
}
EXPOSURE_PARAMS = {"exposure", "offset", "gamma"}
VIBRANCE_PARAMS = {"vibrance", "saturation"}
COLOR_BALANCE_PARAMS = {"shadows", "midtones", "highlights", "preserve_luminosity"}
HUE_SATURATION_PARAMS = {"range", "hue", "saturation", "lightness"}
HUE_SATURATION_RANGES = {
    "master",
    "reds",
    "yellows",
    "greens",
    "cyans",
    "blues",
    "magentas",
}
CURVES_PARAMS = {"channel", "points", "preset"}
CURVES_CHANNELS = {"composite", "red", "green", "blue", "rgb"}
LEVELS_PARAMS = {"channel", "input_black", "input_white", "gamma", "output_black", "output_white", "preset"}
LEVELS_CHANNELS = CURVES_CHANNELS
SELECTIVE_COLOR_PARAMS = {"method", "colors", "corrections"}
SELECTIVE_COLOR_METHODS = {"relative", "absolute"}
SELECTIVE_COLOR_RANGES = {
    "reds",
    "yellows",
    "greens",
    "cyans",
    "blues",
    "magentas",
    "whites",
    "neutrals",
    "blacks",
}
GRADIENT_MAP_PARAMS = {"stops", "reverse", "dither", "preset"}
COLOR_LOOKUP_PARAMS = {"name", "lookup", "profile", "type"}
CAMERA_RAW_BASIC_PARAMS = {
    "temperature",
    "tint",
    "exposure",
    "contrast",
    "highlights",
    "shadows",
    "whites",
    "blacks",
}
CAMERA_RAW_COLOR_PARAMS = {
    "vibrance",
    "saturation",
}
CAMERA_RAW_PRESENCE_PARAMS = {
    "dehaze",
    "clarity",
    "texture",
}
CAMERA_RAW_DETAIL_PARAMS = {
    "luminance_noise_reduction",
    "color_noise_reduction",
    "sharpening",
}
CAMERA_RAW_GROUPS = {
    "basic": CAMERA_RAW_BASIC_PARAMS,
    "color": CAMERA_RAW_COLOR_PARAMS,
    "presence": CAMERA_RAW_PRESENCE_PARAMS,
    "detail": CAMERA_RAW_DETAIL_PARAMS,
}
CAMERA_RAW_FLAT_PARAMS = set().union(*CAMERA_RAW_GROUPS.values())
CAMERA_RAW_PARAMS = CAMERA_RAW_FLAT_PARAMS | set(CAMERA_RAW_GROUPS)
SUPPORTED_TARGET_TYPES = {"global", "selection_mask", "acr_ai_mask"}
SELECTION_MASK_SOURCES = {"current_selection", "bbox", "polygon", "composite", "alpha_mask"}
SELECTION_MASK_ITEM_SOURCES = {"current_selection", "bbox", "polygon"}
SELECTION_MASK_OPERATIONS = {"replace", "add", "subtract", "intersect"}
NATIVE_SELECTION_ACTIONS = {"select_subject", "select_sky", "color_range", "focus_area"}
SELECTION_COMMAND_ACTIONS = {
    "select_all",
    "deselect",
    "inverse",
    "modify",
    "save_selection",
    "load_selection",
}
SELECTION_MODIFY_OPERATIONS = {"feather", "expand", "contract", "smooth", "border"}
TONAL_RANGE_PRESET_ALIASES = {
    "highlight": "highlights",
    "highlights": "highlights",
    "lights": "highlights",
    "高光": "highlights",
    "亮部": "highlights",
    "midtone": "midtones",
    "midtones": "midtones",
    "mids": "midtones",
    "中间调": "midtones",
    "中间色调": "midtones",
    "shadow": "shadows",
    "shadows": "shadows",
    "darks": "shadows",
    "阴影": "shadows",
    "暗部": "shadows",
}
TONAL_RANGE_PRESETS = {"highlights", "midtones", "shadows"}
MAX_COMPOSITE_SELECTION_ITEMS = 16
ACR_AI_MASK_ENGINES = {"camera_raw_internal", "photoshop_selection_fallback"}
ACR_AI_MASK_TYPES = {
    "subject",
    "background",
    "sky",
    "person",
    "face_skin",
    "body_skin",
    "eyes",
    "lips",
    "hair",
    "teeth",
}
ACR_AI_MASK_PARTS = {
    "face_skin",
    "body_skin",
    "eyebrows",
    "eye_sclera",
    "iris_pupil",
    "lips",
    "teeth",
    "hair",
}
ACR_AI_MASK_COMBINE_OPS = {"add", "subtract", "intersect"}
MIN_POLYGON_POINTS = 3
MAX_POLYGON_POINTS = 256
FACE_SELECTION_PARTS = {
    "left_eye",
    "right_eye",
    "both_eyes",
    "lips_outer",
    "face_oval",
    "left_cheek",
    "right_cheek",
}
FACE_PART_LANDMARKS = {
    "left_eye": [33, 7, 163, 144, 145, 153, 154, 155, 133, 173, 157, 158, 159, 160, 161, 246],
    "right_eye": [362, 382, 381, 380, 374, 373, 390, 249, 263, 466, 388, 387, 386, 385, 384, 398],
    "lips_outer": [61, 146, 91, 181, 84, 17, 314, 405, 321, 375, 291, 409, 270, 269, 267, 0, 37, 39, 40, 185],
    "face_oval": [10, 338, 297, 332, 284, 251, 389, 356, 454, 323, 361, 288, 397, 365, 379, 378, 400, 377, 152, 148, 176, 149, 150, 136, 172, 58, 132, 93, 234, 127, 162, 21, 54, 103, 67, 109],
    "left_cheek": [50, 101, 118, 117, 123, 147, 187, 205, 36, 203],
    "right_cheek": [280, 330, 347, 346, 352, 376, 411, 425, 266, 423],
}

QUEUE = JobQueue(DB_PATH)


def json_response(handler: BaseHTTPRequestHandler, status: int, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type")
    handler.end_headers()
    handler.wfile.write(body)


def error_response(
    handler: BaseHTTPRequestHandler,
    status: int,
    code: str,
    message: str,
    details: Any | None = None,
) -> None:
    payload: dict[str, Any] = {
        "status": "error",
        "error": {"code": code, "message": message},
    }
    if details is not None:
        payload["error"]["details"] = details
    json_response(handler, status, payload)


def binary_response(handler: BaseHTTPRequestHandler, status: int, payload: bytes, mime_type: str) -> None:
    handler.send_response(status)
    handler.send_header("Content-Type", mime_type)
    handler.send_header("Content-Length", str(len(payload)))
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.end_headers()
    handler.wfile.write(payload)


def console_write(message: str) -> None:
    try:
        print(message, flush=True)
    except Exception:
        pass


def ensure_runtime_dirs() -> None:
    RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)
    ASSET_ROOT.mkdir(parents=True, exist_ok=True)
    MPL_CONFIG_ROOT.mkdir(parents=True, exist_ok=True)


def ensure_shutdown_token() -> str:
    ensure_runtime_dirs()
    if TOKEN_PATH.is_file():
        token = TOKEN_PATH.read_text(encoding="utf-8").strip()
        if token:
            return token
    token = secrets.token_urlsafe(32)
    TOKEN_PATH.write_text(token, encoding="utf-8")
    return token


def read_shutdown_token() -> str | None:
    try:
        return TOKEN_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return None


def write_pid_files(host: str, port: int) -> None:
    ensure_runtime_dirs()
    payload = {
        "pid": os.getpid(),
        "host": host,
        "port": port,
        "backend_version": BACKEND_VERSION,
        "started_at": utc_now(),
        "workspace_root": str(WORKSPACE_ROOT),
    }
    PID_PATH.write_text(str(os.getpid()), encoding="utf-8")
    LOCK_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def clear_pid_files() -> None:
    for path in (PID_PATH, LOCK_PATH):
        try:
            if path.is_file():
                path.unlink()
        except OSError:
            pass


def client_is_loopback(handler: BaseHTTPRequestHandler) -> bool:
    host = handler.client_address[0] if handler.client_address else ""
    return host in {"127.0.0.1", "::1", "localhost"}


def process_info() -> dict[str, Any]:
    return {
        "pid": os.getpid(),
        "started_at_epoch": STARTED_AT_EPOCH,
        "uptime_seconds": round(max(0.0, time.time() - STARTED_AT_EPOCH), 3),
    }


def backend_health_payload() -> dict[str, Any]:
    payload = QUEUE.health()
    payload.update(
        {
            "backend_version": BACKEND_VERSION,
            "process": process_info(),
            "workspace_root": str(WORKSPACE_ROOT),
            "runtime_root": str(RUNTIME_ROOT),
            "db_path": str(DB_PATH),
        }
    )
    return payload


def path_check(path: Path, kind: str) -> dict[str, Any]:
    exists = path.exists()
    writable = False
    try:
        target = path if kind == "dir" else path.parent
        target.mkdir(parents=True, exist_ok=True)
        probe = target / ".write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        writable = True
    except OSError:
        writable = False
    return {
        "path": str(path),
        "kind": kind,
        "exists": exists,
        "writable": writable,
        "size_bytes": path.stat().st_size if path.is_file() else None,
    }


def diagnostics_payload() -> dict[str, Any]:
    diagnostics = QUEUE.diagnostics()
    diagnostics.update(
        {
            "status": "ok",
            "schema_version": "ps-agent/v1",
            "backend_version": BACKEND_VERSION,
            "process": process_info(),
            "paths": {
                "workspace_root": path_check(WORKSPACE_ROOT, "dir"),
                "runtime_root": path_check(RUNTIME_ROOT, "dir"),
                "db": path_check(DB_PATH, "file"),
                "log": path_check(LOG_PATH, "file"),
                "pid": path_check(PID_PATH, "file"),
                "token": {
                    "path": str(TOKEN_PATH),
                    "exists": TOKEN_PATH.is_file(),
                },
                "face_landmarker_model": {
                    "path": str(CURRENT_DIR / "models" / "face_landmarker.task"),
                    "exists": (CURRENT_DIR / "models" / "face_landmarker.task").is_file(),
                },
            },
            "sam": sam_worker_status(timeout=1.5),
            "grounding_hq": grounding_hq_worker_status(timeout=1.5),
            "health": backend_health_payload(),
        }
    )
    return diagnostics


def parse_json_body(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    length = int(handler.headers.get("Content-Length", "0") or "0")
    if length <= 0:
        return {}
    raw = handler.rfile.read(length).decode("utf-8")
    if not raw.strip():
        return {}
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("JSON request body must be an object.")
    return data


def safe_path_part(value: str, default: str = "asset") -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in value)
    cleaned = cleaned.strip("._")
    return cleaned or default


def make_asset_url(handler: BaseHTTPRequestHandler, relative_asset_path: str) -> str:
    host = handler.headers.get("Host") or f"{HOST}:{PORT}"
    return f"http://{host}/assets/{relative_asset_path}"


def save_asset_upload(handler: BaseHTTPRequestHandler, job_id: str, filename: str) -> None:
    content_length = int(handler.headers.get("Content-Length", "0") or "0")
    if content_length <= 0:
        error_response(handler, 400, "empty_asset", "Asset upload body is empty.")
        return

    safe_job_id = safe_path_part(job_id, "job")
    safe_filename = safe_path_part(filename, "asset.bin")
    asset_dir = ASSET_ROOT / safe_job_id
    asset_dir.mkdir(parents=True, exist_ok=True)
    asset_path = asset_dir / safe_filename
    payload = handler.rfile.read(content_length)
    asset_path.write_bytes(payload)

    relative_asset_path = f"{safe_job_id}/{safe_filename}"
    mime_type = handler.headers.get("Content-Type") or mimetypes.guess_type(safe_filename)[0] or "application/octet-stream"
    json_response(
        handler,
        200,
        {
            "status": "ok",
            "asset": {
                "id": relative_asset_path,
                "uri": make_asset_url(handler, relative_asset_path),
                "path": str(asset_path),
                "mime_type": mime_type,
                "size_bytes": len(payload),
            },
        },
    )


def wait_or_return(job: dict[str, Any], request_body: dict[str, Any]) -> dict[str, Any]:
    wait = bool(request_body.get("wait", True))
    timeout_ms = int(request_body.get("timeout_ms", DEFAULT_WAIT_TIMEOUT_MS))
    timeout_ms = max(1000, min(timeout_ms, 300000))

    if wait and QUEUE.uxp_connected():
        completed = QUEUE.wait_for_job(job["job_id"], timeout_ms)
        if completed and completed["status"] in {"done", "error", "cancelled", "expired"}:
            return {
                "status": completed["status"],
                "job_id": completed["job_id"],
                "job": completed,
                "result": completed.get("result"),
            }
        return {
            "status": "timeout",
            "job_id": job["job_id"],
            "job": completed or job,
            "message": "Timed out waiting for the UXP plugin to finish the job.",
        }

    return {
        "status": "queued",
        "job_id": job["job_id"],
        "job": job,
        "uxp_connected": QUEUE.uxp_connected(),
    }


def clamp_int(value: Any, minimum: int, maximum: int, fallback: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = fallback
    return max(minimum, min(parsed, maximum))


def is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def validate_number(
    errors: list[str],
    value: Any,
    path: str,
    minimum: float | None = None,
    maximum: float | None = None,
) -> None:
    if not is_number(value):
        errors.append(f"{path} must be a number")
        return
    if minimum is not None and value < minimum:
        errors.append(f"{path} must be >= {minimum}")
    if maximum is not None and value > maximum:
        errors.append(f"{path} must be <= {maximum}")


def validate_color_balance_triplet(errors: list[str], value: Any, path: str) -> None:
    if not isinstance(value, list) or len(value) != 3:
        errors.append(f"{path} must be an array of three numbers")
        return
    for index, item in enumerate(value):
        validate_number(errors, item, f"{path}[{index}]", -100, 100)


def validate_camera_raw_param(errors: list[str], key: str, value: Any, path: str) -> None:
    if key == "exposure":
        validate_number(errors, value, path, -5, 5)
    elif key in {"luminance_noise_reduction", "color_noise_reduction"}:
        validate_number(errors, value, path, 0, 100)
    elif key == "sharpening":
        validate_number(errors, value, path, 0, 150)
    else:
        validate_number(errors, value, path, -100, 100)


def validate_camera_raw_params(errors: list[str], params: dict[str, Any], prefix: str) -> None:
    unknown = set(params) - CAMERA_RAW_PARAMS
    if unknown:
        errors.append(f"{prefix}.params has unsupported keys: {', '.join(sorted(unknown))}")

    for key in sorted(CAMERA_RAW_FLAT_PARAMS):
        if key in params:
            validate_camera_raw_param(errors, key, params[key], f"{prefix}.params.{key}")

    for group_name, allowed_keys in CAMERA_RAW_GROUPS.items():
        if group_name not in params:
            continue
        group = params[group_name]
        if not isinstance(group, dict):
            errors.append(f"{prefix}.params.{group_name} must be an object")
            continue
        group_unknown = set(group) - allowed_keys
        if group_unknown:
            errors.append(
                f"{prefix}.params.{group_name} has unsupported keys: "
                f"{', '.join(sorted(group_unknown))}"
            )
        for key in sorted(allowed_keys):
            if key in group:
                validate_camera_raw_param(
                    errors,
                    key,
                    group[key],
                    f"{prefix}.params.{group_name}.{key}",
                )


def validate_bool(errors: list[str], value: Any, path: str) -> None:
    if not isinstance(value, bool):
        errors.append(f"{path} must be a boolean")


def validate_bbox(errors: list[str], value: Any, path: str) -> None:
    if not isinstance(value, dict):
        errors.append(f"{path} must be an object")
        return
    for key in ("x", "y", "width", "height"):
        if key not in value:
            errors.append(f"{path}.{key} is required")
            continue
        minimum = 0 if key in {"x", "y"} else 0.000001
        validate_number(errors, value[key], f"{path}.{key}", minimum, None)


def polygon_point(value: Any) -> tuple[float, float] | None:
    if isinstance(value, dict):
        x = value.get("x")
        y = value.get("y")
    elif isinstance(value, (list, tuple)) and len(value) == 2:
        x = value[0]
        y = value[1]
    else:
        return None
    if not is_number(x) or not is_number(y):
        return None
    return float(x), float(y)


def normalize_polygon_points(value: Any) -> list[list[float]]:
    if not isinstance(value, list):
        raise ValueError("points must be an array")
    if len(value) < MIN_POLYGON_POINTS:
        raise ValueError(f"points must contain at least {MIN_POLYGON_POINTS} points")
    if len(value) > MAX_POLYGON_POINTS:
        raise ValueError(f"points must contain at most {MAX_POLYGON_POINTS} points")

    normalized: list[list[float]] = []
    for index, item in enumerate(value):
        point = polygon_point(item)
        if point is None:
            raise ValueError(f"points[{index}] must be [x, y] or {{x, y}}")
        x, y = point
        if x < 0 or y < 0:
            raise ValueError(f"points[{index}] coordinates must be >= 0")
        normalized.append([round(x, 3), round(y, 3)])

    unique_points = {(round(point[0], 3), round(point[1], 3)) for point in normalized}
    if len(unique_points) < MIN_POLYGON_POINTS:
        raise ValueError("points must contain at least three unique coordinates")
    return normalized


def validate_polygon_points(errors: list[str], value: Any, path: str) -> None:
    try:
        normalize_polygon_points(value)
    except ValueError as exc:
        errors.append(f"{path} {exc}")


def validate_selection_mask(
    errors: list[str],
    mask: Any,
    prefix: str,
    safety: dict[str, Any],
    target_bbox: Any | None = None,
    allow_composite: bool = True,
) -> None:
    if not isinstance(mask, dict):
        errors.append(f"{prefix}.selection_mask must be an object")
        return

    source = mask.get("source")
    allowed_sources = SELECTION_MASK_SOURCES if allow_composite else SELECTION_MASK_ITEM_SOURCES
    if source not in allowed_sources:
        allowed = ", ".join(sorted(allowed_sources))
        errors.append(f"{prefix}.selection_mask.source must be one of: {allowed}")

    operation = mask.get("operation")
    if operation is not None and operation not in SELECTION_MASK_OPERATIONS:
        allowed = ", ".join(sorted(SELECTION_MASK_OPERATIONS))
        errors.append(f"{prefix}.selection_mask.operation must be one of: {allowed}")
    if source == "current_selection" and operation not in {None, "replace"}:
        errors.append(f"{prefix}.selection_mask.operation must be replace for source current_selection")
    if source == "alpha_mask" and operation not in {None, "replace"}:
        errors.append(
            f"{prefix}.selection_mask.operation must be replace for source alpha_mask; "
            "generate a new alpha mask instead of combining soft masks through Photoshop selection booleans"
        )
    if mask.get("invert") is True and operation not in {None, "replace"}:
        errors.append(f"{prefix}.selection_mask.invert is only supported when operation is replace")

    if source == "composite":
        items = mask.get("items")
        if not allow_composite:
            errors.append(f"{prefix}.selection_mask.source composite cannot be nested")
        elif not isinstance(items, list):
            errors.append(f"{prefix}.selection_mask.items must be an array")
        elif len(items) < 1 or len(items) > MAX_COMPOSITE_SELECTION_ITEMS:
            errors.append(
                f"{prefix}.selection_mask.items must contain 1-{MAX_COMPOSITE_SELECTION_ITEMS} items"
            )
        else:
            for index, item in enumerate(items):
                item_prefix = f"{prefix}.selection_mask.items[{index}]"
                if not isinstance(item, dict):
                    errors.append(f"{item_prefix} must be an object")
                    continue
                item_operation = item.get("operation")
                if item_operation not in SELECTION_MASK_OPERATIONS:
                    allowed = ", ".join(sorted(SELECTION_MASK_OPERATIONS))
                    errors.append(f"{item_prefix}.operation must be one of: {allowed}")
                elif index == 0 and item_operation != "replace":
                    errors.append(f"{item_prefix}.operation must be replace")
                item_source = item.get("source")
                if item_source == "current_selection" and item_operation != "replace":
                    errors.append(f"{item_prefix}.operation must be replace for source current_selection")
                if item.get("invert") is True and item_operation != "replace":
                    errors.append(f"{item_prefix}.invert is only supported when operation is replace")
                validate_selection_mask(
                    errors,
                    item,
                    item_prefix,
                    safety,
                    None,
                    allow_composite=False,
                )

    if source == "bbox":
        if "bbox" not in mask and target_bbox is None:
            errors.append(f"{prefix}.selection_mask.bbox or {prefix}.bbox is required when source is 'bbox'")
        else:
            validate_bbox(
                errors,
                mask["bbox"] if "bbox" in mask else target_bbox,
                f"{prefix}.selection_mask.bbox",
            )

    if source == "polygon":
        if "points" not in mask:
            errors.append(f"{prefix}.selection_mask.points is required when source is 'polygon'")
        else:
            validate_polygon_points(errors, mask["points"], f"{prefix}.selection_mask.points")
        if "label" in mask and not isinstance(mask["label"], str):
            errors.append(f"{prefix}.selection_mask.label must be a string")

    if source == "color_range":
        color = mask.get("color")
        preset = mask.get("preset")
        if color is None and preset is None:
            errors.append(f"{prefix}.selection_mask.color_range requires color or preset")
        if color is not None:
            if not isinstance(color, dict):
                errors.append(f"{prefix}.selection_mask.color must be an object with r,g,b")
            else:
                for channel in ("r", "g", "b"):
                    validate_number(errors, color.get(channel), f"{prefix}.selection_mask.color.{channel}", 0, 255)
        if preset is not None and preset not in {
            "sampled",
            "reds",
            "yellows",
            "greens",
            "cyans",
            "blues",
            "magentas",
            "skin_tones",
            "highlights",
            "midtones",
            "shadows",
        }:
            errors.append(f"{prefix}.selection_mask.preset is not supported")
        if preset == "sampled" and color is None:
            errors.append(f"{prefix}.selection_mask.preset sampled requires color")
        if "fuzziness" in mask:
            validate_number(errors, mask["fuzziness"], f"{prefix}.selection_mask.fuzziness", 0, 200)
        if "localized_color_clusters" in mask:
            validate_bool(errors, mask["localized_color_clusters"], f"{prefix}.selection_mask.localized_color_clusters")

    if source == "alpha_mask":
        asset_path = mask.get("asset_path")
        asset_uri = mask.get("asset_uri") or mask.get("uri")
        if not asset_path and not asset_uri:
            errors.append(f"{prefix}.selection_mask.asset_path or asset_uri is required when source is 'alpha_mask'")
        if asset_path is not None:
            try:
                resolved_alpha_path = resolve_workspace_asset_path(asset_path)
                if resolved_alpha_path.suffix.lower() != ".png":
                    errors.append(f"{prefix}.selection_mask.asset_path must point to a PNG alpha mask")
            except (FileNotFoundError, TypeError, ValueError) as exc:
                errors.append(f"{prefix}.selection_mask.asset_path invalid: {exc}")
        if asset_uri is not None:
            if not isinstance(asset_uri, str) or not (
                asset_uri.startswith("http://127.0.0.1:17860/assets/")
                or asset_uri.startswith("http://localhost:17860/assets/")
            ):
                errors.append(
                    f"{prefix}.selection_mask.asset_uri must point to local backend /assets when source is 'alpha_mask'"
                )
            elif not urllib.parse.urlparse(asset_uri).path.lower().endswith(".png"):
                errors.append(f"{prefix}.selection_mask.asset_uri must point to a PNG alpha mask")
        if "threshold" in mask:
            validate_number(errors, mask["threshold"], f"{prefix}.selection_mask.threshold", 0, 1)
        if "show_marching_ants" in mask:
            validate_bool(errors, mask["show_marching_ants"], f"{prefix}.selection_mask.show_marching_ants")
        if "label" in mask and not isinstance(mask["label"], str):
            errors.append(f"{prefix}.selection_mask.label must be a string")

    if "feather" in mask:
        validate_number(errors, mask["feather"], f"{prefix}.selection_mask.feather", 0, 500)
    if "invert" in mask:
        validate_bool(errors, mask["invert"], f"{prefix}.selection_mask.invert")
    if "use_acr_mask" in mask:
        validate_bool(errors, mask["use_acr_mask"], f"{prefix}.selection_mask.use_acr_mask")
        if mask["use_acr_mask"] and safety.get("allow_experimental_acr_masks") is not True:
            errors.append(
                f"{prefix}.selection_mask.use_acr_mask requires "
                "plan.safety.allow_experimental_acr_masks to be true"
            )


def validate_acr_ai_mask_item(errors: list[str], value: Any, path: str) -> None:
    if not isinstance(value, dict):
        errors.append(f"{path} must be an object")
        return

    mask_type = value.get("mask_type")
    if mask_type not in ACR_AI_MASK_TYPES:
        allowed = ", ".join(sorted(ACR_AI_MASK_TYPES))
        errors.append(f"{path}.mask_type must be one of: {allowed}")

    if "op" in value and value["op"] not in ACR_AI_MASK_COMBINE_OPS:
        allowed = ", ".join(sorted(ACR_AI_MASK_COMBINE_OPS))
        errors.append(f"{path}.op must be one of: {allowed}")

    if "person_index" in value:
        if not isinstance(value["person_index"], int) or value["person_index"] < 0:
            errors.append(f"{path}.person_index must be a non-negative integer")

    if "parts" in value:
        parts = value["parts"]
        if not isinstance(parts, list) or not parts:
            errors.append(f"{path}.parts must be a non-empty array")
        else:
            for index, part in enumerate(parts):
                if part not in ACR_AI_MASK_PARTS:
                    allowed = ", ".join(sorted(ACR_AI_MASK_PARTS))
                    errors.append(f"{path}.parts[{index}] must be one of: {allowed}")


def validate_acr_ai_mask(
    errors: list[str],
    value: Any,
    prefix: str,
    safety: dict[str, Any],
) -> None:
    if safety.get("allow_experimental_acr_masks") is not True:
        errors.append(
            f"{prefix}.acr_ai_mask requires plan.safety.allow_experimental_acr_masks to be true"
        )
    if not isinstance(value, dict):
        errors.append(f"{prefix}.acr_ai_mask must be an object")
        return

    engine = value.get("engine", "camera_raw_internal")
    if engine not in ACR_AI_MASK_ENGINES:
        allowed = ", ".join(sorted(ACR_AI_MASK_ENGINES))
        errors.append(f"{prefix}.acr_ai_mask.engine must be one of: {allowed}")

    validate_acr_ai_mask_item(errors, value, f"{prefix}.acr_ai_mask")

    combine = value.get("combine")
    if combine is not None:
        if not isinstance(combine, list):
            errors.append(f"{prefix}.acr_ai_mask.combine must be an array")
        elif len(combine) > 8:
            errors.append(f"{prefix}.acr_ai_mask.combine must contain at most 8 items")
        else:
            for index, item in enumerate(combine):
                validate_acr_ai_mask_item(errors, item, f"{prefix}.acr_ai_mask.combine[{index}]")


def validate_target(
    errors: list[str],
    target: Any,
    prefix: str,
    safety: dict[str, Any],
) -> None:
    if not isinstance(target, dict):
        errors.append(f"{prefix}.target must be an object")
        return

    target_type = target.get("type")
    if target_type not in SUPPORTED_TARGET_TYPES:
        allowed = ", ".join(sorted(SUPPORTED_TARGET_TYPES))
        errors.append(f"{prefix}.target.type must be one of: {allowed}")
        return

    if target_type == "selection_mask":
        validate_selection_mask(
            errors,
            target.get("selection_mask"),
            f"{prefix}.target",
            safety,
            target.get("bbox"),
        )
    elif target_type == "acr_ai_mask":
        validate_acr_ai_mask(
            errors,
            target.get("acr_ai_mask"),
            f"{prefix}.target",
            safety,
        )
    elif "selection_mask" in target:
        errors.append(f"{prefix}.target.selection_mask is only valid when target.type is 'selection_mask'")

    if target_type != "acr_ai_mask" and "acr_ai_mask" in target:
        errors.append(f"{prefix}.target.acr_ai_mask is only valid when target.type is 'acr_ai_mask'")

    if "bbox" in target:
        validate_bbox(errors, target["bbox"], f"{prefix}.target.bbox")


def validate_supported_op(
    errors: list[str],
    op: dict[str, Any],
    index: int,
    safety: dict[str, Any],
) -> None:
    op_name = op.get("op")
    prefix = f"plan.ops[{index}]"
    if op_name not in SUPPORTED_APPLY_OPS:
        errors.append(
            f"{prefix}.op {op_name!r} is not supported in phase 3A; "
            f"allowed: {', '.join(sorted(SUPPORTED_APPLY_OPS))}"
        )
        return

    validate_target(errors, op.get("target"), prefix, safety)

    params = op.get("params")
    if not isinstance(params, dict) or not params:
        errors.append(f"{prefix}.params must be a non-empty object")
        return

    if op_name == "adjust_exposure":
        unknown = set(params) - EXPOSURE_PARAMS
        if unknown:
            errors.append(f"{prefix}.params has unsupported keys: {', '.join(sorted(unknown))}")
        if "exposure" in params:
            validate_number(errors, params["exposure"], f"{prefix}.params.exposure", -5, 5)
        if "offset" in params:
            validate_number(errors, params["offset"], f"{prefix}.params.offset", -1, 1)
        if "gamma" in params:
            validate_number(errors, params["gamma"], f"{prefix}.params.gamma", 0.01, 9.99)

    if op_name == "adjust_vibrance":
        unknown = set(params) - VIBRANCE_PARAMS
        if unknown:
            errors.append(f"{prefix}.params has unsupported keys: {', '.join(sorted(unknown))}")
        if "vibrance" in params:
            validate_number(errors, params["vibrance"], f"{prefix}.params.vibrance", -100, 100)
        if "saturation" in params:
            validate_number(errors, params["saturation"], f"{prefix}.params.saturation", -100, 100)

    if op_name == "adjust_color_balance":
        unknown = set(params) - COLOR_BALANCE_PARAMS
        if unknown:
            errors.append(f"{prefix}.params has unsupported keys: {', '.join(sorted(unknown))}")
        for key in ("shadows", "midtones", "highlights"):
            if key in params:
                validate_color_balance_triplet(errors, params[key], f"{prefix}.params.{key}")
        if "preserve_luminosity" in params and not isinstance(params["preserve_luminosity"], bool):
            errors.append(f"{prefix}.params.preserve_luminosity must be a boolean")

    if op_name == "adjust_hue_saturation":
        unknown = set(params) - HUE_SATURATION_PARAMS
        if unknown:
            errors.append(f"{prefix}.params has unsupported keys: {', '.join(sorted(unknown))}")
        if "range" in params and params["range"] not in HUE_SATURATION_RANGES:
            allowed = ", ".join(sorted(HUE_SATURATION_RANGES))
            errors.append(f"{prefix}.params.range must be one of: {allowed}")
        if "hue" in params:
            validate_number(errors, params["hue"], f"{prefix}.params.hue", -180, 180)
        if "saturation" in params:
            validate_number(errors, params["saturation"], f"{prefix}.params.saturation", -100, 100)
        if "lightness" in params:
            validate_number(errors, params["lightness"], f"{prefix}.params.lightness", -100, 100)

    if op_name == "adjust_curves":
        unknown = set(params) - CURVES_PARAMS
        if unknown:
            errors.append(f"{prefix}.params has unsupported keys: {', '.join(sorted(unknown))}")
        channel = str(params.get("channel", "composite"))
        if channel not in CURVES_CHANNELS:
            allowed = ", ".join(sorted(CURVES_CHANNELS))
            errors.append(f"{prefix}.params.channel must be one of: {allowed}")
        points = params.get("points")
        if points is not None:
            if not isinstance(points, list) or not 2 <= len(points) <= 32:
                errors.append(f"{prefix}.params.points must contain 2..32 points")
            else:
                for point_index, point in enumerate(points):
                    point_path = f"{prefix}.params.points[{point_index}]"
                    if isinstance(point, dict):
                        x = point.get("x", point.get("input"))
                        y = point.get("y", point.get("output"))
                    elif isinstance(point, (list, tuple)) and len(point) >= 2:
                        x, y = point[0], point[1]
                    else:
                        errors.append(f"{point_path} must be {{x,y}} or [x,y]")
                        continue
                    validate_number(errors, x, f"{point_path}.x", 0, 255)
                    validate_number(errors, y, f"{point_path}.y", 0, 255)

    if op_name == "adjust_levels":
        unknown = set(params) - LEVELS_PARAMS
        if unknown:
            errors.append(f"{prefix}.params has unsupported keys: {', '.join(sorted(unknown))}")
        channel = str(params.get("channel", "composite"))
        if channel not in LEVELS_CHANNELS:
            allowed = ", ".join(sorted(LEVELS_CHANNELS))
            errors.append(f"{prefix}.params.channel must be one of: {allowed}")
        validate_number(errors, params.get("input_black", 0), f"{prefix}.params.input_black", 0, 255)
        validate_number(errors, params.get("input_white", 255), f"{prefix}.params.input_white", 0, 255)
        validate_number(errors, params.get("gamma", 1), f"{prefix}.params.gamma", 0.1, 9.99)
        validate_number(errors, params.get("output_black", 0), f"{prefix}.params.output_black", 0, 255)
        validate_number(errors, params.get("output_white", 255), f"{prefix}.params.output_white", 0, 255)
        if (
            isinstance(params.get("input_black", 0), (int, float))
            and isinstance(params.get("input_white", 255), (int, float))
            and float(params.get("input_black", 0)) >= float(params.get("input_white", 255))
        ):
            errors.append(f"{prefix}.params.input_black must be smaller than input_white")
        if (
            isinstance(params.get("output_black", 0), (int, float))
            and isinstance(params.get("output_white", 255), (int, float))
            and float(params.get("output_black", 0)) >= float(params.get("output_white", 255))
        ):
            errors.append(f"{prefix}.params.output_black must be smaller than output_white")

    if op_name == "adjust_selective_color":
        unknown = set(params) - SELECTIVE_COLOR_PARAMS
        if unknown:
            errors.append(f"{prefix}.params has unsupported keys: {', '.join(sorted(unknown))}")
        method = str(params.get("method", "relative"))
        if method not in SELECTIVE_COLOR_METHODS:
            errors.append(f"{prefix}.params.method must be relative or absolute")
        corrections = params.get("corrections", params.get("colors"))
        if not isinstance(corrections, list) or not corrections:
            errors.append(f"{prefix}.params.corrections must be a non-empty array")
        elif len(corrections) > 12:
            errors.append(f"{prefix}.params.corrections must contain at most 12 items")
        else:
            for correction_index, correction in enumerate(corrections):
                correction_path = f"{prefix}.params.corrections[{correction_index}]"
                if not isinstance(correction, dict):
                    errors.append(f"{correction_path} must be an object")
                    continue
                color_range = str(correction.get("range", correction.get("color", "")))
                if color_range not in SELECTIVE_COLOR_RANGES:
                    allowed = ", ".join(sorted(SELECTIVE_COLOR_RANGES))
                    errors.append(f"{correction_path}.range must be one of: {allowed}")
                for key in ("cyan", "magenta", "yellow", "black"):
                    if key in correction:
                        validate_number(errors, correction[key], f"{correction_path}.{key}", -100, 100)

    if op_name == "adjust_gradient_map":
        unknown = set(params) - GRADIENT_MAP_PARAMS
        if unknown:
            errors.append(f"{prefix}.params has unsupported keys: {', '.join(sorted(unknown))}")
        stops = params.get("stops")
        if stops is not None:
            if not isinstance(stops, list) or not 2 <= len(stops) <= 16:
                errors.append(f"{prefix}.params.stops must contain 2..16 color stops")
            else:
                for stop_index, stop in enumerate(stops):
                    stop_path = f"{prefix}.params.stops[{stop_index}]"
                    if not isinstance(stop, dict):
                        errors.append(f"{stop_path} must be an object")
                        continue
                    validate_number(errors, stop.get("location", stop.get("position", 0)), f"{stop_path}.location", 0, 100)
                    color = stop.get("color", stop)
                    if not isinstance(color, dict):
                        errors.append(f"{stop_path}.color must be an RGB object")

    if op_name == "adjust_color_lookup":
        unknown = set(params) - COLOR_LOOKUP_PARAMS
        if unknown:
            errors.append(f"{prefix}.params has unsupported keys: {', '.join(sorted(unknown))}")
        if not any(params.get(key) for key in ("name", "lookup", "profile")):
            errors.append(f"{prefix}.params.name, lookup, or profile is required")

    if op_name == "camera_raw_filter":
        target = op.get("target") if isinstance(op.get("target"), dict) else {}
        if target.get("type") not in {"global", "acr_ai_mask"}:
            errors.append(
                f"{prefix}.target.type must be 'global' or 'acr_ai_mask' for camera_raw_filter"
            )
        validate_camera_raw_params(errors, params, prefix)

    target = op.get("target") if isinstance(op.get("target"), dict) else {}
    mask = target.get("selection_mask") if isinstance(target, dict) else None
    if isinstance(mask, dict) and mask.get("use_acr_mask") and op_name != "camera_raw_filter":
        errors.append(f"{prefix}.target.selection_mask.use_acr_mask is only supported by camera_raw_filter")
    if target.get("type") == "acr_ai_mask" and op_name != "camera_raw_filter":
        errors.append(f"{prefix}.target.type acr_ai_mask is only supported by camera_raw_filter")

    layer = op.get("layer")
    if layer is not None:
        if not isinstance(layer, dict):
            errors.append(f"{prefix}.layer must be an object")
        else:
            opacity = layer.get("opacity")
            if opacity is not None:
                validate_number(errors, opacity, f"{prefix}.layer.opacity", 0, 100)


def plan_summary(plan: dict[str, Any]) -> dict[str, Any]:
    ops = plan.get("ops") if isinstance(plan, dict) else []
    if not isinstance(ops, list):
        ops = []

    def summarize_mask(mask: Any) -> dict[str, Any]:
        if not isinstance(mask, dict):
            return {"source": None, "operation": None}
        summary: dict[str, Any] = {
            "source": mask.get("source"),
            "operation": mask.get("operation", "replace"),
            "label": mask.get("label"),
            "feather": mask.get("feather"),
            "invert": mask.get("invert", False),
        }
        if mask.get("source") == "polygon":
            points = mask.get("points")
            summary["point_count"] = len(points) if isinstance(points, list) else None
        if mask.get("source") == "bbox":
            summary["bbox"] = mask.get("bbox")
        if mask.get("source") == "alpha_mask":
            summary["asset_path"] = mask.get("asset_path")
            summary["asset_uri"] = mask.get("asset_uri") or mask.get("uri")
            summary["threshold"] = mask.get("threshold")
            summary["show_marching_ants"] = mask.get("show_marching_ants", False)
        if mask.get("source") == "composite":
            items = mask.get("items")
            summary["item_count"] = len(items) if isinstance(items, list) else 0
            summary["items"] = [
                summarize_mask(item) for item in items
            ] if isinstance(items, list) else []
        return summary

    def summarize_op(op: Any, index: int) -> dict[str, Any]:
        if not isinstance(op, dict):
            return {
                "index": index,
                "op": None,
                "target": None,
                "mask_source": None,
                "layer_id": None,
                "layer_name": None,
            }
        target = op.get("target") if isinstance(op.get("target"), dict) else {}
        mask = target.get("selection_mask") if isinstance(target.get("selection_mask"), dict) else {}
        layer = op.get("layer") if isinstance(op.get("layer"), dict) else {}
        params = op.get("params") if isinstance(op.get("params"), dict) else {}
        summary = {
            "index": index,
            "op": op.get("op"),
            "target": target.get("type"),
            "mask_source": mask.get("source"),
            "mask_operation": mask.get("operation", "replace") if mask else None,
            "layer_id": target.get("layer_id"),
            "layer_name": layer.get("name"),
        }
        if target.get("type") == "acr_ai_mask":
            acr_mask = target.get("acr_ai_mask") if isinstance(target.get("acr_ai_mask"), dict) else {}
            summary["acr_ai_mask_engine"] = acr_mask.get("engine", "camera_raw_internal")
            summary["acr_ai_mask_type"] = acr_mask.get("mask_type")
            summary["acr_ai_mask_parts"] = acr_mask.get("parts")
            summary["acr_ai_mask_combine_count"] = len(acr_mask.get("combine", [])) if isinstance(acr_mask.get("combine"), list) else 0
        if mask.get("source") == "polygon":
            points = mask.get("points")
            summary["mask_label"] = mask.get("label")
            summary["mask_point_count"] = len(points) if isinstance(points, list) else None
        if mask:
            summary["mask"] = summarize_mask(mask)
        if op.get("op") == "camera_raw_filter":
            summary["param_groups"] = [
                key for key in ("basic", "color", "presence", "detail") if isinstance(params.get(key), dict)
            ]
            summary["flat_params"] = sorted(set(params) & CAMERA_RAW_FLAT_PARAMS)
        return summary

    return {
        "plan_id": plan.get("plan_id"),
        "goal": plan.get("goal"),
        "op_count": len(ops),
        "ops": [summarize_op(op, index) for index, op in enumerate(ops)],
    }


def api_error(code: str, message: str, details: Any | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "status": "error",
        "schema_version": "ps-agent/v1",
        "error": {"code": code, "message": message},
    }
    if details is not None:
        payload["error"]["details"] = details
    return payload


def parse_document_bbox(value: Any) -> dict[str, float]:
    errors: list[str] = []
    validate_bbox(errors, value, "document_bbox")
    if errors:
        raise ValueError("; ".join(errors))
    return {
        "x": float(value["x"]),
        "y": float(value["y"]),
        "width": float(value["width"]),
        "height": float(value["height"]),
    }


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


def selection_candidate_alpha_path(candidate: dict[str, Any]) -> Path:
    params = candidate.get("params") if isinstance(candidate.get("params"), dict) else {}
    selection_mask = params.get("selection_mask") if isinstance(params.get("selection_mask"), dict) else {}
    alpha_mask = params.get("alpha_mask") if isinstance(params.get("alpha_mask"), dict) else {}
    for source in (selection_mask, alpha_mask, params):
        asset_path = source.get("asset_path") if isinstance(source, dict) else None
        if asset_path:
            return resolve_workspace_asset_path(asset_path)
    raise ValueError(f"candidate {candidate.get('candidate_id')} does not provide params.asset_path or params.selection_mask.asset_path")


def apply_soft_selection_recipe(body: dict[str, Any], asset_url_builder) -> dict[str, Any]:
    recipe = body.get("selection_recipe") or body.get("recipe")
    validation = validate_selection_recipe({"selection_recipe": recipe})
    if not validation.get("valid"):
        return {
            "status": "error",
            "schema_version": "ps-agent/v1",
            "error": {
                "code": "invalid_selection_recipe",
                "message": "Selection recipe validation failed.",
                "details": validation.get("errors", []),
            },
            "warnings": validation.get("warnings", []),
        }
    merge_plan = recipe.get("merge_plan") if isinstance(recipe, dict) else {}
    if merge_plan.get("mode") != "soft_alpha":
        return create_api_job("selection_recipe", body)

    candidates = {
        str(candidate.get("candidate_id")): candidate
        for candidate in recipe.get("candidates", [])
        if isinstance(candidate, dict)
    }
    include_paths: list[Path] = []
    exclude_paths: list[Path] = []
    intersect_mode = False
    try:
        for index, item in enumerate(merge_plan.get("items", [])):
            candidate_id = str(item.get("candidate_id") or "")
            operation = str(item.get("operation") or ("replace" if index == 0 else "add"))
            path = selection_candidate_alpha_path(candidates[candidate_id])
            if operation == "subtract":
                exclude_paths.append(path)
            else:
                include_paths.append(path)
            if operation == "intersect":
                intersect_mode = True
    except Exception as exc:
        return api_error("soft_alpha_candidate_unavailable", str(exc))

    job_id = f"job-{time.strftime('%Y%m%d%H%M%S', time.gmtime())}-{secrets.token_hex(4)}"
    asset_dir = ASSET_ROOT / job_id
    luma_path = asset_dir / "selection_recipe_alpha_luma.png"
    alpha_path = asset_dir / "selection_recipe_alpha_mask.png"
    try:
        report = compose_soft_masks(
            include_paths,
            luma_path,
            merge_mode="intersect" if intersect_mode else "union",
            exclude_paths=exclude_paths,
        )
        from PIL import Image

        with Image.open(luma_path) as luma_file:
            mask = luma_file.convert("L")
            rgba = Image.new("RGBA", mask.size, (255, 255, 255, 0))
            rgba.putalpha(mask)
            rgba.save(alpha_path)
    except Exception as exc:
        return api_error("soft_alpha_compose_failed", str(exc))

    relative_path = f"{job_id}/{alpha_path.name}"
    luma_relative_path = f"{job_id}/{luma_path.name}"
    raw_relative_path = f"{job_id}/selection_recipe_alpha_mask.gray"
    raw_path = asset_dir / "selection_recipe_alpha_mask.gray"
    from PIL import Image
    with Image.open(luma_path) as luma_file:
        mask = luma_file.convert("L")
        mask_width, mask_height = mask.size
        raw_path.write_bytes(mask.tobytes())
    selection_mask = {
        "source": "alpha_mask",
        "asset_path": str(alpha_path),
        "asset_uri": asset_url_builder(relative_path) if asset_url_builder else None,
        "raw_asset_path": str(raw_path),
        "raw_asset_uri": asset_url_builder(raw_relative_path) if asset_url_builder else None,
        "mask_width": mask_width,
        "mask_height": mask_height,
        "threshold": float((merge_plan.get("threshold") if isinstance(merge_plan, dict) else None) or 0.5),
        "feather": float((merge_plan.get("feather") if isinstance(merge_plan, dict) else None) or 0),
        "invert": bool((merge_plan.get("invert") if isinstance(merge_plan, dict) else False)),
        "label": str(recipe.get("recipe_id") or "selection_recipe_alpha")[:128],
    }
    return {
        "status": "ok",
        "schema_version": "ps-agent/v1",
        "recipe_id": recipe.get("recipe_id"),
        "workflow_id": recipe.get("workflow_id"),
        "stage_id": recipe.get("stage_id"),
        "selection_recipe": validation.get("summary"),
        "selection_mask": selection_mask,
        "alpha_mask": asset_payload(relative_path, alpha_path, "image/png", asset_url_builder),
        "alpha_luma": asset_payload(luma_relative_path, luma_path, "image/png", asset_url_builder),
        "alpha_raw": asset_payload(raw_relative_path, raw_path, "application/octet-stream", asset_url_builder),
        "merge_report": report,
        "warnings": validation.get("warnings", []),
    }


def polygon_bbox(points: list[list[float]]) -> dict[str, float]:
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    left = min(xs)
    top = min(ys)
    return {
        "x": round(left, 3),
        "y": round(top, 3),
        "width": round(max(xs) - left, 3),
        "height": round(max(ys) - top, 3),
    }


def clamp_point_to_bbox(point: list[float], bbox: dict[str, float]) -> list[float]:
    x_min = bbox["x"]
    y_min = bbox["y"]
    x_max = bbox["x"] + bbox["width"]
    y_max = bbox["y"] + bbox["height"]
    return [
        round(max(x_min, min(point[0], x_max)), 3),
        round(max(y_min, min(point[1], y_max)), 3),
    ]


def expand_polygon(points: list[list[float]], expand_px: float, bbox: dict[str, float]) -> list[list[float]]:
    if expand_px <= 0:
        return [clamp_point_to_bbox(point, bbox) for point in points]
    center_x = sum(point[0] for point in points) / len(points)
    center_y = sum(point[1] for point in points) / len(points)
    expanded: list[list[float]] = []
    for x, y in points:
        dx = x - center_x
        dy = y - center_y
        length = math.hypot(dx, dy)
        if length <= 0.000001:
            expanded.append(clamp_point_to_bbox([x, y], bbox))
            continue
        expanded.append(
            clamp_point_to_bbox(
                [
                    x + (dx / length) * expand_px,
                    y + (dy / length) * expand_px,
                ],
                bbox,
            )
        )
    return expanded


def smooth_polygon(points: list[list[float]], amount: float) -> list[list[float]]:
    amount = max(0.0, min(float(amount), 0.75))
    if amount <= 0 or len(points) < 4:
        return points
    smoothed: list[list[float]] = []
    for index, point in enumerate(points):
        previous_point = points[index - 1]
        next_point = points[(index + 1) % len(points)]
        neighbor_x = (previous_point[0] + next_point[0]) / 2
        neighbor_y = (previous_point[1] + next_point[1]) / 2
        smoothed.append(
            [
                round(point[0] * (1 - amount) + neighbor_x * amount, 3),
                round(point[1] * (1 - amount) + neighbor_y * amount, 3),
            ]
        )
    return smoothed


def requested_face_parts(parts: Any) -> list[tuple[str, str]]:
    if not isinstance(parts, list) or not parts:
        raise ValueError("parts must be a non-empty array")
    expanded: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for raw_part in parts:
        if raw_part not in FACE_SELECTION_PARTS:
            allowed = ", ".join(sorted(FACE_SELECTION_PARTS))
            raise ValueError(f"unsupported face part {raw_part!r}; allowed: {allowed}")
        candidates = [("both_eyes", "left_eye"), ("both_eyes", "right_eye")] if raw_part == "both_eyes" else [(raw_part, raw_part)]
        for candidate in candidates:
            if candidate not in seen:
                seen.add(candidate)
                expanded.append(candidate)
    return expanded


def face_landmarks_bbox(landmarks: Any) -> tuple[float, float, float, float]:
    xs = [float(landmark.x) for landmark in landmarks if math.isfinite(float(landmark.x))]
    ys = [float(landmark.y) for landmark in landmarks if math.isfinite(float(landmark.y))]
    return min(xs), min(ys), max(xs), max(ys)


def choose_face_index(face_landmarks: list[Any], requested_index: Any) -> tuple[int, str]:
    if not face_landmarks:
        raise ValueError("face_not_detected")
    if requested_index is not None:
        try:
            parsed = int(requested_index)
        except (TypeError, ValueError) as exc:
            raise ValueError("face_index must be an integer") from exc
        if parsed < 0 or parsed >= len(face_landmarks):
            raise ValueError(f"face_index must be between 0 and {len(face_landmarks) - 1}")
        return parsed, "requested_face_index"

    largest_index = 0
    largest_area = -1.0
    for index, landmarks in enumerate(face_landmarks):
        left, top, right, bottom = face_landmarks_bbox(landmarks)
        area = max(0.0, right - left) * max(0.0, bottom - top)
        if area > largest_area:
            largest_area = area
            largest_index = index
    return largest_index, "largest_face"


def run_face_landmarker(asset_path: Path, max_faces: int) -> tuple[Any, tuple[int, int] | None, dict[str, Any] | None]:
    try:
        from PIL import Image
    except ImportError:
        return None, None, api_error(
            "pillow_not_installed",
            "Pillow is required to read face crop dimensions. Install backend/requirements-ml.txt.",
        )

    try:
        import mediapipe as mp
        from mediapipe.tasks import python as mp_python
        from mediapipe.tasks.python import vision
    except ImportError:
        return None, None, api_error(
            "mediapipe_not_installed",
            "MediaPipe is not installed. Install backend/requirements-ml.txt to enable local face landmarks.",
        )

    model_path = CURRENT_DIR / "models" / "face_landmarker.task"
    if not model_path.is_file():
        return None, None, api_error(
            "face_landmarker_model_missing",
            f"Missing MediaPipe model file: {model_path}",
            {
                "expected_path": str(model_path),
                "note": "Download the official MediaPipe face_landmarker.task model and place it here.",
            },
        )

    with Image.open(asset_path) as image_file:
        image_size = image_file.size

    options = vision.FaceLandmarkerOptions(
        base_options=mp_python.BaseOptions(model_asset_path=str(model_path)),
        running_mode=vision.RunningMode.IMAGE,
        num_faces=max(1, min(int(max_faces), 8)),
    )
    image = mp.Image.create_from_file(str(asset_path))
    with vision.FaceLandmarker.create_from_options(options) as landmarker:
        result = landmarker.detect(image)
    return result, image_size, None


def landmark_polygon(
    landmarks: Any,
    indexes: list[int],
    image_size: tuple[int, int],
    document_bbox: dict[str, float],
    scale_factor: float,
) -> list[list[float]]:
    width, height = image_size
    points: list[list[float]] = []
    for index in indexes:
        if index < 0 or index >= len(landmarks):
            continue
        landmark = landmarks[index]
        x = document_bbox["x"] + (float(landmark.x) * width) / scale_factor
        y = document_bbox["y"] + (float(landmark.y) * height) / scale_factor
        points.append(clamp_point_to_bbox([x, y], document_bbox))
    return normalize_polygon_points(points)


def generate_face_selection(body: dict[str, Any]) -> dict[str, Any]:
    try:
        asset_path = resolve_workspace_asset_path(body.get("asset_path"))
        document_bbox = parse_document_bbox(body.get("document_bbox"))
        parts = requested_face_parts(body.get("parts"))
        scale_factor = float(body.get("scale_factor"))
        if not math.isfinite(scale_factor) or scale_factor <= 0:
            raise ValueError("scale_factor must be a positive number")
        expand_px = max(0.0, min(float(body.get("expand_px", 0) or 0), 256.0))
        smooth_amount = max(0.0, min(float(body.get("smooth", 0) or 0), 0.75))
        feather = max(0.0, min(float(body.get("feather", 12) or 0), 500.0))
        max_faces = clamp_int(body.get("max_faces", 4), 1, 8, 4)
    except FileNotFoundError as exc:
        return api_error("asset_not_found", str(exc))
    except (TypeError, ValueError) as exc:
        return api_error("invalid_face_selection_request", str(exc))

    result, image_size, dependency_error = run_face_landmarker(asset_path, max_faces)
    if dependency_error is not None:
        return dependency_error

    face_landmarks = list(getattr(result, "face_landmarks", []) or [])
    if not face_landmarks:
        return api_error("face_not_detected", "MediaPipe Face Landmarker did not detect a face in this crop.")

    try:
        face_index, policy = choose_face_index(face_landmarks, body.get("face_index"))
    except ValueError as exc:
        return api_error("invalid_face_index", str(exc))

    selected_landmarks = face_landmarks[face_index]
    selections = []
    for requested_part, part in parts:
        try:
            raw_points = landmark_polygon(
                selected_landmarks,
                FACE_PART_LANDMARKS[part],
                image_size,
                document_bbox,
                scale_factor,
            )
            points = smooth_polygon(expand_polygon(raw_points, expand_px, document_bbox), smooth_amount)
            points = normalize_polygon_points(points)
        except ValueError as exc:
            return api_error(
                "face_part_polygon_invalid",
                f"Could not generate a usable polygon for {part}: {exc}",
            )

        label = part if requested_part == part else f"{requested_part}:{part}"
        selection_mask = {
            "source": "polygon",
            "label": label,
            "points": points,
            "feather": feather,
            "invert": False,
        }
        selections.append(
            {
                "provider": "face_landmarker",
                "requested_part": requested_part,
                "part": part,
                "source": "polygon",
                "points": points,
                "point_count": len(points),
                "bbox": polygon_bbox(points),
                "feather": feather,
                "confidence": 1.0,
                "selection_mask": selection_mask,
            }
        )

    return {
        "status": "ok",
        "schema_version": "ps-agent/v1",
        "provider": "face_landmarker",
        "asset_path": str(asset_path),
        "document_bbox": document_bbox,
        "scale_factor": scale_factor,
        "face_count": len(face_landmarks),
        "face_index": face_index,
        "selection_policy": policy,
        "selections": selections,
    }


def normalize_api_body(job_type: str, body: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(body)
    if job_type == "export_regions":
        normalized["max_side"] = clamp_int(
            normalized.get("max_side"),
            MIN_REGION_MAX_SIDE,
            MAX_REGION_MAX_SIDE,
            DEFAULT_REGION_MAX_SIDE,
        )
        normalized["quality"] = clamp_int(normalized.get("quality"), 1, 12, 8)
        normalized["upscale_small_regions"] = bool(normalized.get("upscale_small_regions", DEFAULT_REGION_UPSCALE))
    return normalized


def validate_make_selection(body: dict[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    mask = body.get("selection_mask")
    validate_selection_mask(
        errors,
        mask,
        "payload",
        {"allow_experimental_acr_masks": False},
    )
    return {
        "status": "ok",
        "valid": not errors,
        "errors": errors,
        "schema_version": "ps-agent/v1",
    }


def validate_selection_command(body: dict[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    action = body.get("action")
    if action not in SELECTION_COMMAND_ACTIONS:
        allowed = ", ".join(sorted(SELECTION_COMMAND_ACTIONS))
        errors.append(f"action must be one of: {allowed}")

    if action == "modify":
        operation = body.get("operation")
        if operation not in SELECTION_MODIFY_OPERATIONS:
            allowed = ", ".join(sorted(SELECTION_MODIFY_OPERATIONS))
            errors.append(f"operation must be one of: {allowed}")
        validate_number(errors, body.get("amount"), "amount", 0, 500)

    if action in {"save_selection", "load_selection"}:
        channel_name = body.get("channel_name")
        if not isinstance(channel_name, str) or not channel_name.strip():
            errors.append("channel_name must be a non-empty string")
        elif len(channel_name) > 128:
            errors.append("channel_name must be 128 characters or fewer")

    return {
        "status": "ok",
        "valid": not errors,
        "errors": errors,
        "schema_version": "ps-agent/v1",
    }


def validate_native_selection_mask(body: dict[str, Any], *, require_selector_input: bool = True) -> dict[str, Any]:
    errors: list[str] = []
    action = body.get("action")
    if action not in NATIVE_SELECTION_ACTIONS:
        allowed = ", ".join(sorted(NATIVE_SELECTION_ACTIONS))
        errors.append(f"action must be one of: {allowed}")

    if action == "color_range" and require_selector_input:
        color = body.get("color")
        preset = body.get("preset")
        if color is None and preset is None:
            errors.append("color_range requires color or preset")
        if color is not None:
            if not isinstance(color, dict):
                errors.append("color must be an object with r,g,b")
            else:
                for channel in ("r", "g", "b"):
                    validate_number(errors, color.get(channel), f"color.{channel}", 0, 255)
        if preset is not None and preset not in {"sampled", "reds", "yellows", "greens", "cyans", "blues", "magentas", "skin_tones", "highlights", "midtones", "shadows"}:
            errors.append("preset is not supported")
        if preset == "sampled" and color is None:
            errors.append("preset sampled requires color")
        if "fuzziness" in body:
            validate_number(errors, body["fuzziness"], "fuzziness", 0, 200)
        if "localized_color_clusters" in body:
            validate_bool(errors, body["localized_color_clusters"], "localized_color_clusters")

    if action == "focus_area":
        if "in_focus_range" in body:
            validate_number(errors, body["in_focus_range"], "in_focus_range", 0, 10)
        if "noise_level" in body:
            validate_number(errors, body["noise_level"], "noise_level", 0, 10)

    if "feather" in body:
        validate_number(errors, body["feather"], "feather", 0, 500)
    if "invert" in body:
        validate_bool(errors, body["invert"], "invert")
    if "threshold" in body:
        validate_number(errors, body["threshold"], "threshold", 0, 1)
    if "show_marching_ants" in body:
        validate_bool(errors, body["show_marching_ants"], "show_marching_ants")

    return {
        "status": "ok",
        "valid": not errors,
        "errors": errors,
        "schema_version": "ps-agent/v1",
    }


def materialize_native_selection_mask(body: dict[str, Any], asset_url_builder) -> dict[str, Any]:
    validation = validate_native_selection_mask(body, require_selector_input=False)
    if not validation["valid"]:
        return api_error("invalid_native_selection_mask", "Native selection request validation failed.", validation["errors"])

    raw_asset_path = body.get("raw_asset_path")
    if raw_asset_path is None:
        return api_error("native_selection_raw_asset_missing", "raw_asset_path is required to materialize a native selection mask.")

    try:
        raw_path = resolve_workspace_asset_path(raw_asset_path)
        document_size = normalize_size(body.get("document_size") or {"width": body.get("width"), "height": body.get("height")})
        threshold = float(body.get("threshold", 0.5) or 0.5)
        feather = float(body.get("feather", 0) or 0)
        invert = body.get("invert") is True
    except (FileNotFoundError, TypeError, ValueError) as exc:
        return api_error("native_selection_materialize_invalid", str(exc))

    source = str(body.get("action") or "native_selector")
    label = str(body.get("label") or f"photoshop_native_{source}")[:128]
    job_id = safe_path_part(str(body.get("job_id") or f"job-{uuid.uuid4().hex[:8]}"), "job")
    asset_dir = ASSET_ROOT / job_id
    asset_dir.mkdir(parents=True, exist_ok=True)
    try:
        outputs = materialize_full_document_alpha_mask(
            raw_path,
            asset_dir,
            job_id,
            document_size,
            threshold=threshold,
            feather=feather,
            invert=invert,
        )
    except Exception as exc:
        return api_error("native_selection_materialize_failed", str(exc))

    relative_alpha = outputs["relative"]["alpha"]
    relative_luma = outputs["relative"]["luma"]
    relative_raw = outputs["relative"]["raw"]
    selection_mask = {
        "source": "alpha_mask",
        "label": label,
        "asset_path": str(outputs["alpha_path"]),
        "asset_uri": asset_url_builder(relative_alpha) if asset_url_builder else None,
        "raw_asset_path": str(outputs["raw_path"]),
        "raw_asset_uri": asset_url_builder(relative_raw) if asset_url_builder else None,
        "mask_width": document_size["width"],
        "mask_height": document_size["height"],
        "threshold": threshold,
        "feather": 0.0,
        "invert": False,
        "show_marching_ants": body.get("show_marching_ants") is True,
    }
    result = {
        "status": "ok",
        "schema_version": "ps-agent/v1",
        "job_id": job_id,
        "source": f"photoshop_native.{source}",
        "provider": f"photoshop_native.{source}",
        "selection_kind": "native_selection_mask",
        "document_size": document_size,
        "coordinate_space": "document",
        "soft_alpha": True,
        "bounds": outputs["mask_bbox"],
        "area_ratio": outputs["area_ratio"],
        "selected_pixels": outputs["selected_pixels"],
        "selection_mask": selection_mask,
        "alpha_mask": asset_payload(relative_alpha, outputs["alpha_path"], "image/png", asset_url_builder),
        "alpha_luma": asset_payload(relative_luma, outputs["luma_path"], "image/png", asset_url_builder),
        "alpha_raw": asset_payload(relative_raw, outputs["raw_path"], "application/octet-stream", asset_url_builder),
        "warnings": outputs["warnings"],
    }
    artifact = create_region_artifact(
        {
            "label": label,
            "source": f"photoshop_native.{source}",
            "source_result": result,
        },
        asset_url_builder,
    )
    if artifact.get("status") == "ok":
        result["artifact"] = artifact.get("artifact")
        result["artifacts"] = artifact.get("artifacts")
        result["artifact_count"] = artifact.get("artifact_count", 1)
    return result


def normalize_tonal_range_name(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("tonal_range must be a non-empty string")
    key = re.sub(r"[\s\-]+", "_", value.strip().lower())
    normalized = TONAL_RANGE_PRESET_ALIASES.get(key)
    if normalized:
        return normalized
    normalized = TONAL_RANGE_PRESET_ALIASES.get(value.strip())
    if normalized:
        return normalized
    allowed = ", ".join(sorted(TONAL_RANGE_PRESETS))
    raise ValueError(f"tonal_range must be one of: {allowed}")


def validate_modify_steps(errors: list[str], value: Any, path: str = "modify") -> None:
    if not isinstance(value, list) or not value:
        errors.append(f"{path} must be a non-empty array")
        return
    if len(value) > 8:
        errors.append(f"{path} must contain at most 8 steps")
        return
    for index, item in enumerate(value):
        item_path = f"{path}[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{item_path} must be an object")
            continue
        operation = item.get("operation")
        if operation not in SELECTION_MODIFY_OPERATIONS:
            allowed = ", ".join(sorted(SELECTION_MODIFY_OPERATIONS))
            errors.append(f"{item_path}.operation must be one of: {allowed}")
        validate_number(errors, item.get("amount"), f"{item_path}.amount", 0, 500)


def validate_tonal_range_request(body: dict[str, Any], require_channel_name: bool = False) -> dict[str, Any]:
    errors: list[str] = []
    tonal_value = body.get("tonal_range", body.get("range"))
    try:
        tonal_range = normalize_tonal_range_name(tonal_value)
    except ValueError as exc:
        tonal_range = None
        errors.append(str(exc))

    if "fuzziness" in body:
        validate_number(errors, body["fuzziness"], "fuzziness", 0, 200)
    if "localized_color_clusters" in body:
        validate_bool(errors, body["localized_color_clusters"], "localized_color_clusters")
    if "feather" in body:
        validate_number(errors, body["feather"], "feather", 0, 500)
    if "invert" in body:
        validate_bool(errors, body["invert"], "invert")
    if "wait" in body:
        validate_bool(errors, body["wait"], "wait")
    if "timeout_ms" in body:
        validate_number(errors, body["timeout_ms"], "timeout_ms", 1000, 300000)
    if "modify" in body:
        validate_modify_steps(errors, body["modify"])

    if require_channel_name or "channel_name" in body:
        channel_name = body.get("channel_name")
        if not isinstance(channel_name, str) or not channel_name.strip():
            errors.append("channel_name must be a non-empty string")
        elif len(channel_name.strip()) > 128:
            errors.append("channel_name must be 128 characters or fewer")

    return {
        "status": "ok",
        "valid": not errors,
        "errors": errors,
        "schema_version": "ps-agent/v1",
        "tonal_range": tonal_range,
    }


def tonal_range_selection_payload(body: dict[str, Any], forced_tonal_range: str | None = None) -> dict[str, Any]:
    tonal_range = forced_tonal_range or normalize_tonal_range_name(body.get("tonal_range", body.get("range")))
    payload: dict[str, Any] = {
        "action": "color_range",
        "preset": tonal_range,
        "wait": body.get("wait", True),
        "timeout_ms": body.get("timeout_ms", 60000),
    }
    for key in ("fuzziness", "localized_color_clusters", "feather", "invert", "workflow_id", "stage_id", "parent_job_id", "stage_status"):
        if key in body and body[key] is not None:
            payload[key] = body[key]
    return payload


def extract_tonal_range_request(body: dict[str, Any], forced_tonal_range: str | None = None) -> dict[str, Any]:
    validation = validate_tonal_range_request(
        body if forced_tonal_range is None else dict(body, tonal_range=forced_tonal_range),
        require_channel_name=False,
    )
    if not validation["valid"]:
        return {
            "status": "error",
            "schema_version": "ps-agent/v1",
            "valid": False,
            "error": {
                "code": "invalid_tonal_range_request",
                "message": "Tonal range request validation failed.",
                "details": validation["errors"],
            },
        }

    payload = tonal_range_selection_payload(body, forced_tonal_range=forced_tonal_range or validation.get("tonal_range"))
    if bool(body.get("dry_run", False)):
        return {
            "status": "ok",
            "schema_version": "ps-agent/v1",
            "dry_run": True,
            "tonal_range": payload["preset"],
            "native_selection_mask": payload,
        }

    result = create_api_job("native_selection_mask", payload)
    result["tonal_range"] = payload["preset"]
    result["selection_kind"] = "tonal_range"
    return result


def build_luminosity_mask_request(body: dict[str, Any]) -> dict[str, Any]:
    validation = validate_tonal_range_request(body, require_channel_name=True)
    if not validation["valid"]:
        return {
            "status": "error",
            "schema_version": "ps-agent/v1",
            "valid": False,
            "error": {
                "code": "invalid_luminosity_mask_request",
                "message": "Luminosity mask request validation failed.",
                "details": validation["errors"],
            },
        }

    tonal_range = str(validation["tonal_range"])
    channel_name = str(body.get("channel_name", "")).strip()
    modify_steps = body.get("modify") if isinstance(body.get("modify"), list) else []

    if bool(body.get("dry_run", False)):
        return {
            "status": "ok",
            "schema_version": "ps-agent/v1",
            "dry_run": True,
            "tonal_range": tonal_range,
            "channel_name": channel_name,
            "steps": [
                {"step": "select_tonal_range", "payload": tonal_range_selection_payload(body, forced_tonal_range=tonal_range)},
                *[
                    {
                        "step": "modify_selection",
                        "payload": {
                            "action": "modify",
                            "operation": step["operation"],
                            "amount": step["amount"],
                            "wait": True,
                            "timeout_ms": body.get("timeout_ms", 60000),
                        },
                    }
                    for step in modify_steps
                    if isinstance(step, dict)
                ],
                {
                    "step": "save_selection_channel",
                    "payload": {
                        "action": "save_selection",
                        "channel_name": channel_name,
                        "wait": True,
                        "timeout_ms": body.get("timeout_ms", 60000),
                    },
                },
            ],
        }

    if not bool(body.get("wait", True)):
        return api_error(
            "wait_required",
            "ps_build_luminosity_mask must run with wait=true because it executes multiple sequential Photoshop steps.",
        )
    if not QUEUE.uxp_connected():
        return api_error(
            "uxp_not_connected",
            "ps_build_luminosity_mask requires a live UXP connection so the selection can be created and saved immediately.",
        )

    steps: list[dict[str, Any]] = []

    select_result = create_api_job("native_selection_mask", tonal_range_selection_payload(body, forced_tonal_range=tonal_range))
    steps.append(
        {
            "step": "materialize_tonal_range_mask",
            "tonal_range": tonal_range,
            "job_id": select_result.get("job_id"),
            "status": select_result.get("status"),
            "selection_mask": select_result.get("result", {}).get("selection_mask"),
        }
    )
    if select_result.get("status") != "done":
        return {
            "status": select_result.get("status", "error"),
            "schema_version": "ps-agent/v1",
            "tonal_range": tonal_range,
            "channel_name": channel_name,
            "steps": steps,
            "message": "Tonal range selection did not complete, so the alpha channel was not saved.",
            "job": select_result.get("job"),
        }

    tonal_selection_mask = select_result.get("result", {}).get("selection_mask")
    if not isinstance(tonal_selection_mask, dict):
        return {
            "status": "error",
            "schema_version": "ps-agent/v1",
            "tonal_range": tonal_range,
            "channel_name": channel_name,
            "steps": steps,
            "message": "The tonal range mask result did not include a reusable alpha selection_mask.",
            "job": select_result.get("job"),
        }

    make_result = create_api_job(
        "make_selection",
        {
            "selection_mask": tonal_selection_mask,
            "wait": True,
            "timeout_ms": body.get("timeout_ms", 60000),
        },
    )
    steps.append(
        {
            "step": "make_selection_from_alpha_mask",
            "job_id": make_result.get("job_id"),
            "status": make_result.get("status"),
            "selection": make_result.get("result", {}).get("selection"),
        }
    )
    if make_result.get("status") != "done":
        return {
            "status": make_result.get("status", "error"),
            "schema_version": "ps-agent/v1",
            "tonal_range": tonal_range,
            "channel_name": channel_name,
            "steps": steps,
            "message": "The alpha mask could not be written back to the active Photoshop selection.",
            "job": make_result.get("job"),
        }

    for index, modify_step in enumerate(modify_steps):
        modify_payload = {
            "action": "modify",
            "operation": modify_step["operation"],
            "amount": modify_step["amount"],
            "wait": True,
            "timeout_ms": body.get("timeout_ms", 60000),
        }
        modify_result = create_api_job("selection_command", modify_payload)
        steps.append(
            {
                "step": "modify_selection",
                "index": index,
                "operation": modify_step["operation"],
                "amount": modify_step["amount"],
                "job_id": modify_result.get("job_id"),
                "status": modify_result.get("status"),
                "selection": modify_result.get("result", {}).get("selection"),
            }
        )
        if modify_result.get("status") != "done":
            return {
                "status": modify_result.get("status", "error"),
                "schema_version": "ps-agent/v1",
                "tonal_range": tonal_range,
                "channel_name": channel_name,
                "steps": steps,
                "message": "A selection modification step did not complete, so the alpha channel was not saved.",
                "job": modify_result.get("job"),
            }

    save_payload = {
        "action": "save_selection",
        "channel_name": channel_name,
        "wait": True,
        "timeout_ms": body.get("timeout_ms", 60000),
    }
    save_result = create_api_job("selection_command", save_payload)
    steps.append(
        {
            "step": "save_selection_channel",
            "channel_name": channel_name,
            "job_id": save_result.get("job_id"),
            "status": save_result.get("status"),
        }
    )
    if save_result.get("status") != "done":
        return {
            "status": save_result.get("status", "error"),
            "schema_version": "ps-agent/v1",
            "tonal_range": tonal_range,
            "channel_name": channel_name,
            "steps": steps,
            "message": "Saving the active selection to an alpha channel did not complete.",
            "job": save_result.get("job"),
        }

    return {
        "status": "ok",
        "schema_version": "ps-agent/v1",
        "tonal_range": tonal_range,
        "channel_name": channel_name,
        "selection_kind": "luminosity_mask",
        "steps": steps,
        "selection": make_result.get("result", {}).get("selection"),
        "channel": {
            "name": channel_name,
            "saved": True,
        },
    }


def validate_delete_agent_group(body: dict[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    job_id = body.get("job_id")
    group_name = body.get("group_name")
    if group_name is not None:
        if not isinstance(group_name, str) or not group_name.startswith("Codex Agent - ") or len(group_name) > 160:
            errors.append("group_name must start with 'Codex Agent - ' and be 160 characters or fewer")
    elif not isinstance(job_id, str) or not JOB_ID_RE.match(job_id):
        errors.append("job_id must match job-[A-Za-z0-9_.:-]{1,120} when group_name is not provided")
    if "history_name_prefix" in body and body["history_name_prefix"] != "Codex Agent":
        errors.append("history_name_prefix is fixed to 'Codex Agent' for safe group deletion")
    return {
        "status": "ok",
        "valid": not errors,
        "errors": errors,
        "schema_version": "ps-agent/v1",
    }


def validate_apply_mask_to_layer(body: dict[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    layer_id = body.get("layer_id", body.get("target_layer_id"))
    if layer_id is not None and not isinstance(layer_id, (int, str)):
        errors.append("layer_id/target_layer_id must be a Photoshop layer id")
    mask = body.get("selection_mask") or body.get("mask")
    if not isinstance(mask, dict):
        errors.append("selection_mask must be an object")
    else:
        validate_selection_mask(
            errors,
            mask,
            "payload",
            {"allow_experimental_acr_masks": False},
            allow_composite=True,
        )
    return {
        "status": "ok",
        "valid": not errors,
        "errors": errors,
        "schema_version": "ps-agent/v1",
    }


def create_api_job(job_type: str, body: dict[str, Any]) -> dict[str, Any]:
    body = normalize_api_body(job_type, body)
    if job_type == "operation_recipe":
        validation = validate_operation_recipe(body)
        if not validation["valid"]:
            return {
                "status": "error",
                "schema_version": "ps-agent/v1",
                "valid": False,
                "summary": validation.get("summary"),
                "error": {
                    "code": "invalid_operation_recipe",
                    "message": "Operation recipe validation failed.",
                    "details": validation["errors"],
                },
                "warnings": validation["warnings"],
            }
        if bool(body.get("dry_run", False)):
            return {
                "status": "ok",
                "schema_version": "ps-agent/v1",
                "dry_run": True,
                "valid": True,
                "summary": validation.get("summary"),
                "warnings": validation["warnings"],
                "message": "Operation recipe validated; no Photoshop job was queued.",
            }

    if job_type == "selection_recipe":
        validation = validate_selection_recipe(body)
        if not validation["valid"]:
            return {
                "status": "error",
                "schema_version": "ps-agent/v1",
                "valid": False,
                "summary": validation.get("summary"),
                "error": {
                    "code": "invalid_selection_recipe",
                    "message": "Selection recipe validation failed.",
                    "details": validation["errors"],
                },
                "warnings": validation["warnings"],
            }
        if bool(body.get("dry_run", False)):
            return {
                "status": "ok",
                "schema_version": "ps-agent/v1",
                "dry_run": True,
                "valid": True,
                "summary": validation.get("summary"),
                "warnings": validation["warnings"],
                "message": "Selection recipe validated; no Photoshop job was queued.",
            }

    if job_type == "apply_mask_to_layer":
        validation = validate_apply_mask_to_layer(body)
        if not validation["valid"]:
            return {
                "status": "error",
                "schema_version": "ps-agent/v1",
                "valid": False,
                "error": {
                    "code": "invalid_apply_mask_to_layer",
                    "message": "Apply-mask request validation failed.",
                    "details": validation["errors"],
                },
            }

    if job_type == "apply_plan":
        validation = validate_plan(body)
        if not validation["valid"]:
            return {
                "status": "error",
                "schema_version": "ps-agent/v1",
                "valid": False,
                "summary": validation.get("summary"),
                "error": {
                    "code": "invalid_plan",
                    "message": "Plan validation failed.",
                    "details": validation["errors"],
                },
            }
        if bool(body.get("dry_run", False)):
            return {
                "status": "ok",
                "schema_version": "ps-agent/v1",
                "dry_run": True,
                "valid": True,
                "summary": validation.get("summary"),
                "message": "Plan validated; no Photoshop job was queued.",
            }

    if job_type == "make_selection":
        validation = validate_make_selection(body)
        if not validation["valid"]:
            return {
                "status": "error",
                "schema_version": "ps-agent/v1",
                "valid": False,
                "error": {
                    "code": "invalid_selection_mask",
                    "message": "Selection mask validation failed.",
                    "details": validation["errors"],
                },
            }

    if job_type == "selection_command":
        validation = validate_selection_command(body)
        if not validation["valid"]:
            return {
                "status": "error",
                "schema_version": "ps-agent/v1",
                "valid": False,
                "error": {
                    "code": "invalid_selection_command",
                    "message": "Selection command validation failed.",
                    "details": validation["errors"],
                },
            }

    if job_type == "native_selection_mask":
        validation = validate_native_selection_mask(body)
        if not validation["valid"]:
            return {
                "status": "error",
                "schema_version": "ps-agent/v1",
                "valid": False,
                "error": {
                    "code": "invalid_native_selection_mask",
                    "message": "Native selection request validation failed.",
                    "details": validation["errors"],
                },
            }

    if job_type == "delete_agent_group":
        validation = validate_delete_agent_group(body)
        if not validation["valid"]:
            return {
                "status": "error",
                "schema_version": "ps-agent/v1",
                "valid": False,
                "error": {
                    "code": "invalid_delete_agent_group_request",
                    "message": "Delete agent group request validation failed.",
                    "details": validation["errors"],
                },
            }
        if bool(body.get("dry_run", False)):
            job_id = body.get("job_id")
            group_name = body.get("group_name") or f"Codex Agent - {job_id}"
            return {
                "status": "ok",
                "schema_version": "ps-agent/v1",
                "dry_run": True,
                "valid": True,
                "target_group_name": group_name,
                "message": "Delete request validated; no Photoshop job was queued.",
            }

    timeout_ms = int(body.get("timeout_ms", 60000))
    timeout_ms = max(1000, min(timeout_ms, 300000))
    job = QUEUE.create_job(job_type=job_type, payload=body, timeout_ms=timeout_ms)
    return wait_or_return(job, body)


def execute_capability_request(body: dict[str, Any]) -> dict[str, Any]:
    validation = validate_capability_call(body)
    if not validation["valid"]:
        return {
            "status": "error",
            "schema_version": "ps-agent/v1",
            "valid": False,
            "error": {
                "code": "invalid_capability_call",
                "message": "Capability call validation failed.",
                "details": validation["errors"],
            },
            "warnings": validation["warnings"],
        }
    if bool(body.get("dry_run", False)):
        return dry_run_capability_call(body)

    capability = validation["capability"]
    params = body.get("params") if isinstance(body.get("params"), dict) else {}
    wait_payload = {
        "wait": body.get("wait", True),
        "timeout_ms": body.get("timeout_ms", 60000),
        "workflow_id": body.get("workflow_id"),
        "stage_id": body.get("stage_id"),
        "parent_job_id": body.get("parent_job_id"),
        "stage_status": "pending" if body.get("stage_id") else None,
    }
    transport = capability.get("transport")
    if transport == "existing_tool":
        tool = capability.get("tool")
        payload = dict(params)
        payload.update({key: value for key, value in wait_payload.items() if value is not None})
        if tool == "ps_get_state":
            return create_api_job("get_state", payload)
        if tool == "ps_export_preview":
            return create_api_job("export_preview", payload)
        if tool == "ps_export_regions":
            return create_api_job("export_regions", payload)
        if tool == "ps_make_selection":
            return create_api_job("make_selection", payload)
        if tool == "ps_selection_command":
            return create_api_job("selection_command", payload)
        if tool == "ps_apply_plan":
            return create_api_job("apply_plan", payload)
        if tool == "ps_apply_layer_recipe":
            recipe = payload.get("layer_recipe") or payload.get("recipe")
            validation_recipe = validate_layer_recipe({"layer_recipe": recipe})
            if not validation_recipe.get("valid"):
                return {
                    "status": "error",
                    "schema_version": "ps-agent/v1",
                    "error": {
                        "code": "invalid_layer_recipe",
                        "message": "Layer recipe validation failed.",
                        "details": validation_recipe.get("errors", []),
                    },
                }
            plan = lower_layer_recipe_to_plan(recipe)
            return create_api_job("apply_plan", dict(payload, plan=plan))
    if transport == "uxp_job":
        payload = dict(wait_payload)
        payload.update(
            {
                "capability_id": body.get("capability_id") or body.get("id"),
                "params": params,
                "user_confirmed": body.get("user_confirmed", False),
                "risk_acknowledged": body.get("risk_acknowledged", False),
            }
        )
        return create_api_job(str(capability.get("job_type") or "execute_capability"), payload)
    return {
        "status": "error",
        "schema_version": "ps-agent/v1",
        "error": {
            "code": "capability_transport_unavailable",
            "message": f"Capability transport is not executable yet: {transport}",
        },
    }


def apply_design_stage_request(body: dict[str, Any], asset_url_builder=None) -> dict[str, Any]:
    lowered = lower_design_stage_to_operation_recipe(body)
    if lowered.get("status") != "ok":
        return lowered
    if body.get("dry_run"):
        return lowered
    recipe_type = lowered.get("recipe_type")
    if recipe_type == "selection_recipe":
        payload = {
            "selection_recipe": lowered["selection_recipe"],
            "wait": body.get("wait", True),
            "timeout_ms": body.get("timeout_ms", 120000),
            "workflow_id": lowered["selection_recipe"].get("workflow_id"),
            "stage_id": lowered["selection_recipe"].get("stage_id"),
        }
        result = apply_soft_selection_recipe(payload, asset_url_builder)
        result["design_stage"] = {
            "workflow_id": lowered["selection_recipe"].get("workflow_id"),
            "stage_id": lowered["selection_recipe"].get("stage_id"),
            "recipe_type": "selection_recipe",
        }
        return result
    payload = {
        "operation_recipe": lowered["operation_recipe"],
        "wait": body.get("wait", True),
        "timeout_ms": body.get("timeout_ms", 120000),
    }
    result = create_api_job("operation_recipe", payload)
    result["design_stage"] = {
        "workflow_id": lowered["operation_recipe"].get("workflow_id"),
        "stage_id": lowered["operation_recipe"].get("stage_id"),
        "recipe_type": "operation_recipe",
        "lowered_from_layer_graph": lowered.get("lowered_from_layer_graph"),
    }
    if lowered.get("warnings"):
        result["warnings"] = lowered["warnings"]
    return result


def retouch_spot_heal_points_request(body: dict[str, Any]) -> dict[str, Any]:
    params_keys = {
        "source_layer_id",
        "target_layer_id",
        "layer_id",
        "name",
        "points",
        "feather",
        "expand",
        "duplicate_source",
        "rasterize_duplicate",
        "clear_selection",
        "opacity",
        "blend_mode",
    }
    params = {key: body[key] for key in params_keys if key in body}
    recipe = {
        "schema_version": "ps-agent/v1",
        "recipe_id": body.get("recipe_id") or f"oprec-retouch-spot-heal-{uuid.uuid4().hex[:8]}",
        "workflow_id": body.get("workflow_id"),
        "stage_id": body.get("stage_id") or "retouch_spot_heal",
        "goal": body.get("goal") or "Non-destructively remove Codex-reviewed blemish points using content-aware fill.",
        "steps": [
            {
                "step_id": "spot_heal_points",
                "atom_id": "retouch.spot_heal_points",
                "params": params,
            }
        ],
        "review": body.get("review") if isinstance(body.get("review"), dict) else {"export_global": True, "regions": body.get("review_regions", [])},
        "safety": {
            "non_destructive": True,
            "allow_destructive": False,
            "create_history_state": True,
        },
    }
    payload = {
        "operation_recipe": recipe,
        "wait": body.get("wait", True),
        "timeout_ms": body.get("timeout_ms", 120000),
    }
    return create_api_job("operation_recipe", payload)


def retouch_content_aware_fill_selection_request(body: dict[str, Any]) -> dict[str, Any]:
    params_keys = {
        "source_layer_id",
        "target_layer_id",
        "layer_id",
        "name",
        "feather",
        "expand",
        "duplicate_source",
        "rasterize_duplicate",
        "clear_selection",
        "opacity",
        "blend_mode",
    }
    params = {key: body[key] for key in params_keys if key in body}
    recipe = {
        "schema_version": "ps-agent/v1",
        "recipe_id": body.get("recipe_id") or f"oprec-retouch-content-aware-{uuid.uuid4().hex[:8]}",
        "workflow_id": body.get("workflow_id"),
        "stage_id": body.get("stage_id") or "retouch_content_aware_fill",
        "goal": body.get("goal") or "Non-destructively content-aware fill the current active selection on a retouch layer.",
        "steps": [
            {
                "step_id": "content_aware_fill_selection",
                "atom_id": "retouch.content_aware_fill_selection",
                "params": params,
            }
        ],
        "review": body.get("review") if isinstance(body.get("review"), dict) else {"export_global": True, "regions": body.get("review_regions", [])},
        "safety": {
            "non_destructive": True,
            "allow_destructive": False,
            "create_history_state": True,
        },
    }
    payload = {
        "operation_recipe": recipe,
        "wait": body.get("wait", True),
        "timeout_ms": body.get("timeout_ms", 120000),
    }
    return create_api_job("operation_recipe", payload)


def export_design_package_request(body: dict[str, Any]) -> dict[str, Any]:
    package = export_design_package(body)
    if package.get("status") != "ok" or bool(body.get("dry_run", False)):
        return package
    result = create_api_job("export_preview", package.get("export_preview_payload", {}))
    result["design_export"] = package
    return result


def apply_workflow_stage_request(body: dict[str, Any]) -> dict[str, Any]:
    workflow_plan = body.get("workflow_plan") or body.get("workflow")
    stage_id = str(body.get("stage_id") or "")
    validation = validate_workflow_plan({"workflow_plan": workflow_plan})
    if not validation["valid"]:
        return {
            "status": "error",
            "schema_version": "ps-agent/v1",
            "error": {
                "code": "invalid_workflow_plan",
                "message": "Workflow validation failed.",
                "details": validation["errors"],
            },
            "warnings": validation["warnings"],
        }
    stage = find_stage(workflow_plan, stage_id)
    if not stage:
        return api_error("stage_not_found", f"Unknown stage_id: {stage_id}")

    workflow_id = workflow_plan.get("workflow_id")
    operation_recipe = stage.get("operation_recipe")
    selection_recipe = stage.get("selection_recipe")
    recipe_or_plan = stage.get("recipe_or_plan")
    if isinstance(recipe_or_plan, dict):
        if not isinstance(operation_recipe, dict):
            operation_recipe = recipe_or_plan.get("operation_recipe")
        if not isinstance(selection_recipe, dict):
            selection_recipe = recipe_or_plan.get("selection_recipe")
        if not isinstance(operation_recipe, dict) and isinstance(recipe_or_plan.get("steps"), list):
            operation_recipe = recipe_or_plan
        if not isinstance(selection_recipe, dict) and isinstance(recipe_or_plan.get("candidates"), list) and isinstance(recipe_or_plan.get("merge_plan"), dict):
            selection_recipe = recipe_or_plan

    if isinstance(operation_recipe, dict):
        recipe = dict(operation_recipe)
        recipe.setdefault("schema_version", "ps-agent/v1")
        recipe.setdefault("workflow_id", workflow_id)
        recipe.setdefault("stage_id", stage_id)
        recipe.setdefault("goal", stage.get("objective") or workflow_plan.get("goal") or "Apply workflow operation stage.")
        payload = {
            "operation_recipe": recipe,
            "dry_run": bool(body.get("dry_run", False)),
            "wait": body.get("wait", True),
            "timeout_ms": body.get("timeout_ms", 120000),
            "workflow_id": workflow_id,
            "stage_id": stage_id,
            "stage_status": "pending",
        }
        result = create_api_job("operation_recipe", payload)
        result["workflow"] = {
            "workflow_id": workflow_id,
            "stage_id": stage_id,
            "stage_route": stage.get("route"),
            "stage_objective": stage.get("objective"),
            "expected_result": stage.get("expected_result"),
            "review_regions": stage.get("review_regions", []),
        }
        return result

    if isinstance(selection_recipe, dict):
        recipe = dict(selection_recipe)
        recipe.setdefault("schema_version", "ps-agent/v1")
        recipe.setdefault("workflow_id", workflow_id)
        recipe.setdefault("stage_id", stage_id)
        return api_error(
            "workflow_selection_stage_requires_selection_endpoint",
            "Workflow stages may carry selection_recipe metadata, but executable selection trials must be applied with ps_apply_selection_recipe so hard-selection and soft-alpha bus handling stays explicit.",
            {"workflow_id": workflow_id, "stage_id": stage_id, "selection_recipe_id": recipe.get("recipe_id")},
        )

    if not isinstance(recipe_or_plan, dict):
        return api_error("stage_not_executable", "stage.recipe_or_plan must be an object before applying a stage.")

    if "layer_recipe" in recipe_or_plan or recipe_or_plan.get("type") == "layer_recipe":
        recipe = recipe_or_plan.get("layer_recipe") or recipe_or_plan.get("recipe")
        recipe_validation = validate_layer_recipe({"layer_recipe": recipe})
        if not recipe_validation.get("valid"):
            return api_error("invalid_stage_layer_recipe", "Stage layer recipe validation failed.", recipe_validation.get("errors", []))
        plan = lower_layer_recipe_to_plan(recipe)
        lowered_validation = validate_plan({"plan": plan})
    else:
        plan = recipe_or_plan.get("plan") if "plan" in recipe_or_plan else recipe_or_plan
        lowered_validation = validate_plan({"plan": plan})

    if not lowered_validation.get("valid"):
        return api_error("invalid_stage_plan", "Stage plan validation failed.", lowered_validation.get("errors", []))

    plan = dict(plan)
    metadata = dict(plan.get("metadata") or {})
    metadata.update(
        {
            "workflow_id": workflow_id,
            "stage_id": stage_id,
            "stage_objective": stage.get("objective"),
            "feedback_path": f"{workflow_id}->{stage_id}",
        }
    )
    plan["metadata"] = metadata
    if bool(body.get("dry_run", False)):
        return {
            "status": "ok",
            "schema_version": "ps-agent/v1",
            "dry_run": True,
            "workflow_id": workflow_id,
            "stage_id": stage_id,
            "stage": stage,
            "plan": plan,
            "validation": lowered_validation,
        }

    apply_payload = {
        "plan": plan,
        "wait": body.get("wait", True),
        "timeout_ms": body.get("timeout_ms", 120000),
        "workflow_id": workflow_id,
        "stage_id": stage_id,
        "stage_status": "pending",
    }
    result = create_api_job("apply_plan", apply_payload)
    result["workflow"] = {
        "workflow_id": workflow_id,
        "stage_id": stage_id,
        "stage_objective": stage.get("objective"),
        "expected_result": stage.get("expected_result"),
        "review_regions": stage.get("review_regions", []),
    }
    return result


def delete_workflow_stage_request(body: dict[str, Any]) -> dict[str, Any]:
    workflow_id = str(body.get("workflow_id") or "")
    stage_id = str(body.get("stage_id") or "")
    job_id = body.get("job_id")
    group_name = body.get("group_name")
    if not group_name:
        if not workflow_id or not stage_id or not job_id:
            return api_error("missing_stage_delete_target", "workflow_id, stage_id, and job_id are required unless group_name is provided.")
        group_name = stage_group_name(workflow_id, stage_id, str(job_id))
    payload = {
        "group_name": group_name,
        "job_id": job_id or "job-stage_delete_target",
        "wait": body.get("wait", True),
        "timeout_ms": body.get("timeout_ms", 60000),
        "dry_run": body.get("dry_run", False),
        "workflow_id": workflow_id or None,
        "stage_id": stage_id or None,
    }
    return create_api_job("delete_agent_group", payload)


class AgentHandler(BaseHTTPRequestHandler):
    server_version = f"PSUXPAgent/{BACKEND_VERSION}"

    def log_message(self, format: str, *args: Any) -> None:
        try:
            sys.stderr.write("%s - - [%s] %s\n" % (self.address_string(), self.log_date_time_string(), format % args))
        except Exception:
            pass

    def do_OPTIONS(self) -> None:
        json_response(self, 200, {"status": "ok"})

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        if path == "/":
            payload = backend_health_payload()
            payload["message"] = "PS UXP Agent backend is running. Use /health for machine-readable status."
            payload["endpoints"] = [
                "GET /health",
                "GET /api/diagnostics",
                "POST /api/analyze-image-metrics",
                "POST /api/retrieve-style-guidance",
                "POST /api/capabilities/list",
                "POST /api/capabilities/probe",
                "POST /api/capabilities/validate-call",
                "POST /api/capabilities/execute",
                "POST /api/design/assets/scan",
                "POST /api/design/assets/analyze",
                "POST /api/design/assets/prepare-variant",
                "POST /api/design/create-plan",
                "POST /api/design/validate-plan",
                "POST /api/design/lower-stage",
                "POST /api/design/apply-stage",
                "POST /api/design/review-stage",
                "POST /api/design/export-package",
                "POST /api/selections/create-strategy",
                "POST /api/selections/validate-strategy",
                "POST /api/workflows/create-plan",
                "POST /api/workflows/validate-plan",
                "POST /api/workflows/apply-stage",
                "POST /api/workflows/review-stage",
                "POST /api/workflows/finalize-review",
                "POST /api/workflows/delete-stage",
                "POST /api/score-grade-preview",
                "POST /api/preview/compare-reference",
                "POST /api/layout/detect-overflow",
                "POST /api/layout/visual-score",
                "POST /api/sam-mask",
                "POST /api/grounding/detect-boxes",
                "POST /api/grounding/hqsam-mask",
                "POST /api/grounding/grounded-mask",
                "POST /uxp/heartbeat",
                "GET /uxp/jobs/next",
                "POST /uxp/jobs/{job_id}/result",
                "POST /uxp/assets/{job_id}/{filename}",
                "GET /assets/{job_id}/{filename}",
                "POST /api/state",
                "POST /api/export-preview",
                "POST /api/export-regions",
                "POST /api/face-selection",
                "POST /api/region-artifacts/create",
                "POST /api/region-artifacts/extract-contour",
                "POST /api/region-artifacts/lower-selection",
                "POST /api/region-artifacts/lower-path",
                "POST /api/make-selection",
                "POST /api/extract-tonal-range",
                "POST /api/select-highlights",
                "POST /api/select-midtones",
                "POST /api/select-shadows",
                "POST /api/build-luminosity-mask",
                "POST /api/apply-plan",
                "POST /api/delete-agent-group",
                "POST /api/undo-last",
                "POST /api/shutdown",
                "GET /api/jobs/{job_id}",
            ]
            json_response(self, 200, payload)
            return

        if path.startswith("/assets/"):
            relative_path = path.removeprefix("/assets/")
            parts = [safe_path_part(part) for part in relative_path.split("/") if part]
            if len(parts) != 2:
                error_response(self, 404, "asset_not_found", f"Unknown asset path: {relative_path}")
                return
            asset_path = ASSET_ROOT / parts[0] / parts[1]
            if not asset_path.is_file():
                error_response(self, 404, "asset_not_found", f"Unknown asset path: {relative_path}")
                return
            mime_type = mimetypes.guess_type(asset_path.name)[0] or "application/octet-stream"
            binary_response(self, 200, asset_path.read_bytes(), mime_type)
            return

        if path == "/health":
            json_response(self, 200, backend_health_payload())
            return

        if path == "/api/diagnostics":
            json_response(self, 200, diagnostics_payload())
            return

        if path == "/uxp/jobs/next":
            query = urllib.parse.parse_qs(parsed.query)
            claimed_by = query.get("claimed_by", ["uxp-plugin"])[0] or "uxp-plugin"
            job = QUEUE.next_job(claimed_by=claimed_by)
            json_response(
                self,
                200,
                {
                    "schema_version": "ps-agent/v1",
                    "status": "ok",
                    "server_time": utc_now(),
                    "job": job,
                    "queue": QUEUE.health()["queue"],
                },
            )
            return

        if path.startswith("/api/jobs/"):
            job_id = path.removeprefix("/api/jobs/")
            job = QUEUE.get_job(job_id)
            if job is None:
                error_response(self, 404, "job_not_found", f"Unknown job_id: {job_id}")
                return
            json_response(self, 200, {"status": "ok", "job": job})
            return

        error_response(self, 404, "not_found", f"Unknown endpoint: {path}")

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        if path.startswith("/uxp/assets/"):
            relative_path = path.removeprefix("/uxp/assets/")
            parts = relative_path.split("/", 1)
            if len(parts) != 2:
                error_response(self, 404, "asset_route_invalid", "Expected /uxp/assets/{job_id}/{filename}.")
                return
            save_asset_upload(self, parts[0], parts[1])
            return

        try:
            body = parse_json_body(self)
        except Exception as exc:
            error_response(self, 400, "invalid_json", str(exc))
            return

        if path == "/uxp/heartbeat":
            health = QUEUE.set_heartbeat(body)
            json_response(self, 200, health)
            return

        if path == "/api/face-selection":
            json_response(self, 200, generate_face_selection(body))
            return

        if path == "/api/region-artifacts/create":
            json_response(self, 200, create_region_artifact(body, lambda relative_path: make_asset_url(self, relative_path)))
            return

        if path == "/api/region-artifacts/extract-contour":
            json_response(self, 200, extract_region_contour(body, lambda relative_path: make_asset_url(self, relative_path)))
            return

        if path == "/api/region-artifacts/lower-selection":
            json_response(self, 200, lower_region_to_selection_recipe(body))
            return

        if path == "/api/region-artifacts/lower-path":
            json_response(self, 200, lower_region_to_path(body))
            return

        if path == "/api/native-selection-mask/materialize":
            json_response(self, 200, materialize_native_selection_mask(body, lambda relative_path: make_asset_url(self, relative_path)))
            return

        if path == "/api/analyze-image-metrics":
            json_response(self, 200, analyze_image_metrics(body))
            return

        if path == "/api/extract-tonal-range":
            json_response(self, 200, extract_tonal_range_request(body))
            return

        tonal_range_routes = {
            "/api/select-highlights": "highlights",
            "/api/select-midtones": "midtones",
            "/api/select-shadows": "shadows",
        }
        if path in tonal_range_routes:
            json_response(self, 200, extract_tonal_range_request(body, forced_tonal_range=tonal_range_routes[path]))
            return

        if path == "/api/build-luminosity-mask":
            json_response(self, 200, build_luminosity_mask_request(body))
            return

        if path == "/api/retrieve-style-guidance":
            json_response(self, 200, retrieve_style_guidance(body))
            return

        if path == "/api/capabilities/list":
            json_response(self, 200, list_capabilities(body))
            return

        if path == "/api/capabilities/probe":
            json_response(self, 200, probe_capability(body))
            return

        if path == "/api/capabilities/validate-call":
            json_response(self, 200, validate_capability_call(body))
            return

        if path == "/api/capabilities/execute":
            json_response(self, 200, execute_capability_request(body))
            return

        if path == "/api/design/assets/scan":
            json_response(self, 200, scan_asset_library(body, lambda relative_path: make_asset_url(self, relative_path)))
            return

        if path == "/api/design/assets/analyze":
            json_response(self, 200, analyze_design_assets(body, lambda relative_path: make_asset_url(self, relative_path)))
            return

        if path == "/api/design/assets/prepare-variant":
            json_response(self, 200, prepare_asset_variant(body, lambda relative_path: make_asset_url(self, relative_path)))
            return

        if path == "/api/design/create-plan":
            json_response(self, 200, create_design_plan(body))
            return

        if path == "/api/design/validate-plan":
            json_response(self, 200, validate_design_plan(body))
            return

        if path == "/api/design/lower-stage":
            json_response(self, 200, lower_design_stage_to_operation_recipe(body))
            return

        if path == "/api/svg/compile-object":
            json_response(self, 200, compile_svg_object(body))
            return
        if path == "/api/design/apply-stage":
            json_response(self, 200, apply_design_stage_request(body, lambda relative_path: make_asset_url(self, relative_path)))
            return

        if path == "/api/design/review-stage":
            json_response(self, 200, review_design_stage(body))
            return

        if path == "/api/design/export-package":
            json_response(self, 200, export_design_package_request(body))
            return

        if path == "/api/operation-atoms/list":
            json_response(self, 200, list_operation_atoms(body))
            return

        if path == "/api/operation-atoms/probe":
            json_response(self, 200, probe_operation_atom(body))
            return

        if path == "/api/operation-recipe/validate":
            json_response(self, 200, validate_operation_recipe(body))
            return

        if path == "/api/operation-recipe/review":
            json_response(self, 200, review_operation_recipe(body))
            return

        if path == "/api/path/audit-bezier-handles":
            json_response(self, 200, audit_bezier_handles(body))
            return

        if path == "/api/operation-recipe/apply":
            json_response(self, 200, create_api_job("operation_recipe", body))
            return

        if path == "/api/retouch/spot-heal-points":
            json_response(self, 200, retouch_spot_heal_points_request(body))
            return

        if path == "/api/retouch/content-aware-fill-selection":
            json_response(self, 200, retouch_content_aware_fill_selection_request(body))
            return

        if path == "/api/selection-atoms/list":
            json_response(self, 200, list_selection_atoms(body))
            return

        if path == "/api/selection-atoms/probe":
            json_response(self, 200, probe_selection_atom(body))
            return

        if path == "/api/selection-recipe/validate":
            json_response(self, 200, validate_selection_recipe(body))
            return

        if path == "/api/selection-recipe/review":
            json_response(self, 200, review_selection_recipe(body))
            return

        if path == "/api/selection-recipe/apply":
            json_response(self, 200, apply_soft_selection_recipe(body, lambda relative_path: make_asset_url(self, relative_path)))
            return

        if path == "/api/selections/create-strategy":
            json_response(self, 200, create_selection_strategy(body))
            return

        if path == "/api/selections/validate-strategy":
            json_response(self, 200, validate_selection_strategy(body))
            return

        if path == "/api/workflows/create-plan":
            json_response(self, 200, create_workflow_plan(body))
            return

        if path == "/api/workflows/validate-plan":
            json_response(self, 200, validate_workflow_plan(body))
            return

        if path == "/api/workflows/apply-stage":
            json_response(self, 200, apply_workflow_stage_request(body))
            return

        if path == "/api/workflows/review-stage":
            json_response(self, 200, review_workflow_stage(body))
            return

        if path == "/api/workflows/finalize-review":
            json_response(self, 200, finalize_workflow_review(body))
            return

        if path == "/api/workflows/delete-stage":
            json_response(self, 200, delete_workflow_stage_request(body))
            return

        if path == "/api/effect-primitives/list":
            json_response(self, 200, list_effect_primitives(body))
            return

        if path == "/api/effect-primitives/retrieve":
            json_response(self, 200, retrieve_effect_primitives(body))
            return

        if path == "/api/layer-recipe/generate":
            json_response(self, 200, generate_layer_recipe(body))
            return

        if path == "/api/layer-recipe/validate":
            result = validate_layer_recipe(body)
            recipe = body.get("layer_recipe") or body.get("recipe")
            if result.get("valid") and isinstance(recipe, dict):
                result["lowered_plan_validation"] = validate_plan({"plan": lower_layer_recipe_to_plan(recipe)})
            json_response(self, 200, result)
            return

        if path == "/api/layer-recipe/review":
            json_response(self, 200, review_layer_recipe(body))
            return

        if path == "/api/layer-recipe/apply":
            recipe = body.get("layer_recipe") or body.get("recipe")
            validation = validate_layer_recipe({"layer_recipe": recipe})
            if not validation.get("valid"):
                json_response(
                    self,
                    200,
                    {
                        "status": "error",
                        "schema_version": "ps-agent/v1",
                        "error": {
                            "code": "invalid_layer_recipe",
                            "message": "Layer recipe validation failed.",
                            "details": validation.get("errors", []),
                        },
                    },
                )
                return
            plan = lower_layer_recipe_to_plan(recipe)
            plan_validation = validate_plan({"plan": plan})
            if not plan_validation.get("valid"):
                json_response(
                    self,
                    200,
                    {
                        "status": "error",
                        "schema_version": "ps-agent/v1",
                        "validation": validation,
                        "lowered_plan": plan,
                        "error": {
                            "code": "invalid_lowered_plan",
                            "message": "Layer recipe lowered to an invalid ps_apply_plan.",
                            "details": plan_validation.get("errors", []),
                        },
                    },
                )
                return
            if bool(body.get("dry_run", False)):
                json_response(
                    self,
                    200,
                    {
                        "status": "ok",
                        "schema_version": "ps-agent/v1",
                        "dry_run": True,
                        "validation": validation,
                        "lowered_plan_validation": plan_validation,
                        "lowered_plan": plan,
                    },
                )
                return
            apply_payload = {
                "plan": plan,
                "wait": body.get("wait", True),
                "timeout_ms": body.get("timeout_ms", 120000),
            }
            result = create_api_job("apply_plan", apply_payload)
            result["layer_recipe"] = {
                "recipe_id": recipe.get("recipe_id"),
                "primitive_ids": [step.get("primitive_id") for step in recipe.get("steps", []) if isinstance(step, dict)],
            }
            result["lowered_plan_validation"] = plan_validation
            json_response(self, 200, result)
            return

        if path == "/api/score-grade-preview":
            json_response(self, 200, score_grade_preview(body))
            return

        if path == "/api/preview/compare-reference":
            json_response(self, 200, compare_reference(body))
            return

        if path == "/api/layout/detect-overflow":
            json_response(self, 200, detect_overflow(body))
            return

        if path == "/api/layout/visual-score":
            json_response(self, 200, visual_score(body))
            return

        if path == "/api/sam-mask":
            json_response(self, 200, generate_sam_mask(body, lambda relative_path: make_asset_url(self, relative_path)))
            return

        if path == "/api/grounding/detect-boxes":
            json_response(self, 200, detect_grounding_boxes(body, lambda relative_path: make_asset_url(self, relative_path)))
            return

        if path == "/api/grounding/hqsam-mask":
            json_response(self, 200, generate_hqsam_mask(body, lambda relative_path: make_asset_url(self, relative_path)))
            return

        if path == "/api/grounding/grounded-mask":
            json_response(self, 200, generate_grounded_hq_mask(body, lambda relative_path: make_asset_url(self, relative_path)))
            return

        if path.startswith("/uxp/jobs/") and path.endswith("/result"):
            job_id = path.removeprefix("/uxp/jobs/").removesuffix("/result").strip("/")
            result = dict(body)
            result.setdefault("schema_version", "ps-agent/v1")
            result.setdefault("job_id", job_id)
            result.setdefault("status", "ok")
            job = QUEUE.finish_job(job_id, result)
            if job is None:
                error_response(self, 404, "job_not_found", f"Unknown job_id: {job_id}")
                return
            json_response(self, 200, {"status": "ok", "job": job})
            return

        api_job_routes = {
            "/api/state": "get_state",
            "/api/export-preview": "export_preview",
            "/api/export-regions": "export_regions",
            "/api/make-selection": "make_selection",
            "/api/selection-command": "selection_command",
            "/api/apply-mask-to-layer": "apply_mask_to_layer",
            "/api/apply-plan": "apply_plan",
            "/api/delete-agent-group": "delete_agent_group",
            "/api/undo-last": "undo_last_agent_edit",
        }
        if path in api_job_routes:
            json_response(self, 200, create_api_job(api_job_routes[path], body))
            return

        native_selection_routes = {
            "/api/select-subject": "select_subject",
            "/api/select-sky": "select_sky",
            "/api/select-color-range": "color_range",
            "/api/select-focus-area": "focus_area",
        }
        if path in native_selection_routes:
            payload = dict(body)
            payload["action"] = native_selection_routes[path]
            json_response(self, 200, create_api_job("native_selection_mask", payload))
            return

        selection_command_routes = {
            "/api/select-all": "select_all",
            "/api/deselect": "deselect",
            "/api/inverse-selection": "inverse",
            "/api/modify-selection": "modify",
            "/api/save-selection-channel": "save_selection",
            "/api/load-selection-channel": "load_selection",
        }
        if path in selection_command_routes:
            payload = dict(body)
            payload["action"] = selection_command_routes[path]
            json_response(self, 200, create_api_job("selection_command", payload))
            return

        if path == "/api/validate-plan":
            json_response(self, 200, validate_plan(body))
            return

        if path == "/api/shutdown":
            if not client_is_loopback(self):
                error_response(self, 403, "shutdown_forbidden", "Shutdown is only allowed from loopback.")
                return
            expected_token = read_shutdown_token()
            if not expected_token or body.get("token") != expected_token:
                error_response(self, 403, "shutdown_token_invalid", "Invalid backend shutdown token.")
                return
            json_response(
                self,
                200,
                {
                    "status": "ok",
                    "schema_version": "ps-agent/v1",
                    "message": "Backend shutdown requested.",
                    "process": process_info(),
                },
            )
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return

        error_response(self, 404, "not_found", f"Unknown endpoint: {path}")


def validate_plan(body: dict[str, Any]) -> dict[str, Any]:
    plan = body.get("plan")
    errors: list[str] = []
    if not isinstance(plan, dict):
        errors.append("plan must be an object")
    else:
        safety = plan.get("safety")
        safety_for_ops = safety if isinstance(safety, dict) else {}
        if plan.get("schema_version") != "ps-agent/v1":
            errors.append("plan.schema_version must be ps-agent/v1")
        if not isinstance(plan.get("ops"), list) or not plan.get("ops"):
            errors.append("plan.ops must be a non-empty array")
        else:
            for index, op in enumerate(plan["ops"]):
                if not isinstance(op, dict):
                    errors.append(f"plan.ops[{index}] must be an object")
                    continue
                validate_supported_op(errors, op, index, safety_for_ops)
        if not isinstance(safety, dict):
            errors.append("plan.safety must be an object")
        else:
            if safety.get("non_destructive") is not True:
                errors.append("plan.safety.non_destructive must be true")
            if safety.get("allow_destructive") is not False:
                errors.append("plan.safety.allow_destructive must be false")
            if (
                "allow_experimental_acr_masks" in safety
                and not isinstance(safety["allow_experimental_acr_masks"], bool)
            ):
                errors.append("plan.safety.allow_experimental_acr_masks must be a boolean")

    return {
        "status": "ok",
        "valid": not errors,
        "errors": errors,
        "summary": plan_summary(plan) if isinstance(plan, dict) else None,
        "schema_version": "ps-agent/v1",
    }


def run_server(host: str = HOST, port: int = PORT) -> None:
    ensure_runtime_dirs()
    ensure_shutdown_token()
    server = ThreadingHTTPServer((host, port), AgentHandler)
    write_pid_files(host, port)
    QUEUE.event("backend_started", {"pid": os.getpid(), "host": host, "port": port, "version": BACKEND_VERSION})
    console_write(f"PS UXP Agent backend listening on http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        console_write("Stopping PS UXP Agent backend.")
    finally:
        QUEUE.event("backend_stopped", {"pid": os.getpid(), "host": host, "port": port})
        server.server_close()
        QUEUE.close()
        clear_pid_files()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the local PS UXP Agent backend.")
    parser.add_argument("--host", default=HOST)
    parser.add_argument("--port", type=int, default=PORT)
    args = parser.parse_args()
    run_server(args.host, args.port)


if __name__ == "__main__":
    main()
