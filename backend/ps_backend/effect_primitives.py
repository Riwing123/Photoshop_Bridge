from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
PRIMITIVES_PATH = ROOT / "effect_primitives" / "effect_primitives.json"

SUPPORTED_RECIPE_OPS = {
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
SUPPORTED_BLEND_MODES = {
    "normal",
    "screen",
    "softLight",
    "overlay",
    "multiply",
    "colorDodge",
    "linearDodge",
    "lighten",
    "color",
    "luminosity",
}


def _error(code: str, message: str, details: Any | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "status": "error",
        "schema_version": "ps-agent/v1",
        "error": {"code": code, "message": message},
    }
    if details is not None:
        payload["error"]["details"] = details
    return payload


def load_effect_library(path: Path = PRIMITIVES_PATH) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    data.setdefault("primitives", [])
    data.setdefault("example_recipes", [])
    return data


def _tokens(value: Any) -> set[str]:
    text = str(value or "").lower()
    tokens = set(re.findall(r"[a-z0-9_+-]+", text))
    cjk = re.findall(r"[\u4e00-\u9fff]", text)
    tokens.update(cjk)
    for index in range(max(0, len(cjk) - 1)):
        tokens.add(cjk[index] + cjk[index + 1])
    return {token for token in tokens if token.strip()}


def _normalized_text(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "").lower())


def _phrase_match(phrase: Any, text: str) -> bool:
    phrase_text = _normalized_text(phrase)
    return bool(phrase_text and phrase_text in text)


def _tag_matches_text(tags: list[str], text: str) -> list[str]:
    return [tag for tag in tags if _phrase_match(tag, text)]


def _primitive_tokens(primitive: dict[str, Any]) -> set[str]:
    values: list[Any] = [
        primitive.get("id"),
        primitive.get("category"),
        primitive.get("visual_effect"),
    ]
    values.extend(primitive.get("intent_tags") or [])
    values.extend(primitive.get("photoshop_strategy") or [])
    values.extend(primitive.get("protect") or [])
    values.extend(primitive.get("review_regions") or [])
    tokens: set[str] = set()
    for value in values:
        tokens.update(_tokens(value))
    return tokens


def _as_str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]


