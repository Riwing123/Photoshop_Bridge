from __future__ import annotations

from typing import Any

from .style_metrics import analyze_image_metrics, lab_distance, metric_value, unwrap_global_metrics


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


def _round(value: float, digits: int = 4) -> float:
    return round(float(value), digits)


def _metrics_from_payload(payload: dict[str, Any], key: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    metrics_key = f"{key}_metrics"
    asset_key = f"{key}_asset_path"
    metrics = payload.get(metrics_key)
    if isinstance(metrics, dict):
        return metrics, None
    asset_path = payload.get(asset_key)
    if isinstance(asset_path, str) and asset_path.strip():
        result = analyze_image_metrics(
            {
                "asset_path": asset_path,
                "regions": payload.get(f"{key}_regions", []),
                "sample_max_side": payload.get("sample_max_side", 1000),
            }
        )
        if result.get("status") != "ok":
            return None, result
        return result, None
    return None, _error(f"missing_{key}_metrics", f"{metrics_key} or {asset_key} is required.")


def _score_closeness(value: float, good_at_zero: float) -> float:
    return _clamp(100.0 - (abs(value) / max(0.001, good_at_zero)) * 100.0, 0.0, 100.0)


def _risk_penalty(metrics: dict[str, Any]) -> tuple[float, list[str]]:
    global_metrics = unwrap_global_metrics(metrics)
    risk_flags = global_metrics.get("risk_flags") if isinstance(global_metrics.get("risk_flags"), list) else []
    severe = {"clipped_highlights", "crushed_blacks"}
    penalty = 0.0
    for flag in risk_flags:
        penalty += 18.0 if flag in severe else 8.0
    return min(70.0, penalty), [str(flag) for flag in risk_flags]


def score_grade_preview(payload: dict[str, Any]) -> dict[str, Any]:
    before_metrics, before_error = _metrics_from_payload(payload, "before")
    if before_error:
        return before_error
    after_metrics, after_error = _metrics_from_payload(payload, "after")
    if after_error:
        return after_error

    reference_metrics = payload.get("reference_metrics")
    reference_error = None
    if not isinstance(reference_metrics, dict):
        reference_metrics, reference_error = _metrics_from_payload(payload, "reference")
    if reference_error:
        reference_metrics = None

    assert before_metrics is not None
    assert after_metrics is not None

    before_after_lab = lab_distance(before_metrics, after_metrics)
    risk_penalty, risk_flags = _risk_penalty(after_metrics)

    if reference_metrics:
        before_ref_lab = lab_distance(before_metrics, reference_metrics)
        after_ref_lab = lab_distance(after_metrics, reference_metrics)
        improvement = before_ref_lab - after_ref_lab
        reference_similarity = _score_closeness(after_ref_lab, 28.0)
        improvement_score = _clamp(50.0 + improvement * 3.0, 0.0, 100.0)
    else:
        before_ref_lab = None
        after_ref_lab = None
        reference_similarity = 70.0
        improvement_score = 60.0

    tone_delta = abs(metric_value(after_metrics, "tone_percentiles.luma.p50") - metric_value(before_metrics, "tone_percentiles.luma.p50"))
    contrast = metric_value(after_metrics, "tone_percentiles.contrast_p95_p5")
    tone_safety = _clamp(100.0 - risk_penalty - max(0.0, tone_delta - 22.0) * 1.8, 0.0, 100.0)
    if contrast < 20 or contrast > 88:
        tone_safety = max(0.0, tone_safety - 12.0)

    subject_protection = _clamp(100.0 - max(0.0, before_after_lab - 18.0) * 2.0, 25.0, 100.0)
    color_consistency = _clamp((reference_similarity * 0.65) + (improvement_score * 0.35), 0.0, 100.0)
    black_white_safety = _clamp(100.0 - risk_penalty, 0.0, 100.0)
    total = (
        reference_similarity * 0.35
        + subject_protection * 0.25
        + tone_safety * 0.20
        + color_consistency * 0.15
        + black_white_safety * 0.05
    )

    failure_reasons: list[str] = []
    suggestions: list[str] = []
    if risk_flags:
        failure_reasons.append(f"after preview has risk flags: {', '.join(risk_flags)}")
        suggestions.append("Reduce exposure/contrast strength or protect clipped highlight/shadow regions.")
    if reference_metrics and after_ref_lab is not None and before_ref_lab is not None and after_ref_lab > before_ref_lab:
        failure_reasons.append("after preview moved farther from reference Lab average than before preview")
        suggestions.append("Revise Codex-authored stage parameters, mask strategy, or RAG guidance; do not use automatic plan candidates.")
    if before_after_lab > 30:
        failure_reasons.append("global Lab shift is large; important subjects may be polluted")
        suggestions.append("Add region-specific protection crops or lower global color balance opacity.")

    return {
        "status": "ok",
        "schema_version": "ps-agent/v1",
        "scores": {
            "total": _round(total, 2),
            "reference_similarity": _round(reference_similarity, 2),
            "subject_protection": _round(subject_protection, 2),
            "tone_safety": _round(tone_safety, 2),
            "color_consistency": _round(color_consistency, 2),
            "black_white_safety": _round(black_white_safety, 2),
        },
        "metrics": {
            "before_after_lab_distance": _round(before_after_lab, 3),
            "before_reference_lab_distance": None if before_ref_lab is None else _round(before_ref_lab, 3),
            "after_reference_lab_distance": None if after_ref_lab is None else _round(after_ref_lab, 3),
        },
        "pass": total >= float(payload.get("pass_threshold", 65)),
        "failure_reasons": failure_reasons,
        "suggestions": suggestions,
    }
