from __future__ import annotations

import re
import uuid
from typing import Any


SCHEMA_VERSION = "ps-agent/v1"
MAX_EXECUTION_STAGES = 6
STAGE_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
WORKFLOW_ID_RE = re.compile(r"^wf-[A-Za-z0-9_.:-]{1,96}$")
LOW_RISK_STAGE_TYPES = {"state_capture", "final_review"}
PHOTO_EFFECT_ROUTES = {"photo_effect"}
PHOTO_EFFECT_STAGE_TYPES = {"atmosphere_effects", "photo_effect"}
EXECUTABLE_RECIPE_KEYS = {"recipe_or_plan", "operation_recipe", "selection_recipe"}
DEFAULT_STAGE_ORDER = [
    "state_capture",
    "mask_preparation",
    "base_tone",
    "color_direction",
    "local_protection",
    "atmosphere_effects",
    "detail_finish",
    "final_review",
]
DEFAULT_GLOBAL_REVIEW_REGION = {
    "id": "global",
    "type": "global",
    "purpose": "Whole image stage review",
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
    return cleaned[:64] or fallback


def default_workflow_id() -> str:
    return f"wf-{uuid.uuid4().hex[:10]}"


def default_stage(stage_id: str, index: int) -> dict[str, Any]:
    objectives = {
        "state_capture": "Capture document state, before preview, and key review crops.",
        "mask_preparation": "Prepare and verify selections, masks, or alpha channels needed by later stages.",
        "base_tone": "Set exposure, black/white points, contrast, highlights, and shadows.",
        "color_direction": "Set color temperature, hue direction, HSL, and selective color direction.",
        "local_protection": "Protect subject, eyes, logo, text, sky, product, or other sensitive areas.",
        "atmosphere_effects": "Add style effects such as bloom, haze, white-soft, halation, glare, grain, or vignette.",
        "detail_finish": "Tune clarity, sharpening, texture, noise, and edge artifacts.",
        "final_review": "Review the full image and important crops for cross-stage side effects.",
    }
    route_by_stage = {
        "state_capture": "state_capture",
        "mask_preparation": "mask_preparation",
        "base_tone": "photo_grade",
        "color_direction": "photo_grade",
        "local_protection": "mask_preparation",
        "atmosphere_effects": "photo_effect",
        "detail_finish": "simple_image_adjust",
        "final_review": "final_review",
    }
    return {
        "stage_id": stage_id,
        "stage_type": stage_id,
        "route": route_by_stage.get(stage_id, stage_id),
        "order": index,
        "objective": objectives.get(stage_id, f"Complete stage {stage_id}."),
        "expected_result": "Stage-specific result must match objective without introducing obvious artifacts.",
        "recipe_or_plan": None,
        "review_regions": [],
        "pass_criteria": [
            "Matches the stage objective.",
            "No unacceptable artifacts in the declared review regions.",
            "Does not undo or pollute previous accepted stages.",
        ],
        "rollback_target": {"type": "stage_group"},
        "depends_on": [] if index <= 1 else ["previous_stage"],
        "pre_stage_checks": [
            "Confirm current document state and active layer still match the workflow expectation.",
            "Confirm accepted previous stages still meet their pass criteria before applying this stage.",
            "If a dependency has regressed, stop this stage and revise or delete the responsible earlier stage.",
        ],
        "previous_stage_recheck": {
            "required": index > 1 and stage_id not in LOW_RISK_STAGE_TYPES,
            "if_failed": "revise_dependency",
        },
        "requires_review": stage_id not in LOW_RISK_STAGE_TYPES,
    }


def _stage_route(stage: dict[str, Any]) -> str:
    return str(stage.get("route") or stage.get("stage_route") or stage.get("stage_type") or stage.get("stage_id") or "")


def _is_photo_effect_stage(stage: dict[str, Any]) -> bool:
    route = _stage_route(stage)
    stage_type = str(stage.get("stage_type") or "")
    stage_id = str(stage.get("stage_id") or "")
    return route in PHOTO_EFFECT_ROUTES or stage_type in PHOTO_EFFECT_STAGE_TYPES or stage_id in PHOTO_EFFECT_STAGE_TYPES


def _stage_executable_payload(stage: dict[str, Any]) -> Any:
    for key in EXECUTABLE_RECIPE_KEYS:
        payload = stage.get(key)
        if payload is not None:
            return payload
    return None


def _has_executable_recipe_shape(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    if isinstance(payload.get("operation_recipe"), dict):
        return _has_executable_recipe_shape(payload["operation_recipe"])
    if isinstance(payload.get("selection_recipe"), dict):
        return True
    if isinstance(payload.get("layer_recipe"), dict):
        return True
    if isinstance(payload.get("recipe"), dict):
        return _has_executable_recipe_shape(payload["recipe"])
    if isinstance(payload.get("plan"), dict):
        return _has_executable_recipe_shape(payload["plan"])
    if isinstance(payload.get("steps"), list) and payload["steps"]:
        return True
    if isinstance(payload.get("ops"), list) and payload["ops"]:
        return True
    if isinstance(payload.get("candidates"), list) and isinstance(payload.get("merge_plan"), dict):
        return True
    return False


def validate_photo_effect_stage(stage: dict[str, Any], path: str, errors: list[str], warnings: list[str]) -> None:
    if not _is_photo_effect_stage(stage):
        return

    effect_intent = stage.get("effect_intent")
    executable_payload = _stage_executable_payload(stage)

    if effect_intent is None:
        message = (
            f"{path}.effect_intent should describe the visual goal and mechanisms for photo_effect stages; "
            "white-soft, bloom, halation, haze, and similar effect concepts are not executable atoms."
        )
        if executable_payload is None:
            warnings.append(message)
        else:
            errors.append(message)
    elif not isinstance(effect_intent, dict):
        errors.append(f"{path}.effect_intent must be an object when provided")
    else:
        if executable_payload is not None:
            if not str(effect_intent.get("visual_goal") or effect_intent.get("goal") or "").strip():
                errors.append(f"{path}.effect_intent.visual_goal must be a non-empty string before execution")
            mechanisms = effect_intent.get("mechanisms")
            if not isinstance(mechanisms, list) or not mechanisms:
                errors.append(f"{path}.effect_intent.mechanisms must be a non-empty array before execution")
        if effect_intent.get("not_atoms") is False:
            errors.append(f"{path}.effect_intent.not_atoms must not be false; effect concepts are strategy only")

    if executable_payload is None:
        return
    if not isinstance(executable_payload, dict):
        errors.append(f"{path} executable recipe must be an object")
        return
    if not _has_executable_recipe_shape(executable_payload):
        errors.append(
            f"{path} photo_effect stages must lower effect_intent into operation_recipe, selection_recipe, "
            "layer_recipe, or a ps-agent plan with concrete steps/ops before execution"
        )


def create_workflow_plan(payload: dict[str, Any]) -> dict[str, Any]:
    workflow_id = str(payload.get("workflow_id") or default_workflow_id())
    goal = str(payload.get("goal") or payload.get("user_goal") or "Staged Photoshop workflow").strip()
    requested = payload.get("stage_ids")
    if isinstance(requested, list) and requested:
        stage_ids = [safe_identifier(item, f"stage_{index + 1}") for index, item in enumerate(requested)]
    else:
        stage_ids = ["state_capture", "base_tone", "color_direction", "atmosphere_effects", "final_review"]
    stages = [default_stage(stage_id, index + 1) for index, stage_id in enumerate(stage_ids)]
    review_regions = payload.get("review_regions") if isinstance(payload.get("review_regions"), list) else []
    for stage in stages:
        if stage["requires_review"] and not stage["review_regions"]:
            stage["review_regions"] = review_regions or [DEFAULT_GLOBAL_REVIEW_REGION]
    plan = {
        "schema_version": SCHEMA_VERSION,
        "workflow_id": workflow_id,
        "goal": goal,
        "visual_brief": str(payload.get("visual_brief") or ""),
        "stages": stages,
        "research_policy": {
            "required_for_modifications": True,
            "sources": ["web_search", "local_rag", "current_preview", "reference_images"],
            "must_record_findings": True,
            "reuse_allowed_when_recent": True,
            "max_age_hours": 24,
            "notes": (
                "Before Photoshop modifications, Codex must gather current technique/style references "
                "from web search when possible and local RAG, then record actionable findings in the workflow."
            ),
        },
        "review_policy": {
            "must_review_each_stage": True,
            "default_global_preview": True,
            "default_region_review": True,
            "max_execution_stages": MAX_EXECUTION_STAGES,
            "must_recheck_previous_stage_before_next": True,
            "allow_backward_stage_revision": True,
        },
        "rollback_policy": {
            "default": "delete_stage_group",
            "delete_only_failed_stage": True,
            "prefer_group_delete_over_history_undo": True,
            "rollback_dependencies_when_later_stage_exposes_issue": True,
        },
        "strategy_rules": {
            "codex_is_strategy_owner": True,
            "primitives_are_not_presets": True,
            "feedback_path": "workflow_id -> stage_id -> step_id/primitive_id/capability_id",
        },
    }
    validation = validate_workflow_plan({"workflow_plan": plan})
    return {
        "status": "ok" if validation["valid"] else "error",
        "schema_version": SCHEMA_VERSION,
        "workflow_plan": plan,
        "validation": validation,
    }


def validate_stage(stage: Any, index: int, errors: list[str], warnings: list[str]) -> None:
    path = f"workflow_plan.stages[{index}]"
    if not isinstance(stage, dict):
        errors.append(f"{path} must be an object")
        return
    stage_id = stage.get("stage_id")
    if not isinstance(stage_id, str) or not STAGE_ID_RE.match(stage_id):
        errors.append(f"{path}.stage_id must match {STAGE_ID_RE.pattern}")
    for key in ("objective", "expected_result"):
        if not isinstance(stage.get(key), str) or not stage.get(key, "").strip():
            errors.append(f"{path}.{key} must be a non-empty string")
    pass_criteria = stage.get("pass_criteria")
    if not isinstance(pass_criteria, list) or not pass_criteria:
        errors.append(f"{path}.pass_criteria must be a non-empty array")
    rollback = stage.get("rollback_target")
    if not isinstance(rollback, dict) or rollback.get("type") not in {"stage_group", "job_group", "none"}:
        errors.append(f"{path}.rollback_target.type must be stage_group, job_group, or none")
    review_regions = stage.get("review_regions")
    if "route" in stage and (not isinstance(stage.get("route"), str) or not stage.get("route", "").strip()):
        errors.append(f"{path}.route must be a non-empty string when provided")
    validate_photo_effect_stage(stage, path, errors, warnings)
    modifies = bool(_stage_executable_payload(stage)) or stage.get("stage_type") not in LOW_RISK_STAGE_TYPES
    requires_review = bool(stage.get("requires_review", modifies))
    if requires_review and not isinstance(review_regions, list):
        errors.append(f"{path}.review_regions must be an array")
    if requires_review and isinstance(review_regions, list) and not review_regions:
        errors.append(f"{path}.review_regions must not be empty for a reviewable stage")
    if modifies:
        pre_stage_checks = stage.get("pre_stage_checks")
        if not isinstance(pre_stage_checks, list) or not pre_stage_checks:
            errors.append(f"{path}.pre_stage_checks must be a non-empty array for modification stages")
        previous_recheck = stage.get("previous_stage_recheck")
        if not isinstance(previous_recheck, dict):
            errors.append(f"{path}.previous_stage_recheck must be an object for modification stages")
        else:
            if previous_recheck.get("required") is not True and index > 0:
                errors.append(f"{path}.previous_stage_recheck.required must be true after the first stage")
            if previous_recheck.get("if_failed") not in {"rollback_dependency", "revise_dependency", "stop"}:
                errors.append(f"{path}.previous_stage_recheck.if_failed must be rollback_dependency, revise_dependency, or stop")
    if stage.get("destructive") and not stage.get("user_confirmed"):
        errors.append(f"{path} is destructive and requires user_confirmed=true")
    if stage.get("raw_descriptor") and not (stage.get("advanced") and stage.get("user_confirmed")):
        errors.append(f"{path} raw descriptor stage requires advanced=true and user_confirmed=true")


def validate_workflow_plan(payload: dict[str, Any]) -> dict[str, Any]:
    plan = payload.get("workflow_plan") or payload.get("workflow")
    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(plan, dict):
        errors.append("workflow_plan must be an object")
    else:
        if plan.get("schema_version") != SCHEMA_VERSION:
            errors.append("workflow_plan.schema_version must be ps-agent/v1")
        workflow_id = plan.get("workflow_id")
        if not isinstance(workflow_id, str) or not WORKFLOW_ID_RE.match(workflow_id):
            errors.append(f"workflow_plan.workflow_id must match {WORKFLOW_ID_RE.pattern}")
        if not isinstance(plan.get("goal"), str) or not plan.get("goal", "").strip():
            errors.append("workflow_plan.goal must be a non-empty string")
        research_policy = plan.get("research_policy")
        if not isinstance(research_policy, dict):
            errors.append("workflow_plan.research_policy must be an object")
        else:
            if research_policy.get("required_for_modifications") is not True:
                errors.append("workflow_plan.research_policy.required_for_modifications must be true")
            sources = research_policy.get("sources")
            if not isinstance(sources, list) or not sources:
                errors.append("workflow_plan.research_policy.sources must be a non-empty array")
            if research_policy.get("must_record_findings") is not True:
                errors.append("workflow_plan.research_policy.must_record_findings must be true")
        stages = plan.get("stages")
        if not isinstance(stages, list) or not stages:
            errors.append("workflow_plan.stages must be a non-empty array")
        else:
            execution_stages = [stage for stage in stages if isinstance(stage, dict) and stage.get("stage_type") not in LOW_RISK_STAGE_TYPES]
            if len(execution_stages) > MAX_EXECUTION_STAGES and not plan.get("allow_many_stages"):
                errors.append(f"workflow_plan has more than {MAX_EXECUTION_STAGES} execution stages")
            seen: set[str] = set()
            for index, stage in enumerate(stages):
                validate_stage(stage, index, errors, warnings)
                if isinstance(stage, dict):
                    stage_id = str(stage.get("stage_id") or "")
                    if stage_id in seen:
                        errors.append(f"Duplicate stage_id: {stage_id}")
                    seen.add(stage_id)
    return {
        "status": "ok",
        "schema_version": SCHEMA_VERSION,
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "summary": summarize_workflow(plan) if isinstance(plan, dict) else None,
    }


def summarize_workflow(plan: dict[str, Any]) -> dict[str, Any]:
    stages = plan.get("stages") if isinstance(plan.get("stages"), list) else []
    return {
        "workflow_id": plan.get("workflow_id"),
        "goal": plan.get("goal"),
        "stage_count": len(stages),
        "stage_ids": [stage.get("stage_id") for stage in stages if isinstance(stage, dict)],
        "routes": [stage.get("route") for stage in stages if isinstance(stage, dict) and stage.get("route")],
    }


def find_stage(plan: dict[str, Any], stage_id: str) -> dict[str, Any] | None:
    for stage in plan.get("stages", []):
        if isinstance(stage, dict) and stage.get("stage_id") == stage_id:
            return stage
    return None


def stage_group_name(workflow_id: str, stage_id: str, job_id: str | None = None) -> str:
    suffix = f" - {job_id}" if job_id else ""
    return f"Codex Agent - {workflow_id} - {stage_id}{suffix}"[:120]


def review_workflow_stage(payload: dict[str, Any]) -> dict[str, Any]:
    plan = payload.get("workflow_plan") or payload.get("workflow")
    stage_id = str(payload.get("stage_id") or "")
    validation = validate_workflow_plan({"workflow_plan": plan})
    if not validation["valid"]:
        return _error("invalid_workflow_plan", "Workflow validation failed.", validation["errors"])
    stage = find_stage(plan, stage_id)
    if not stage:
        return _error("stage_not_found", f"Unknown stage_id: {stage_id}")
    return {
        "status": "ok",
        "schema_version": SCHEMA_VERSION,
        "workflow_id": plan.get("workflow_id"),
        "stage_id": stage_id,
        "stage_status": "needs_visual_review",
        "objective": stage.get("objective"),
        "expected_result": stage.get("expected_result"),
        "pass_criteria": stage.get("pass_criteria", []),
        "review_regions": stage.get("review_regions", []),
        "feedback_mapping": {
            "too_strong": "lower the responsible step opacity/strength",
            "color_pollution": "add or strengthen local protection before rerunning this stage",
            "mask_error": "rerun mask_preparation or replace the stage mask",
            "cross_stage_regression": "delete this stage group and revise only this stage",
        },
    }


def finalize_workflow_review(payload: dict[str, Any]) -> dict[str, Any]:
    plan = payload.get("workflow_plan") or payload.get("workflow")
    validation = validate_workflow_plan({"workflow_plan": plan})
    if not validation["valid"]:
        return _error("invalid_workflow_plan", "Workflow validation failed.", validation["errors"])
    stages = plan.get("stages", [])
    return {
        "status": "ok",
        "schema_version": SCHEMA_VERSION,
        "workflow_id": plan.get("workflow_id"),
        "final_status": "needs_visual_review",
        "stage_review_order": [stage.get("stage_id") for stage in stages if isinstance(stage, dict)],
        "checks": [
            "Global result matches the user goal.",
            "Accepted earlier stages were not polluted by later stages.",
            "All declared review regions pass visual inspection.",
            "Failed stages are deleted or revised before final acceptance.",
        ],
    }
