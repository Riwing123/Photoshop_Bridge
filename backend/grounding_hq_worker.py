from __future__ import annotations

import importlib.util
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


CURRENT_DIR = Path(__file__).resolve().parent
WORKSPACE_ROOT = CURRENT_DIR.parent
RUNTIME_ROOT = CURRENT_DIR / "runtime"
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))
DEFAULT_HOST = os.environ.get("PS_AGENT_GROUNDING_HQ_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.environ.get("PS_AGENT_GROUNDING_HQ_PORT", "17862"))
DEFAULT_GROUNDING_DINO_MODEL_PATH = CURRENT_DIR / "models" / "grounding_dino" / "groundingdino_swint_ogc.pth"
DEFAULT_GROUNDING_DINO_CONFIG_PATH = CURRENT_DIR / "models" / "grounding_dino" / "GroundingDINO_SwinT_OGC.py"
DEFAULT_HQSAM_MODEL_PATH = CURRENT_DIR / "models" / "sam_hq" / "sam_hq_vit_l.pth"
GROUNDING_DINO_MODEL_PATH = Path(
    os.environ.get("PS_AGENT_GROUNDING_DINO_MODEL_PATH", str(DEFAULT_GROUNDING_DINO_MODEL_PATH))
).expanduser()
GROUNDING_DINO_CONFIG_PATH = Path(
    os.environ.get("PS_AGENT_GROUNDING_DINO_CONFIG_PATH", str(DEFAULT_GROUNDING_DINO_CONFIG_PATH))
).expanduser()
HQSAM_MODEL_PATH = Path(os.environ.get("PS_AGENT_HQSAM_MODEL_PATH", str(DEFAULT_HQSAM_MODEL_PATH))).expanduser()
HQSAM_MODEL_TYPE = os.environ.get("PS_AGENT_HQSAM_MODEL_TYPE", "vit_l")
GROUNDING_DEVICE_POLICY = os.environ.get("PS_AGENT_GROUNDING_DEVICE", "auto").strip().lower() or "auto"
HQSAM_DEVICE_POLICY = os.environ.get("PS_AGENT_HQSAM_DEVICE", "auto").strip().lower() or "auto"
PID_PATH = RUNTIME_ROOT / "grounding-hq-worker.pid"

GROUNDING_MODEL = None
GROUNDING_DEVICE = None
GROUNDING_LOADED_AT = None
GROUNDING_FALLBACK_REASON = None
GROUNDING_CUDA_EXTENSION_AVAILABLE = None
GROUNDING_CUDA_EXTENSION_ERROR = None
GROUNDING_LOCK = threading.Lock()
HQSAM_PREDICTOR = None
HQSAM_DEVICE = None
HQSAM_LOADED_AT = None
HQSAM_FALLBACK_REASON = None
HQSAM_LOCK = threading.Lock()


class WorkerError(RuntimeError):
    def __init__(self, code: str, message: str, details: Any | None = None):
        super().__init__(message)
        self.code = code
        self.details = details


def json_response(handler: BaseHTTPRequestHandler, status: int, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type")
    handler.end_headers()
    try:
        handler.wfile.write(body)
    except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
        return


def api_error(code: str, message: str, details: Any | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"status": "error", "error": {"code": code, "message": message}}
    if details is not None:
        payload["error"]["details"] = details
    return payload


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


def dependency_status(module_name: str) -> dict[str, Any]:
    return {"module": module_name, "available": importlib.util.find_spec(module_name) is not None}


def torch_status() -> dict[str, Any]:
    status = dependency_status("torch")
    if not status["available"]:
        return status
    try:
        import torch

        status.update(
            {
                "version": getattr(torch, "__version__", None),
                "cuda_available": torch.cuda.is_available(),
                "cuda_device": torch.cuda.get_device_name(0) if torch.cuda.is_available() else None,
                "cuda_memory_total": torch.cuda.get_device_properties(0).total_memory if torch.cuda.is_available() else None,
            }
        )
    except Exception as exc:
        status.update({"available": False, "error": str(exc)})
    return status


def validate_device_policy(value: str, field_name: str) -> str:
    if value not in {"auto", "cpu", "cuda"}:
        raise WorkerError(
            "invalid_device_policy",
            f"{field_name} must be auto, cpu, or cuda.",
            {"field": field_name, "value": value},
        )
    return value


def grounding_cuda_extension_status() -> tuple[bool, str | None]:
    global GROUNDING_CUDA_EXTENSION_AVAILABLE, GROUNDING_CUDA_EXTENSION_ERROR
    if GROUNDING_CUDA_EXTENSION_AVAILABLE is not None:
        return bool(GROUNDING_CUDA_EXTENSION_AVAILABLE), GROUNDING_CUDA_EXTENSION_ERROR
    try:
        import groundingdino._C  # noqa: F401

        GROUNDING_CUDA_EXTENSION_AVAILABLE = True
        GROUNDING_CUDA_EXTENSION_ERROR = None
    except Exception as exc:
        GROUNDING_CUDA_EXTENSION_AVAILABLE = False
        GROUNDING_CUDA_EXTENSION_ERROR = str(exc)
    return bool(GROUNDING_CUDA_EXTENSION_AVAILABLE), GROUNDING_CUDA_EXTENSION_ERROR


def choose_grounding_device(torch_module) -> tuple[str, str | None]:
    policy = validate_device_policy(GROUNDING_DEVICE_POLICY, "PS_AGENT_GROUNDING_DEVICE")
    cuda_available = bool(torch_module.cuda.is_available())
    extension_available, extension_error = grounding_cuda_extension_status()
    if policy == "cpu":
        return "cpu", "forced_cpu"
    if policy == "cuda":
        if not cuda_available:
            raise WorkerError("grounding_cuda_unavailable", "GroundingDINO CUDA was requested but torch CUDA is unavailable.")
        if not extension_available:
            raise WorkerError(
                "grounding_cuda_extension_missing",
                "GroundingDINO CUDA was requested but groundingdino._C is not available.",
                {"extension_error": extension_error},
            )
        return "cuda", None
    if cuda_available and extension_available:
        return "cuda", None
    if not cuda_available:
        return "cpu", "torch_cuda_unavailable"
    return "cpu", "grounding_cuda_extension_missing"


def choose_hqsam_device(torch_module) -> tuple[str, str | None]:
    policy = validate_device_policy(HQSAM_DEVICE_POLICY, "PS_AGENT_HQSAM_DEVICE")
    cuda_available = bool(torch_module.cuda.is_available())
    if policy == "cpu":
        return "cpu", "forced_cpu"
    if policy == "cuda":
        if not cuda_available:
            raise WorkerError("hqsam_cuda_unavailable", "HQ-SAM CUDA was requested but torch CUDA is unavailable.")
        return "cuda", None
    if cuda_available:
        return "cuda", None
    return "cpu", "torch_cuda_unavailable"


def process_payload() -> dict[str, Any]:
    return {
        "pid": os.getpid(),
        "started_at": STARTED_AT_ISO,
        "uptime_seconds": round(time.time() - STARTED_AT_TS, 3),
    }


def health_payload() -> dict[str, Any]:
    extension_available, extension_error = grounding_cuda_extension_status()
    return {
        "status": "ok",
        "schema_version": "ps-agent/v1",
        "worker": "grounding_dino_hqsam",
        "process": process_payload(),
        "models": {
            "grounding_dino": {
                "path": str(GROUNDING_DINO_MODEL_PATH),
                "exists": GROUNDING_DINO_MODEL_PATH.is_file(),
                "size_bytes": GROUNDING_DINO_MODEL_PATH.stat().st_size if GROUNDING_DINO_MODEL_PATH.is_file() else None,
                "config_path": str(GROUNDING_DINO_CONFIG_PATH),
                "config_exists": GROUNDING_DINO_CONFIG_PATH.is_file(),
            },
            "hqsam": {
                "path": str(HQSAM_MODEL_PATH),
                "exists": HQSAM_MODEL_PATH.is_file(),
                "size_bytes": HQSAM_MODEL_PATH.stat().st_size if HQSAM_MODEL_PATH.is_file() else None,
                "model_type": HQSAM_MODEL_TYPE,
            },
        },
        "dependencies": {
            "python": sys.version,
            "pillow": dependency_status("PIL"),
            "numpy": dependency_status("numpy"),
            "torch": torch_status(),
            "groundingdino": dependency_status("groundingdino"),
            "segment_anything_hq": dependency_status("segment_anything_hq"),
        },
        "device_policy": {
            "grounding_dino": GROUNDING_DEVICE_POLICY,
            "hqsam": HQSAM_DEVICE_POLICY,
        },
        "capabilities": {
            "grounding_cuda_extension_available": extension_available,
            "grounding_cuda_extension_error": extension_error,
        },
        "loaded": {
            "grounding_dino": {
                "loaded": GROUNDING_MODEL is not None,
                "device": GROUNDING_DEVICE,
                "loaded_at": GROUNDING_LOADED_AT,
                "fallback_reason": GROUNDING_FALLBACK_REASON,
            },
            "hqsam": {
                "loaded": HQSAM_PREDICTOR is not None,
                "device": HQSAM_DEVICE,
                "loaded_at": HQSAM_LOADED_AT,
                "fallback_reason": HQSAM_FALLBACK_REASON,
            },
        },
    }


def load_grounding_model():
    global GROUNDING_MODEL, GROUNDING_DEVICE, GROUNDING_LOADED_AT, GROUNDING_FALLBACK_REASON
    if GROUNDING_MODEL is not None:
        return GROUNDING_MODEL, __import__("torch"), str(GROUNDING_DEVICE)
    if not GROUNDING_DINO_MODEL_PATH.is_file():
        raise RuntimeError(f"Missing Grounding DINO checkpoint: {GROUNDING_DINO_MODEL_PATH}")
    if not GROUNDING_DINO_CONFIG_PATH.is_file():
        raise RuntimeError(f"Missing Grounding DINO config: {GROUNDING_DINO_CONFIG_PATH}")
    try:
        import torch
        from groundingdino.util.inference import load_model
    except ImportError as exc:
        raise RuntimeError(
            "Grounding DINO dependencies are missing. Install groundingdino in D:\\Photo_sontrol\\.venv-sam."
        ) from exc
    device, fallback_reason = choose_grounding_device(torch)
    GROUNDING_MODEL = load_model(str(GROUNDING_DINO_CONFIG_PATH), str(GROUNDING_DINO_MODEL_PATH), device=device)
    GROUNDING_DEVICE = device
    GROUNDING_FALLBACK_REASON = fallback_reason
    GROUNDING_LOADED_AT = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return GROUNDING_MODEL, torch, device


def load_hqsam_predictor():
    global HQSAM_PREDICTOR, HQSAM_DEVICE, HQSAM_LOADED_AT, HQSAM_FALLBACK_REASON
    if HQSAM_PREDICTOR is not None:
        return HQSAM_PREDICTOR, __import__("torch"), str(HQSAM_DEVICE)
    if not HQSAM_MODEL_PATH.is_file():
        raise RuntimeError(f"Missing HQ-SAM checkpoint: {HQSAM_MODEL_PATH}")
    try:
        import torch
        from segment_anything_hq import SamPredictor, sam_model_registry
    except ImportError as exc:
        raise RuntimeError(
            "HQ-SAM dependencies are missing. Install segment-anything-hq in D:\\Photo_sontrol\\.venv-sam."
        ) from exc
    if HQSAM_MODEL_TYPE not in sam_model_registry:
        raise RuntimeError(f"Unsupported HQ-SAM model type: {HQSAM_MODEL_TYPE}")
    device, fallback_reason = choose_hqsam_device(torch)
    original_torch_load = torch.load
    if device == "cpu":
        def load_with_cpu_map(*args, **kwargs):
            kwargs.setdefault("map_location", torch.device("cpu"))
            return original_torch_load(*args, **kwargs)

        torch.load = load_with_cpu_map
    try:
        model = sam_model_registry[HQSAM_MODEL_TYPE](checkpoint=str(HQSAM_MODEL_PATH))
    finally:
        if device == "cpu":
            torch.load = original_torch_load
    model.to(device=device)
    HQSAM_PREDICTOR = SamPredictor(model)
    HQSAM_DEVICE = device
    HQSAM_FALLBACK_REASON = fallback_reason
    HQSAM_LOADED_AT = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return HQSAM_PREDICTOR, torch, device


def normalize_box(box: Any, field_name: str = "box") -> list[float]:
    if not isinstance(box, list) or len(box) != 4:
        raise ValueError(f"{field_name} must be [left, top, right, bottom]")
    parsed = [float(item) for item in box]
    if parsed[2] <= parsed[0] or parsed[3] <= parsed[1]:
        raise ValueError(f"{field_name} must have positive width and height")
    return parsed


def normalize_points(body: dict[str, Any]) -> tuple[list[list[float]] | None, list[int] | None]:
    point_coords = body.get("point_coords")
    point_labels = body.get("point_labels")
    if point_coords is None and point_labels is None:
        return None, None
    if not isinstance(point_coords, list) or not isinstance(point_labels, list):
        raise ValueError("point_coords and point_labels must be arrays")
    if len(point_coords) != len(point_labels):
        raise ValueError("point_coords and point_labels must have the same length")
    parsed_coords: list[list[float]] = []
    parsed_labels: list[int] = []
    for index, point in enumerate(point_coords):
        if not isinstance(point, list) or len(point) != 2:
            raise ValueError(f"point_coords[{index}] must be [x, y]")
        x = float(point[0])
        y = float(point[1])
        parsed_coords.append([x, y])
        parsed_labels.append(1 if int(point_labels[index]) > 0 else 0)
    return parsed_coords, parsed_labels


def normalize_queries(value: Any, default_role: str = "include") -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise ValueError("queries must be a non-empty array")
    queries: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ValueError(f"queries[{index}] must be an object")
        prompt_en = str(item.get("prompt_en") or "").strip()
        if not prompt_en:
            raise ValueError(f"queries[{index}].prompt_en must be a non-empty string")
        role = str(item.get("role") or default_role).strip() or default_role
        if role not in {"include", "exclude"}:
            raise ValueError(f"queries[{index}].role must be include or exclude")
        queries.append(
            {
                "id": str(item.get("id") or f"query_{index + 1}"),
                "prompt_en": prompt_en,
                "prompt_zh": str(item.get("prompt_zh") or "").strip() or None,
                "role": role,
            }
        )
    return queries


def bbox_iou_xyxy(a: list[float], b: list[float]) -> float:
    left = max(a[0], b[0])
    top = max(a[1], b[1])
    right = min(a[2], b[2])
    bottom = min(a[3], b[3])
    if right <= left or bottom <= top:
        return 0.0
    intersection = (right - left) * (bottom - top)
    area_a = max(0.0, a[2] - a[0]) * max(0.0, a[3] - a[1])
    area_b = max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])
    union = area_a + area_b - intersection
    if union <= 0:
        return 0.0
    return intersection / union


