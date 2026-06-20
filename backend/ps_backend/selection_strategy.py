from __future__ import annotations

import re
from typing import Any


SCHEMA_VERSION = "ps-agent/v1"
STRATEGY_ID_RE = re.compile(r"^sel-[A-Za-z0-9_.:-]{1,96}$")
VALID_OPERATIONS = {"replace", "add", "subtract", "intersect", "soft_union", "soft_subtract", "soft_intersect"}
VALID_TOOLS = {
    "ps_select_subject",
    "ps_select_sky",
    "ps_select_color_range",
    "ps_select_focus_area",
    "ps_extract_tonal_range",
    "ps_select_highlights",
    "ps_select_midtones",
    "ps_select_shadows",
    "ps_build_luminosity_mask",
    "ps_generate_face_selection",
    "ps_detect_grounding_boxes",
    "ps_generate_grounded_hq_mask",
    "ps_generate_sam_mask",
    "ps_generate_hqsam_mask",
    "ps_make_selection",
    "codex_polygon",
    "bbox",
    "current_selection",
}


def _error(code: str, message: str, details: Any | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "status": "error",
        "schema_version": SCHEMA_VERSION,
        "error": {"code": code, "message": message},
    }
    if details is not None:
        payload["error"]["details"] = details
    return payload


def safe_identifier(value: Any, fallback: str) -> str:
    raw = str(value or fallback)
    cleaned = re.sub(r"[^A-Za-z0-9_.:-]+", "_", raw).strip("._:-")
    return cleaned[:96] or fallback


def normalize_terms(*values: Any) -> str:
    parts: list[str] = []
    for value in values:
        if isinstance(value, str):
            parts.append(value.lower())
        elif isinstance(value, list):
            parts.extend(str(item).lower() for item in value)
    return " ".join(parts)


def candidate(candidate_id: str, tool: str, role: str, why: str, trial_hint: dict[str, Any], risks: list[str]) -> dict[str, Any]:
    return {
        "candidate_id": candidate_id,
        "tool": tool,
        "role": role,
        "operation": "replace" if role == "base" else ("subtract" if role.startswith("exclude") else "add"),
        "why_try": why,
        "trial_hint": trial_hint,
        "lowering_rule": (
            "This is a strategy candidate, not an executable mask payload. "
            "Only lower it to a concrete Photoshop execution payload after RAG/web findings, "
            "trial overlay, and local crop review justify this route."
        ),
        "review_outputs": ["overlay_preview", "selection_preview", "local_crop"],
        "score_dimensions": ["coverage", "leakage", "edge_quality", "softness", "edit_safety", "latency"],
        "known_risks": risks,
    }


def make_seed_trial(
    trial_id: str,
    intent: str,
    starting_point: dict[str, Any],
    review_goal: str,
    when_to_prefer: str,
) -> dict[str, Any]:
    return {
        "trial_id": trial_id,
        "intent": intent,
        "starting_point": starting_point,
        "review_goal": review_goal,
        "when_to_prefer": when_to_prefer,
    }


def color_range_seed_trials(
    *,
    low: int,
    medium: int,
    high: int,
    localized_default: bool,
    preset_candidates: list[str] | None = None,
    sampled_colors: bool = False,
) -> list[dict[str, Any]]:
    shared: dict[str, Any] = {"localized_color_clusters": localized_default}
    if preset_candidates:
        shared["preset_candidates"] = preset_candidates
    if sampled_colors:
        shared["sample_count_hint"] = {"target": [2, 5], "negative": [1, 3]}
    return [
        make_seed_trial(
            "tight",
            "Start conservative to protect edges and reduce leakage.",
            dict(shared, fuzziness=low),
            "Check whether the target is mostly covered without eating protected foreground.",
            "Use first when protected regions share similar color or when edge hygiene matters more than total coverage.",
        ),
        make_seed_trial(
            "balanced",
            "Use the middle pass as the default comparison seed.",
            dict(shared, fuzziness=medium),
            "Check whether this gives the best balance of coverage and leakage.",
            "Use when the target is color-separated but not perfectly isolated.",
        ),
        make_seed_trial(
            "broad",
            "Use a wider color catchment when the target is soft, hazy, or internally varied.",
            dict(shared, fuzziness=high),
            "Check whether a broader catchment adds useful missing areas or starts polluting protected zones.",
            "Use only after a tighter seed clearly under-selects the target.",
        ),
    ]


