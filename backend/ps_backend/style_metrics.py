from __future__ import annotations

import colorsys
import math
from pathlib import Path
from typing import Any

from PIL import Image


SUPPORTED_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff", ".bmp"}
DEFAULT_SAMPLE_MAX_SIDE = 1200
MAX_ANALYSIS_PIXELS = 220_000


def _error(code: str, message: str, details: Any | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "status": "error",
        "schema_version": "ps-agent/v1",
        "error": {"code": code, "message": message},
    }
    if details is not None:
        payload["error"]["details"] = details
    return payload


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _round(value: float, digits: int = 3) -> float:
    return round(float(value), digits)


def _percentile(sorted_values: list[float], percentile: float) -> float:
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    position = (len(sorted_values) - 1) * (percentile / 100.0)
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return sorted_values[lower]
    weight = position - lower
    return sorted_values[lower] * (1.0 - weight) + sorted_values[upper] * weight


def _percentiles(values: list[float], points: tuple[int, ...] = (1, 5, 25, 50, 75, 95, 99)) -> dict[str, float]:
    sorted_values = sorted(values)
    return {f"p{point}": _round(_percentile(sorted_values, point)) for point in points}


def _histogram(values: list[float], low: float, high: float, bin_count: int) -> dict[str, Any]:
    bin_count = max(1, int(bin_count))
    width = (high - low) / bin_count
    counts = [0 for _ in range(bin_count)]
    for value in values:
        if width <= 0:
            index = 0
        else:
            index = int((float(value) - low) / width)
        index = max(0, min(bin_count - 1, index))
        counts[index] += 1

    total = max(1, len(values))
    bins = []
    for index, count in enumerate(counts):
        start = low + width * index
        end = high if index == bin_count - 1 else low + width * (index + 1)
        bins.append(
            {
                "index": index,
                "start": _round(start, 3),
                "end": _round(end, 3),
                "count": count,
                "ratio": _round(count / total, 5),
            }
        )
    return {
        "min": _round(low, 3),
        "max": _round(high, 3),
        "bin_count": bin_count,
        "sample_count": len(values),
        "bins": bins,
    }


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _std(values: list[float], mean: float) -> float:
    if not values:
        return 0.0
    variance = sum((value - mean) ** 2 for value in values) / len(values)
    return math.sqrt(variance)


def _resize_for_sample(image: Image.Image, sample_max_side: int) -> tuple[Image.Image, float]:
    width, height = image.size
    max_side = max(64, min(int(sample_max_side or DEFAULT_SAMPLE_MAX_SIDE), 4096))
    longest = max(width, height)
    if longest <= max_side:
        return image.copy(), 1.0
    scale = max_side / float(longest)
    resized = image.resize((max(1, round(width * scale)), max(1, round(height * scale))), Image.Resampling.LANCZOS)
    return resized, scale


def _sample_pixels(image: Image.Image, max_pixels: int = MAX_ANALYSIS_PIXELS) -> list[tuple[int, int, int]]:
    pixels = list(image.convert("RGB").getdata())
    if len(pixels) <= max_pixels:
        return pixels
    step = max(1, math.ceil(len(pixels) / max_pixels))
    return pixels[::step]


def _srgb_to_linear(value: float) -> float:
    value = value / 255.0
    if value <= 0.04045:
        return value / 12.92
    return ((value + 0.055) / 1.055) ** 2.4


def _rgb_to_lab(pixel: tuple[int, int, int]) -> tuple[float, float, float]:
    r, g, b = (_srgb_to_linear(channel) for channel in pixel)
    x = r * 0.4124564 + g * 0.3575761 + b * 0.1804375
    y = r * 0.2126729 + g * 0.7151522 + b * 0.0721750
    z = r * 0.0193339 + g * 0.1191920 + b * 0.9503041

    x /= 0.95047
    y /= 1.00000
    z /= 1.08883

    def f(value: float) -> float:
        if value > 0.008856:
            return value ** (1.0 / 3.0)
        return 7.787 * value + 16.0 / 116.0

    fx, fy, fz = f(x), f(y), f(z)
    return 116.0 * fy - 16.0, 500.0 * (fx - fy), 200.0 * (fy - fz)


