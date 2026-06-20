from __future__ import annotations

import math
import time
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable

from .alpha_masks import (
    ASSET_ROOT,
    api_error,
    asset_payload,
    normalize_bbox,
    normalize_point,
    normalize_size,
    resolve_workspace_asset_path,
    safe_path_part,
)


SCHEMA_VERSION = "ps-agent/v1"
MAX_POLYGON_POINTS = 256
DEFAULT_CONTOUR_MAX_POINTS = 96
DEFAULT_CONTOUR_SAMPLE_MAX_SIDE = 1280

AssetUrlBuilder = Callable[[str], str]


def _new_region_id(prefix: str = "region") -> str:
    return f"{prefix}-{time.strftime('%Y%m%d%H%M%S', time.gmtime())}-{uuid.uuid4().hex[:8]}"


def _new_asset_prefix(region_id: str) -> str:
    return f"{safe_path_part(region_id, 'region')}-{uuid.uuid4().hex[:8]}"


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(value, maximum))


def _point(value: Any, field_name: str = "point") -> list[float]:
    point = normalize_point(value, field_name)
    return [round(float(point[0]), 3), round(float(point[1]), 3)]


def _normalize_points(value: Any, field_name: str = "points") -> list[list[float]]:
    if not isinstance(value, list):
        raise ValueError(f"{field_name} must be an array")
    if len(value) < 2:
        raise ValueError(f"{field_name} must contain at least two points")
    if len(value) > MAX_POLYGON_POINTS:
        raise ValueError(f"{field_name} may contain at most {MAX_POLYGON_POINTS} points")
    return [_point(point, f"{field_name}[{index}]") for index, point in enumerate(value)]


def _polygon_bbox(points: list[list[float]]) -> dict[str, float]:
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    left = min(xs)
    top = min(ys)
    right = max(xs)
    bottom = max(ys)
    return {
        "x": round(left, 3),
        "y": round(top, 3),
        "width": round(max(0.001, right - left), 3),
        "height": round(max(0.001, bottom - top), 3),
    }


def _bbox_polygon(bbox: dict[str, float]) -> list[list[float]]:
    x = float(bbox["x"])
    y = float(bbox["y"])
    width = float(bbox["width"])
    height = float(bbox["height"])
    return [
        [round(x, 3), round(y, 3)],
        [round(x + width, 3), round(y, 3)],
        [round(x + width, 3), round(y + height, 3)],
        [round(x, 3), round(y + height, 3)],
    ]