def tonal_seed_trials(tonal_range_candidates: list[str]) -> list[dict[str, Any]]:
    return [
        make_seed_trial(
            "tight",
            "Start with a narrower tonal slice for cleaner separation.",
            {"fuzziness": 22, "tonal_range_candidates": tonal_range_candidates},
            "Check whether the selected tonal band isolates the intended brightness region cleanly.",
            "Use first when you need clean highlight/shadow isolation or protected edges matter.",
        ),
        make_seed_trial(
            "balanced",
            "Use the standard tonal band for most atmosphere and grading masks.",
            {"fuzziness": 40, "tonal_range_candidates": tonal_range_candidates},
            "Check whether the tonal range covers the useful region without flattening nearby tones.",
            "Use when the target is reasonably separated by brightness but still needs natural rolloff.",
        ),
        make_seed_trial(
            "broad",
            "Use a wider tonal band when the desired region is soft or spans more of the histogram.",
            {"fuzziness": 62, "tonal_range_candidates": tonal_range_candidates},
            "Check whether the wider band recovers missing tone area or starts spilling too far.",
            "Use after narrower seeds under-select haze, bloom, fog, or lifted atmosphere regions.",
        ),
    ]


def make_feedback_loop(
    *,
    tune_axes: list[str],
    if_undercoverage: list[str],
    if_leakage: list[str],
    if_edges_too_hard: list[str],
    stop_when: list[str],
    max_micro_tune_rounds: int = 3,
) -> dict[str, Any]:
    return {
        "seed_then_micro_tune": True,
        "max_micro_tune_rounds": max_micro_tune_rounds,
        "tune_axes": tune_axes,
        "if_undercoverage": if_undercoverage,
        "if_leakage": if_leakage,
        "if_edges_too_hard": if_edges_too_hard,
        "stop_when": stop_when,
    }


def default_review_regions(target: str) -> list[dict[str, Any]]:
    label = target or "selection target"
    return [
        {"id": "global_mask_overlay", "type": "global", "purpose": f"Check overall coverage and leakage for {label}."},
        {"id": "target_edge_crop", "type": "region", "purpose": "Check target boundary, holes, and soft edge quality."},
        {"id": "protected_area_crop", "type": "region", "purpose": "Check leakage into protected subject, face, text, logo, or background."},
    ]


