from __future__ import annotations

from copy import deepcopy
from typing import Any


SCHEMA_VERSION = "ps-agent/v1"


CAPABILITIES: list[dict[str, Any]] = [
    {
        "id": "document.get_state",
        "category": "document",
        "status": "stable",
        "transport": "existing_tool",
        "tool": "ps_get_state",
        "modifies_document": False,
        "requires_active_document": True,
        "destructive": False,
        "requires_user_confirmation": False,
        "summary": "Read active document state and layer metadata.",
        "params_schema": {"type": "object", "additionalProperties": True},
    },
    {
        "id": "preview.export_global",
        "category": "preview",
        "status": "stable",
        "transport": "existing_tool",
        "tool": "ps_export_preview",
        "modifies_document": False,
        "requires_active_document": True,
        "destructive": False,
        "requires_user_confirmation": False,
        "summary": "Export a downscaled full-document preview.",
        "params_schema": {"type": "object", "additionalProperties": True},
    },
    {
        "id": "preview.export_regions",
        "category": "preview",
        "status": "stable",
        "transport": "existing_tool",
        "tool": "ps_export_regions",
        "modifies_document": False,
        "requires_active_document": True,
        "destructive": False,
        "requires_user_confirmation": False,
        "summary": "Export local review crops.",
        "params_schema": {"type": "object", "additionalProperties": True},
    },
    {
        "id": "selection.make_mask",
        "category": "selection",
        "status": "stable",
        "transport": "existing_tool",
        "tool": "ps_make_selection",
        "modifies_document": True,
        "requires_active_document": True,
        "destructive": False,
        "requires_user_confirmation": False,
        "summary": "Create a Photoshop selection from bbox, polygon, composite, or alpha mask.",
        "params_schema": {"type": "object", "required": ["selection_mask"], "additionalProperties": True},
    },
    {
        "id": "selection.native_command",
        "category": "selection",
        "status": "stable",
        "transport": "existing_tool",
        "tool": "ps_selection_command",
        "modifies_document": True,
        "requires_active_document": True,
        "destructive": False,
        "requires_user_confirmation": False,
        "summary": "Run whitelisted Photoshop native selection commands.",
        "params_schema": {"type": "object", "required": ["action"], "additionalProperties": True},
    },
    {
        "id": "adjustment.apply_plan",
        "category": "adjustment",
        "status": "stable",
        "transport": "existing_tool",
        "tool": "ps_apply_plan",
        "modifies_document": True,
        "requires_active_document": True,
        "destructive": False,
        "requires_user_confirmation": True,
        "summary": "Apply non-destructive adjustment layers or Camera Raw smart filters.",
        "params_schema": {"type": "object", "required": ["plan"], "additionalProperties": True},
    },
    {
        "id": "recipe.apply_layer_recipe",
        "category": "recipe",
        "status": "stable",
        "transport": "existing_tool",
        "tool": "ps_apply_layer_recipe",
        "modifies_document": True,
        "requires_active_document": True,
        "destructive": False,
        "requires_user_confirmation": True,
        "summary": "Apply an effect primitive layer recipe through the validated plan path.",
        "params_schema": {"type": "object", "required": ["layer_recipe"], "additionalProperties": True},
    },
    {
        "id": "layer.create_group",
        "category": "layer",
        "status": "stable",
        "transport": "uxp_job",
        "job_type": "execute_capability",
        "modifies_document": True,
        "requires_active_document": True,
        "destructive": False,
        "requires_user_confirmation": False,
        "summary": "Create a named layer group.",
        "params_schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
    },
    {
        "id": "layer.duplicate_active",
        "category": "layer",
        "status": "stable",
        "transport": "uxp_job",
        "job_type": "execute_capability",
        "modifies_document": True,
        "requires_active_document": True,
        "destructive": False,
        "requires_user_confirmation": False,
        "summary": "Duplicate the active layer and optionally set name, opacity, and blend mode.",
        "params_schema": {"type": "object", "additionalProperties": True},
    },
    {
        "id": "filter.gaussian_blur_duplicate",
        "category": "filter",
        "status": "calibrated_batchplay",
        "transport": "uxp_job",
        "job_type": "execute_capability",
        "modifies_document": True,
        "requires_active_document": True,
        "destructive": False,
        "requires_user_confirmation": False,
        "summary": "Duplicate the active layer, apply Gaussian Blur to the duplicate, then set blend/opacity.",
        "params_schema": {"type": "object", "additionalProperties": True},
    },
    {
        "id": "descriptor.raw_batchplay",
        "category": "advanced",
        "status": "raw_advanced",
        "transport": "uxp_job",
        "job_type": "execute_capability",
        "modifies_document": True,
        "requires_active_document": False,
        "destructive": True,
        "requires_user_confirmation": True,
        "summary": "Execute an advanced user-provided batchPlay descriptor list.",
        "params_schema": {"type": "object", "required": ["descriptors"], "additionalProperties": True},
    },
]