def _hue_band(hue: float, saturation: float, value: float) -> str:
    if saturation < 8 or value < 5:
        return "neutral"
    if hue < 15 or hue >= 345:
        return "reds"
    if hue < 45:
        return "oranges"
    if hue < 75:
        return "yellows"
    if hue < 165:
        return "greens"
    if hue < 210:
        return "cyans"
    if hue < 260:
        return "blues"
    if hue < 315:
        return "purples"
    return "magentas"


def _dominant_colors(pixels: list[tuple[int, int, int]], limit: int = 8) -> list[dict[str, Any]]:
    if not pixels:
        return []
    buckets: dict[tuple[int, int, int], int] = {}
    for r, g, b in pixels:
        key = (round(r / 32) * 32, round(g / 32) * 32, round(b / 32) * 32)
        buckets[key] = buckets.get(key, 0) + 1
    total = len(pixels)
    colors = []
    for (r, g, b), count in sorted(buckets.items(), key=lambda item: item[1], reverse=True)[:limit]:
        rr, gg, bb = int(_clamp(r, 0, 255)), int(_clamp(g, 0, 255)), int(_clamp(b, 0, 255))
        h, s, v = colorsys.rgb_to_hsv(rr / 255.0, gg / 255.0, bb / 255.0)
        colors.append(
            {
                "rgb": [rr, gg, bb],
                "hex": f"#{rr:02x}{gg:02x}{bb:02x}",
                "ratio": _round(count / total, 4),
                "hue": _round(h * 360.0),
                "saturation": _round(s * 100.0),
                "value": _round(v * 100.0),
            }
        )
    return colors


def _risk_flags(luma: dict[str, float], saturation_mean: float, rgb_mean: list[float]) -> list[str]:
    flags: list[str] = []
    contrast = luma["p95"] - luma["p5"]
    if luma["p5"] < 4:
        flags.append("crushed_blacks")
    if luma["p99"] > 96:
        flags.append("clipped_highlights")
    if contrast < 28:
        flags.append("low_contrast")
    if contrast > 82:
        flags.append("high_contrast")
    if saturation_mean < 14:
        flags.append("low_saturation")
    if saturation_mean > 58:
        flags.append("high_saturation")
    r, g, b = rgb_mean
    if abs(r - b) > 18:
        flags.append("warm_cast" if r > b else "cool_cast")
    if abs(g - ((r + b) / 2.0)) > 16:
        flags.append("green_cast" if g > ((r + b) / 2.0) else "magenta_cast")
    return flags


