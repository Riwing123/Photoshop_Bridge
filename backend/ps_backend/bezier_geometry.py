from __future__ import annotations

import math
from typing import Any

SCHEMA_VERSION = "ps-agent/v1"


def _error(code: str, message: str, details: Any | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"status": "error", "schema_version": SCHEMA_VERSION, "error": {"code": code, "message": message}}
    if details is not None:
        payload["error"]["details"] = details
    return payload


def _num(value: Any, fallback: float | None = None) -> float | None:
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return float(value)
    return fallback


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _point(value: Any) -> dict[str, float] | None:
    if isinstance(value, dict):
        x = _num(value.get("x"))
        y = _num(value.get("y"))
        if x is not None and y is not None:
            return {"x": x, "y": y}
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        x = _num(value[0])
        y = _num(value[1])
        if x is not None and y is not None:
            return {"x": x, "y": y}
    return None


def _handle(input_point: dict[str, Any], keys: tuple[str, ...]) -> dict[str, float] | None:
    for key in keys:
        if key in input_point:
            parsed = _point(input_point.get(key))
            if parsed is not None:
                return parsed
    return None


def _normalize_point(value: Any, index: int) -> dict[str, Any]:
    anchor = _point(value)
    if anchor is None:
        raise ValueError(f"point {index} must contain numeric x/y coordinates")
    input_point = value if isinstance(value, dict) else {}
    backward = _handle(input_point, ("backward", "back", "in", "in_handle"))
    forward = _handle(input_point, ("forward", "out", "out_handle"))
    raw_kind = str(input_point.get("kind")).lower() if input_point.get("kind") is not None else None
    if raw_kind is not None and raw_kind not in {"smooth", "corner"}:
        raise ValueError(f"point {index}.kind must be smooth or corner")
    if raw_kind == "corner" and input_point.get("smooth") is True:
        raise ValueError(f"point {index} cannot set kind=corner and smooth=true at the same time")
    kind = raw_kind or ("smooth" if input_point.get("smooth") is True else "corner")
    return {
        "x": anchor["x"],
        "y": anchor["y"],
        "backward": backward or dict(anchor),
        "forward": forward or dict(anchor),
        "has_backward": backward is not None,
        "has_forward": forward is not None,
        "smooth": kind == "smooth",
        "kind": kind,
    }


def _distance(a: dict[str, float], b: dict[str, float]) -> float:
    return math.hypot(float(a["x"]) - float(b["x"]), float(a["y"]) - float(b["y"]))


def _vector(a: dict[str, float], b: dict[str, float]) -> dict[str, float]:
    return {"x": float(b["x"]) - float(a["x"]), "y": float(b["y"]) - float(a["y"])}


def _length(v: dict[str, float]) -> float:
    return math.hypot(float(v["x"]), float(v["y"]))


def _dot(a: dict[str, float], b: dict[str, float]) -> float:
    return float(a["x"]) * float(b["x"]) + float(a["y"]) * float(b["y"])


def _cross(a: dict[str, float], b: dict[str, float]) -> float:
    return float(a["x"]) * float(b["y"]) - float(a["y"]) * float(b["x"])


def _handle_mode(params: dict[str, Any], subpath: dict[str, Any] | None = None) -> str:
    raw = str((subpath or {}).get("handle_mode") or (subpath or {}).get("handles") or params.get("handle_mode") or params.get("handles") or "manual").lower()
    normalized = raw.replace("-", "_").replace(" ", "_")
    if normalized in {"auto", "auto_smooth", "catmullrom", "catmull_rom"}:
        return "catmull_rom"
    if normalized in {"geometric", "geometry"}:
        return "geometric"
    return "manual"


def _handle_scale(params: dict[str, Any], subpath: dict[str, Any] | None = None) -> float:
    raw = (subpath or {}).get("handle_scale", params.get("handle_scale", 1))
    return _clamp(float(raw) if isinstance(raw, (int, float)) else 1.0, 0.05, 2.0)


def _turn_angle(previous: dict[str, float], point: dict[str, float], nxt: dict[str, float]) -> float:
    incoming = _vector(previous, point)
    outgoing = _vector(point, nxt)
    denom = max(_length(incoming) * _length(outgoing), 0.001)
    cosine = max(-1.0, min(1.0, _dot(incoming, outgoing) / denom))
    return math.degrees(math.acos(cosine))


