from __future__ import annotations

import re
from copy import deepcopy
from typing import Any


SCHEMA_VERSION = "ps-agent/v1"
MAX_CANDIDATES = 16
MAX_MERGE_ITEMS = 24
CANDIDATE_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
HARD_BUS = "hard_selection"
SOFT_BUS = "soft_alpha"
GENERATOR_BUS = "native_alpha_generator"
SELECTION_OPERATIONS = {"replace", "add", "subtract", "intersect"}


SELECTION_ATOMS: list[dict[str, Any]] = [
    {
        "atom_id": "selection.select_subject",
        "category": "native",
        "bus": GENERATOR_BUS,
        "status": "stable",
        "summary": "Photoshop native Select Subject captured immediately as a reusable soft alpha artifact.",
        "candidate_roles": ["base", "protect"],
        "params_schema": {"type": "object", "additionalProperties": True},
        "returns": ["alpha_mask", "artifact"],
        "failure_codes": ["select_subject_unavailable", "selection_empty", "modal_busy"],
    },
    {
        "atom_id": "selection.select_sky",
        "category": "native",
        "bus": GENERATOR_BUS,
        "status": "stable",
        "summary": "Photoshop native Select Sky captured immediately as a reusable soft alpha artifact.",
        "candidate_roles": ["base"],
        "params_schema": {"type": "object", "additionalProperties": True},
        "returns": ["alpha_mask", "artifact"],
        "failure_codes": ["select_sky_unavailable", "selection_empty", "modal_busy"],
    },
    {
        "atom_id": "selection.color_range",
        "category": "native",
        "bus": GENERATOR_BUS,
        "status": "calibrated_batchplay",
        "summary": "Photoshop Color Range generator captured immediately as a reusable soft alpha artifact.",
        "candidate_roles": ["base", "protect"],
        "params_schema": {"type": "object", "properties": {"preset": {"type": "string"}, "color": {"type": "object"}, "fuzziness": {"type": "number"}}},
        "seed_profiles": ["sampled", "reds", "yellows", "greens", "cyans", "blues", "magentas", "skin_tones", "highlights", "midtones", "shadows"],
        "feedback_tuning": ["fuzziness", "localized_color_clusters", "sampled color"],
        "returns": ["alpha_mask", "artifact"],
        "failure_codes": ["color_range_descriptor_unavailable", "selection_empty", "modal_busy"],
    },
    {
        "atom_id": "selection.tonal_range",
        "category": "native",
        "bus": GENERATOR_BUS,
        "status": "stable",
        "summary": "Photoshop tonal Color Range generator for highlights, midtones, or shadows captured as soft alpha.",
        "candidate_roles": ["base", "protect"],
        "params_schema": {"type": "object", "properties": {"preset": {"type": "string", "enum": ["highlights", "midtones", "shadows"]}, "fuzziness": {"type": "number"}}},
        "seed_profiles": ["lights_seed", "midtones_seed", "darks_seed"],
        "feedback_tuning": ["preset", "fuzziness", "feather"],
        "returns": ["alpha_mask", "artifact"],
        "failure_codes": ["selection_empty", "modal_busy"],
    },
    {
        "atom_id": "selection.focus_area",
        "category": "native",
        "bus": GENERATOR_BUS,
        "status": "calibrated_batchplay",
        "summary": "Photoshop Focus Area generator captured immediately as a reusable soft alpha artifact.",
        "candidate_roles": ["base", "protect"],
        "params_schema": {"type": "object", "properties": {"in_focus_range": {"type": "number"}, "noise_level": {"type": "number"}}},
        "returns": ["alpha_mask", "artifact"],
        "failure_codes": ["focus_area_descriptor_unavailable", "selection_empty", "modal_busy"],
    },
    {
        "atom_id": "selection.object_selection",
        "category": "native",
        "bus": HARD_BUS,
        "status": "calibrated_batchplay",
        "summary": "Reviewed object-selection seed from bbox or polygon. Native Photoshop AI Object Selection descriptor is not claimed; this atom executes the reviewed geometry reliably.",
        "candidate_roles": ["base", "add", "subtract", "intersect", "fallback"],
        "params_schema": {"type": "object", "properties": {"bbox": {"type": "object"}, "points": {"type": "array"}, "mode": {"type": "string"}}},
        "returns": ["channel_name", "has_active_selection"],
        "failure_codes": ["invalid_object_selection_prompt", "selection_empty", "modal_busy"],
    },
    {
        "atom_id": "selection.current",
        "category": "native",
        "bus": HARD_BUS,
        "status": "stable",
        "summary": "Use the current Photoshop active selection.",
        "candidate_roles": ["base", "add", "subtract", "intersect"],
        "params_schema": {"type": "object", "additionalProperties": True},
        "returns": ["channel_name", "has_active_selection"],
        "failure_codes": ["no_active_selection"],
    },
    {
        "atom_id": "selection.bbox",
        "category": "geometry",
        "bus": HARD_BUS,
        "status": "stable",
        "summary": "Create a rectangular selection from document coordinates.",
        "candidate_roles": ["base", "add", "subtract", "intersect", "fallback"],
        "params_schema": {"type": "object", "required": ["bbox"], "properties": {"bbox": {"type": "object"}}},
        "returns": ["channel_name", "has_active_selection"],
        "failure_codes": ["invalid_selection_bbox", "selection_empty", "modal_busy"],
    },
    {
        "atom_id": "selection.polygon",
        "category": "geometry",
        "bus": HARD_BUS,
        "status": "stable",
        "summary": "Create a polygon/lasso-like selection from document coordinates.",
        "candidate_roles": ["base", "add", "subtract", "intersect", "fallback"],
        "params_schema": {"type": "object", "required": ["points"], "properties": {"points": {"type": "array"}}},
        "returns": ["channel_name", "has_active_selection"],
        "failure_codes": ["invalid_polygon_points", "selection_empty", "modal_busy"],
    },
    {
        "atom_id": "selection.alpha_mask",
        "category": "channel_alpha",
        "bus": SOFT_BUS,
        "status": "stable",
        "summary": "Use a full-document alpha mask PNG generated by backend or a local model.",
        "candidate_roles": ["base", "add", "subtract", "intersect", "protect"],
        "params_schema": {"type": "object", "properties": {"asset_path": {"type": "string"}, "asset_uri": {"type": "string"}, "threshold": {"type": "number"}, "feather": {"type": "number"}}},
        "returns": ["alpha_mask", "mask_asset", "area_ratio"],
        "failure_codes": ["alpha_mask_asset_missing", "alpha_mask_empty"],
    },
    {
        "atom_id": "selection.face_landmarker",
        "category": "local_model",
        "bus": HARD_BUS,
        "status": "existing_tool",
        "summary": "Use ps_generate_face_selection to create face-part polygons, then lower to selection.polygon.",
        "candidate_roles": ["base", "add", "subtract", "intersect", "protect"],
        "params_schema": {"type": "object", "additionalProperties": True},
        "returns": ["points", "bbox"],
        "failure_codes": ["face_landmarker_unavailable"],
    },
    {
        "atom_id": "selection.sam_mask",
        "category": "local_model",
        "bus": SOFT_BUS,
        "status": "existing_tool",
        "summary": "Use ps_generate_sam_mask to generate an alpha mask candidate.",
        "candidate_roles": ["base", "add", "subtract", "intersect", "protect"],
        "params_schema": {"type": "object", "additionalProperties": True},
        "returns": ["alpha_mask", "overlay_preview"],
        "failure_codes": ["sam_worker_unavailable", "mask_empty"],
    },
    {
        "atom_id": "selection.hqsam_mask",
        "category": "local_model",
        "bus": SOFT_BUS,
        "status": "existing_tool",
        "summary": "Use ps_generate_hqsam_mask to generate an alpha mask candidate.",
        "candidate_roles": ["base", "add", "subtract", "intersect", "protect"],
        "params_schema": {"type": "object", "additionalProperties": True},
        "returns": ["alpha_mask", "overlay_preview"],
        "failure_codes": ["grounding_hq_worker_unavailable", "mask_empty"],
    },
    {
        "atom_id": "selection.grounding_dino_boxes",
        "category": "local_model",
        "bus": "detector",
        "status": "existing_tool",
        "summary": "Use ps_detect_grounding_boxes to create bbox candidates before segmentation.",
        "candidate_roles": ["detect"],
        "params_schema": {"type": "object", "additionalProperties": True},
        "returns": ["detections", "overlay_preview"],
        "failure_codes": ["grounding_hq_worker_unavailable", "no_detections"],
    },
    {
        "atom_id": "selection.grounded_hqsam_mask",
        "category": "local_model",
        "bus": SOFT_BUS,
        "status": "existing_tool",
        "summary": "Use Grounding DINO + HQ-SAM to generate semantic soft alpha masks.",
        "candidate_roles": ["base", "add", "subtract", "intersect", "protect"],
        "params_schema": {"type": "object", "additionalProperties": True},
        "returns": ["alpha_mask", "detections", "overlay_preview"],
        "failure_codes": ["grounding_hq_worker_unavailable", "mask_empty"],
    },
    {
        "atom_id": "selection.channel_load",
        "category": "channel_alpha",
        "bus": HARD_BUS,
        "status": "stable",
        "summary": "Load an existing alpha channel as a hard Photoshop selection.",
        "candidate_roles": ["base", "add", "subtract", "intersect"],
        "params_schema": {"type": "object", "required": ["channel_name"], "properties": {"channel_name": {"type": "string"}}},
        "returns": ["channel_name", "has_active_selection"],
        "failure_codes": ["load_selection_channel_failed", "selection_empty"],
    },
    {
        "atom_id": "selection.refine",
        "category": "refinement",
        "bus": HARD_BUS,
        "status": "stable",
        "summary": "Apply feather, expand, contract, smooth, border, or inverse to the active selection.",
        "candidate_roles": ["refine"],
        "params_schema": {"type": "object", "properties": {"operation": {"type": "string"}, "amount": {"type": "number"}, "invert": {"type": "boolean"}}},
        "returns": ["has_active_selection"],
        "failure_codes": ["selection_command_failed", "no_active_selection"],
    },
    {
        "atom_id": "selection.refine_edge",
        "category": "refinement",
        "bus": HARD_BUS,
        "status": "stable",
        "summary": "Refine the active selection with ordered smooth/expand/contract/feather/border/invert commands.",
        "candidate_roles": ["refine"],
        "params_schema": {
            "type": "object",
            "properties": {
                "smooth": {"type": "number"},
                "expand": {"type": "number"},
                "contract": {"type": "number"},
                "feather": {"type": "number"},
                "border": {"type": "number"},
                "invert": {"type": "boolean"},
            },
        },
        "returns": ["has_active_selection"],
        "failure_codes": ["selection_command_failed", "no_active_selection"],
    },
]