def dedupe_detections(detections: list[dict[str, Any]], dedupe_iou: float) -> list[dict[str, Any]]:
    if dedupe_iou <= 0:
        return detections
    kept: list[dict[str, Any]] = []
    for candidate in sorted(detections, key=lambda item: float(item.get("score", 0)), reverse=True):
        box = candidate["asset_bbox_xyxy"]
        if any(
            candidate.get("role") == existing.get("role")
            and bbox_iou_xyxy(box, existing["asset_bbox_xyxy"]) >= dedupe_iou
            for existing in kept
        ):
            continue
        kept.append(candidate)
    return kept


def run_detection(body: dict[str, Any]) -> dict[str, Any]:
    image_path = Path(str(body.get("image_path") or "")).expanduser()
    if not image_path.is_file():
        return api_error("image_not_found", f"Image does not exist: {image_path}")
    try:
        queries = normalize_queries(body.get("queries"))
        box_threshold = float(body.get("box_threshold", 0.35))
        text_threshold = float(body.get("text_threshold", 0.25))
        max_candidates = max(1, min(int(body.get("max_candidates", 32)), 256))
        dedupe_iou = max(0.0, min(float(body.get("dedupe_iou", 0.85)), 1.0))
    except (TypeError, ValueError) as exc:
        return api_error("invalid_grounding_request", str(exc))

    try:
        with GROUNDING_LOCK:
            model, torch, device = load_grounding_model()
            from groundingdino.util import box_ops
            from groundingdino.util.inference import load_image, predict

            image_source, image = load_image(str(image_path))
            height, width = image_source.shape[:2]
            detections: list[dict[str, Any]] = []
            box_index = 0
            for query in queries:
                boxes, logits, phrases = predict(
                    model=model,
                    image=image,
                    caption=query["prompt_en"],
                    box_threshold=box_threshold,
                    text_threshold=text_threshold,
                    device=device,
                )
                if getattr(boxes, "numel", lambda: 0)() == 0:
                    continue
                scale = torch.tensor([width, height, width, height], device=boxes.device)
                boxes_xyxy = box_ops.box_cxcywh_to_xyxy(boxes) * scale
                for idx in range(boxes_xyxy.shape[0]):
                    xyxy = boxes_xyxy[idx].detach().cpu().tolist()
                    left = max(0.0, min(float(width - 1), float(xyxy[0])))
                    top = max(0.0, min(float(height - 1), float(xyxy[1])))
                    right = max(left + 1.0, min(float(width), float(xyxy[2])))
                    bottom = max(top + 1.0, min(float(height), float(xyxy[3])))
                    detections.append(
                        {
                            "query_id": query["id"],
                            "label": str(phrases[idx]) if idx < len(phrases) else query["prompt_en"],
                            "score": float(logits[idx].detach().cpu().item()),
                            "role": query["role"],
                            "asset_bbox_xyxy": [round(left, 3), round(top, 3), round(right, 3), round(bottom, 3)],
                            "asset_bbox": {
                                "x": round(left, 3),
                                "y": round(top, 3),
                                "width": round(right - left, 3),
                                "height": round(bottom - top, 3),
                            },
                            "box_index": box_index,
                        }
                    )
                    box_index += 1

            deduped = dedupe_detections(detections, dedupe_iou)[:max_candidates]
            warnings = [] if deduped else ["grounding_no_detections"]
            if GROUNDING_FALLBACK_REASON == "grounding_cuda_extension_missing":
                warnings.append("grounding_cpu_fallback_no_cuda_extension")
            elif GROUNDING_FALLBACK_REASON:
                warnings.append(f"grounding_cpu_fallback_{GROUNDING_FALLBACK_REASON}")
            return {
                "status": "ok",
                "schema_version": "ps-agent/v1",
                "device": device,
                "devices": {
                    "grounding_device": device,
                    "grounding_device_policy": GROUNDING_DEVICE_POLICY,
                    "grounding_fallback_reason": GROUNDING_FALLBACK_REASON,
                },
                "image_size": {"width": width, "height": height},
                "candidate_count": len(deduped),
                "detections": deduped,
                "prompt_summary": {
                    "box_threshold": box_threshold,
                    "text_threshold": text_threshold,
                    "queries": queries,
                },
                "warnings": warnings,
            }
    except WorkerError as exc:
        return api_error(
            exc.code,
            str(exc),
            {
                "grounding_model_path": str(GROUNDING_DINO_MODEL_PATH),
                "grounding_config_path": str(GROUNDING_DINO_CONFIG_PATH),
                "device_policy": GROUNDING_DEVICE_POLICY,
                **(exc.details if isinstance(exc.details, dict) else {}),
            },
        )
    except RuntimeError as exc:
        message = str(exc)
        details = {
            "grounding_model_path": str(GROUNDING_DINO_MODEL_PATH),
            "grounding_config_path": str(GROUNDING_DINO_CONFIG_PATH),
        }
        try:
            import torch

            if "out of memory" in message.lower() and torch.cuda.is_available():
                torch.cuda.empty_cache()
                details["cuda_cache_cleared"] = True
        except Exception:
            pass
        code = "grounding_cuda_oom" if "out of memory" in message.lower() else "grounding_detect_failed"
        return api_error(code, message, details)
    except Exception as exc:
        return api_error(
            "grounding_detect_failed",
            str(exc),
            {
                "grounding_model_path": str(GROUNDING_DINO_MODEL_PATH),
                "grounding_config_path": str(GROUNDING_DINO_CONFIG_PATH),
            },
        )