def _auto_handles(points: list[dict[str, Any]], closed: bool, mode: str, scale: float, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    params = params or {}
    if mode == "manual":
        return [dict(point) for point in points]
    count = len(points)
    output: list[dict[str, Any]] = []
    corner_threshold = _clamp(float(params.get("corner_angle_threshold", 118)) if isinstance(params.get("corner_angle_threshold"), (int, float)) else 118.0, 45.0, 175.0)
    min_smooth_segment = _clamp(float(params.get("min_smooth_segment_length", 12)) if isinstance(params.get("min_smooth_segment_length"), (int, float)) else 12.0, 0.0, 1000.0)
    for index, point in enumerate(points):
        previous = points[index - 1] if index > 0 else (points[-1] if closed else None)
        nxt = points[index + 1] if index + 1 < count else (points[0] if closed else None)
        updated = dict(point)
        if previous is None or nxt is None:
            updated.update({"backward": {"x": point["x"], "y": point["y"]}, "forward": {"x": point["x"], "y": point["y"]}, "has_backward": False, "has_forward": False, "smooth": False, "kind": "corner"})
            output.append(updated)
            continue
        prev_distance = max(_distance(previous, point), 0.001)
        next_distance = max(_distance(point, nxt), 0.001)
        min_distance = min(prev_distance, next_distance)
        turn_angle = _turn_angle(previous, point, nxt)
        if point.get("kind") == "corner" and not point.get("has_backward") and not point.get("has_forward"):
            keep_corner = True
        else:
            keep_corner = turn_angle >= corner_threshold or min_distance <= min_smooth_segment
        if keep_corner:
            updated.update({
                "backward": {"x": point["x"], "y": point["y"]},
                "forward": {"x": point["x"], "y": point["y"]},
                "has_backward": False,
                "has_forward": False,
                "smooth": False,
                "kind": "corner",
                "corner_reason": "sharp_turn" if turn_angle >= corner_threshold else "short_segment",
            })
            output.append(updated)
            continue
        tangent = _vector(previous, nxt)
        tangent_length = max(_length(tangent), 0.001)
        ux = tangent["x"] / tangent_length
        uy = tangent["y"] / tangent_length
        base_factor = 0.28 if mode == "geometric" else 0.30
        angle_damping = max(0.08, math.cos(math.radians(turn_angle) / 2.0))
        loop_safe_cap = max(4.0, min_distance * 0.36 * angle_damping)
        out_length = min(next_distance * base_factor * angle_damping, loop_safe_cap) * scale
        in_length = min(prev_distance * base_factor * angle_damping, loop_safe_cap) * scale
        updated.update({
            "backward": {"x": point["x"] - ux * in_length, "y": point["y"] - uy * in_length},
            "forward": {"x": point["x"] + ux * out_length, "y": point["y"] + uy * out_length},
            "has_backward": True,
            "has_forward": True,
            "smooth": True,
            "kind": "smooth",
            "turn_angle": round(turn_angle, 2),
        })
        output.append(updated)
    return output


def _repair_manual(points: list[dict[str, Any]], closed: bool, scale: float, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    generated = _auto_handles(points, closed, "catmull_rom", scale, params)
    output: list[dict[str, Any]] = []
    for point, fallback in zip(points, generated):
        updated = dict(point)
        if not point.get("has_backward"):
            updated["backward"] = fallback["backward"]
        if not point.get("has_forward"):
            updated["forward"] = fallback["forward"]
        updated["has_backward"] = True
        updated["has_forward"] = True
        output.append(updated)
    return output


def normalize_bezier_subpaths(params: dict[str, Any]) -> list[dict[str, Any]]:
    raw_subpaths = params.get("subpaths") if isinstance(params.get("subpaths"), list) else None
    subpaths = raw_subpaths or [{"points": params.get("points") or [], "closed": params.get("closed") is not False, "operation": params.get("operation") or "add"}]
    normalized: list[dict[str, Any]] = []
    for subpath_index, subpath in enumerate(subpaths):
        if not isinstance(subpath, dict):
            raise ValueError(f"subpath {subpath_index} must be an object")
        closed = subpath.get("closed") is not False
        raw_points = subpath.get("points")
        min_points = 3 if closed else 2
        if not isinstance(raw_points, list) or not min_points <= len(raw_points) <= 512:
            raise ValueError(f"subpath {subpath_index} requires {min_points}..512 points")
        points = [_normalize_point(point, point_index) for point_index, point in enumerate(raw_points)]
        mode = _handle_mode(params, subpath)
        scale = _handle_scale(params, subpath)
        if mode != "manual":
            points = _auto_handles(points, closed, mode, scale, params)
        elif params.get("auto_repair_handles") is True or subpath.get("auto_repair_handles") is True:
            points = _repair_manual(points, closed, scale, params)
        normalized.append({"closed": closed, "operation": subpath.get("operation") or params.get("operation") or "add", "points": points, "handle_mode": mode})
    return normalized


def _cubic(p0: dict[str, float], p1: dict[str, float], p2: dict[str, float], p3: dict[str, float], t: float) -> dict[str, float]:
    mt = 1.0 - t
    return {
        "x": mt * mt * mt * p0["x"] + 3 * mt * mt * t * p1["x"] + 3 * mt * t * t * p2["x"] + t * t * t * p3["x"],
        "y": mt * mt * mt * p0["y"] + 3 * mt * mt * t * p1["y"] + 3 * mt * t * t * p2["y"] + t * t * t * p3["y"],
    }


def _orientation(a: dict[str, float], b: dict[str, float], c: dict[str, float]) -> int:
    value = (b["y"] - a["y"]) * (c["x"] - b["x"]) - (b["x"] - a["x"]) * (c["y"] - b["y"])
    if abs(value) < 0.0001:
        return 0
    return 1 if value > 0 else 2


def _on_segment(a: dict[str, float], b: dict[str, float], c: dict[str, float]) -> bool:
    return min(a["x"], c["x"]) - 0.0001 <= b["x"] <= max(a["x"], c["x"]) + 0.0001 and min(a["y"], c["y"]) - 0.0001 <= b["y"] <= max(a["y"], c["y"]) + 0.0001


def _segments_intersect(a1: dict[str, float], a2: dict[str, float], b1: dict[str, float], b2: dict[str, float]) -> bool:
    o1 = _orientation(a1, a2, b1)
    o2 = _orientation(a1, a2, b2)
    o3 = _orientation(b1, b2, a1)
    o4 = _orientation(b1, b2, a2)
    if o1 != o2 and o3 != o4:
        return True
    return (o1 == 0 and _on_segment(a1, b1, a2)) or (o2 == 0 and _on_segment(a1, b2, a2)) or (o3 == 0 and _on_segment(b1, a1, b2)) or (o4 == 0 and _on_segment(b1, a2, b2))


def _loop_warnings(subpath: dict[str, Any], subpath_index: int, params: dict[str, Any]) -> list[dict[str, Any]]:
    samples = int(_clamp(float(params.get("samples_per_curve", 16)) if isinstance(params.get("samples_per_curve"), (int, float)) else 16.0, 6, 48))
    max_warnings = int(_clamp(float(params.get("max_loop_warnings", 6)) if isinstance(params.get("max_loop_warnings"), (int, float)) else 6.0, 1, 64))
    points = subpath.get("points") or []
    curve_count = len(points) if subpath.get("closed") else max(0, len(points) - 1)
    segments: list[dict[str, Any]] = []
    for curve_index in range(curve_count):
        current = points[curve_index]
        nxt = points[(curve_index + 1) % len(points)]
        previous = _cubic(current, current.get("forward") or current, nxt.get("backward") or nxt, nxt, 0)
        for sample_index in range(1, samples + 1):
            current_sample = _cubic(current, current.get("forward") or current, nxt.get("backward") or nxt, nxt, sample_index / samples)
            segments.append({"curve_index": curve_index, "sample_index": sample_index - 1, "a": previous, "b": current_sample})
            previous = current_sample
    warnings: list[dict[str, Any]] = []
    for i, first in enumerate(segments):
        for j in range(i + 1, len(segments)):
            second = segments[j]
            if j - i <= 1:
                continue
            if subpath.get("closed") and i == 0 and j == len(segments) - 1:
                continue
            if first["curve_index"] == second["curve_index"] and abs(first["sample_index"] - second["sample_index"]) <= 1:
                continue
            if _segments_intersect(first["a"], first["b"], second["a"], second["b"]):
                warnings.append({
                    "subpath_index": subpath_index,
                    "point_index": None,
                    "flags": ["self_intersection"],
                    "segment_a": {"curve_index": first["curve_index"], "sample_index": first["sample_index"]},
                    "segment_b": {"curve_index": second["curve_index"], "sample_index": second["sample_index"]},
                })
                if len(warnings) >= max_warnings:
                    return warnings
    return warnings


def audit_subpath(subpath: dict[str, Any], subpath_index: int, params: dict[str, Any]) -> dict[str, Any]:
    tolerance = _clamp(float(params.get("tolerance", 12)) if isinstance(params.get("tolerance"), (int, float)) else 12.0, 0.5, 180.0)
    min_ratio = _clamp(float(params.get("min_handle_ratio", 0.03)) if isinstance(params.get("min_handle_ratio"), (int, float)) else 0.03, 0.0, 1.0)
    max_ratio = _clamp(float(params.get("max_handle_ratio", 0.75)) if isinstance(params.get("max_handle_ratio"), (int, float)) else 0.75, 0.05, 3.0)
    points = subpath.get("points") or []
    warnings: list[dict[str, Any]] = []
    metrics: list[dict[str, Any]] = []
    count = len(points)
    for index, point in enumerate(points):
        previous = points[index - 1] if index > 0 else (points[-1] if subpath.get("closed") else None)
        nxt = points[index + 1] if index + 1 < count else (points[0] if subpath.get("closed") else None)
        in_vector = _vector(point.get("backward") or point, point)
        out_vector = _vector(point, point.get("forward") or point)
        in_length = _length(in_vector)
        out_length = _length(out_vector)
        item: dict[str, Any] = {
            "subpath_index": subpath_index,
            "point_index": index,
            "anchor": {"x": round(float(point["x"]), 3), "y": round(float(point["y"]), 3)},
            "in_length": round(in_length, 3),
            "out_length": round(out_length, 3),
            "flags": [],
        }
        if previous is not None:
            ratio = in_length / max(_distance(previous, point), 0.001)
            item["in_ratio"] = round(ratio, 3)
            if in_length > 0.001 and _dot(_vector(point, previous), _vector(point, point.get("backward") or point)) <= 0:
                item["flags"].append("in_wrong_direction")
            if in_length > 0.001 and (ratio < min_ratio or ratio > max_ratio):
                item["flags"].append("in_ratio_out_of_range")
        if nxt is not None:
            ratio = out_length / max(_distance(point, nxt), 0.001)
            item["out_ratio"] = round(ratio, 3)
            if out_length > 0.001 and _dot(_vector(point, nxt), _vector(point, point.get("forward") or point)) <= 0:
                item["flags"].append("out_wrong_direction")
            if out_length > 0.001 and (ratio < min_ratio or ratio > max_ratio):
                item["flags"].append("out_ratio_out_of_range")
        if point.get("kind") == "smooth" or point.get("smooth") is True:
            if not point.get("has_backward") or not point.get("has_forward") or in_length <= 0.001 or out_length <= 0.001:
                item["flags"].append("smooth_missing_handle")
            else:
                angle = math.degrees(math.asin(min(1.0, abs(_cross(in_vector, out_vector)) / max(in_length * out_length, 0.001))))
                item["smooth_collinear_error"] = round(angle, 2)
                if angle > tolerance:
                    item["flags"].append("not_smooth_collinear")
        elif point.get("kind") == "corner" and (in_length > 0.001 or out_length > 0.001):
            item["intentional_corner"] = True
        if item["flags"]:
            warnings.append({"subpath_index": subpath_index, "point_index": index, "flags": list(item["flags"])})
        metrics.append(item)
    loop_warnings = _loop_warnings(subpath, subpath_index, params)
    warnings.extend(loop_warnings)
    return {
        "subpath_index": subpath_index,
        "closed": bool(subpath.get("closed")),
        "point_count": count,
        "warning_count": len(warnings),
        "loop_warning_count": len(loop_warnings),
        "warnings": warnings,
        "metrics": metrics,
    }


def audit_bezier_handles(payload: dict[str, Any]) -> dict[str, Any]:
    params = payload.get("params") if isinstance(payload.get("params"), dict) else payload
    if not isinstance(params, dict):
        return _error("invalid_path_points", "payload must be an object")
    try:
        subpaths = normalize_bezier_subpaths(params)
    except ValueError as exc:
        return _error("invalid_path_points", str(exc))
    audits = [audit_subpath(subpath, index, params) for index, subpath in enumerate(subpaths)]
    warnings = [warning for audit in audits for warning in audit["warnings"]]
    loop_warning_count = sum(audit.get("loop_warning_count", 0) for audit in audits)
    return {
        "status": "ok",
        "schema_version": SCHEMA_VERSION,
        "handle_mode": _handle_mode(params),
        "subpath_count": len(subpaths),
        "point_count": sum(len(subpath["points"]) for subpath in subpaths),
        "path_audit": {
            "status": "warning" if warnings else "ok",
            "subpath_count": len(subpaths),
            "point_count": sum(len(subpath["points"]) for subpath in subpaths),
            "warning_count": len(warnings),
            "loop_warning_count": loop_warning_count,
            "warnings": warnings,
            "subpaths": audits,
        },
    }