def _analyze_image_object(image: Image.Image, sample_max_side: int) -> dict[str, Any]:
    original_width, original_height = image.size
    sampled_image, scale = _resize_for_sample(image.convert("RGB"), sample_max_side)
    pixels = _sample_pixels(sampled_image)

    lumas: list[float] = []
    lab_l: list[float] = []
    lab_a: list[float] = []
    lab_b: list[float] = []
    sats: list[float] = []
    values: list[float] = []
    chromatic_hues: list[float] = []
    reds: list[float] = []
    greens: list[float] = []
    blues: list[float] = []
    hue_bands: dict[str, dict[str, float]] = {}

    for pixel in pixels:
        r, g, b = pixel
        reds.append(r)
        greens.append(g)
        blues.append(b)
        luma = (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255.0 * 100.0
        lumas.append(luma)
        h, s, v = colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)
        hue = h * 360.0
        sat = s * 100.0
        val = v * 100.0
        sats.append(sat)
        values.append(val)
        band = _hue_band(hue, sat, val)
        if band != "neutral":
            chromatic_hues.append(hue)
        bucket = hue_bands.setdefault(band, {"count": 0.0, "hue_sum": 0.0, "saturation_sum": 0.0, "value_sum": 0.0})
        bucket["count"] += 1
        bucket["hue_sum"] += hue
        bucket["saturation_sum"] += sat
        bucket["value_sum"] += val
        l_value, a_value, b_value = _rgb_to_lab(pixel)
        lab_l.append(l_value)
        lab_a.append(a_value)
        lab_b.append(b_value)

    pixel_count = max(1, len(pixels))
    luma_percentiles = _percentiles(lumas)
    sat_mean = _mean(sats)
    rgb_mean = [_round(_mean(channel), 2) for channel in (reds, greens, blues)]
    band_summary: dict[str, dict[str, float]] = {}
    for band, values_for_band in hue_bands.items():
        count = values_for_band["count"]
        band_summary[band] = {
            "ratio": _round(count / pixel_count, 4),
            "hue_mean": _round(values_for_band["hue_sum"] / count if count else 0),
            "saturation_mean": _round(values_for_band["saturation_sum"] / count if count else 0),
            "value_mean": _round(values_for_band["value_sum"] / count if count else 0),
        }

    lab_mean = {
        "l": _round(_mean(lab_l)),
        "a": _round(_mean(lab_a)),
        "b": _round(_mean(lab_b)),
    }
    return {
        "image": {
            "width": original_width,
            "height": original_height,
            "sample_width": sampled_image.size[0],
            "sample_height": sampled_image.size[1],
            "sample_scale": _round(scale, 6),
            "sampled_pixels": len(pixels),
        },
        "tone_percentiles": {
            "luma": luma_percentiles,
            "lab_l": _percentiles(lab_l),
            "contrast_p95_p5": _round(luma_percentiles["p95"] - luma_percentiles["p5"]),
            "mean": _round(_mean(lumas)),
            "std": _round(_std(lumas, _mean(lumas))),
        },
        "rgb": {
            "mean": rgb_mean,
            "warm_cool_bias": _round((rgb_mean[0] - rgb_mean[2]) / 255.0 * 100.0),
            "green_magenta_bias": _round((rgb_mean[1] - ((rgb_mean[0] + rgb_mean[2]) / 2.0)) / 255.0 * 100.0),
        },
        "lab": {
            "mean": lab_mean,
            "a_percentiles": _percentiles(lab_a, (5, 50, 95)),
            "b_percentiles": _percentiles(lab_b, (5, 50, 95)),
        },
        "hsv": {
            "saturation": {"mean": _round(sat_mean), "percentiles": _percentiles(sats)},
            "value": {"mean": _round(_mean(values)), "percentiles": _percentiles(values)},
        },
        "histograms": {
            "luma": _histogram(lumas, 0.0, 100.0, 20),
            "saturation": _histogram(sats, 0.0, 100.0, 20),
            "value": _histogram(values, 0.0, 100.0, 20),
            "hue": {
                **_histogram(chromatic_hues, 0.0, 360.0, 12),
                "note": "Hue histogram excludes near-neutral or near-black pixels where hue is visually unstable.",
                "chromatic_sample_ratio": _round(len(chromatic_hues) / pixel_count, 5),
            },
            "rgb": {
                "red": _histogram(reds, 0.0, 255.0, 16),
                "green": _histogram(greens, 0.0, 255.0, 16),
                "blue": _histogram(blues, 0.0, 255.0, 16),
            },
        },
        "hue_bands": band_summary,
        "dominant_colors": _dominant_colors(pixels),
        "risk_flags": _risk_flags(luma_percentiles, sat_mean, rgb_mean),
    }


