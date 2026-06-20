from __future__ import annotations

import json
import math
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .alpha_masks import (
    ASSET_ROOT,
    AssetUrlBuilder,
    WORKSPACE_ROOT,
    api_error,
    asset_payload,
    build_alpha_outputs,
    dependency_status,
    is_number,
    new_asset_job_id,
    normalize_mask_prompt,
    normalize_bbox,
    normalize_size,
    read_pid,
    resolve_workspace_asset_path,
    safe_path_part,
)


BACKEND_DIR = Path(__file__).resolve().parents[1]
GROUNDING_HQ_HOST = "127.0.0.1"
GROUNDING_HQ_PORT = 17862
GROUNDING_HQ_BASE_URL = f"http://{GROUNDING_HQ_HOST}:{GROUNDING_HQ_PORT}"
GROUNDING_HQ_VENV_PYTHON = WORKSPACE_ROOT / ".venv-sam" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
GROUNDING_HQ_WORKER_SCRIPT = BACKEND_DIR / "grounding_hq_worker.py"
GROUNDING_HQ_PID_PATH = BACKEND_DIR / "runtime" / "grounding-hq-worker.pid"
GROUNDING_HQ_LOG_PATH = BACKEND_DIR / "runtime" / "grounding-hq-worker.log"
GROUNDING_DINO_MODEL_PATH = BACKEND_DIR / "models" / "grounding_dino" / "groundingdino_swint_ogc.pth"
GROUNDING_DINO_CONFIG_PATH = BACKEND_DIR / "models" / "grounding_dino" / "GroundingDINO_SwinT_OGC.py"
HQSAM_MODEL_PATH = BACKEND_DIR / "models" / "sam_hq" / "sam_hq_vit_l.pth"