def build_candidates(target_text: str, scene_text: str, protected: list[Any]) -> list[dict[str, Any]]:
    text = normalize_terms(target_text, scene_text)
    candidates: list[dict[str, Any]] = []

    wants_sky = any(term in text for term in ["sky", "天空", "blue sky", "cloud", "云"])
    wants_person = any(term in text for term in ["person", "human", "subject", "人物", "主体", "人像", "body"])
    wants_face = any(term in text for term in ["face", "eye", "eyes", "lip", "skin", "脸", "眼", "唇", "肤"])
    wants_semantic_object = any(
        term in text
        for term in [
            "building",
            "architecture",
            "clothes",
            "clothing",
            "blackboard",
            "flower",
            "product",
            "sign",
            "car",
            "water",
            "tree",
            "foliage",
            "建筑",
            "衣服",
            "黑板",
            "花",
            "产品",
            "招牌",
            "车辆",
            "水面",
            "树",
            "植被",
        ]
    )
    wants_color_region = any(
        term in text
        for term in [
            "color",
            "tone",
            "blue",
            "cyan",
            "green",
            "red",
            "yellow",
            "highlight",
            "shadow",
            "颜色",
            "色彩",
            "蓝",
            "青",
            "绿",
            "红",
            "黄",
            "高光",
            "阴影",
            "背景色",
        ]
    )

    wants_tonal_region = any(
        term in text
        for term in [
            "highlight",
            "highlights",
            "shadow",
            "shadows",
            "midtone",
            "midtones",
            "bright",
            "brightness",
            "dark",
            "luminosity",
            "tone range",
        ]
    )

    if wants_sky:
        candidates.append(
            candidate(
                "sky_native_select_sky",
                "ps_select_sky",
                "base",
                "Try Photoshop's semantic sky selector when the sky boundary is clear and sky is a coherent scene region.",
                {"method_family": "photoshop_semantic", "method": "select_sky", "suggested_trials": [{"strength": "default"}]},
                ["Can fail on indoor walls, low-contrast haze, reflections, or sky-like backgrounds."],
            )
        )
        candidates.append(
            candidate(
                "sky_color_range_blue_cyan_highlight",
                "ps_select_color_range",
                "base_or_refine",
                "Try Color Range when the sky is primarily separated by blue/cyan/highlight tone rather than semantic boundary.",
                {
                    "method_family": "photoshop_color_range",
                    "preset_candidates": ["blues", "cyans", "highlights"],
                    "sampled_color_policy": "sample target colors only after reviewing preview/crops",
                    "seed_trials": color_range_seed_trials(
                        low=24,
                        medium=42,
                        high=66,
                        localized_default=True,
                        preset_candidates=["blues", "cyans", "highlights"],
                    ),
                    "feedback_loop": make_feedback_loop(
                        tune_axes=["fuzziness", "localized_color_clusters", "preset_mix", "sampled_color_points"],
                        if_undercoverage=[
                            "move from tight to balanced or broad seed",
                            "add another compatible preset such as cyans or highlights",
                            "sample 2-5 target pixels from missed sky areas",
                        ],
                        if_leakage=[
                            "step back to a tighter seed",
                            "keep localized_color_clusters enabled",
                            "subtract subject/foreground candidate before approving",
                        ],
                        if_edges_too_hard=[
                            "prefer balanced seed over tight-only coverage",
                            "apply light feather only after the candidate wins review",
                        ],
                        stop_when=[
                            "coverage is sufficient for the edit goal",
                            "foreground leakage is acceptable or clearly subtractable",
                        ],
                    ),
                },
                ["Can pick blue clothes, glass, water, chalk, or other similar colors unless constrained by ROI/subtract masks."],
            )
        )
        candidates.append(
            candidate(
                "sky_multi_color_range_composite",
                "ps_make_selection",
                "base_composite",
                "Build the sky from multiple Color Range passes, then subtract subject/foreground selectors when a single sky selector or single color threshold leaks.",
                {
                    "method_family": "composite_trial",
                    "trial_sequence": [
                        {"method": "Color Range", "variant": "blues", "combine": "base", "seed_preference": "balanced"},
                        {"method": "Color Range", "variant": "cyans", "combine": "add", "seed_preference": "tight_or_balanced"},
                        {"method": "Color Range", "variant": "highlights", "combine": "optional_add", "seed_preference": "tight"},
                        {"method": "Subject Selection", "combine": "subtract", "purpose": "protect foreground subject"}
                    ],
                    "feedback_loop": make_feedback_loop(
                        tune_axes=["component_fuzziness", "component_enable_disable", "subtract_strength"],
                        if_undercoverage=[
                            "promote a missing component from optional_add to add",
                            "widen only the component that missed the target",
                        ],
                        if_leakage=[
                            "tighten the leaking component first rather than the whole composite",
                            "strengthen subtract subject/foreground protection",
                        ],
                        if_edges_too_hard=[
                            "keep the composite in hard-selection review only until the winning structure is known",
                            "prefer backend alpha-mask route if soft edge quality becomes the limiting factor",
                        ],
                        stop_when=[
                            "the composite beats any single selector on both coverage and leakage",
                        ],
                    ),
                    "lower_after_review_to": "concrete composite execution payload only after this candidate wins trial review"
                },
                ["Requires overlay review because highlights can include skin, clothes, chalk, windows, or white objects."],
            )
        )

    if wants_person:
        candidates.append(
            candidate(
                "subject_native_select_subject",
                "ps_select_subject",
                "base",
                "Try Photoshop's subject selector for whole-person or product-subject separation.",
                {"method_family": "photoshop_semantic", "method": "select_subject", "suggested_trials": [{"strength": "default"}]},
                ["May include props or miss fine hair/transparent edges; review face, hands, clothes, and background leakage."],
            )
        )

    if wants_face:
        candidates.append(
            candidate(
                "face_landmarker_parts",
                "ps_generate_face_selection",
                "base_or_refine",
                "Use Face Landmarker for facial parts because landmarks are more controllable than generic object masks.",
                {"method_family": "face_landmarker", "part_candidates": ["face_oval", "both_eyes"], "expand_px_range": [0, 24], "smooth_range": [0, 0.35], "feather_range": [4, 16]},
                ["Does not cover hair, ears, clothing, or non-frontal faces reliably."],
            )
        )

    if wants_tonal_region:
        candidates.append(
            candidate(
                "tonal_range_seed_ladder",
                "ps_extract_tonal_range",
                "base_or_refine",
                "Use tonal-range extraction when the region is better separated by brightness structure than by object semantics or hue.",
                {
                    "method_family": "photoshop_tonal_range",
                    "tonal_range_candidates": ["highlights", "midtones", "shadows"],
                    "seed_trials": tonal_seed_trials(["highlights", "midtones", "shadows"]),
                    "feedback_loop": make_feedback_loop(
                        tune_axes=["tonal_range", "fuzziness", "localized_color_clusters", "followup_modify_steps"],
                        if_undercoverage=[
                            "move from tight to balanced or broad seed",
                            "switch tonal band if the mask is clearly centered on the wrong brightness zone",
                            "add a light feather or smooth only after the band itself is correct",
                        ],
                        if_leakage=[
                            "reduce fuzziness",
                            "switch from broad to balanced or tight seed",
                            "combine with subtract subject/face/foreground protection if the tonal band is otherwise correct",
                        ],
                        if_edges_too_hard=[
                            "prefer modify.feather after a correct tonal hit instead of widening the tonal band too early",
                        ],
                        stop_when=[
                            "the tonal band isolates the intended brightness mechanism for the planned edit",
                            "remaining leakage is explicitly controlled by later protection refinement",
                        ],
                    ),
                },
                ["Broad tonal bands can flatten nearby image structure; review whether the intended glow, haze, or shadow region is actually isolated."],
            )
        )

    if wants_semantic_object:
        candidates.append(
            candidate(
                "semantic_grounding_hq",
                "ps_generate_grounded_hq_mask",
                "base",
                "Use text-guided detection before segmentation for non-face semantic objects and multi-instance targets.",
                {
                    "method_family": "text_grounded_segmentation",
                    "include_prompt_policy": "translate target into short English phrases after visual inspection",
                    "exclude_prompt_policy": "add protected classes such as person, face, hands, text, logo when relevant",
                    "merge_mode_candidates": ["union", "subtract_excludes"],
                    "threshold_range": [0.35, 0.65],
                },
                ["Grounding can miss ambiguous categories; HQ-SAM can over-grow if bbox is loose."],
            )
        )
        candidates.append(
            candidate(
                "bbox_or_points_hqsam",
                "ps_generate_hqsam_mask",
                "base_or_refine",
                "Use bbox/positive/negative points when Codex can localize the object more precisely than text detection.",
                {"method_family": "prompted_segmentation", "prompt_candidates": ["bbox", "positive_points", "negative_points"], "threshold_range": [0.35, 0.65]},
                ["Quality depends strongly on prompt box/points; requires overlay review before use."],
            )
        )

    if wants_color_region or not candidates:
        candidates.append(
            candidate(
                "sampled_color_range",
                "ps_select_color_range",
                "base_or_refine",
                "Use sampled Color Range for regions primarily separable by color or tone, including sky/background cases where semantic tools leak.",
                {
                    "method_family": "photoshop_color_range",
                    "sample_policy": "sample 2-5 representative pixels from target crop and 1-3 negative colors from protected regions",
                    "seed_trials": color_range_seed_trials(
                        low=18,
                        medium=34,
                        high=56,
                        localized_default=True,
                        sampled_colors=True,
                    ),
                    "feedback_loop": make_feedback_loop(
                        tune_axes=["fuzziness", "sampled_color_points", "negative_color_points", "localized_color_clusters"],
                        if_undercoverage=[
                            "add 1-2 more target sample points in the missed hue variation",
                            "promote tight to balanced or broad seed",
                        ],
                        if_leakage=[
                            "add 1-3 negative sample points from polluted protected zones",
                            "tighten fuzziness before abandoning the sampled route",
                        ],
                        if_edges_too_hard=[
                            "evaluate whether the sampled route should stay a hard selection or move to a soft alpha workflow",
                        ],
                        stop_when=[
                            "sampled colors cover the intended area with controllable spill",
                        ],
                    ),
                },
                ["Can leak into same-color foreground; must be combined with subtract/intersect masks when target color is shared."],
            )
        )

    if protected:
        candidates.append(
            candidate(
                "protected_area_subtract",
                "ps_make_selection",
                "exclude_refine",
                "Subtract protected regions after a broad base selection so edits do not pollute face, eyes, logo, text, product, or clothing.",
                {
                    "method_family": "protection_refinement",
                    "refine_candidates": [
                        {"method": "Subject Selection", "combine": "subtract"},
                        {"method": "Face Landmarker or polygon", "combine": "subtract"},
                        {"method": "bbox/polygon foreground guard", "combine": "subtract"},
                    ]
                },
                ["Hard Photoshop boolean operations can harden soft edges; use backend alpha compositing for soft alpha masks."],
            )
        )

    candidates.append(
        candidate(
            "codex_polygon_fallback",
            "codex_polygon",
            "fallback_refine",
            "Use Codex polygon/bbox only when automated selection misses a simple, visually clear region or for subtract/intersect cleanup.",
            {"method_family": "manual_geometry_fallback", "geometry_candidates": ["polygon", "bbox"], "combine_candidates": ["add", "subtract", "intersect"], "feather_range": [0, 8]},
            ["Not suitable as the only path for fine hair, complex foliage, transparent edges, or detailed faces."],
        )
    )
    return candidates