def run_hqsam_segment(body: dict[str, Any]) -> dict[str, Any]:
    image_path = Path(str(body.get("image_path") or "")).expanduser()
    output_mask_path = Path(str(body.get("output_mask_path") or "")).expanduser()
    if not image_path.is_file():
        return api_error("image_not_found", f"Image does not exist: {image_path}")
    if not str(output_mask_path):
        return api_error("output_mask_path_missing", "output_mask_path is required")
    output_mask_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        box = normalize_box(body.get("box"), "box") if body.get("box") is not None else None
        point_coords, point_labels = normalize_points(body)
        if box is None and point_coords is None:
            raise ValueError("box or point_coords is required")
    except (TypeError, ValueError) as exc:
        return api_error("invalid_hqsam_prompt", str(exc))

    try:
        import numpy as np
        from PIL import Image

        with HQSAM_LOCK:
            predictor, torch, device = load_hqsam_predictor()
            with Image.open(image_path) as image_file:
                image = np.array(image_file.convert("RGB"))
            predictor.set_image(image)
            prompt_box = np.array(box, dtype=np.float32) if box is not None else None
            prompt_points = np.array(point_coords, dtype=np.float32) if point_coords is not None else None
            prompt_labels = np.array(point_labels, dtype=np.int32) if point_labels is not None else None
            with torch.inference_mode():
                masks, scores, _ = predictor.predict(
                    point_coords=prompt_points,
                    point_labels=prompt_labels,
                    box=prompt_box,
                    multimask_output=body.get("multimask_output", True) is not False,
                )
            best_index = int(np.argmax(scores))
            best_mask = masks[best_index].astype("uint8") * 255
            Image.fromarray(best_mask, mode="L").save(output_mask_path)
            return {
                "status": "ok",
                "schema_version": "ps-agent/v1",
                "score": float(scores[best_index]),
                "score_candidates": [float(item) for item in scores.tolist()],
                "best_index": best_index,
                "device": device,
                "devices": {
                    "hqsam_device": device,
                    "hqsam_device_policy": HQSAM_DEVICE_POLICY,
                    "hqsam_fallback_reason": HQSAM_FALLBACK_REASON,
                },
                "mask_path": str(output_mask_path),
                "mask_size": {"width": int(best_mask.shape[1]), "height": int(best_mask.shape[0])},
            }
    except WorkerError as exc:
        return api_error(
            exc.code,
            str(exc),
            {
                "hqsam_model_path": str(HQSAM_MODEL_PATH),
                "model_type": HQSAM_MODEL_TYPE,
                "device_policy": HQSAM_DEVICE_POLICY,
                **(exc.details if isinstance(exc.details, dict) else {}),
            },
        )
    except RuntimeError as exc:
        message = str(exc)
        details = {"hqsam_model_path": str(HQSAM_MODEL_PATH), "model_type": HQSAM_MODEL_TYPE}
        try:
            import torch

            if "out of memory" in message.lower() and torch.cuda.is_available():
                torch.cuda.empty_cache()
                details["cuda_cache_cleared"] = True
        except Exception:
            pass
        code = "hqsam_cuda_oom" if "out of memory" in message.lower() else "hqsam_segment_failed"
        return api_error(code, message, details)
    except Exception as exc:
        return api_error(
            "hqsam_segment_failed",
            str(exc),
            {"hqsam_model_path": str(HQSAM_MODEL_PATH), "model_type": HQSAM_MODEL_TYPE},
        )


