from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageStat


SUPPORTED_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff", ".bmp"}


def _error(code: str, message: str, details: Any | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "status": "error",
        "schema_version": "ps-agent/v1",
        "error": {"code": code, "message": message},
    }
    if details is not None:
        payload["error"]["details"] = details
    return payload


def _round(value: float, digits: int = 4) -> float:
    return round(float(value), digits)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _image_path(payload: dict[str, Any], *keys: str) -> Path | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return Path(value).expanduser()
    return None


def _load_image(path: Path) -> Image.Image:
    if not path.exists():
        raise FileNotFoundError(str(path))
    if path.suffix.lower() not in SUPPORTED_SUFFIXES:
        raise ValueError(f"Unsupported image suffix: {path.suffix}")
    return Image.open(path).convert("RGBA")


def _sample_image(image: Image.Image, max_side: int = 900) -> tuple[Image.Image, float]:
    width, height = image.size
    longest = max(width, height)
    if longest <= max_side:
        return image.copy(), 1.0
    scale = max_side / float(longest)
    return image.resize((max(1, round(width * scale)), max(1, round(height * scale))), Image.Resampling.LANCZOS), scale


def _corner_background_rgb(image: Image.Image) -> tuple[float, float, float]:
    width, height = image.size
    size = max(1, min(width, height, 30))
    boxes = [
        (0, 0, size, size),
        (width - size, 0, width, size),
        (0, height - size, size, height),
        (width - size, height - size, width, height),
    ]
    values: list[tuple[int, int, int]] = []
    for box in boxes:
        crop = image.crop(box).convert("RGB")
        values.extend(list(crop.getdata())[:: max(1, len(list(crop.getdata())) // 80)])
    if not values:
        return (255.0, 255.0, 255.0)
    return tuple(sum(pixel[index] for pixel in values) / len(values) for index in range(3))  # type: ignore[return-value]


def _subject_bbox(image: Image.Image) -> dict[str, Any]:
    sample, scale = _sample_image(image, 700)
    width, height = sample.size
    bg = _corner_background_rgb(sample)
    pixels = sample.load()
    xs: list[int] = []
    ys: list[int] = []
    for yy in range(height):
        for xx in range(width):
            r, g, b, a = pixels[xx, yy]
            color_delta = math.sqrt((r - bg[0]) ** 2 + (g - bg[1]) ** 2 + (b - bg[2]) ** 2)
            if a > 16 and (a < 245 or color_delta > 28):
                xs.append(xx)
                ys.append(yy)
    if not xs:
        return {
            "bbox": None,
            "occupancy_ratio": 0.0,
            "touches_edge": False,
            "edge_margins": None,
        }
    left, right = min(xs), max(xs)
    top, bottom = min(ys), max(ys)
    inv_scale = 1.0 / scale
    bbox = {
        "x": round(left * inv_scale),
        "y": round(top * inv_scale),
        "width": round((right - left + 1) * inv_scale),
        "height": round((bottom - top + 1) * inv_scale),
    }
    margin = max(4, round(min(width, height) * 0.025))
    edge_margins = {
        "left": round(left * inv_scale),
        "top": round(top * inv_scale),
        "right": round((width - 1 - right) * inv_scale),
        "bottom": round((height - 1 - bottom) * inv_scale),
    }
    return {
        "bbox": bbox,
        "occupancy_ratio": _round((right - left + 1) * (bottom - top + 1) / max(1, width * height), 4),
        "touches_edge": left <= margin or top <= margin or (width - 1 - right) <= margin or (height - 1 - bottom) <= margin,
        "edge_margins": edge_margins,
    }


def _edge_density(image: Image.Image) -> float:
    sample, _ = _sample_image(image, 500)
    gray = sample.convert("L")
    width, height = gray.size
    if width < 2 or height < 2:
        return 0.0
    pix = gray.load()
    edges = 0
    total = 0
    for yy in range(height - 1):
        for xx in range(width - 1):
            gx = abs(int(pix[xx + 1, yy]) - int(pix[xx, yy]))
            gy = abs(int(pix[xx, yy + 1]) - int(pix[xx, yy]))
            if gx + gy > 42:
                edges += 1
            total += 1
    return _round(edges / max(1, total), 5)


def _mean_rgb(image: Image.Image) -> list[float]:
    stat = ImageStat.Stat(image.convert("RGB"))
    return [_round(value, 3) for value in stat.mean[:3]]


def _image_summary(path: Path, sample_max_side: int = 900) -> dict[str, Any]:
    image = _load_image(path)
    subject = _subject_bbox(image)
    return {
        "path": str(path),
        "width": image.width,
        "height": image.height,
        "aspect_ratio": _round(image.width / max(1, image.height), 5),
        "mean_rgb": _mean_rgb(image),
        "edge_density": _edge_density(image),
        "subject": subject,
    }


def _pixel_similarity(a: Image.Image, b: Image.Image) -> dict[str, float]:
    size = (256, 256)
    aa = a.convert("RGB").resize(size, Image.Resampling.LANCZOS)
    bb = b.convert("RGB").resize(size, Image.Resampling.LANCZOS)
    diff = ImageChops.difference(aa, bb)
    stat = ImageStat.Stat(diff)
    mad = sum(stat.mean[:3]) / 3.0
    rms = math.sqrt(sum(value * value for value in stat.rms[:3]) / 3.0)
    return {
        "mean_abs_delta": _round(mad, 3),
        "rms_delta": _round(rms, 3),
        "similarity": _round(_clamp(100.0 - mad / 2.55, 0.0, 100.0), 2),
    }


def compare_reference(payload: dict[str, Any]) -> dict[str, Any]:
    preview_path = _image_path(payload, "preview_path", "after_path", "asset_path")
    reference_path = _image_path(payload, "reference_path", "ref_path")
    if preview_path is None or reference_path is None:
        return _error("missing_image_path", "preview_path and reference_path are required.")
    try:
        preview = _load_image(preview_path)
        reference = _load_image(reference_path)
        preview_summary = _image_summary(preview_path)
        reference_summary = _image_summary(reference_path)
        pixel = _pixel_similarity(preview, reference)
        p_subject = preview_summary["subject"]
        r_subject = reference_summary["subject"]
        occupancy_delta = abs(float(p_subject.get("occupancy_ratio") or 0) - float(r_subject.get("occupancy_ratio") or 0))
        edge_delta = abs(float(preview_summary["edge_density"]) - float(reference_summary["edge_density"]))
        aspect_delta = abs(float(preview_summary["aspect_ratio"]) - float(reference_summary["aspect_ratio"]))
        score = _clamp(pixel["similarity"] * 0.58 + max(0.0, 100 - occupancy_delta * 180) * 0.18 + max(0.0, 100 - edge_delta * 360) * 0.14 + max(0.0, 100 - aspect_delta * 130) * 0.10, 0.0, 100.0)
        suggestions: list[str] = []
        if occupancy_delta > 0.12:
            suggestions.append("Subject occupancy differs from reference; adjust scale or layer spread before style tweaks.")
        if edge_delta > 0.08:
            suggestions.append("Edge density differs from reference; revise path detail, bead count, or shape complexity.")
        if p_subject.get("touches_edge"):
            suggestions.append("Preview subject touches document edge; add margin or reduce outer shapes.")
        return {
            "status": "ok",
            "schema_version": "ps-agent/v1",
            "comparison_mode": payload.get("comparison_mode", "deterministic_shape_color"),
            "score": _round(score, 2),
            "metrics": {
                "pixel": pixel,
                "occupancy_delta": _round(occupancy_delta, 4),
                "edge_density_delta": _round(edge_delta, 5),
                "aspect_ratio_delta": _round(aspect_delta, 5),
            },
            "preview": preview_summary,
            "reference": reference_summary,
            "pass": score >= float(payload.get("pass_threshold", 62)),
            "suggestions": suggestions,
        }
    except Exception as exc:
        return _error("compare_reference_failed", str(exc))


def detect_overflow(payload: dict[str, Any]) -> dict[str, Any]:
    image_path = _image_path(payload, "preview_path", "image_path", "asset_path")
    if image_path is None:
        return _error("missing_image_path", "preview_path, image_path, or asset_path is required.")
    try:
        summary = _image_summary(image_path)
        margin_ratio = float(payload.get("margin_ratio", 0.035))
        min_margin = round(min(summary["width"], summary["height"]) * margin_ratio)
        margins = summary["subject"].get("edge_margins") or {}
        contacts = [side for side, value in margins.items() if isinstance(value, (int, float)) and value < min_margin]
        risks = []
        if contacts:
            risks.append("content_near_document_edge")
        if float(summary["subject"].get("occupancy_ratio") or 0) > 0.92:
            risks.append("content_bbox_overfills_canvas")
        return {
            "status": "ok",
            "schema_version": "ps-agent/v1",
            "summary": summary,
            "margin_px": min_margin,
            "edge_contacts": contacts,
            "risks": risks,
            "pass": not risks,
        }
    except Exception as exc:
        return _error("detect_overflow_failed", str(exc))


def visual_score(payload: dict[str, Any]) -> dict[str, Any]:
    image_path = _image_path(payload, "preview_path", "image_path", "asset_path")
    if image_path is None:
        return _error("missing_image_path", "preview_path, image_path, or asset_path is required.")
    try:
        summary = _image_summary(image_path)
        recipe = payload.get("recipe") if isinstance(payload.get("recipe"), dict) else {}
        if not recipe and isinstance(payload.get("operation_recipe"), dict):
            recipe = payload["operation_recipe"]
        steps = recipe.get("steps", []) if isinstance(recipe.get("steps"), list) else []
        atom_ids = [str(step["atom_id"]) for step in steps if isinstance(step, dict) and step.get("atom_id")]
        non_rect_atoms = [atom for atom in atom_ids if atom.startswith("shape.") and atom not in {"shape.rectangle", "shape.rounded_rectangle"}]

        svg_steps = [step for step in steps if isinstance(step, dict) and step.get("atom_id") == "shape.svg_asset_place"]
        svg_objects: set[str] = set()
        svg_roles: set[str] = set()
        svg_bytes: list[int] = []
        sharded_parts = 0
        for step in svg_steps:
            params = step.get("params") if isinstance(step.get("params"), dict) else {}
            if params.get("object_id"):
                svg_objects.add(str(params["object_id"]))
            if params.get("style_role"):
                svg_roles.add(str(params["style_role"]))
            if isinstance(params.get("svg"), str):
                svg_bytes.append(len(params["svg"].encode("utf-8")))
            if "__" in str(params.get("part_id") or ""):
                sharded_parts += 1

        result_payload = payload.get("operation_result") if isinstance(payload.get("operation_result"), dict) else {}
        result_recipe = result_payload.get("operation_recipe") if isinstance(result_payload.get("operation_recipe"), dict) else {}
        result_steps = result_recipe.get("steps") if isinstance(result_recipe.get("steps"), list) else []
        bounds_by_object: dict[str, list[dict[str, float]]] = {}
        for step in result_steps:
            if not isinstance(step, dict) or not step.get("object_id") or not isinstance(step.get("bounds"), dict):
                continue
            bounds = step["bounds"]
            if all(isinstance(bounds.get(key), (int, float)) for key in ("left", "top", "right", "bottom")):
                bounds_by_object.setdefault(str(step["object_id"]), []).append({key: float(bounds[key]) for key in ("left", "top", "right", "bottom")})
        alignment_deviation = 0.0
        for bounds_list in bounds_by_object.values():
            if len(bounds_list) < 2:
                continue
            for key in ("left", "top", "right", "bottom"):
                values = [bounds[key] for bounds in bounds_list]
                alignment_deviation = max(alignment_deviation, max(values) - min(values))

        edge_density = float(summary["edge_density"])
        occupancy = float(summary["subject"].get("occupancy_ratio") or 0)
        svg_role_bonus = min(24.0, len(svg_roles) * 4.0)
        shape_richness = _clamp(len(set(non_rect_atoms)) * 12 + edge_density * 180 + svg_role_bonus, 0.0, 100.0)
        composition = _clamp(100 - abs(occupancy - 0.58) * 120, 0.0, 100.0)
        edge_safety = 45.0 if summary["subject"].get("touches_edge") else 100.0
        total = shape_richness * 0.38 + composition * 0.34 + edge_safety * 0.28
        monolith_risk = max(svg_bytes, default=0) > 96 * 1024
        alignment_risk = alignment_deviation > 1.0
        return {
            "status": "ok",
            "schema_version": "ps-agent/v1",
            "scores": {
                "total": _round(total, 2),
                "shape_richness": _round(shape_richness, 2),
                "composition": _round(composition, 2),
                "edge_safety": _round(edge_safety, 2),
            },
            "summary": summary,
            "atom_usage": {
                "atom_count": len(atom_ids),
                "non_rect_shape_atoms": sorted(set(non_rect_atoms)),
            },
            "svg_metrics": {
                "object_count": len(svg_objects),
                "asset_count": len(svg_steps),
                "style_roles": sorted(svg_roles),
                "max_asset_bytes": max(svg_bytes, default=0),
                "sharded_asset_count": sharded_parts,
                "bounds_alignment_deviation_px": _round(alignment_deviation, 3),
                "monolith_risk": monolith_risk,
                "alignment_risk": alignment_risk,
            },
            "pass": total >= float(payload.get("pass_threshold", 65)) and not monolith_risk and not alignment_risk,
        }
    except Exception as exc:
        return _error("visual_score_failed", str(exc))