def create_selection_strategy(payload: dict[str, Any]) -> dict[str, Any]:
    target = str(payload.get("target") or payload.get("target_description") or payload.get("goal") or "").strip()
    if not target:
        return _error("missing_selection_target", "target or target_description is required.")
    visual_brief = str(payload.get("visual_brief") or "")
    scene_tags = payload.get("scene_tags") if isinstance(payload.get("scene_tags"), list) else []
    protected = payload.get("protected_regions") if isinstance(payload.get("protected_regions"), list) else []
    web_findings = payload.get("research_findings") if isinstance(payload.get("research_findings"), list) else []
    strategy_id = safe_identifier(payload.get("strategy_id"), "sel-" + re.sub(r"[^A-Za-z0-9]+", "-", target)[:32].strip("-").lower())
    if not strategy_id.startswith("sel-"):
        strategy_id = "sel-" + strategy_id

    candidates = build_candidates(target, normalize_terms(visual_brief, scene_tags), protected)
    strategy = {
        "schema_version": SCHEMA_VERSION,
        "strategy_id": strategy_id,
        "target": target,
        "visual_brief": visual_brief,
        "scene_tags": scene_tags,
        "protected_regions": protected,
        "research_findings": web_findings,
        "tool_policy": {
            "avoid_fixed_routing": True,
            "try_multiple_candidates_when_ambiguous": True,
            "native_and_model_tools_are_comparable_candidates": True,
            "color_range_may_beat_semantic_selectors": True,
            "seed_then_micro_tune_for_color_and_tone_tools": True,
            "record_why_each_tool_was_tried": True,
        },
        "candidate_methods": candidates,
        "comparison_policy": {
            "minimum_candidates_before_edit": 2 if len(candidates) >= 2 else 1,
            "must_export_overlay_or_selection_preview": True,
            "must_score_dimensions": ["coverage", "leakage", "edge_quality", "softness", "edit_safety", "latency"],
            "choose_best_or_composite": True,
            "if_all_fail": "revise_prompt_or_roi_then_retry_once",
        },
        "refinement_policy": {
            "hard_selection_ops": ["replace", "add", "subtract", "intersect"],
            "soft_alpha_ops": ["soft_union", "soft_subtract", "soft_intersect"],
            "first_operation_must_be_replace": True,
            "use_backend_alpha_compositing_for_soft_masks": True,
            "use_photoshop_boolean_ops_for_bbox_polygon_native_selection": True,
            "never_silently_degrade_alpha_to_hard_selection": True,
        },
        "review_regions": payload.get("review_regions") if isinstance(payload.get("review_regions"), list) else default_review_regions(target),
        "pass_criteria": [
            "Target coverage is sufficient for the intended edit.",
            "Leakage into protected regions is acceptable or explicitly subtracted.",
            "Edges are appropriate for the edit: soft for tonal/color edits, hard only for graphic or object-boundary edits.",
            "The selected method is justified against at least one plausible alternative when ambiguity exists.",
        ],
        "rollback_policy": {
            "selection_trials_are_read_only_until_apply": True,
            "bad_masks_must_not_be_used_for_apply_plan": True,
        },
    }
    validation = validate_selection_strategy({"selection_strategy": strategy})
    return {
        "status": "ok" if validation["valid"] else "error",
        "schema_version": SCHEMA_VERSION,
        "selection_strategy": strategy,
        "validation": validation,
    }