def request_worker_json(method: str, path: str, payload: dict[str, Any] | None = None, timeout: float = 5) -> dict[str, Any]:
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(GROUNDING_HQ_BASE_URL + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return api_error("grounding_hq_worker_http_error", raw or str(exc))
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return api_error(
            "grounding_hq_worker_unreachable",
            f"Could not reach Grounding HQ worker at {GROUNDING_HQ_BASE_URL}: {getattr(exc, 'reason', exc)}",
            {
                "base_url": GROUNDING_HQ_BASE_URL,
                "start_command": "python backend/cli.py grounding start",
                "status_command": "python backend/cli.py grounding status",
                "log_path": str(GROUNDING_HQ_LOG_PATH),
            },
        )


def grounding_hq_worker_status(timeout: float = 2) -> dict[str, Any]:
    health = request_worker_json("GET", "/health", timeout=timeout)
    return {
        "base_url": GROUNDING_HQ_BASE_URL,
        "reachable": health.get("status") == "ok",
        "health": health,
        "pid": read_pid(GROUNDING_HQ_PID_PATH),
        "paths": {
            "venv_python": {"path": str(GROUNDING_HQ_VENV_PYTHON), "exists": GROUNDING_HQ_VENV_PYTHON.is_file()},
            "worker_script": {"path": str(GROUNDING_HQ_WORKER_SCRIPT), "exists": GROUNDING_HQ_WORKER_SCRIPT.is_file()},
            "grounding_dino_model": {
                "path": str(GROUNDING_DINO_MODEL_PATH),
                "exists": GROUNDING_DINO_MODEL_PATH.is_file(),
                "size_bytes": GROUNDING_DINO_MODEL_PATH.stat().st_size if GROUNDING_DINO_MODEL_PATH.is_file() else None,
            },
            "grounding_dino_config": {
                "path": str(GROUNDING_DINO_CONFIG_PATH),
                "exists": GROUNDING_DINO_CONFIG_PATH.is_file(),
            },
            "hqsam_model": {
                "path": str(HQSAM_MODEL_PATH),
                "exists": HQSAM_MODEL_PATH.is_file(),
                "size_bytes": HQSAM_MODEL_PATH.stat().st_size if HQSAM_MODEL_PATH.is_file() else None,
            },
            "log": {"path": str(GROUNDING_HQ_LOG_PATH), "exists": GROUNDING_HQ_LOG_PATH.is_file()},
            "pid_file": {"path": str(GROUNDING_HQ_PID_PATH), "exists": GROUNDING_HQ_PID_PATH.is_file()},
        },
        "dependencies_in_main_env": {
            "pillow": dependency_status("PIL"),
            "numpy": dependency_status("numpy"),
            "torch": dependency_status("torch"),
            "groundingdino": dependency_status("groundingdino"),
            "segment_anything_hq": dependency_status("segment_anything_hq"),
        },
        "start_command": "python backend/cli.py grounding start",
    }


def validate_scale_factor(value: Any) -> float:
    scale_factor = float(value)
    if not math.isfinite(scale_factor) or scale_factor <= 0:
        raise ValueError("scale_factor must be a positive number")
    return scale_factor


def validate_threshold(value: Any, field_name: str, default: float) -> float:
    parsed = float(default if value is None else value)
    if not math.isfinite(parsed) or parsed < 0 or parsed > 1:
        raise ValueError(f"{field_name} must be between 0 and 1")
    return parsed


def normalize_query_groups(body: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    include_queries = body.get("include_queries")
    exclude_queries = body.get("exclude_queries")
    if include_queries is None:
        include_queries = body.get("queries")
    includes = normalize_queries(include_queries, "include")
    excludes = normalize_queries(exclude_queries, "exclude") if exclude_queries else []
    return includes, excludes


def normalize_queries(value: Any, default_role: str) -> list[dict[str, Any]]:
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
        query_id = safe_path_part(str(item.get("id") or f"query_{index + 1}"), f"query_{index + 1}")
        queries.append(
            {
                "id": query_id,
                "prompt_en": prompt_en,
                "prompt_zh": str(item.get("prompt_zh") or "").strip() or None,
                "role": role,
            }
        )
    return queries


def detection_to_document_bbox(
    asset_bbox: dict[str, float],
    document_bbox: dict[str, float],
    scale_factor: float,
    document_size: dict[str, int],
) -> dict[str, float]:
    left = document_bbox["x"] + (asset_bbox["x"] / scale_factor)
    top = document_bbox["y"] + (asset_bbox["y"] / scale_factor)
    width = asset_bbox["width"] / scale_factor
    height = asset_bbox["height"] / scale_factor
    right = min(float(document_size["width"]), left + width)
    bottom = min(float(document_size["height"]), top + height)
    return {
        "x": round(max(0.0, left), 3),
        "y": round(max(0.0, top), 3),
        "width": round(max(1.0, right - left), 3),
        "height": round(max(1.0, bottom - top), 3),
    }


def build_detection_overlay(asset_path: Path, detections: list[dict[str, Any]], overlay_path: Path) -> None:
    from PIL import Image, ImageDraw

    with Image.open(asset_path) as image_file:
        image = image_file.convert("RGBA")
    draw = ImageDraw.Draw(image)
    for detection in detections:
        bbox = detection.get("asset_bbox", {})
        left = float(bbox.get("x", 0))
        top = float(bbox.get("y", 0))
        right = left + float(bbox.get("width", 0))
        bottom = top + float(bbox.get("height", 0))
        role = detection.get("role")
        color = (255, 80, 40, 255) if role != "exclude" else (40, 160, 255, 255)
        draw.rectangle([left, top, right, bottom], outline=color, width=3)
        label = f"{detection.get('label', 'object')} {float(detection.get('score', 0)):.2f}"
        draw.text((left + 4, max(0.0, top - 18)), label, fill=color)
    overlay_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(overlay_path)


def detect_grounding_boxes(body: dict[str, Any], asset_url_builder: AssetUrlBuilder | None = None) -> dict[str, Any]:
    try:
        asset_path = resolve_workspace_asset_path(body.get("asset_path"))
        document_size = normalize_size(body.get("document_size"))
        document_bbox = normalize_bbox(body.get("document_bbox"), "document_bbox")
        scale_factor = validate_scale_factor(body.get("scale_factor"))
        queries = normalize_queries(body.get("queries"), "include")
        box_threshold = validate_threshold(body.get("box_threshold"), "box_threshold", 0.35)
        text_threshold = validate_threshold(body.get("text_threshold"), "text_threshold", 0.25)
        max_candidates = max(1, min(int(body.get("max_candidates", 32)), 256))
        dedupe_iou = validate_threshold(body.get("dedupe_iou"), "dedupe_iou", 0.85)
    except FileNotFoundError as exc:
        return api_error("asset_not_found", str(exc))
    except (TypeError, ValueError) as exc:
        return api_error("invalid_grounding_detect_request", str(exc))

    worker = grounding_hq_worker_status(timeout=1.5)
    if not worker.get("reachable"):
        return api_error(
            "grounding_hq_worker_unreachable",
            "Grounding HQ worker is not reachable. Start it before detecting boxes.",
            worker,
        )

    worker_result = request_worker_json(
        "POST",
        "/detect",
        {
            "image_path": str(asset_path),
            "queries": queries,
            "box_threshold": box_threshold,
            "text_threshold": text_threshold,
            "max_candidates": max_candidates,
            "dedupe_iou": dedupe_iou,
        },
        timeout=max(5.0, min(float(body.get("timeout_ms", 120000) or 120000) / 1000.0, 300.0)),
    )
    if worker_result.get("status") != "ok":
        return worker_result

    job_id = safe_path_part(str(body.get("job_id") or new_asset_job_id()), "job")
    asset_dir = ASSET_ROOT / job_id
    asset_dir.mkdir(parents=True, exist_ok=True)
    overlay_path = asset_dir / "grounding_overlay.png"

    detections: list[dict[str, Any]] = []
    for detection in worker_result.get("detections", []):
        detection = dict(detection)
        asset_bbox = detection.get("asset_bbox")
        if isinstance(asset_bbox, dict):
            detection["document_bbox"] = detection_to_document_bbox(asset_bbox, document_bbox, scale_factor, document_size)
        detections.append(detection)

    build_detection_overlay(asset_path, detections, overlay_path)
    relative_overlay = f"{job_id}/grounding_overlay.png"

    return {
        "status": "ok",
        "schema_version": "ps-agent/v1",
        "provider": "grounding_dino",
        "job_id": job_id,
        "asset_path": str(asset_path),
        "detections": detections,
        "candidate_count": worker_result.get("candidate_count", len(detections)),
        "devices": worker_result.get("devices"),
        "prompt_summary": worker_result.get("prompt_summary"),
        "warnings": worker_result.get("warnings", []),
        "overlay_preview": asset_payload(relative_overlay, overlay_path, "image/png", asset_url_builder),
    }


def generate_hqsam_mask(body: dict[str, Any], asset_url_builder: AssetUrlBuilder | None = None) -> dict[str, Any]:
    try:
        asset_path = resolve_workspace_asset_path(body.get("asset_path"))
        document_size = normalize_size(body.get("document_size"))
        document_bbox = normalize_bbox(body.get("document_bbox"), "document_bbox")
        scale_factor = validate_scale_factor(body.get("scale_factor"))
        threshold = validate_threshold(body.get("threshold"), "threshold", 0.5)
        feather = float(body.get("feather", 0) or 0)
        if not math.isfinite(feather) or feather < 0 or feather > 500:
            raise ValueError("feather must be between 0 and 500")
    except FileNotFoundError as exc:
        return api_error("asset_not_found", str(exc))
    except (TypeError, ValueError) as exc:
        return api_error("invalid_hqsam_mask_request", str(exc))

    try:
        from PIL import Image
    except ImportError:
        return api_error("pillow_not_installed", "Pillow is required in the main backend to compose full-size alpha masks.")

    with Image.open(asset_path) as image_file:
        image_size = image_file.size

    try:
        prompt = normalize_mask_prompt(body.get("prompt"), document_bbox, scale_factor, image_size)
    except ValueError as exc:
        return api_error("invalid_hqsam_prompt", str(exc))

    worker = grounding_hq_worker_status(timeout=1.5)
    if not worker.get("reachable"):
        return api_error(
            "grounding_hq_worker_unreachable",
            "Grounding HQ worker is not reachable. Start it before generating masks.",
            worker,
        )

    job_id = safe_path_part(str(body.get("job_id") or new_asset_job_id()), "job")
    asset_dir = ASSET_ROOT / job_id
    asset_dir.mkdir(parents=True, exist_ok=True)
    crop_mask_path = asset_dir / "hqsam_crop_mask.png"
    worker_payload = {
        "image_path": str(asset_path),
        "output_mask_path": str(crop_mask_path),
        "box": prompt.get("box"),
        "point_coords": prompt.get("point_coords"),
        "point_labels": prompt.get("point_labels"),
        "multimask_output": body.get("multimask_output", True) is not False,
    }
    worker_result = request_worker_json(
        "POST",
        "/segment/hqsam",
        worker_payload,
        timeout=max(5.0, min(float(body.get("timeout_ms", 120000) or 120000) / 1000.0, 300.0)),
    )
    if worker_result.get("status") != "ok":
        return worker_result
    if not crop_mask_path.is_file():
        return api_error(
            "hqsam_mask_missing",
            "HQ-SAM worker returned ok but did not create the expected mask file.",
            {"expected_path": str(crop_mask_path), "worker_result": worker_result},
        )

    outputs = build_alpha_outputs(asset_path, crop_mask_path, asset_dir, job_id, document_size, document_bbox, threshold)
    selection_mask = {
        "source": "alpha_mask",
        "asset_path": str(outputs["alpha_path"]),
        "asset_uri": asset_url_builder(outputs["relative"]["alpha"]) if asset_url_builder else None,
        "threshold": threshold,
        "feather": feather,
        "invert": body.get("invert") is True,
        "show_marching_ants": body.get("show_marching_ants") is True,
        "label": str(body.get("label") or "hqsam_alpha_mask")[:128],
    }
    warnings = list(outputs["warnings"])
    if worker_result.get("warnings"):
        warnings.extend(worker_result["warnings"])

    return {
        "status": "ok",
        "schema_version": "ps-agent/v1",
        "provider": "hqsam",
        "model": "sam_hq_vit_l",
        "job_id": job_id,
        "asset_path": str(asset_path),
        "document_size": document_size,
        "document_bbox": document_bbox,
        "scale_factor": scale_factor,
        "prompt": prompt,
        "threshold": threshold,
        "feather": feather,
        "warnings": warnings,
        "area_ratio": outputs["area_ratio"],
        "mask_bbox": outputs["mask_bbox"],
        "selected_pixels": outputs["selected_pixels"],
        "worker": {"score": worker_result.get("score"), "score_candidates": worker_result.get("score_candidates")},
        "devices": worker_result.get("devices"),
        "alpha_mask": asset_payload(outputs["relative"]["alpha"], outputs["alpha_path"], "image/png", asset_url_builder),
        "mask_preview": asset_payload(outputs["relative"]["mask_preview"], outputs["mask_preview_path"], "image/png", asset_url_builder),
        "overlay_preview": asset_payload(outputs["relative"]["overlay"], outputs["overlay_path"], "image/png", asset_url_builder),
        "selection_mask": selection_mask,
    }


def normalize_merge_mode(value: Any, has_excludes: bool) -> str:
    merge_mode = str(value or ("subtract_excludes" if has_excludes else "union")).strip()
    if merge_mode not in {"union", "subtract_excludes", "intersect"}:
        raise ValueError("merge_mode must be union, subtract_excludes, or intersect")
    return merge_mode


def generate_grounded_hq_mask(body: dict[str, Any], asset_url_builder: AssetUrlBuilder | None = None) -> dict[str, Any]:
    try:
        asset_path = resolve_workspace_asset_path(body.get("asset_path"))
        document_size = normalize_size(body.get("document_size"))
        document_bbox = normalize_bbox(body.get("document_bbox"), "document_bbox")
        scale_factor = validate_scale_factor(body.get("scale_factor"))
        include_queries, exclude_queries = normalize_query_groups(body)
        threshold = validate_threshold(body.get("threshold"), "threshold", 0.5)
        feather = float(body.get("feather", 0) or 0)
        if not math.isfinite(feather) or feather < 0 or feather > 500:
            raise ValueError("feather must be between 0 and 500")
        merge_mode = normalize_merge_mode(body.get("merge_mode"), bool(exclude_queries))
        box_threshold = validate_threshold(body.get("box_threshold"), "box_threshold", 0.35)
        text_threshold = validate_threshold(body.get("text_threshold"), "text_threshold", 0.25)
        max_candidates = max(1, min(int(body.get("max_candidates", 32)), 256))
        dedupe_iou = validate_threshold(body.get("dedupe_iou"), "dedupe_iou", 0.85)
    except FileNotFoundError as exc:
        return api_error("asset_not_found", str(exc))
    except (TypeError, ValueError) as exc:
        return api_error("invalid_grounded_hq_mask_request", str(exc))

    worker = grounding_hq_worker_status(timeout=1.5)
    if not worker.get("reachable"):
        return api_error(
            "grounding_hq_worker_unreachable",
            "Grounding HQ worker is not reachable. Start it before generating grounded masks.",
            worker,
        )

    job_id = safe_path_part(str(body.get("job_id") or new_asset_job_id()), "job")
    asset_dir = ASSET_ROOT / job_id
    asset_dir.mkdir(parents=True, exist_ok=True)

    worker_result = request_worker_json(
        "POST",
        "/grounded-mask",
        {
            "image_path": str(asset_path),
            "output_dir": str(asset_dir),
            "include_queries": include_queries,
            "exclude_queries": exclude_queries,
            "selection_policy": body.get("selection_policy") or {},
            "segmentation_prompt_policy": body.get("segmentation_prompt_policy") or {},
            "merge_mode": merge_mode,
            "box_threshold": box_threshold,
            "text_threshold": text_threshold,
            "max_candidates": max_candidates,
            "dedupe_iou": dedupe_iou,
        },
        timeout=max(10.0, min(float(body.get("timeout_ms", 180000) or 180000) / 1000.0, 300.0)),
    )
    if worker_result.get("status") != "ok":
        return worker_result

    merged_mask_path = Path(str(worker_result.get("merged_mask_path") or ""))
    if not merged_mask_path.is_file():
        return api_error(
            "grounded_hq_mask_missing",
            "Grounded HQ worker returned ok but did not create the expected merged mask file.",
            {"expected_path": str(merged_mask_path), "worker_result": worker_result},
        )

    outputs = build_alpha_outputs(asset_path, merged_mask_path, asset_dir, job_id, document_size, document_bbox, threshold)
    selection_mask = {
        "source": "alpha_mask",
        "asset_path": str(outputs["alpha_path"]),
        "asset_uri": asset_url_builder(outputs["relative"]["alpha"]) if asset_url_builder else None,
        "threshold": threshold,
        "feather": feather,
        "invert": body.get("invert") is True,
        "show_marching_ants": body.get("show_marching_ants") is True,
        "label": str(body.get("label") or "grounded_hq_alpha_mask")[:128],
    }

    include_detections = worker_result.get("detections", {}).get("include", [])
    exclude_detections = worker_result.get("detections", {}).get("exclude", [])
    for detection in include_detections:
        if isinstance(detection.get("asset_bbox"), dict):
            detection["document_bbox"] = detection_to_document_bbox(detection["asset_bbox"], document_bbox, scale_factor, document_size)
    for detection in exclude_detections:
        if isinstance(detection.get("asset_bbox"), dict):
            detection["document_bbox"] = detection_to_document_bbox(detection["asset_bbox"], document_bbox, scale_factor, document_size)

    instance_masks = []
    for item in worker_result.get("instance_masks", []):
        mask_path = Path(str(item.get("mask_path") or ""))
        if mask_path.is_file():
            relative_name = f"{job_id}/{mask_path.name}"
            item = dict(item)
            item["mask"] = asset_payload(relative_name, mask_path, "image/png", asset_url_builder)
        instance_masks.append(item)

    warnings = list(outputs["warnings"])
    warnings.extend(worker_result.get("warnings", []))

    return {
        "status": "ok",
        "schema_version": "ps-agent/v1",
        "provider": "grounding_dino_hqsam",
        "model": {
            "grounding_dino": "groundingdino_swint_ogc",
            "hqsam": "sam_hq_vit_l",
        },
        "job_id": job_id,
        "asset_path": str(asset_path),
        "document_size": document_size,
        "document_bbox": document_bbox,
        "scale_factor": scale_factor,
        "include_queries": include_queries,
        "exclude_queries": exclude_queries,
        "merge_mode": merge_mode,
        "threshold": threshold,
        "feather": feather,
        "warnings": warnings,
        "area_ratio": outputs["area_ratio"],
        "mask_bbox": outputs["mask_bbox"],
        "selected_pixels": outputs["selected_pixels"],
        "alpha_mask": asset_payload(outputs["relative"]["alpha"], outputs["alpha_path"], "image/png", asset_url_builder),
        "mask_preview": asset_payload(outputs["relative"]["mask_preview"], outputs["mask_preview_path"], "image/png", asset_url_builder),
        "overlay_preview": asset_payload(outputs["relative"]["overlay"], outputs["overlay_path"], "image/png", asset_url_builder),
        "selection_mask": selection_mask,
        "detections": {"include": include_detections, "exclude": exclude_detections},
        "instance_masks": instance_masks,
        "merge_report": worker_result.get("merge_report", {}),
        "worker": {
            "base_url": GROUNDING_HQ_BASE_URL,
            "device": worker_result.get("device"),
        },
        "devices": worker_result.get("devices"),
    }