CAPABILITY_BY_ID = {item["id"]: item for item in CAPABILITIES}
DESTRUCTIVE_IDS = {item["id"] for item in CAPABILITIES if item.get("destructive")}


def _error(code: str, message: str, details: Any | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "status": "error",
        "schema_version": SCHEMA_VERSION,
        "error": {"code": code, "message": message},
    }
    if details is not None:
        payload["error"]["details"] = details
    return payload


def _filtered_capabilities(payload: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    payload = payload or {}
    category = payload.get("category")
    status = payload.get("status")
    include_advanced = bool(payload.get("include_advanced", False))
    items = []
    for item in CAPABILITIES:
        if category and item.get("category") != category:
            continue
        if status and item.get("status") != status:
            continue
        if item.get("status") == "raw_advanced" and not include_advanced:
            continue
        items.append(deepcopy(item))
    return items


def list_capabilities(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    items = _filtered_capabilities(payload)
    categories: dict[str, int] = {}
    statuses: dict[str, int] = {}
    for item in CAPABILITIES:
        categories[item["category"]] = categories.get(item["category"], 0) + 1
        statuses[item["status"]] = statuses.get(item["status"], 0) + 1
    return {
        "status": "ok",
        "schema_version": SCHEMA_VERSION,
        "capability_count": len(CAPABILITIES),
        "returned_count": len(items),
        "categories": categories,
        "statuses": statuses,
        "capabilities": items,
    }


def probe_capability(payload: dict[str, Any]) -> dict[str, Any]:
    capability_id = str(payload.get("capability_id") or payload.get("id") or "")
    capability = CAPABILITY_BY_ID.get(capability_id)
    if not capability:
        return _error("unknown_capability", f"Unknown capability_id: {capability_id}")
    result = deepcopy(capability)
    result["available"] = capability.get("status") in {"stable", "calibrated_batchplay", "raw_advanced"}
    result["notes"] = []
    if capability.get("status") == "raw_advanced":
        result["notes"].append("Requires user_confirmed=true and risk_acknowledged=true.")
    if capability.get("status") in {"experimental", "interactive_required"}:
        result["available"] = False
    return {"status": "ok", "schema_version": SCHEMA_VERSION, "capability": result}


def validate_capability_call(payload: dict[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    capability_id = str(payload.get("capability_id") or payload.get("id") or "")
    capability = CAPABILITY_BY_ID.get(capability_id)
    params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
    dry_run = bool(payload.get("dry_run", False))

    if not capability:
        errors.append(f"Unknown capability_id: {capability_id}")
    else:
        if capability.get("status") in {"experimental", "interactive_required"}:
            warnings.append(f"Capability {capability_id} is {capability.get('status')}.")
        if capability.get("destructive") and not payload.get("user_confirmed"):
            errors.append("Destructive capability requires user_confirmed=true.")
        if capability.get("requires_user_confirmation") and not (payload.get("user_confirmed") or dry_run):
            errors.append("Capability requires user_confirmed=true unless dry_run=true.")
        if capability.get("status") == "raw_advanced":
            descriptors = params.get("descriptors")
            if not isinstance(descriptors, list) or not descriptors:
                errors.append("descriptor.raw_batchplay params.descriptors must be a non-empty array.")
            if not payload.get("risk_acknowledged"):
                errors.append("raw_advanced capability requires risk_acknowledged=true.")

    return {
        "status": "ok",
        "schema_version": SCHEMA_VERSION,
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "capability": deepcopy(capability) if capability else None,
        "summary": {
            "capability_id": capability_id,
            "dry_run": dry_run,
            "destructive": bool(capability and capability.get("destructive")),
            "transport": capability.get("transport") if capability else None,
        },
    }


def dry_run_capability_call(payload: dict[str, Any]) -> dict[str, Any]:
    validation = validate_capability_call(dict(payload, dry_run=True))
    if not validation["valid"]:
        return validation
    capability = validation["capability"]
    return {
        "status": "ok",
        "schema_version": SCHEMA_VERSION,
        "dry_run": True,
        "valid": True,
        "capability_id": validation["summary"]["capability_id"],
        "capability": capability,
        "execution_summary": {
            "transport": capability.get("transport"),
            "job_type": capability.get("job_type"),
            "tool": capability.get("tool"),
            "modifies_document": capability.get("modifies_document"),
            "destructive": capability.get("destructive"),
        },
        "warnings": validation["warnings"],
    }