def validate_selection_strategy(payload: dict[str, Any]) -> dict[str, Any]:
    strategy = payload.get("selection_strategy") or payload.get("strategy")
    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(strategy, dict):
        errors.append("selection_strategy must be an object")
    else:
        if strategy.get("schema_version") != SCHEMA_VERSION:
            errors.append("selection_strategy.schema_version must be ps-agent/v1")
        strategy_id = strategy.get("strategy_id")
        if not isinstance(strategy_id, str) or not STRATEGY_ID_RE.match(strategy_id):
            errors.append(f"selection_strategy.strategy_id must match {STRATEGY_ID_RE.pattern}")
        if not isinstance(strategy.get("target"), str) or not strategy.get("target", "").strip():
            errors.append("selection_strategy.target must be a non-empty string")
        tool_policy = strategy.get("tool_policy")
        if not isinstance(tool_policy, dict):
            errors.append("selection_strategy.tool_policy must be an object")
        else:
            if tool_policy.get("avoid_fixed_routing") is not True:
                errors.append("selection_strategy.tool_policy.avoid_fixed_routing must be true")
            if tool_policy.get("record_why_each_tool_was_tried") is not True:
                errors.append("selection_strategy.tool_policy.record_why_each_tool_was_tried must be true")
        candidates = strategy.get("candidate_methods")
        if not isinstance(candidates, list) or not candidates:
            errors.append("selection_strategy.candidate_methods must be a non-empty array")
        else:
            seen: set[str] = set()
            has_native = False
            has_model_or_generated = False
            for index, item in enumerate(candidates):
                path = f"selection_strategy.candidate_methods[{index}]"
                if not isinstance(item, dict):
                    errors.append(f"{path} must be an object")
                    continue
                candidate_id = item.get("candidate_id")
                if not isinstance(candidate_id, str) or not candidate_id:
                    errors.append(f"{path}.candidate_id must be a non-empty string")
                elif candidate_id in seen:
                    errors.append(f"Duplicate candidate_id: {candidate_id}")
                seen.add(str(candidate_id))
                tool = item.get("tool")
                if tool not in VALID_TOOLS:
                    errors.append(f"{path}.tool must be a registered selection tool or internal candidate")
                if tool in {
                    "ps_select_subject",
                    "ps_select_sky",
                    "ps_select_color_range",
                    "ps_select_focus_area",
                    "ps_extract_tonal_range",
                    "ps_select_highlights",
                    "ps_select_midtones",
                    "ps_select_shadows",
                }:
                    has_native = True
                if tool in {"ps_generate_face_selection", "ps_generate_grounded_hq_mask", "ps_generate_sam_mask", "ps_generate_hqsam_mask", "codex_polygon"}:
                    has_model_or_generated = True
                if item.get("operation") not in VALID_OPERATIONS:
                    errors.append(f"{path}.operation must be one of: {', '.join(sorted(VALID_OPERATIONS))}")
                if not isinstance(item.get("why_try"), str) or not item.get("why_try", "").strip():
                    errors.append(f"{path}.why_try must explain why this candidate is being tried")
                trial_hint = item.get("trial_hint")
                if not isinstance(trial_hint, dict):
                    errors.append(f"{path}.trial_hint must be an abstract object")
                elif "selection_mask" in trial_hint:
                    errors.append(
                        f"{path}.trial_hint must not contain executable selection_mask; "
                        "lower candidates only after trial review"
                    )
                else:
                    method_family = trial_hint.get("method_family")
                    if method_family in {"photoshop_color_range", "photoshop_tonal_range"}:
                        seed_trials = trial_hint.get("seed_trials")
                        if not isinstance(seed_trials, list) or not seed_trials:
                            errors.append(f"{path}.trial_hint.seed_trials must be a non-empty array for {method_family}")
                        feedback_loop = trial_hint.get("feedback_loop")
                        if not isinstance(feedback_loop, dict):
                            errors.append(f"{path}.trial_hint.feedback_loop must be an object for {method_family}")
                        elif feedback_loop.get("seed_then_micro_tune") is not True:
                            errors.append(f"{path}.trial_hint.feedback_loop.seed_then_micro_tune must be true for {method_family}")
                if not isinstance(item.get("lowering_rule"), str) or not item.get("lowering_rule", "").strip():
                    errors.append(f"{path}.lowering_rule must state when this candidate may be lowered")
                if not isinstance(item.get("review_outputs"), list) or not item.get("review_outputs"):
                    errors.append(f"{path}.review_outputs must be a non-empty array")
            if len(candidates) >= 2 and not (has_native or has_model_or_generated):
                warnings.append("selection_strategy should compare at least one native or one generated/model candidate when possible")
        comparison = strategy.get("comparison_policy")
        if not isinstance(comparison, dict):
            errors.append("selection_strategy.comparison_policy must be an object")
        else:
            if comparison.get("must_export_overlay_or_selection_preview") is not True:
                errors.append("selection_strategy.comparison_policy.must_export_overlay_or_selection_preview must be true")
            if not isinstance(comparison.get("must_score_dimensions"), list) or not comparison.get("must_score_dimensions"):
                errors.append("selection_strategy.comparison_policy.must_score_dimensions must be a non-empty array")
        refinement = strategy.get("refinement_policy")
        if not isinstance(refinement, dict):
            errors.append("selection_strategy.refinement_policy must be an object")
        else:
            if refinement.get("first_operation_must_be_replace") is not True:
                errors.append("selection_strategy.refinement_policy.first_operation_must_be_replace must be true")
            if refinement.get("use_backend_alpha_compositing_for_soft_masks") is not True:
                errors.append("selection_strategy.refinement_policy.use_backend_alpha_compositing_for_soft_masks must be true")
        if not isinstance(strategy.get("review_regions"), list) or not strategy.get("review_regions"):
            errors.append("selection_strategy.review_regions must be a non-empty array")
        if not isinstance(strategy.get("pass_criteria"), list) or not strategy.get("pass_criteria"):
            errors.append("selection_strategy.pass_criteria must be a non-empty array")
    return {
        "status": "ok",
        "schema_version": SCHEMA_VERSION,
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
    }