def _clamp(value: Any, low: float, high: float, fallback: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = fallback
    return max(low, min(high, parsed))


def list_effect_primitives(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = payload or {}
    try:
        library = load_effect_library()
    except Exception as exc:
        return _error("effect_primitives_unavailable", str(exc), {"path": str(PRIMITIVES_PATH)})

    category = payload.get("category")
    include_details = bool(payload.get("include_details", True))
    primitives = library.get("primitives", [])
    if category:
        primitives = [item for item in primitives if item.get("category") == category]

    if not include_details:
        primitives = [
            {
                "id": item.get("id"),
                "category": item.get("category"),
                "intent_tags": item.get("intent_tags", []),
                "visual_effect": item.get("visual_effect"),
            }
            for item in primitives
        ]

    categories: dict[str, int] = {}
    for item in library.get("primitives", []):
        key = str(item.get("category") or "uncategorized")
        categories[key] = categories.get(key, 0) + 1

    return {
        "status": "ok",
        "schema_version": "ps-agent/v1",
        "library_version": library.get("library_version"),
        "primitive_count": len(library.get("primitives", [])),
        "categories": categories,
        "primitives": primitives,
        "example_recipes": library.get("example_recipes", []),
    }


def retrieve_effect_primitives(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        library = load_effect_library()
    except Exception as exc:
        return _error("effect_primitives_unavailable", str(exc), {"path": str(PRIMITIVES_PATH)})

    user_goal = payload.get("user_goal") or payload.get("goal") or ""
    visual_brief = payload.get("visual_brief") or ""
    scene_tags = payload.get("scene_tags") if isinstance(payload.get("scene_tags"), list) else []
    avoid = payload.get("avoid") if isinstance(payload.get("avoid"), list) else []
    requested_categories = set(str(item) for item in payload.get("categories") or [] if str(item).strip())
    limit = int(_clamp(payload.get("limit"), 1, 24, 8))

    query_values: list[Any] = [user_goal, visual_brief]
    query_values.extend(scene_tags)
    query_text = _normalized_text(" ".join(str(item) for item in query_values))
    avoid_text = _normalized_text(" ".join(str(item) for item in avoid))
    query_tokens: set[str] = set()
    for value in query_values:
        query_tokens.update(_tokens(value))
    avoid_tokens: set[str] = set()
    for value in avoid:
        avoid_tokens.update(_tokens(value))

    scored: list[tuple[float, dict[str, Any], list[str], list[str]]] = []
    for primitive in library.get("primitives", []):
        if requested_categories and primitive.get("category") not in requested_categories:
            continue
        tokens = _primitive_tokens(primitive)
        token_positive = sorted(tokens & query_tokens)
        positive = list(token_positive)
        negative_tags = _as_str_list(primitive.get("negative_tags"))
        phrase_positive: list[str] = []
        phrase_negative: list[str] = []
        score = len(token_positive) * 0.35
        reasons: list[str] = []
        primitive_id = str(primitive.get("id") or "")
        if primitive_id and _phrase_match(primitive_id, query_text):
            score += 10.0
            positive.append(primitive_id)
            reasons.append(f"id:{primitive_id}")

        id_words = primitive_id.replace("_", " ")
        if primitive_id and id_words != primitive_id and _phrase_match(id_words, query_text):
            score += 4.0
            positive.append(id_words)

        for tag in _as_str_list(primitive.get("intent_tags")):
            if _phrase_match(tag, query_text):
                score += 3.0
                positive.append(tag)
                phrase_positive.append(tag)

        strategy_matches = _tag_matches_text(_as_str_list(primitive.get("photoshop_strategy")), query_text)
        if strategy_matches:
            score += 0.8 * len(strategy_matches)
            positive.extend(strategy_matches)

        for field in ["visual_effect", "category"]:
            value = primitive.get(field)
            if value and _phrase_match(value, query_text):
                score += 1.2
                positive.append(str(value))

        if positive:
            reasons.append("match:" + ",".join(sorted(set(positive))[:6]))
        for tag in negative_tags:
            if _phrase_match(tag, query_text) or _phrase_match(tag, avoid_text):
                phrase_negative.append(tag)
        negative = sorted(set(phrase_negative) | (tokens & avoid_tokens))
        if negative:
            score -= 3.0 * len(set(negative))
            reasons.append("avoid:" + ",".join(sorted(set(negative))[:4]))
        if phrase_positive:
            score += min(4.0, len(set(phrase_positive)) * 0.75)
        if score > 0:
            scored.append((score, primitive, sorted(set(positive)), reasons))

    selected = [
        {
            "score": round(score, 3),
            "primitive": primitive,
            "matched_terms": matched,
            "selected_reason": "; ".join(reasons) if reasons else "token_match",
        }
        for score, primitive, matched, reasons in sorted(scored, key=lambda item: item[0], reverse=True)[:limit]
    ]

    if not selected:
        fallback_ids = ["exposure_lift", "white_rolloff", "contrast_pop", "pastel_desaturate"]
        by_id = {item.get("id"): item for item in library.get("primitives", [])}
        selected = [
            {
                "score": 0.0,
                "primitive": by_id[item],
                "matched_terms": [],
                "selected_reason": "safe_default",
            }
            for item in fallback_ids
            if item in by_id
        ][:limit]

    guidance = {
        "selected_primitive_ids": [item["primitive"].get("id") for item in selected],
        "categories": sorted({str(item["primitive"].get("category")) for item in selected}),
        "protect": sorted({protect for item in selected for protect in _as_str_list(item["primitive"].get("protect"))}),
        "review_regions": sorted({region for item in selected for region in _as_str_list(item["primitive"].get("review_regions"))}),
        "failure_modes": sorted({mode for item in selected for mode in _as_str_list(item["primitive"].get("failure_modes"))}),
    }

    return {
        "status": "ok",
        "schema_version": "ps-agent/v1",
        "query": {
            "user_goal": user_goal,
            "visual_brief": visual_brief,
            "scene_tags": scene_tags,
            "avoid": avoid,
        },
        "matches": selected,
        "guidance": guidance,
    }


def _primitive_by_id(library: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(item.get("id")): item for item in library.get("primitives", []) if item.get("id")}


def _selected_primitives(payload: dict[str, Any], library: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    by_id = _primitive_by_id(library)
    warnings: list[str] = []
    selected = payload.get("selected_primitives")
    primitive_ids = payload.get("primitive_ids")

    ids: list[str] = []
    if isinstance(selected, list) and selected:
        for item in selected:
            if isinstance(item, dict) and item.get("id"):
                ids.append(str(item["id"]))
            elif isinstance(item, str):
                ids.append(item)
    elif isinstance(primitive_ids, list) and primitive_ids:
        ids = [str(item) for item in primitive_ids]
    else:
        retrieved = retrieve_effect_primitives(payload)
        if retrieved.get("status") != "ok":
            return [], [retrieved.get("error", {}).get("message", "primitive retrieval failed")]
        ids = [str(item) for item in retrieved.get("guidance", {}).get("selected_primitive_ids", [])]

    unique_ids: list[str] = []
    for item in ids:
        if item not in unique_ids:
            unique_ids.append(item)
    primitives: list[dict[str, Any]] = []
    for primitive_id in unique_ids[:8]:
        primitive = by_id.get(primitive_id)
        if primitive:
            primitives.append(primitive)
        else:
            warnings.append(f"Unknown primitive_id ignored: {primitive_id}")

    executable = [item for item in primitives if isinstance(item.get("operation_template"), dict)]
    if len(executable) < 3:
        for fallback_id in ["exposure_lift", "white_rolloff", "contrast_pop", "pastel_desaturate", "warm_highlights"]:
            if fallback_id in by_id and all(item.get("id") != fallback_id for item in executable):
                executable.append(by_id[fallback_id])
            if len(executable) >= 3:
                break
        warnings.append("Fewer than three executable primitives were selected; safe defaults were added.")
    return executable[:8], warnings


def _operation_from_primitive(primitive: dict[str, Any], index: int, strength: float) -> dict[str, Any]:
    template = primitive.get("operation_template") if isinstance(primitive.get("operation_template"), dict) else {}
    op_name = str(template.get("op") or "")
    params = json.loads(json.dumps(template.get("params") or {}))
    defaults = primitive.get("default_params") if isinstance(primitive.get("default_params"), dict) else {}
    opacity_default = defaults.get("opacity", 50)
    opacity = _clamp(float(opacity_default) * strength, 5, 100, 50)
    blend_mode = str(defaults.get("blend_mode") or "normal")
    if blend_mode not in SUPPORTED_BLEND_MODES:
        blend_mode = "normal"
    return {
        "step_id": f"step_{index:02d}_{primitive.get('id')}",
        "primitive_id": primitive.get("id"),
        "op": op_name,
        "target": {"type": "global"},
        "params": params,
        "layer": {
            "name": f"Primitive {index:02d} - {primitive.get('id')}"[:80],
            "opacity": round(opacity, 2),
            "blend_mode": blend_mode,
        },
        "reason": primitive.get("visual_effect") or f"Apply primitive {primitive.get('id')}.",
        "expected_effect": primitive.get("visual_effect"),
        "failure_modes": primitive.get("failure_modes", []),
        "review_regions": primitive.get("review_regions", []),
        "implementation": "apply_plan_op_v1",
    }


def generate_layer_recipe(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        library = load_effect_library()
    except Exception as exc:
        return _error("effect_primitives_unavailable", str(exc), {"path": str(PRIMITIVES_PATH)})

    user_goal = str(payload.get("user_goal") or payload.get("goal") or "Open-ended effect recipe")
    visual_brief = str(payload.get("visual_brief") or "")
    strength_name = str(payload.get("strength") or "standard")
    strength_map = {"subtle": 0.65, "standard": 1.0, "strong": 1.25}
    strength = strength_map.get(strength_name, _clamp(payload.get("strength"), 0.3, 1.5, 1.0))
    primitives, warnings = _selected_primitives(payload, library)
    if not primitives:
        return _error("no_effect_primitives", "No effect primitives could be selected.")

    steps = [_operation_from_primitive(primitive, index + 1, strength) for index, primitive in enumerate(primitives)]
    recipe_id = str(payload.get("recipe_id") or f"recipe-{uuid.uuid4().hex[:8]}")
    review_regions = sorted({region for primitive in primitives for region in _as_str_list(primitive.get("review_regions"))})
    layer_recipe = {
        "schema_version": "ps-agent/v1",
        "recipe_id": recipe_id,
        "goal": user_goal,
        "visual_brief": visual_brief,
        "intent": {
            "user_goal": user_goal,
            "scene_tags": payload.get("scene_tags") if isinstance(payload.get("scene_tags"), list) else [],
            "strength": strength_name,
        },
        "selected_primitives": [
            {
                "id": primitive.get("id"),
                "category": primitive.get("category"),
                "reason": primitive.get("visual_effect"),
                "failure_modes": primitive.get("failure_modes", []),
            }
            for primitive in primitives
        ],
        "steps": steps,
        "review": {
            "export_global": True,
            "primitive_review_regions": review_regions,
            "regions": payload.get("review_regions", []),
        },
        "safety": {
            "non_destructive": True,
            "allow_destructive": False,
            "create_history_state": True,
            "max_primitives": 8,
        },
        "metadata": {
            "library_version": library.get("library_version"),
            "warnings": warnings,
            "lowering": "ps_apply_plan_v1",
        },
    }

    validation = validate_layer_recipe({"layer_recipe": layer_recipe})
    return {
        "status": "ok" if validation["valid"] else "error",
        "schema_version": "ps-agent/v1",
        "layer_recipe": layer_recipe,
        "validation": validation,
        "warnings": warnings,
    }


def validate_layer_recipe(payload: dict[str, Any]) -> dict[str, Any]:
    recipe = payload.get("layer_recipe") or payload.get("recipe")
    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(recipe, dict):
        errors.append("layer_recipe must be an object")
    else:
        if recipe.get("schema_version") != "ps-agent/v1":
            errors.append("layer_recipe.schema_version must be ps-agent/v1")
        if not isinstance(recipe.get("goal"), str) or not recipe.get("goal", "").strip():
            errors.append("layer_recipe.goal must be a non-empty string")
        steps = recipe.get("steps")
        if not isinstance(steps, list) or len(steps) < 1:
            errors.append("layer_recipe.steps must be a non-empty array")
        elif len(steps) > 20:
            errors.append("layer_recipe.steps must contain at most 20 steps")
        else:
            primitive_ids = []
            for index, step in enumerate(steps):
                path = f"layer_recipe.steps[{index}]"
                if not isinstance(step, dict):
                    errors.append(f"{path} must be an object")
                    continue
                primitive_id = step.get("primitive_id")
                if not isinstance(primitive_id, str) or not primitive_id:
                    errors.append(f"{path}.primitive_id must be a non-empty string")
                else:
                    primitive_ids.append(primitive_id)
                op_name = step.get("op")
                if op_name not in SUPPORTED_RECIPE_OPS:
                    errors.append(f"{path}.op must be one of: {', '.join(sorted(SUPPORTED_RECIPE_OPS))}")
                if not isinstance(step.get("params"), dict) or not step["params"]:
                    errors.append(f"{path}.params must be a non-empty object")
                target = step.get("target")
                if not isinstance(target, dict) or target.get("type") not in {"global", "selection_mask", "acr_ai_mask"}:
                    errors.append(f"{path}.target.type must be global, selection_mask, or acr_ai_mask")
                layer = step.get("layer") if isinstance(step.get("layer"), dict) else {}
                opacity = layer.get("opacity")
                if opacity is not None and not (isinstance(opacity, (int, float)) and 0 <= float(opacity) <= 100):
                    errors.append(f"{path}.layer.opacity must be 0..100")
                blend_mode = layer.get("blend_mode")
                if blend_mode is not None and blend_mode not in SUPPORTED_BLEND_MODES:
                    errors.append(f"{path}.layer.blend_mode is not supported")
            if len(set(primitive_ids)) < 3:
                warnings.append("A robust open-ended recipe should usually include at least three distinct primitives.")

    return {
        "status": "ok",
        "schema_version": "ps-agent/v1",
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "summary": {
            "step_count": len(recipe.get("steps", [])) if isinstance(recipe, dict) and isinstance(recipe.get("steps"), list) else 0,
            "primitive_ids": [
                step.get("primitive_id")
                for step in recipe.get("steps", [])
                if isinstance(recipe, dict) and isinstance(recipe.get("steps"), list) and isinstance(step, dict)
            ] if isinstance(recipe, dict) else [],
        },
    }


def lower_layer_recipe_to_plan(recipe: dict[str, Any]) -> dict[str, Any]:
    ops = []
    for step in recipe.get("steps", []):
        op = {
            "op": step["op"],
            "target": step.get("target") or {"type": "global"},
            "params": step.get("params") or {},
            "layer": step.get("layer") or {},
            "reason": f"{step.get('primitive_id')}: {step.get('reason') or step.get('expected_effect') or ''}"[:500],
        }
        ops.append(op)
    return {
        "schema_version": "ps-agent/v1",
        "plan_id": recipe.get("recipe_id"),
        "goal": recipe.get("goal") or "Layer recipe",
        "ops": ops,
        "review": {
            "export_global": bool((recipe.get("review") or {}).get("export_global", True)),
            "regions": (recipe.get("review") or {}).get("regions", []),
        },
        "safety": recipe.get("safety") or {
            "non_destructive": True,
            "allow_destructive": False,
            "create_history_state": True,
        },
        "metadata": {
            "source": "layer_recipe",
            "recipe_id": recipe.get("recipe_id"),
            "primitive_ids": [step.get("primitive_id") for step in recipe.get("steps", [])],
        },
    }


def review_layer_recipe(payload: dict[str, Any]) -> dict[str, Any]:
    recipe = payload.get("layer_recipe") or payload.get("recipe")
    validation = validate_layer_recipe({"layer_recipe": recipe})
    if not validation["valid"]:
        return {
            "status": "error",
            "schema_version": "ps-agent/v1",
            "error": {
                "code": "invalid_layer_recipe",
                "message": "Layer recipe validation failed.",
                "details": validation["errors"],
            },
        }
    reviews = []
    for step in recipe.get("steps", []):
        reviews.append(
            {
                "primitive_id": step.get("primitive_id"),
                "status": "needs_visual_review",
                "review_regions": step.get("review_regions", []),
                "failure_modes": step.get("failure_modes", []),
                "suggested_feedback_mapping": {
                    "too_strong": "lower layer.opacity or reduce primitive strength",
                    "color_pollution": "add a protection primitive or mask this step",
                    "too_flat": "reduce matte/contrast-compress primitives or add contrast_pop",
                    "too_soft": "reduce glow/soften primitives or add eye/text protection",
                },
            }
        )
    return {
        "status": "ok",
        "schema_version": "ps-agent/v1",
        "recipe_id": recipe.get("recipe_id"),
        "primitive_reviews": reviews,
        "overall": {
            "status": "needs_preview",
            "message": "Export global and region previews after apply; map visual issues back to primitive_ids.",
        },
    }