ATOM_BY_ID = {atom["atom_id"]: atom for atom in SELECTION_ATOMS}


def _error(code: str, message: str, details: Any | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "status": "error",
        "schema_version": SCHEMA_VERSION,
        "error": {"code": code, "message": message},
    }
    if details is not None:
        payload["error"]["details"] = details
    return payload


def list_selection_atoms(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = payload or {}
    category = payload.get("category")
    bus = payload.get("bus")
    include_details = bool(payload.get("include_details", True))
    atoms = [
        deepcopy(atom)
        for atom in SELECTION_ATOMS
        if (not category or atom.get("category") == category) and (not bus or atom.get("bus") == bus)
    ]
    if not include_details:
        atoms = [
            {
                "atom_id": atom["atom_id"],
                "category": atom["category"],
                "bus": atom["bus"],
                "status": atom["status"],
                "summary": atom["summary"],
            }
            for atom in atoms
        ]
    categories: dict[str, int] = {}
    buses: dict[str, int] = {}
    for atom in SELECTION_ATOMS:
        categories[atom["category"]] = categories.get(atom["category"], 0) + 1
        buses[atom["bus"]] = buses.get(atom["bus"], 0) + 1
    return {
        "status": "ok",
        "schema_version": SCHEMA_VERSION,
        "atom_count": len(SELECTION_ATOMS),
        "returned_count": len(atoms),
        "categories": categories,
        "buses": buses,
        "atoms": atoms,
        "rules": {
            "hard_selection": "Photoshop selections can use replace/add/subtract/intersect and refinement commands.",
            "soft_alpha": "Soft masks must be composited in backend before Photoshop receives one final alpha mask.",
            "strategy": "Do not hard-route object classes to a single atom; trial candidates and review overlays/crops first.",
        },
    }


def probe_selection_atom(payload: dict[str, Any]) -> dict[str, Any]:
    atom_id = str(payload.get("atom_id") or payload.get("id") or "")
    atom = ATOM_BY_ID.get(atom_id)
    if not atom:
        return _error("unknown_selection_atom", f"Unknown selection atom: {atom_id}")
    return {"status": "ok", "schema_version": SCHEMA_VERSION, "atom": deepcopy(atom), "available": atom.get("status") != "planned"}


def _candidate_bus(candidate: dict[str, Any]) -> str | None:
    atom = ATOM_BY_ID.get(str(candidate.get("atom_id") or ""))
    return str(atom.get("bus")) if atom else None


def validate_selection_recipe(payload: dict[str, Any]) -> dict[str, Any]:
    recipe = payload.get("selection_recipe") or payload.get("recipe")
    errors: list[str] = []
    warnings: list[str] = []
    candidates_by_id: dict[str, dict[str, Any]] = {}

    if not isinstance(recipe, dict):
        errors.append("selection_recipe must be an object")
    else:
        if recipe.get("schema_version") != SCHEMA_VERSION:
            errors.append("selection_recipe.schema_version must be ps-agent/v1")
        if not str(recipe.get("goal") or "").strip():
            errors.append("selection_recipe.goal must be a non-empty string")
        candidates = recipe.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            errors.append("selection_recipe.candidates must be a non-empty array")
        elif len(candidates) > MAX_CANDIDATES:
            errors.append(f"selection_recipe.candidates must contain at most {MAX_CANDIDATES} candidates")
        else:
            for index, candidate in enumerate(candidates):
                path = f"selection_recipe.candidates[{index}]"
                if not isinstance(candidate, dict):
                    errors.append(f"{path} must be an object")
                    continue
                candidate_id = str(candidate.get("candidate_id") or "")
                if not CANDIDATE_ID_RE.match(candidate_id):
                    errors.append(f"{path}.candidate_id must match {CANDIDATE_ID_RE.pattern}")
                elif candidate_id in candidates_by_id:
                    errors.append(f"Duplicate candidate_id: {candidate_id}")
                else:
                    candidates_by_id[candidate_id] = candidate
                atom_id = str(candidate.get("atom_id") or "")
                atom = ATOM_BY_ID.get(atom_id)
                if not atom:
                    errors.append(f"{path}.atom_id is unknown: {atom_id}")
                    continue
                if atom.get("bus") == "detector":
                    warnings.append(f"{path} is a detector atom; lower detections to bbox or alpha-mask candidates before merging.")
                if atom.get("bus") == GENERATOR_BUS:
                    errors.append(f"{path}.atom_id={atom_id} is a native alpha generator, not an executable selection_recipe atom; call the dedicated tool first and merge the returned alpha_mask instead")
                params = candidate.get("params")
                if params is not None and not isinstance(params, dict):
                    errors.append(f"{path}.params must be an object when provided")
                if atom_id == "selection.color_range":
                    params = params or {}
                    if not params.get("preset") and not params.get("color") and not params.get("seed_profile"):
                        errors.append(f"{path}.params must include preset, color, or seed_profile")
                    if params.get("seed_profile") and params.get("fuzziness") is None:
                        warnings.append(f"{path} uses a seed_profile; include fuzziness so Codex can tune from a visible starting point.")
                if atom_id == "selection.tonal_range":
                    params = params or {}
                    if params.get("preset") not in {"highlights", "midtones", "shadows"} and not params.get("seed_profile"):
                        errors.append(f"{path}.params.preset must be highlights, midtones, or shadows, unless seed_profile is supplied")

        merge_plan = recipe.get("merge_plan")
        if not isinstance(merge_plan, dict):
            errors.append("selection_recipe.merge_plan must be an object")
        else:
            mode = merge_plan.get("mode")
            if mode not in {HARD_BUS, SOFT_BUS}:
                errors.append("selection_recipe.merge_plan.mode must be hard_selection or soft_alpha")
            items = merge_plan.get("items")
            if not isinstance(items, list) or not items:
                errors.append("selection_recipe.merge_plan.items must be a non-empty array")
            elif len(items) > MAX_MERGE_ITEMS:
                errors.append(f"selection_recipe.merge_plan.items must contain at most {MAX_MERGE_ITEMS} items")
            else:
                for index, item in enumerate(items):
                    path = f"selection_recipe.merge_plan.items[{index}]"
                    if not isinstance(item, dict):
                        errors.append(f"{path} must be an object")
                        continue
                    candidate_id = str(item.get("candidate_id") or "")
                    operation = str(item.get("operation") or ("replace" if index == 0 else "add"))
                    if candidate_id not in candidates_by_id:
                        errors.append(f"{path}.candidate_id is not defined in candidates")
                        continue
                    if operation not in SELECTION_OPERATIONS:
                        errors.append(f"{path}.operation must be replace, add, subtract, or intersect")
                    if index == 0 and operation != "replace":
                        errors.append(f"{path}.operation must be replace for deterministic base selection")
                    candidate_bus = _candidate_bus(candidates_by_id[candidate_id])
                    if mode == HARD_BUS and candidate_bus == SOFT_BUS:
                        errors.append(f"{path} uses soft alpha candidate {candidate_id} in hard_selection merge; composite soft masks in backend first")
                    if mode == SOFT_BUS and candidate_bus == HARD_BUS:
                        warnings.append(f"{path} uses hard candidate {candidate_id} in soft_alpha merge; convert it to alpha before compositing.")

        review = recipe.get("review")
        if not isinstance(review, dict):
            errors.append("selection_recipe.review must be an object")
        else:
            regions = review.get("regions")
            if not isinstance(regions, list) or not regions:
                errors.append("selection_recipe.review.regions must be a non-empty array")
            if review.get("overlay") is not True:
                warnings.append("selection_recipe.review.overlay should be true for mask trial review.")

    return {
        "status": "ok",
        "schema_version": SCHEMA_VERSION,
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "summary": summarize_selection_recipe(recipe) if isinstance(recipe, dict) else None,
    }


def summarize_selection_recipe(recipe: dict[str, Any]) -> dict[str, Any]:
    candidates = recipe.get("candidates") if isinstance(recipe.get("candidates"), list) else []
    merge_plan = recipe.get("merge_plan") if isinstance(recipe.get("merge_plan"), dict) else {}
    return {
        "recipe_id": recipe.get("recipe_id"),
        "goal": recipe.get("goal"),
        "stage_id": recipe.get("stage_id"),
        "workflow_id": recipe.get("workflow_id"),
        "candidate_count": len(candidates),
        "candidate_atom_ids": [candidate.get("atom_id") for candidate in candidates if isinstance(candidate, dict)],
        "merge_mode": merge_plan.get("mode"),
        "merge_item_count": len(merge_plan.get("items", [])) if isinstance(merge_plan.get("items"), list) else 0,
    }


def review_selection_recipe(payload: dict[str, Any]) -> dict[str, Any]:
    recipe = payload.get("selection_recipe") or payload.get("recipe")
    validation = validate_selection_recipe({"selection_recipe": recipe})
    if not validation["valid"]:
        return {
            "status": "error",
            "schema_version": SCHEMA_VERSION,
            "error": {
                "code": "invalid_selection_recipe",
                "message": "Selection recipe validation failed.",
                "details": validation["errors"],
            },
        }
    return {
        "status": "ok",
        "schema_version": SCHEMA_VERSION,
        "recipe_id": recipe.get("recipe_id"),
        "reviews": [
            {
                "candidate_id": candidate.get("candidate_id"),
                "atom_id": candidate.get("atom_id"),
                "status": "needs_overlay_and_crop_review",
                "review_regions": (recipe.get("review") or {}).get("regions", []),
                "suggested_feedback_mapping": {
                    "workflow_id": recipe.get("workflow_id"),
                    "stage_id": recipe.get("stage_id"),
                    "candidate_id": candidate.get("candidate_id"),
                    "atom_id": candidate.get("atom_id"),
                },
            }
            for candidate in recipe.get("candidates", [])
            if isinstance(candidate, dict)
        ],
    }