def _normalize_landmarks(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ValueError("landmarks must be an array")
    landmarks: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        if isinstance(item, dict):
            point = _point(item, f"landmarks[{index}]")
            landmarks.append(
                {
                    "name": str(item.get("name") or item.get("id") or f"landmark_{index}"),
                    "x": point[0],
                    "y": point[1],
                    "index": item.get("index", index),
                }
            )
        else:
            point = _point(item, f"landmarks[{index}]")
            landmarks.append({"name": f"landmark_{index}", "x": point[0], "y": point[1], "index": index})
    return landmarks


def _points_to_bezier_path(points: list[list[float]], closed: bool = True, smooth: float = 0.0) -> dict[str, Any]:
    # First implementation keeps anchors exact and tangent handles on-anchor.
    # This is intentionally lossless for polygon/lasso usage; smoothing can be added later.
    smooth = _clamp(float(smooth or 0), 0.0, 1.0)
    path_points = []
    for index, point in enumerate(points):
        path_points.append(
            {
                "kind": "corner" if smooth <= 0 else "smooth_candidate",
                "anchor": {"x": round(point[0], 3), "y": round(point[1], 3)},
                "in": {"x": round(point[0], 3), "y": round(point[1], 3)},
                "out": {"x": round(point[0], 3), "y": round(point[1], 3)},
                "source_index": index,
            }
        )
    return {
        "type": "bezier_path",
        "closed": bool(closed),
        "subpaths": [
            {
                "closed": bool(closed),
                "points": path_points,
            }
        ],
        "point_count": len(path_points),
        "smoothing": smooth,
        "photoshop_lowering": {
            "status": "path_artifact_only",
            "note": "This is a pen/path representation. Convert to Photoshop work path after path.create_work_path atom is implemented.",
        },
    }


def _alpha_path_from_value(value: Any) -> Path:
    if isinstance(value, dict):
        candidate = value.get("path") or value.get("asset_path")
    else:
        candidate = value
    return resolve_workspace_asset_path(candidate)


def _alpha_selection_mask(alpha: dict[str, Any] | str, label: str, feather: float = 0, threshold: float = 0.5) -> dict[str, Any]:
    if isinstance(alpha, dict):
        path = alpha.get("path") or alpha.get("asset_path")
        uri = alpha.get("uri") or alpha.get("asset_uri")
    else:
        path = str(alpha)
        uri = None
    return {
        "source": "alpha_mask",
        "label": label,
        "asset_path": path,
        "asset_uri": uri,
        "threshold": threshold,
        "feather": feather,
        "invert": False,
    }


def _normalize_region_artifact(raw: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("region_artifact must be an object")
    artifact = deepcopy(raw)
    artifact.setdefault("schema_version", SCHEMA_VERSION)
    artifact.setdefault("artifact_type", "region_artifact")
    artifact.setdefault("region_id", _new_region_id())
    artifact.setdefault("label", artifact["region_id"])
    reps = artifact.setdefault("representations", {})
    if not isinstance(reps, dict):
        raise ValueError("region_artifact.representations must be an object")
    return artifact


def _artifact_from_face_selection(selection: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
    label = str(selection.get("part") or selection.get("requested_part") or body.get("label") or "face_region")
    region_id = safe_path_part(str(body.get("region_id") or f"face-{label}"), "face_region")
    points = _normalize_points(selection.get("points"), "selection.points")
    polygon = {
        "type": "polygon_contour",
        "points": points,
        "closed": True,
        "source": "face_landmarker",
        "point_count": len(points),
        "bbox": selection.get("bbox") or _polygon_bbox(points),
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "artifact_type": "region_artifact",
        "region_id": region_id,
        "label": label,
        "source": "face_landmarker",
        "quality": {
            "confidence": selection.get("confidence", 1.0),
            "edge_softness": 0.0,
            "area_ratio": None,
        },
        "representations": {
            "polygon": polygon,
            "selection_mask": selection.get("selection_mask") or {
                "source": "polygon",
                "label": label,
                "points": points,
                "feather": selection.get("feather", body.get("feather", 0)),
                "invert": False,
            },
            "bezier_path": _points_to_bezier_path(points, closed=True, smooth=float(body.get("path_smooth", 0) or 0)),
        },
        "provenance": {
            "provider": "face_landmarker",
            "requested_part": selection.get("requested_part"),
            "part": selection.get("part"),
        },
    }


def _artifact_from_alpha_result(result: dict[str, Any], body: dict[str, Any], asset_url_builder: AssetUrlBuilder | None) -> dict[str, Any]:
    selection_mask = result.get("selection_mask") if isinstance(result.get("selection_mask"), dict) else None
    alpha = result.get("alpha_mask") if isinstance(result.get("alpha_mask"), dict) else {}
    if selection_mask:
        label = str(body.get("label") or selection_mask.get("label") or "alpha_region")
    else:
        label = str(body.get("label") or "alpha_region")
    region_id = safe_path_part(str(body.get("region_id") or label or "alpha_region"), "alpha_region")
    alpha_path = selection_mask.get("asset_path") if selection_mask else alpha.get("path")
    alpha_uri = selection_mask.get("asset_uri") if selection_mask else alpha.get("uri")
    representations: dict[str, Any] = {
        "alpha_mask": {
            "type": "alpha_mask",
            "asset_path": alpha_path,
            "asset_uri": alpha_uri,
            "threshold": selection_mask.get("threshold", body.get("threshold", 0.5)) if selection_mask else body.get("threshold", 0.5),
            "feather": selection_mask.get("feather", body.get("feather", 0)) if selection_mask else body.get("feather", 0),
            "invert": bool(selection_mask.get("invert", False)) if selection_mask else False,
        },
        "selection_mask": selection_mask or _alpha_selection_mask(alpha, label),
    }
    if body.get("extract_polygon") is True and alpha_path:
        contour = extract_region_contour({"alpha_mask": {"asset_path": alpha_path}, "max_points": body.get("max_points", DEFAULT_CONTOUR_MAX_POINTS)}, asset_url_builder)
        if contour.get("status") == "ok":
            representations["polygon"] = contour["polygon"]
            representations["bezier_path"] = contour["bezier_path"]
    return {
        "schema_version": SCHEMA_VERSION,
        "artifact_type": "region_artifact",
        "region_id": region_id,
        "label": label,
        "source": result.get("provider") or result.get("engine") or body.get("source") or "alpha_mask",
        "quality": {
            "confidence": result.get("confidence"),
            "area_ratio": result.get("area_ratio"),
            "mask_bbox": result.get("mask_bbox"),
        },
        "representations": representations,
        "provenance": {
            "result_status": result.get("status"),
            "device": result.get("device"),
        },
    }


def create_region_artifact(body: dict[str, Any], asset_url_builder: AssetUrlBuilder | None = None) -> dict[str, Any]:
    try:
        if isinstance(body.get("region_artifact"), dict):
            return {"status": "ok", "schema_version": SCHEMA_VERSION, "artifact": _normalize_region_artifact(body["region_artifact"])}

        source_result = body.get("source_result") if isinstance(body.get("source_result"), dict) else None
        artifacts: list[dict[str, Any]] = []
        if source_result and isinstance(source_result.get("selections"), list):
            selected_part = body.get("part")
            for selection in source_result["selections"]:
                if selected_part and selection.get("part") != selected_part and selection.get("requested_part") != selected_part:
                    continue
                artifacts.append(_artifact_from_face_selection(selection, body))
            if not artifacts:
                raise ValueError(f"No face selection matched part={selected_part!r}")
            return {
                "status": "ok",
                "schema_version": SCHEMA_VERSION,
                "artifact_count": len(artifacts),
                "artifacts": artifacts,
                "artifact": artifacts[0],
            }

        if source_result and (source_result.get("selection_mask") or source_result.get("alpha_mask")):
            artifact = _artifact_from_alpha_result(source_result, body, asset_url_builder)
            return {"status": "ok", "schema_version": SCHEMA_VERSION, "artifact": artifact, "artifact_count": 1, "artifacts": [artifact]}

        region_id = safe_path_part(str(body.get("region_id") or _new_region_id()), "region")
        label = str(body.get("label") or region_id)
        source = str(body.get("source") or "manual")
        reps: dict[str, Any] = {}
        bbox = None
        if body.get("bbox") is not None:
            bbox = normalize_bbox(body["bbox"], "bbox")
            points = _bbox_polygon(bbox)
            reps["bbox"] = {"type": "bbox", "bbox": bbox}
            reps["polygon"] = {
                "type": "polygon_contour",
                "points": points,
                "closed": True,
                "source": "bbox",
                "point_count": len(points),
                "bbox": bbox,
            }
        if body.get("points") is not None or body.get("polygon") is not None:
            points = _normalize_points(body.get("points") or body.get("polygon"), "points")
            reps["polygon"] = {
                "type": "polygon_contour",
                "points": points,
                "closed": True,
                "source": source,
                "point_count": len(points),
                "bbox": _polygon_bbox(points),
            }
        if body.get("landmarks") is not None:
            landmarks = _normalize_landmarks(body["landmarks"])
            reps["landmarks"] = {"type": "landmark_points", "points": landmarks, "point_count": len(landmarks)}
        if body.get("alpha_mask") is not None or body.get("selection_mask", {}).get("source") == "alpha_mask":
            alpha = body.get("alpha_mask") or body.get("selection_mask")
            selection_mask = _alpha_selection_mask(alpha, label, float(body.get("feather", 0) or 0), float(body.get("threshold", 0.5) or 0.5))
            reps["alpha_mask"] = {
                "type": "alpha_mask",
                "asset_path": selection_mask.get("asset_path"),
                "asset_uri": selection_mask.get("asset_uri"),
                "threshold": selection_mask.get("threshold"),
                "feather": selection_mask.get("feather"),
                "invert": selection_mask.get("invert", False),
            }
            reps["selection_mask"] = selection_mask
            if body.get("extract_polygon") is True:
                contour = extract_region_contour({"alpha_mask": alpha, "max_points": body.get("max_points", DEFAULT_CONTOUR_MAX_POINTS)}, asset_url_builder)
                if contour.get("status") == "ok":
                    reps["polygon"] = contour["polygon"]
                    reps["bezier_path"] = contour["bezier_path"]
                    bbox = contour["polygon"].get("bbox")
        if "polygon" in reps and "bezier_path" not in reps:
            reps["selection_mask"] = reps.get("selection_mask") or {
                "source": "polygon",
                "label": label,
                "points": reps["polygon"]["points"],
                "feather": float(body.get("feather", 0) or 0),
                "invert": False,
            }
            reps["bezier_path"] = _points_to_bezier_path(reps["polygon"]["points"], closed=True, smooth=float(body.get("path_smooth", 0) or 0))
        if not reps:
            raise ValueError("Provide source_result, alpha_mask, selection_mask, bbox, points/polygon, or landmarks")

        artifact = {
            "schema_version": SCHEMA_VERSION,
            "artifact_type": "region_artifact",
            "region_id": region_id,
            "label": label,
            "source": source,
            "quality": {
                "bbox": bbox or reps.get("polygon", {}).get("bbox"),
                "confidence": body.get("confidence"),
            },
            "representations": reps,
            "provenance": {
                "created_by": "ps_create_region_artifact",
            },
        }
        return {"status": "ok", "schema_version": SCHEMA_VERSION, "artifact": artifact, "artifact_count": 1, "artifacts": [artifact]}
    except (FileNotFoundError, ValueError, TypeError) as exc:
        return api_error("invalid_region_artifact_request", str(exc))


def _selected_boundary_points(mask, threshold: int, max_points: int) -> tuple[list[list[float]], dict[str, float] | None, dict[str, Any]]:
    from PIL import Image

    image = mask.convert("L")
    original_width, original_height = image.size
    longest = max(original_width, original_height)
    sample_max_side = max(128, min(int(max_points * 24), DEFAULT_CONTOUR_SAMPLE_MAX_SIDE))
    scale = 1.0
    if longest > sample_max_side:
        scale = sample_max_side / float(longest)
        image = image.resize((max(1, round(original_width * scale)), max(1, round(original_height * scale))), Image.Resampling.NEAREST)
    binary = image.point(lambda value: 255 if value >= threshold else 0, "L")
    bbox = binary.getbbox()
    if not bbox:
        return [], None, {"sample_scale": scale, "selected_pixels_sampled": 0}
    left, top, right, bottom = bbox
    pixels = binary.load()
    width, height = binary.size
    boundary: list[tuple[float, float]] = []
    selected_count = 0
    for y in range(top, bottom):
        for x in range(left, right):
            if pixels[x, y] < 255:
                continue
            selected_count += 1
            if x == 0 or y == 0 or x >= width - 1 or y >= height - 1:
                boundary.append((x / scale, y / scale))
                continue
            if pixels[x - 1, y] < 255 or pixels[x + 1, y] < 255 or pixels[x, y - 1] < 255 or pixels[x, y + 1] < 255:
                boundary.append((x / scale, y / scale))
    if not boundary:
        doc_bbox = {
            "x": round(left / scale, 3),
            "y": round(top / scale, 3),
            "width": round((right - left) / scale, 3),
            "height": round((bottom - top) / scale, 3),
        }
        return _bbox_polygon(doc_bbox), doc_bbox, {"sample_scale": scale, "selected_pixels_sampled": selected_count, "approximation": "bbox"}

    cx = sum(point[0] for point in boundary) / len(boundary)
    cy = sum(point[1] for point in boundary) / len(boundary)
    bins: dict[int, tuple[float, float, float]] = {}
    for x, y in boundary:
        angle = math.atan2(y - cy, x - cx)
        bin_id = min(max_points - 1, int(((angle + math.pi) / (math.tau)) * max_points))
        distance = (x - cx) ** 2 + (y - cy) ** 2
        current = bins.get(bin_id)
        if current is None or distance > current[0]:
            bins[bin_id] = (distance, x, y)
    points = [[round(item[1], 3), round(item[2], 3)] for _, item in sorted(bins.items())]
    if len(points) < 3:
        doc_bbox = {
            "x": round(left / scale, 3),
            "y": round(top / scale, 3),
            "width": round((right - left) / scale, 3),
            "height": round((bottom - top) / scale, 3),
        }
        points = _bbox_polygon(doc_bbox)
    bbox_result = _polygon_bbox(points)
    return points, bbox_result, {
        "sample_scale": scale,
        "selected_pixels_sampled": selected_count,
        "boundary_points_sampled": len(boundary),
        "approximation": "radial_boundary",
    }


def extract_region_contour(body: dict[str, Any], asset_url_builder: AssetUrlBuilder | None = None) -> dict[str, Any]:
    try:
        alpha_value = body.get("alpha_mask") or body.get("selection_mask") or body.get("asset_path")
        alpha_path = _alpha_path_from_value(alpha_value)
        threshold_float = float(body.get("threshold", body.get("alpha_threshold", 0.5)) or 0.5)
        threshold = int(round(_clamp(threshold_float, 0.0, 1.0) * 255))
        max_points = int(body.get("max_points", DEFAULT_CONTOUR_MAX_POINTS) or DEFAULT_CONTOUR_MAX_POINTS)
        max_points = max(8, min(max_points, MAX_POLYGON_POINTS))
        from PIL import Image

        with Image.open(alpha_path) as image:
            if image.mode == "RGBA":
                alpha = image.getchannel("A")
            else:
                alpha = image.convert("L")
            document_size = {"width": alpha.size[0], "height": alpha.size[1]}
            points, bbox, metrics = _selected_boundary_points(alpha, threshold, max_points)
        if len(points) < 3:
            return api_error("alpha_mask_empty", "Alpha mask did not contain a usable contour.")
        polygon = {
            "type": "polygon_contour",
            "points": points,
            "closed": True,
            "source": "alpha_mask",
            "point_count": len(points),
            "bbox": bbox or _polygon_bbox(points),
            "threshold": round(threshold / 255, 4),
        }
        bezier_path = _points_to_bezier_path(points, closed=True, smooth=float(body.get("path_smooth", 0) or 0))
        return {
            "status": "ok",
            "schema_version": SCHEMA_VERSION,
            "alpha_mask": asset_payload(
                str(alpha_path.relative_to(ASSET_ROOT)) if ASSET_ROOT in alpha_path.parents else alpha_path.name,
                alpha_path,
                "image/png",
                asset_url_builder if ASSET_ROOT in alpha_path.parents else None,
            ),
            "document_size": document_size,
            "polygon": polygon,
            "bezier_path": bezier_path,
            "metrics": metrics,
        }
    except (FileNotFoundError, ValueError, TypeError, OSError) as exc:
        return api_error("region_contour_failed", str(exc))


def _artifact_reps(body: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    artifact = body.get("region_artifact") or body.get("artifact")
    if not isinstance(artifact, dict):
        create_result = create_region_artifact(body)
        if create_result.get("status") != "ok":
            raise ValueError(create_result.get("error", {}).get("message", "Could not create region artifact"))
        artifact = create_result["artifact"]
    artifact = _normalize_region_artifact(artifact)
    return artifact, artifact.get("representations", {})


def _selection_mask_from_reps(artifact: dict[str, Any], reps: dict[str, Any], prefer: str | None = None) -> tuple[dict[str, Any], str]:
    label = str(artifact.get("label") or artifact.get("region_id") or "region")
    if prefer == "alpha_mask" or (prefer is None and "alpha_mask" in reps):
        alpha = reps.get("alpha_mask") or {}
        selection_mask = reps.get("selection_mask")
        if isinstance(selection_mask, dict) and selection_mask.get("source") == "alpha_mask":
            return deepcopy(selection_mask), "alpha_mask"
        return _alpha_selection_mask(alpha, label, float(alpha.get("feather", 0) or 0), float(alpha.get("threshold", 0.5) or 0.5)), "alpha_mask"
    if prefer == "polygon" or (prefer is None and "polygon" in reps):
        polygon = reps.get("polygon") or {}
        points = _normalize_points(polygon.get("points"), "region_artifact.representations.polygon.points")
        return {
            "source": "polygon",
            "label": label,
            "points": points,
            "feather": float(artifact.get("feather", 0) or 0),
            "invert": False,
        }, "polygon"
    if prefer == "bbox" or (prefer is None and "bbox" in reps):
        bbox_rep = reps.get("bbox") or {}
        bbox = bbox_rep.get("bbox") if isinstance(bbox_rep, dict) else None
        if not bbox and "polygon" in reps:
            bbox = reps["polygon"].get("bbox")
        bbox = normalize_bbox(bbox, "bbox")
        return {"source": "bbox", "label": label, "bbox": bbox, "feather": float(artifact.get("feather", 0) or 0), "invert": False}, "bbox"
    raise ValueError("Region artifact does not contain alpha_mask, polygon, or bbox representation")


def lower_region_to_selection_recipe(body: dict[str, Any]) -> dict[str, Any]:
    try:
        artifact, reps = _artifact_reps(body)
        prefer = body.get("prefer")
        selection_mask, representation = _selection_mask_from_reps(artifact, reps, str(prefer) if prefer else None)
        region_id = safe_path_part(str(artifact.get("region_id") or "region"), "region")
        candidate_id = safe_path_part(str(body.get("candidate_id") or region_id), "region")
        atom_id = "selection.alpha_mask" if selection_mask.get("source") == "alpha_mask" else f"selection.{selection_mask.get('source')}"
        params = {"selection_mask": selection_mask} if atom_id == "selection.alpha_mask" else dict(selection_mask)
        recipe = {
            "schema_version": SCHEMA_VERSION,
            "recipe_id": str(body.get("recipe_id") or f"sel-{region_id}"),
            "goal": str(body.get("goal") or f"Create a Photoshop selection from region artifact {region_id}."),
            "stage_id": body.get("stage_id") or "mask_preparation",
            "workflow_id": body.get("workflow_id"),
            "candidates": [
                {
                    "candidate_id": candidate_id,
                    "atom_id": atom_id,
                    "role": "base",
                    "reason": f"Lowered from region_artifact representation={representation}.",
                    "params": params,
                }
            ],
            "merge_plan": {
                "mode": "soft_alpha" if atom_id == "selection.alpha_mask" else "hard_selection",
                "items": [{"candidate_id": candidate_id, "operation": "replace"}],
            },
            "review": {
                "overlay": True,
                "regions": body.get("review_regions") or ["region_bounds"],
            },
        }
        return {
            "status": "ok",
            "schema_version": SCHEMA_VERSION,
            "region_id": region_id,
            "representation": representation,
            "selection_mask": selection_mask,
            "selection_recipe": recipe,
        }
    except (ValueError, TypeError) as exc:
        return api_error("region_lower_selection_failed", str(exc))


def lower_region_to_path(body: dict[str, Any]) -> dict[str, Any]:
    try:
        artifact, reps = _artifact_reps(body)
        representation = "bezier_path"
        if "bezier_path" in reps:
            path = deepcopy(reps["bezier_path"])
        elif "polygon" in reps:
            representation = "polygon"
            points = _normalize_points(reps["polygon"].get("points"), "region_artifact.representations.polygon.points")
            path = _points_to_bezier_path(points, closed=bool(body.get("closed", True)), smooth=float(body.get("smooth", 0) or 0))
        elif "landmarks" in reps:
            representation = "landmarks"
            landmarks = reps["landmarks"].get("points", [])
            points = [[float(item["x"]), float(item["y"])] for item in landmarks if _is_number(item.get("x")) and _is_number(item.get("y"))]
            path = _points_to_bezier_path(points, closed=bool(body.get("closed", False)), smooth=float(body.get("smooth", 0) or 0))
        else:
            raise ValueError("Region artifact does not contain bezier_path, polygon, or landmarks")
        path["path_id"] = str(body.get("path_id") or f"path-{artifact.get('region_id', 'region')}")
        path["label"] = str(body.get("label") or artifact.get("label") or path["path_id"])
        return {
            "status": "ok",
            "schema_version": SCHEMA_VERSION,
            "region_id": artifact.get("region_id"),
            "representation": representation,
            "bezier_path": path,
            "path_recipe": {
                "schema_version": SCHEMA_VERSION,
                "path_id": path["path_id"],
                "path_kind": str(body.get("path_kind") or "work_path"),
                "source_region_id": artifact.get("region_id"),
                "subpaths": path.get("subpaths", []),
                "photoshop_execution": {
                    "status": "not_yet_bound_to_uxp_atom",
                    "next_atom": "path.create_work_path",
                },
            },
        }
    except (ValueError, TypeError, KeyError) as exc:
        return api_error("region_lower_path_failed", str(exc))