def center_point_from_xyxy(box: list[float]) -> list[float]:
    return [round((box[0] + box[2]) / 2.0, 3), round((box[1] + box[3]) / 2.0, 3)]


def overlap_ratio_xyxy(a: list[float], b: list[float]) -> float:
    left = max(a[0], b[0])
    top = max(a[1], b[1])
    right = min(a[2], b[2])
    bottom = min(a[3], b[3])
    if right <= left or bottom <= top:
        return 0.0
    intersection = (right - left) * (bottom - top)
    area_a = max(0.0, a[2] - a[0]) * max(0.0, a[3] - a[1])
    if area_a <= 0:
        return 0.0
    return intersection / area_a


def expand_xyxy(box: list[float], padding: float, width: int, height: int) -> list[float]:
    left = max(0.0, box[0] - padding)
    top = max(0.0, box[1] - padding)
    right = min(float(width), box[2] + padding)
    bottom = min(float(height), box[3] + padding)
    return [round(left, 3), round(top, 3), round(max(left + 1.0, right), 3), round(max(top + 1.0, bottom), 3)]


def normalize_worker_query_groups(body: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    include_queries = body.get("include_queries")
    exclude_queries = body.get("exclude_queries")
    if include_queries is None:
        include_queries = body.get("queries")
    includes = normalize_queries(include_queries, default_role="include")
    excludes = normalize_queries(exclude_queries, default_role="exclude") if exclude_queries else []
    return includes, excludes


def run_grounded_mask(body: dict[str, Any]) -> dict[str, Any]:
    image_path = Path(str(body.get("image_path") or "")).expanduser()
    output_dir = Path(str(body.get("output_dir") or "")).expanduser()
    if not image_path.is_file():
        return api_error("image_not_found", f"Image does not exist: {image_path}")
    if not str(output_dir):
        return api_error("output_dir_missing", "output_dir is required")
    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        includes, excludes = normalize_worker_query_groups(body)
        box_threshold = float(body.get("box_threshold", 0.35))
        text_threshold = float(body.get("text_threshold", 0.25))
        max_candidates = max(1, min(int(body.get("max_candidates", 32)), 256))
        dedupe_iou = max(0.0, min(float(body.get("dedupe_iou", 0.85)), 1.0))
        selection_policy = body.get("selection_policy") if isinstance(body.get("selection_policy"), dict) else {}
        segmentation_prompt_policy = (
            body.get("segmentation_prompt_policy")
            if isinstance(body.get("segmentation_prompt_policy"), dict)
            else {}
        )
        merge_mode = str(body.get("merge_mode") or ("subtract_excludes" if excludes else "union"))
    except (TypeError, ValueError) as exc:
        return api_error("invalid_grounded_mask_request", str(exc))

    per_query_limit = max(1, min(int(selection_policy.get("per_query_limit", 6)), 16))
    max_include_detections = max(1, min(int(selection_policy.get("max_include_detections", 12)), 32))
    max_exclude_detections = max(0, min(int(selection_policy.get("max_exclude_detections", 8)), 32))
    box_expand_px = max(0.0, min(float(segmentation_prompt_policy.get("box_expand_px", 0)), 512.0))
    include_center_point = segmentation_prompt_policy.get("include_center_point", True) is not False
    negative_points_from_exclude = segmentation_prompt_policy.get("negative_points_from_exclude", True) is not False
    negative_point_budget = max(0, min(int(segmentation_prompt_policy.get("negative_point_budget", 8)), 32))
    exclude_overlap_min = max(0.0, min(float(segmentation_prompt_policy.get("exclude_overlap_min", 0.01)), 1.0))
    multimask_output = segmentation_prompt_policy.get("multimask_output", True) is not False

    detection_result = run_detection(
        {
            "image_path": str(image_path),
            "queries": [
                *[{**query, "role": "include"} for query in includes],
                *[{**query, "role": "exclude"} for query in excludes],
            ],
            "box_threshold": box_threshold,
            "text_threshold": text_threshold,
            "max_candidates": max_candidates,
            "dedupe_iou": dedupe_iou,
        }
    )
    if detection_result.get("status") != "ok":
        return detection_result
    warnings = list(detection_result.get("warnings", []))

    detections = detection_result.get("detections", [])
    include_detections: list[dict[str, Any]] = []
    exclude_detections: list[dict[str, Any]] = []
    include_counts: dict[str, int] = {}
    exclude_counts: dict[str, int] = {}
    for item in detections:
        target = include_detections if item.get("role") == "include" else exclude_detections
        counter = include_counts if item.get("role") == "include" else exclude_counts
        limit = per_query_limit
        query_id = str(item.get("query_id"))
        counter[query_id] = counter.get(query_id, 0)
        if counter[query_id] >= limit:
            continue
        if item.get("role") == "include" and len(include_detections) >= max_include_detections:
            continue
        if item.get("role") == "exclude" and len(exclude_detections) >= max_exclude_detections:
            continue
        counter[query_id] += 1
        target.append(item)

    if not include_detections:
        return api_error(
            "grounding_no_include_detections",
            "Grounding DINO did not return any include detections.",
            detection_result,
        )

    try:
        from PIL import Image

        with Image.open(image_path) as image_file:
            image_width, image_height = image_file.size
    except Exception as exc:
        return api_error("image_open_failed", str(exc), {"image_path": str(image_path)})

    include_mask_paths: list[Path] = []
    exclude_mask_paths: list[Path] = []
    instance_masks: list[dict[str, Any]] = []
    exclude_instance_masks: list[dict[str, Any]] = []

    for index, detection in enumerate(include_detections):
        bbox = expand_xyxy(detection["asset_bbox_xyxy"], box_expand_px, image_width, image_height)
        positive_points = [center_point_from_xyxy(bbox)] if include_center_point else []
        negative_points: list[list[float]] = []
        if negative_points_from_exclude and exclude_detections:
            for candidate in exclude_detections:
                if overlap_ratio_xyxy(bbox, candidate["asset_bbox_xyxy"]) >= exclude_overlap_min:
                    negative_points.append(center_point_from_xyxy(candidate["asset_bbox_xyxy"]))
                    if len(negative_points) >= negative_point_budget:
                        break
        output_mask_path = output_dir / f"include_mask_{index:02d}.png"
        segment_result = run_hqsam_segment(
            {
                "image_path": str(image_path),
                "output_mask_path": str(output_mask_path),
                "box": bbox,
                "point_coords": [*positive_points, *negative_points] if (positive_points or negative_points) else None,
                "point_labels": ([1] * len(positive_points)) + ([0] * len(negative_points))
                if (positive_points or negative_points)
                else None,
                "multimask_output": multimask_output,
            }
        )
        if segment_result.get("status") != "ok":
            return segment_result
        include_mask_paths.append(output_mask_path)
        instance_masks.append(
            {
                "kind": "include",
                "index": index,
                "detection": detection,
                "bbox_used": bbox,
                "positive_points": positive_points,
                "negative_points": negative_points,
                "mask_path": str(output_mask_path),
                "score": segment_result.get("score"),
                "device": segment_result.get("device"),
            }
        )

    if exclude_detections and merge_mode == "subtract_excludes":
        for index, detection in enumerate(exclude_detections):
            bbox = expand_xyxy(detection["asset_bbox_xyxy"], box_expand_px, image_width, image_height)
            output_mask_path = output_dir / f"exclude_mask_{index:02d}.png"
            segment_result = run_hqsam_segment(
                {
                    "image_path": str(image_path),
                    "output_mask_path": str(output_mask_path),
                    "box": bbox,
                    "point_coords": [center_point_from_xyxy(bbox)],
                    "point_labels": [1],
                    "multimask_output": multimask_output,
                }
            )
            if segment_result.get("status") != "ok":
                return segment_result
            exclude_mask_paths.append(output_mask_path)
            exclude_instance_masks.append(
                {
                    "kind": "exclude",
                    "index": index,
                    "detection": detection,
                    "bbox_used": bbox,
                    "mask_path": str(output_mask_path),
                    "score": segment_result.get("score"),
                    "device": segment_result.get("device"),
                }
            )

    try:
        from ps_backend.alpha_masks import compose_soft_masks
    except Exception as exc:
        return api_error("alpha_compose_dependency_failed", str(exc))

    merged_mask_path = output_dir / "grounded_hq_merged_mask.png"
    effective_merge_mode = "intersect" if merge_mode == "intersect" else "union"
    merge_report = compose_soft_masks(
        include_mask_paths,
        merged_mask_path,
        merge_mode=effective_merge_mode,
        exclude_paths=exclude_mask_paths if merge_mode == "subtract_excludes" else None,
    )
    merge_report["requested_merge_mode"] = merge_mode

    return {
        "status": "ok",
        "schema_version": "ps-agent/v1",
        "device": HQSAM_DEVICE or GROUNDING_DEVICE,
        "devices": {
            "grounding_device": GROUNDING_DEVICE,
            "grounding_device_policy": GROUNDING_DEVICE_POLICY,
            "grounding_fallback_reason": GROUNDING_FALLBACK_REASON,
            "hqsam_device": HQSAM_DEVICE,
            "hqsam_device_policy": HQSAM_DEVICE_POLICY,
            "hqsam_fallback_reason": HQSAM_FALLBACK_REASON,
        },
        "image_size": {"width": image_width, "height": image_height},
        "detections": {
            "include": include_detections,
            "exclude": exclude_detections,
        },
        "instance_masks": instance_masks,
        "exclude_instance_masks": exclude_instance_masks,
        "merged_mask_path": str(merged_mask_path),
        "merge_report": merge_report,
        "warnings": warnings,
    }


def client_is_loopback(handler: BaseHTTPRequestHandler) -> bool:
    host = handler.client_address[0]
    return host in {"127.0.0.1", "::1", "localhost"}


class GroundingHQWorkerHandler(BaseHTTPRequestHandler):
    server_version = "PSUXPGroundingHQWorker/0.1"

    def log_message(self, format: str, *args: Any) -> None:
        try:
            sys.stderr.write("%s - - [%s] %s\n" % (self.address_string(), self.log_date_time_string(), format % args))
        except Exception:
            pass

    def do_OPTIONS(self) -> None:
        json_response(self, 200, {"status": "ok"})

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path == "/health":
            json_response(self, 200, health_payload())
            return
        json_response(self, 404, api_error("not_found", f"Unknown endpoint: {path}"))

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        try:
            body = parse_json_body(self)
        except Exception as exc:
            json_response(self, 400, api_error("invalid_json", str(exc)))
            return

        if path == "/detect":
            result = run_detection(body)
            json_response(self, 200 if result.get("status") == "ok" else 500, result)
            return

        if path == "/segment/hqsam":
            result = run_hqsam_segment(body)
            json_response(self, 200 if result.get("status") == "ok" else 500, result)
            return

        if path == "/grounded-mask":
            result = run_grounded_mask(body)
            json_response(self, 200 if result.get("status") == "ok" else 500, result)
            return

        if path == "/shutdown":
            if not client_is_loopback(self):
                json_response(self, 403, api_error("shutdown_forbidden", "Shutdown is only allowed from loopback."))
                return
            json_response(self, 200, {"status": "ok", "message": "Grounding HQ worker shutdown requested."})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return

        json_response(self, 404, api_error("not_found", f"Unknown endpoint: {path}"))


def write_pid() -> None:
    RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)
    PID_PATH.write_text(str(os.getpid()), encoding="utf-8")


def clear_pid() -> None:
    try:
        if PID_PATH.is_file():
            PID_PATH.unlink()
    except OSError:
        pass


def main() -> int:
    write_pid()
    server = ThreadingHTTPServer((DEFAULT_HOST, DEFAULT_PORT), GroundingHQWorkerHandler)
    try:
        server.serve_forever()
    finally:
        clear_pid()
        server.server_close()
    return 0


STARTED_AT_TS = time.time()
STARTED_AT_ISO = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(STARTED_AT_TS))


if __name__ == "__main__":
    raise SystemExit(main())