def _normalize_region(region: Any, index: int, width: int, height: int) -> tuple[str, tuple[int, int, int, int], dict[str, Any] | None]:
    if not isinstance(region, dict):
        return f"region_{index + 1}", (0, 0, 0, 0), {"code": "invalid_region", "message": "region must be an object"}
    bbox = region.get("bbox")
    if region.get("type") != "bbox" or not isinstance(bbox, dict):
        return str(region.get("id") or f"region_{index + 1}"), (0, 0, 0, 0), {
            "code": "invalid_region",
            "message": "region must use type=bbox and include bbox",
        }
    try:
        x = int(round(float(bbox["x"])))
        y = int(round(float(bbox["y"])))
        w = int(round(float(bbox["width"])))
        h = int(round(float(bbox["height"])))
    except (KeyError, TypeError, ValueError) as exc:
        return str(region.get("id") or f"region_{index + 1}"), (0, 0, 0, 0), {
            "code": "invalid_bbox",
            "message": str(exc),
        }
    x1 = max(0, min(width, x))
    y1 = max(0, min(height, y))
    x2 = max(0, min(width, x + max(0, w)))
    y2 = max(0, min(height, y + max(0, h)))
    if x2 <= x1 or y2 <= y1:
        return str(region.get("id") or f"region_{index + 1}"), (x1, y1, x2, y2), {
            "code": "empty_region",
            "message": "region bbox is empty or outside the image",
        }
    return str(region.get("id") or f"region_{index + 1}"), (x1, y1, x2, y2), None


def analyze_image_metrics(payload: dict[str, Any]) -> dict[str, Any]:
    asset_path = payload.get("asset_path")
    if not isinstance(asset_path, str) or not asset_path.strip():
        return _error("missing_asset_path", "asset_path is required.")

    path = Path(asset_path).expanduser()
    if not path.is_file():
        return _error("asset_not_found", f"Image asset does not exist: {asset_path}")
    if path.suffix.lower() not in SUPPORTED_SUFFIXES:
        return _error("unsupported_asset_type", f"Unsupported image suffix: {path.suffix}")

    sample_max_side = int(payload.get("sample_max_side") or DEFAULT_SAMPLE_MAX_SIDE)
    sample_max_side = max(64, min(sample_max_side, 4096))
    regions = payload.get("regions") if isinstance(payload.get("regions"), list) else []
    scene_tags = payload.get("scene_tags") if isinstance(payload.get("scene_tags"), list) else []

    try:
        with Image.open(path) as image:
            image = image.convert("RGB")
            width, height = image.size
            global_metrics = _analyze_image_object(image, sample_max_side)
            region_metrics: dict[str, Any] = {}
            for index, region in enumerate(regions):
                region_id, box, error = _normalize_region(region, index, width, height)
                if error:
                    region_metrics[region_id] = {
                        "status": "error",
                        "error": error,
                        "source_region": region,
                    }
                    continue
                crop = image.crop(box)
                region_metrics[region_id] = {
                    "status": "ok",
                    "bbox": {
                        "x": box[0],
                        "y": box[1],
                        "width": box[2] - box[0],
                        "height": box[3] - box[1],
                    },
                    "purpose": region.get("purpose") if isinstance(region, dict) else None,
                    "metrics": _analyze_image_object(crop, sample_max_side),
                }
    except Exception as exc:
        return _error("image_analysis_failed", str(exc))

    return {
        "status": "ok",
        "schema_version": "ps-agent/v1",
        "asset_path": str(path),
        "scene_tags": [str(tag) for tag in scene_tags],
        "sample_max_side": sample_max_side,
        "global_metrics": global_metrics,
        "region_metrics": region_metrics,
    }


def unwrap_global_metrics(metrics: Any) -> dict[str, Any]:
    if not isinstance(metrics, dict):
        return {}
    if isinstance(metrics.get("global_metrics"), dict):
        return metrics["global_metrics"]
    return metrics


def metric_value(metrics: Any, path: str, default: float = 0.0) -> float:
    node: Any = unwrap_global_metrics(metrics)
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    try:
        return float(node)
    except (TypeError, ValueError):
        return default


def lab_distance(left: Any, right: Any) -> float:
    l1 = metric_value(left, "lab.mean.l")
    a1 = metric_value(left, "lab.mean.a")
    b1 = metric_value(left, "lab.mean.b")
    l2 = metric_value(right, "lab.mean.l")
    a2 = metric_value(right, "lab.mean.a")
    b2 = metric_value(right, "lab.mean.b")
    return math.sqrt((l1 - l2) ** 2 + (a1 - a2) ** 2 + (b1 - b2) ** 2)
