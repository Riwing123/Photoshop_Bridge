from __future__ import annotations

import math
import re
import uuid
from copy import deepcopy
from typing import Any


SCHEMA_VERSION = "ps-agent/v1"
MAX_OPERATION_STEPS = 80
STEP_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
TARGET_REF_RE = re.compile(r"^\$steps\.(?:[A-Za-z0-9_.:-]+|\d+)\.[A-Za-z0-9_.:-]+$")


OPERATION_ATOMS: list[dict[str, Any]] = [
    {
        "atom_id": "layer.create_group",
        "category": "layer",
        "bus": "operation",
        "status": "stable",
        "summary": "Create a named Photoshop layer group.",
        "modifies_document": True,
        "requires_active_document": True,
        "destructive": False,
        "params_schema": {"type": "object", "required": ["name"], "properties": {"name": {"type": "string"}}},
        "returns": ["group_id", "group_name"],
        "failure_codes": ["no_active_document", "modal_busy", "operation_atom_failed"],
    },
    {
        "atom_id": "layer.duplicate",
        "category": "layer",
        "bus": "operation",
        "status": "stable",
        "summary": "Duplicate a source layer, or the active layer when source_layer_id is omitted.",
        "modifies_document": True,
        "requires_active_document": True,
        "destructive": False,
        "params_schema": {"type": "object", "properties": {"source_layer_id": {}, "name": {"type": "string"}}},
        "returns": ["layer_id", "layer_name"],
        "failure_codes": ["layer_not_found", "modal_busy", "operation_atom_failed"],
    },
    {
        "atom_id": "layer.select",
        "category": "layer",
        "bus": "operation",
        "status": "stable",
        "summary": "Select one or more layers by id.",
        "modifies_document": True,
        "requires_active_document": True,
        "destructive": False,
        "params_schema": {"type": "object", "properties": {"layer_id": {}, "layer_ids": {"type": "array"}}},
        "returns": ["active_layer_ids"],
        "failure_codes": ["layer_not_found", "modal_busy", "operation_atom_failed"],
    },
    {
        "atom_id": "layer.set_properties",
        "category": "layer",
        "bus": "operation",
        "status": "stable",
        "summary": "Set layer name, opacity, blend mode, or visibility.",
        "modifies_document": True,
        "requires_active_document": True,
        "destructive": False,
        "params_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "opacity": {"type": "number", "minimum": 0, "maximum": 100},
                "blend_mode": {"type": "string"},
                "visible": {"type": "boolean"},
            },
        },
        "returns": ["layer_id"],
        "failure_codes": ["layer_not_found", "modal_busy", "operation_atom_failed"],
    },
    {
        "atom_id": "layer.group",
        "category": "layer",
        "bus": "operation",
        "status": "stable",
        "summary": "Group existing layers into a named group.",
        "modifies_document": True,
        "requires_active_document": True,
        "destructive": False,
        "params_schema": {"type": "object", "required": ["name", "layer_ids"], "properties": {"name": {"type": "string"}, "layer_ids": {"type": "array"}}},
        "returns": ["group_id", "group_name"],
        "failure_codes": ["layer_not_found", "modal_busy", "operation_atom_failed"],
    },
    {
        "atom_id": "layer.delete",
        "category": "layer",
        "bus": "operation",
        "status": "stable",
        "summary": "Delete a specific layer or group by id.",
        "modifies_document": True,
        "requires_active_document": True,
        "destructive": False,
        "params_schema": {"type": "object", "required": ["layer_id"], "properties": {"layer_id": {}}},
        "returns": ["deleted_layer_id"],
        "failure_codes": ["layer_not_found", "modal_busy", "operation_atom_failed"],
    },
    {
        "atom_id": "filter.gaussian_blur",
        "category": "filter",
        "bus": "operation",
        "status": "calibrated_batchplay",
        "summary": "Apply Gaussian Blur to the target layer.",
        "modifies_document": True,
        "requires_active_document": True,
        "destructive": False,
        "params_schema": {"type": "object", "required": ["radius"], "properties": {"radius": {"type": "number", "minimum": 0.1, "maximum": 500}}},
        "returns": ["layer_id", "radius"],
        "failure_codes": ["layer_not_found", "modal_busy", "operation_atom_failed"],
    },
    {
        "atom_id": "adjustment.hue_saturation",
        "category": "adjustment",
        "bus": "operation",
        "status": "calibrated_batchplay",
        "summary": "Create a Hue/Saturation adjustment layer and optionally clip it directly to a target layer.",
        "modifies_document": True,
        "requires_active_document": True,
        "destructive": False,
        "params_schema": {
            "type": "object",
            "properties": {
                "target_layer_id": {},
                "name": {"type": "string"},
                "range": {"type": "string"},
                "hue": {"type": "number", "minimum": -180, "maximum": 180},
                "saturation": {"type": "number", "minimum": -100, "maximum": 100},
                "lightness": {"type": "number", "minimum": -100, "maximum": 100},
                "opacity": {"type": "number", "minimum": 0, "maximum": 100},
                "blend_mode": {"type": "string"},
                "clipping_mask": {"type": "boolean"},
            },
        },
        "returns": ["layer_id", "layer_name", "target_layer_id", "clipping_mask", "implementation"],
        "failure_codes": [
            "adjustment_layer_failed",
            "clipping_mask_failed",
            "clipping_mask_target_is_group",
            "layer_not_found",
            "modal_busy",
        ],
    },
    {
        "atom_id": "retouch.spot_heal_points",
        "category": "retouch",
        "bus": "operation",
        "status": "calibrated_batchplay",
        "summary": "Create a non-destructive duplicate retouch layer and run content-aware fill over Codex-provided blemish points.",
        "modifies_document": True,
        "requires_active_document": True,
        "destructive": False,
        "params_schema": {
            "type": "object",
            "required": ["points"],
            "properties": {
                "source_layer_id": {},
                "name": {"type": "string"},
                "points": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "x": {"type": "number"},
                            "y": {"type": "number"},
                            "radius": {"type": "number", "minimum": 1, "maximum": 500},
                            "width": {"type": "number"},
                            "height": {"type": "number"},
                            "label": {"type": "string"},
                        },
                    },
                },
                "feather": {"type": "number", "minimum": 0, "maximum": 200},
                "expand": {"type": "number", "minimum": 0, "maximum": 200},
                "duplicate_source": {"type": "boolean"},
                "rasterize_duplicate": {"type": "boolean"},
            },
        },
        "returns": ["layer_id", "layer_name", "point_count", "points"],
        "failure_codes": ["invalid_retouch_points", "content_aware_fill_failed", "modal_busy", "operation_atom_failed"],
    },
    {
        "atom_id": "retouch.content_aware_fill_selection",
        "category": "retouch",
        "bus": "operation",
        "status": "calibrated_batchplay",
        "summary": "Run content-aware fill on the active selection, preferably on a duplicated retouch layer.",
        "modifies_document": True,
        "requires_active_document": True,
        "destructive": False,
        "params_schema": {
            "type": "object",
            "properties": {
                "source_layer_id": {},
                "target_layer_id": {},
                "name": {"type": "string"},
                "duplicate_source": {"type": "boolean"},
                "rasterize_duplicate": {"type": "boolean"},
                "feather": {"type": "number", "minimum": 0, "maximum": 200},
                "expand": {"type": "number", "minimum": 0, "maximum": 200},
            },
        },
        "returns": ["layer_id", "layer_name", "used_active_selection"],
        "failure_codes": ["no_active_selection", "content_aware_fill_failed", "modal_busy", "operation_atom_failed"],
    },
    {
        "atom_id": "retouch.clone_patch",
        "category": "retouch",
        "bus": "operation",
        "status": "calibrated_batchplay",
        "summary": "Copy source texture patches to target positions as non-destructive patch layers.",
        "modifies_document": True,
        "requires_active_document": True,
        "destructive": False,
        "params_schema": {
            "type": "object",
            "properties": {
                "source_layer_id": {},
                "name": {"type": "string"},
                "patches": {"type": "array"},
                "source": {"type": "object"},
                "target": {"type": "object"},
                "radius": {"type": "number"},
                "feather": {"type": "number"},
                "opacity": {"type": "number", "minimum": 0, "maximum": 100},
                "blend_mode": {"type": "string"},
            },
        },
        "returns": ["layer_ids", "patch_count", "group_id"],
        "failure_codes": ["invalid_clone_patch", "clone_patch_failed", "modal_busy"],
    },
    {
        "atom_id": "retouch.healing_brush_points",
        "category": "retouch",
        "bus": "operation",
        "status": "calibrated_batchplay",
        "summary": "Healing-brush style point cleanup using reviewed points and content-aware fill on a duplicate layer.",
        "modifies_document": True,
        "requires_active_document": True,
        "destructive": False,
        "params_schema": {
            "type": "object",
            "required": ["points"],
            "properties": {
                "source_layer_id": {},
                "name": {"type": "string"},
                "points": {"type": "array"},
                "feather": {"type": "number", "minimum": 0, "maximum": 200},
                "expand": {"type": "number", "minimum": 0, "maximum": 200},
                "duplicate_source": {"type": "boolean"},
                "rasterize_duplicate": {"type": "boolean"},
            },
        },
        "returns": ["layer_id", "layer_name", "point_count", "points"],
        "failure_codes": ["invalid_retouch_points", "content_aware_fill_failed", "modal_busy", "operation_atom_failed"],
    },
    {
        "atom_id": "adjustment.create",
        "category": "adjustment",
        "bus": "operation",
        "status": "stable",
        "summary": "Create a non-destructive adjustment layer using the existing apply-plan adjustment descriptors.",
        "modifies_document": True,
        "requires_active_document": True,
        "destructive": False,
        "params_schema": {"type": "object", "required": ["op", "params"], "properties": {"op": {"type": "string"}, "params": {"type": "object"}, "target": {"type": "object"}, "layer": {"type": "object"}, "target_layer_id": {}, "clip_to_layer_id": {}, "clipping_mask": {"type": "boolean"}, "clip_to_target": {"type": "boolean"}}},
        "returns": ["layer_id", "op", "target_layer_id", "clipping_mask", "implementation"],
        "failure_codes": ["unsupported_adjustment", "clipping_mask_target_missing", "clipping_mask_target_is_group", "modal_busy", "operation_atom_failed"],
    },
    {
        "atom_id": "mask.apply_current_selection",
        "category": "mask",
        "bus": "operation",
        "status": "stable",
        "summary": "Apply the current Photoshop selection as a layer mask to the target layer.",
        "modifies_document": True,
        "requires_active_document": True,
        "destructive": False,
        "params_schema": {"type": "object", "properties": {"target_layer_id": {}}},
        "returns": ["layer_id", "mask_applied"],
        "failure_codes": ["no_active_selection", "layer_not_found", "modal_busy", "operation_atom_failed"],
    },
    {
        "atom_id": "mask.apply_alpha",
        "category": "mask",
        "bus": "operation",
        "status": "stable",
        "summary": "Load a full-document alpha mask PNG and apply it as a layer mask to the target layer.",
        "modifies_document": True,
        "requires_active_document": True,
        "destructive": False,
        "params_schema": {"type": "object", "required": ["selection_mask"], "properties": {"target_layer_id": {}, "selection_mask": {"type": "object"}}},
        "returns": ["layer_id", "mask_applied", "selection"],
        "failure_codes": ["alpha_mask_asset_missing", "selection_empty", "modal_busy", "operation_atom_failed"],
    },
    {
        "atom_id": "selection.clear",
        "category": "selection",
        "bus": "operation",
        "status": "stable",
        "summary": "Clear the active Photoshop selection.",
        "modifies_document": True,
        "requires_active_document": True,
        "destructive": False,
        "params_schema": {"type": "object", "additionalProperties": True},
        "returns": ["has_active_selection"],
        "failure_codes": ["modal_busy", "operation_atom_failed"],
    },
    {
        "atom_id": "document.create",
        "category": "document",
        "bus": "operation",
        "status": "calibrated_batchplay",
        "summary": "Create a new Photoshop document/canvas for design composition.",
        "modifies_document": True,
        "requires_active_document": False,
        "destructive": False,
        "params_schema": {
            "type": "object",
            "required": ["width", "height"],
            "properties": {
                "name": {"type": "string"},
                "width": {"type": "integer", "minimum": 16, "maximum": 30000},
                "height": {"type": "integer", "minimum": 16, "maximum": 30000},
                "resolution": {"type": "number"},
                "background": {"type": "object"},
            },
        },
        "returns": ["document"],
        "failure_codes": ["document_create_failed", "modal_busy"],
    },
    {
        "atom_id": "document.set_canvas_size",
        "category": "document",
        "bus": "operation",
        "status": "calibrated_batchplay",
        "summary": "Resize the active canvas without scaling layer pixels.",
        "modifies_document": True,
        "requires_active_document": True,
        "destructive": False,
        "params_schema": {
            "type": "object",
            "properties": {
                "width": {"type": "number", "minimum": 1, "maximum": 30000},
                "height": {"type": "number", "minimum": 1, "maximum": 30000},
                "anchor": {"type": "string"},
            },
        },
        "returns": ["document"],
        "failure_codes": ["canvas_resize_failed", "modal_busy"],
    },
    {
        "atom_id": "document.export",
        "category": "document",
        "bus": "operation",
        "status": "existing_tool",
        "summary": "Use ps_export_preview or ps_export_design_package for flattened deliverable export.",
        "modifies_document": False,
        "requires_active_document": True,
        "destructive": False,
        "params_schema": {"type": "object", "additionalProperties": True},
        "returns": ["asset_path", "asset_url"],
        "failure_codes": ["no_active_document"],
    },
    {
        "atom_id": "asset.place_embedded",
        "category": "asset",
        "bus": "operation",
        "status": "calibrated_batchplay",
        "summary": "Place a backend-served image asset as an embedded Photoshop layer.",
        "modifies_document": True,
        "requires_active_document": True,
        "destructive": False,
        "params_schema": {
            "type": "object",
            "properties": {
                "asset_uri": {"type": "string"},
                "asset_path": {"type": "string"},
                "name": {"type": "string"},
                "x": {"type": "number"},
                "y": {"type": "number"},
                "scale_x": {"type": "number"},
                "scale_y": {"type": "number"},
                "rotation": {"type": "number"},
            },
        },
        "returns": ["layer_id", "layer_name", "asset_uri"],
        "failure_codes": ["asset_download_failed", "asset_place_failed", "modal_busy"],
    },
    {
        "atom_id": "asset.replace_contents",
        "category": "asset",
        "bus": "operation",
        "status": "calibrated_batchplay",
        "summary": "Replace the contents of a selected smart object with another asset.",
        "modifies_document": True,
        "requires_active_document": True,
        "destructive": False,
        "params_schema": {"type": "object", "properties": {"layer_id": {}, "asset_uri": {"type": "string"}, "asset_path": {"type": "string"}}},
        "returns": ["layer_id"],
        "failure_codes": ["asset_replace_failed", "modal_busy"],
    },
    {
        "atom_id": "layer.move_to_top",
        "category": "layer",
        "bus": "operation",
        "status": "calibrated_batchplay",
        "summary": "Move a layer to the top of the layer stack.",
        "modifies_document": True,
        "requires_active_document": True,
        "destructive": False,
        "params_schema": {"type": "object", "properties": {"layer_id": {}}},
        "returns": ["layer_id"],
        "failure_codes": ["layer_move_failed", "modal_busy"],
    },
    {
        "atom_id": "layer.move_above",
        "category": "layer",
        "bus": "operation",
        "status": "calibrated_batchplay",
        "summary": "Move a layer above another layer.",
        "modifies_document": True,
        "requires_active_document": True,
        "destructive": False,
        "params_schema": {"type": "object", "properties": {"layer_id": {}, "reference_layer_id": {}}},
        "returns": ["layer_id"],
        "failure_codes": ["layer_move_failed", "modal_busy"],
    },
    {
        "atom_id": "layer.move_below",
        "category": "layer",
        "bus": "operation",
        "status": "calibrated_batchplay",
        "summary": "Move a layer below another layer.",
        "modifies_document": True,
        "requires_active_document": True,
        "destructive": False,
        "params_schema": {"type": "object", "properties": {"layer_id": {}, "reference_layer_id": {}}},
        "returns": ["layer_id"],
        "failure_codes": ["layer_move_failed", "modal_busy"],
    },
    {
        "atom_id": "layer.reorder",
        "category": "layer",
        "bus": "operation",
        "status": "calibrated_batchplay",
        "summary": "Move a layer to front/back or above/below a reference layer with one unified atom.",
        "modifies_document": True,
        "requires_active_document": True,
        "destructive": False,
        "params_schema": {
            "type": "object",
            "properties": {
                "layer_id": {},
                "position": {"type": "string"},
                "reference_layer_id": {},
                "above_layer_id": {},
                "below_layer_id": {},
            },
        },
        "returns": ["layer_id", "position", "reference_layer_id"],
        "failure_codes": ["layer_reorder_failed", "layer_move_failed", "modal_busy"],
    },
    {
        "atom_id": "layer.transform",
        "category": "layer",
        "bus": "operation",
        "status": "calibrated_batchplay",
        "summary": "Transform the target layer with percent scaling, pixel offset, and optional rotation.",
        "modifies_document": True,
        "requires_active_document": True,
        "destructive": False,
        "params_schema": {
            "type": "object",
            "properties": {
                "layer_id": {},
                "scale_x": {"type": "number"},
                "scale_y": {"type": "number"},
                "x": {"type": "number"},
                "y": {"type": "number"},
                "width": {"type": "number"},
                "height": {"type": "number"},
                "offset_x": {"type": "number"},
                "offset_y": {"type": "number"},
                "rotation": {"type": "number"},
            },
        },
        "returns": ["layer_id", "transform"],
        "failure_codes": ["layer_transform_failed", "modal_busy"],
    },
    {
        "atom_id": "layer.align",
        "category": "layer",
        "bus": "operation",
        "status": "calibrated_batchplay",
        "summary": "Align selected layers or a layer to canvas/selection.",
        "modifies_document": True,
        "requires_active_document": True,
        "destructive": False,
        "params_schema": {
            "type": "object",
            "properties": {
                "layer_id": {},
                "layer_ids": {"type": "array"},
                "align": {},
                "alignments": {"type": "array"},
                "to": {"type": "string"},
            },
        },
        "returns": ["layer_ids"],
        "failure_codes": ["layer_align_failed", "modal_busy"],
    },
    {
        "atom_id": "layer.distribute",
        "category": "layer",
        "bus": "operation",
        "status": "calibrated_batchplay",
        "summary": "Distribute selected layers horizontally or vertically.",
        "modifies_document": True,
        "requires_active_document": True,
        "destructive": False,
        "params_schema": {
            "type": "object",
            "required": ["layer_ids"],
            "properties": {
                "layer_ids": {"type": "array"},
                "axis": {"type": "string"},
                "spacing": {"type": "number"},
            },
        },
        "returns": ["layer_ids"],
        "failure_codes": ["layer_distribute_failed", "modal_busy"],
    },
    {
        "atom_id": "layer.create_clipping_mask",
        "category": "layer",
        "bus": "operation",
        "status": "calibrated_batchplay",
        "summary": "Clip the target layer to the layer below.",
        "modifies_document": True,
        "requires_active_document": True,
        "destructive": False,
        "params_schema": {"type": "object", "properties": {"layer_id": {}}},
        "returns": ["layer_id"],
        "failure_codes": ["clipping_mask_failed", "modal_busy"],
    },
    {
        "atom_id": "layer.release_clipping_mask",
        "category": "layer",
        "bus": "operation",
        "status": "calibrated_batchplay",
        "summary": "Release the target layer from its clipping mask relationship.",
        "modifies_document": True,
        "requires_active_document": True,
        "destructive": False,
        "params_schema": {"type": "object", "properties": {"layer_id": {}}},
        "returns": ["layer_id", "clipping_mask"],
        "failure_codes": ["clipping_mask_failed", "modal_busy"],
    },
    {
        "atom_id": "text.create",
        "category": "text",
        "bus": "operation",
        "status": "calibrated_batchplay",
        "summary": "Create an editable Photoshop text layer.",
        "modifies_document": True,
        "requires_active_document": True,
        "destructive": False,
        "params_schema": {
            "type": "object",
            "required": ["text"],
            "properties": {
                "text": {"type": "string"},
                "x": {"type": "number"},
                "y": {"type": "number"},
                "font_size": {"type": "number"},
                "font": {"type": "string"},
                "color": {"type": "object"},
                "name": {"type": "string"},
                "opacity": {"type": "number"},
                "blend_mode": {"type": "string"},
                "text_kind": {"type": "string"},
                "paragraph": {"type": "boolean"},
                "native_paragraph": {"type": "boolean"},
                "use_text_box": {"type": "boolean"},
                "coord_space": {"type": "string"},
                "box_layer_id": {},
                "container_layer_id": {},
                "reference_layer_id": {},
                "box_bounds": {"type": "object"},
                "box_x": {"type": "number"},
                "box_y": {"type": "number"},
                "box_width": {"type": "number"},
                "box_height": {"type": "number"},
                "box_left": {"type": "number"},
                "box_top": {"type": "number"},
                "box_right": {"type": "number"},
                "box_bottom": {"type": "number"},
                "align_x": {"type": "string"},
                "align_y": {"type": "string"},
                "fit_mode": {"type": "string"},
                "padding": {"type": "number"},
                "padding_x": {"type": "number"},
                "padding_y": {"type": "number"},
                "padding_left": {"type": "number"},
                "padding_right": {"type": "number"},
                "padding_top": {"type": "number"},
                "padding_bottom": {"type": "number"},
                "inset": {"type": "number"},
                "inset_x": {"type": "number"},
                "inset_y": {"type": "number"},
                "inset_left": {"type": "number"},
                "inset_right": {"type": "number"},
                "inset_top": {"type": "number"},
                "inset_bottom": {"type": "number"},
                "wrap_text": {"type": "boolean"},
                "wrap_mode": {"type": "string"},
                "wrap_width_factor": {"type": "number"},
                "auto_fit": {"type": "boolean"},
                "min_font_size": {"type": "number"},
                "line_height_multiplier": {"type": "number"},
                "line_height_px": {"type": "number"},
                "first_line_indent": {"type": "number"},
                "left_indent": {"type": "number"},
                "right_indent": {"type": "number"},
                "paragraph_left_indent": {"type": "number"},
                "paragraph_right_indent": {"type": "number"},
                "space_before": {"type": "number"},
                "space_after": {"type": "number"},
                "paragraph_space_before": {"type": "number"},
                "paragraph_space_after": {"type": "number"},
                "max_iterations": {"type": "integer"},
                "tolerance": {"type": "number"},
                "tolerance_px": {"type": "number"},
                "damping": {"type": "number"},
            },
        },
        "returns": ["layer_id", "layer_name"],
        "failure_codes": ["text_create_failed", "modal_busy"],
    },
    {
        "atom_id": "text.fit_to_box",
        "category": "text",
        "bus": "operation",
        "status": "calibrated_batchplay",
        "summary": "Iteratively align a text layer inside a container layer using rendered bounds, with optional fit-to-box scaling.",
        "modifies_document": True,
        "requires_active_document": True,
        "destructive": False,
        "params_schema": {
            "type": "object",
            "required": ["box_layer_id"],
            "properties": {
                "text_layer_id": {},
                "layer_id": {},
                "target_layer_id": {},
                "box_layer_id": {},
                "container_layer_id": {},
                "reference_layer_id": {},
                "align_x": {"type": "string"},
                "align_y": {"type": "string"},
                "fit_mode": {"type": "string"},
                "padding": {"type": "number"},
                "padding_x": {"type": "number"},
                "padding_y": {"type": "number"},
                "padding_left": {"type": "number"},
                "padding_right": {"type": "number"},
                "padding_top": {"type": "number"},
                "padding_bottom": {"type": "number"},
                "max_iterations": {"type": "integer"},
                "tolerance": {"type": "number"},
                "tolerance_px": {"type": "number"},
                "damping": {"type": "number"},
            },
        },
        "returns": ["layer_id", "box_layer_id", "converged", "final_bounds", "final_error", "iterations"],
        "failure_codes": ["text_fit_to_box_failed", "layer_bounds_unavailable", "modal_busy"],
    },
    {
        "atom_id": "shape.rectangle",
        "category": "shape",
        "bus": "operation",
        "status": "calibrated_batchplay",
        "summary": "Create a solid-color rectangle shape layer.",
        "modifies_document": True,
        "requires_active_document": True,
        "destructive": False,
        "params_schema": {"type": "object", "required": ["x", "y", "width", "height"], "properties": {"x": {"type": "number"}, "y": {"type": "number"}, "width": {"type": "number"}, "height": {"type": "number"}, "fill": {"type": "object"}, "name": {"type": "string"}}},
        "returns": ["layer_id", "layer_name"],
        "failure_codes": ["shape_create_failed", "modal_busy"],
    },
    {
        "atom_id": "shape.rounded_rectangle",
        "category": "shape",
        "bus": "operation",
        "status": "calibrated_batchplay",
        "summary": "Create a rounded rectangle fill shape from generated contour points.",
        "modifies_document": True,
        "requires_active_document": True,
        "destructive": False,
        "params_schema": {
            "type": "object",
            "required": ["x", "y", "width", "height"],
            "properties": {
                "x": {"type": "number"},
                "y": {"type": "number"},
                "width": {"type": "number", "minimum": 1, "maximum": 30000},
                "height": {"type": "number", "minimum": 1, "maximum": 30000},
                "radius": {"type": "number", "minimum": 0, "maximum": 30000},
                "radius_top_left": {"type": "number", "minimum": 0, "maximum": 30000},
                "radius_top_right": {"type": "number", "minimum": 0, "maximum": 30000},
                "radius_bottom_right": {"type": "number", "minimum": 0, "maximum": 30000},
                "radius_bottom_left": {"type": "number", "minimum": 0, "maximum": 30000},
                "fill": {"type": "object"},
                "color": {"type": "object"},
                "name": {"type": "string"},
                "opacity": {"type": "number", "minimum": 0, "maximum": 100},
                "blend_mode": {"type": "string"},
                "feather": {"type": "number", "minimum": 0, "maximum": 500},
            },
        },
        "returns": ["layer_id", "layer_name", "point_count", "implementation", "radii"],
        "failure_codes": ["shape_polygon_selection_failed", "selection_empty", "modal_busy"],
    },
    {
        "atom_id": "shape.capsule",
        "category": "shape",
        "bus": "operation",
        "status": "calibrated_batchplay",
        "summary": "Create a capsule or pill label shape from generated rounded contour points.",
        "modifies_document": True,
        "requires_active_document": True,
        "destructive": False,
        "params_schema": {
            "type": "object",
            "required": ["x", "y", "width", "height"],
            "properties": {
                "x": {"type": "number"},
                "y": {"type": "number"},
                "width": {"type": "number", "minimum": 1, "maximum": 30000},
                "height": {"type": "number", "minimum": 1, "maximum": 30000},
                "fill": {"type": "object"},
                "color": {"type": "object"},
                "name": {"type": "string"},
                "opacity": {"type": "number", "minimum": 0, "maximum": 100},
                "blend_mode": {"type": "string"},
                "feather": {"type": "number", "minimum": 0, "maximum": 500},
            },
        },
        "returns": ["layer_id", "layer_name", "point_count", "implementation", "radius"],
        "failure_codes": ["shape_polygon_selection_failed", "selection_empty", "modal_busy"],
    },
    {
        "atom_id": "shape.cut_corner_rect",
        "category": "shape",
        "bus": "operation",
        "status": "calibrated_batchplay",
        "summary": "Create a cut-corner panel or tag with single or per-corner cut sizes.",
        "modifies_document": True,
        "requires_active_document": True,
        "destructive": False,
        "params_schema": {
            "type": "object",
            "required": ["x", "y", "width", "height"],
            "properties": {
                "x": {"type": "number"},
                "y": {"type": "number"},
                "width": {"type": "number", "minimum": 1, "maximum": 30000},
                "height": {"type": "number", "minimum": 1, "maximum": 30000},
                "corner_cut": {"type": "number", "minimum": 0, "maximum": 30000},
                "cut_top_left": {"type": "number", "minimum": 0, "maximum": 30000},
                "cut_top_right": {"type": "number", "minimum": 0, "maximum": 30000},
                "cut_bottom_right": {"type": "number", "minimum": 0, "maximum": 30000},
                "cut_bottom_left": {"type": "number", "minimum": 0, "maximum": 30000},
                "fill": {"type": "object"},
                "color": {"type": "object"},
                "name": {"type": "string"},
                "opacity": {"type": "number", "minimum": 0, "maximum": 100},
                "blend_mode": {"type": "string"},
                "feather": {"type": "number", "minimum": 0, "maximum": 500},
                "smooth": {"type": "boolean"},
                "smooth_iterations": {"type": "integer", "minimum": 0, "maximum": 5},
                "smoothing_iterations": {"type": "integer", "minimum": 0, "maximum": 5},
                "smooth_ratio": {"type": "number", "minimum": 0.05, "maximum": 0.45},
                "smoothing_ratio": {"type": "number", "minimum": 0.05, "maximum": 0.45},
                "max_points": {"type": "integer", "minimum": 3, "maximum": 256},
            },
        },
        "returns": ["layer_id", "layer_name", "point_count", "implementation", "smoothing"],
        "failure_codes": ["shape_polygon_selection_failed", "selection_empty", "modal_busy"],
    },
    {
        "atom_id": "shape.ellipse",
        "category": "shape",
        "bus": "operation",
        "status": "calibrated_batchplay",
        "summary": "Create an ellipse shape layer.",
        "modifies_document": True,
        "requires_active_document": True,
        "destructive": False,
        "params_schema": {
            "type": "object",
            "required": ["x", "y"],
            "properties": {
                "x": {"type": "number"},
                "y": {"type": "number"},
                "width": {"type": "number", "minimum": 1, "maximum": 30000},
                "height": {"type": "number", "minimum": 1, "maximum": 30000},
                "diameter": {"type": "number", "minimum": 1, "maximum": 30000},
                "fill": {"type": "object"},
                "name": {"type": "string"},
            },
        },
        "returns": ["layer_id", "layer_name"],
        "failure_codes": ["shape_create_failed", "modal_busy"],
    },
    {
        "atom_id": "shape.svg_asset_place",
        "category": "shape",
        "bus": "operation",
        "status": "calibrated_place_event",
        "summary": "Place an inline or path-data SVG as a Photoshop placed asset for decorative curves, stickers, badges, highlights, and logo-like elements.",
        "modifies_document": True,
        "requires_active_document": True,
        "destructive": False,
        "params_schema": {
            "type": "object",
            "properties": {
                "svg": {"type": "string"},
                "svg_path": {"type": "string"},
                "path_data": {"type": "string"},
                "d": {"type": "string"},
                "viewBox": {"oneOf": [{"type": "string"}, {"type": "array"}, {"type": "object"}]},
                "view_box": {"oneOf": [{"type": "string"}, {"type": "array"}, {"type": "object"}]},
                "x": {"type": "number"},
                "y": {"type": "number"},
                "width": {"type": "number", "minimum": 1, "maximum": 30000},
                "height": {"type": "number", "minimum": 1, "maximum": 30000},
                "svg_width": {"type": "number", "minimum": 1, "maximum": 30000},
                "svg_height": {"type": "number", "minimum": 1, "maximum": 30000},
                "view_width": {"type": "number", "minimum": 1, "maximum": 30000},
                "view_height": {"type": "number", "minimum": 1, "maximum": 30000},
                "scale": {"type": "number", "minimum": 0.1, "maximum": 10000},
                "scale_x": {"type": "number", "minimum": 0.1, "maximum": 10000},
                "scale_y": {"type": "number", "minimum": 0.1, "maximum": 10000},
                "rotation": {"type": "number", "minimum": -360, "maximum": 360},
                "opacity": {"type": "number", "minimum": 0, "maximum": 100},
                "path_opacity": {"type": "number", "minimum": 0, "maximum": 1},
                "fill": {"oneOf": [{"type": "object"}, {"type": "string"}, {"type": "array"}]},
                "color": {"oneOf": [{"type": "object"}, {"type": "string"}, {"type": "array"}]},
                "stroke": {"oneOf": [{"type": "object"}, {"type": "string"}, {"type": "array"}]},
                "stroke_width": {"type": "number", "minimum": 0, "maximum": 3000},
                "blend_mode": {"type": "string"},
                "name": {"type": "string"},
                "object_id": {"type": "string"},
                "part_id": {"type": "string"},
                "style_role": {"type": "string"},
                "asset_hash": {"type": "string"},
            },
        },
        "returns": ["layer_id", "layer_name", "asset_kind", "implementation", "bounds", "svg_length", "object_id", "part_id", "style_role", "asset_hash"],
        "failure_codes": ["svg_asset_missing", "svg_asset_invalid", "svg_asset_place_failed", "asset_place_failed", "asset_place_unavailable", "modal_busy"],
        "notes": "SVG is a placed decorative asset. It can be transformed, blended, grouped, and styled, but Photoshop anchor-level path editability is not guaranteed.",
    },
    {
        "atom_id": "shape.bezier_ellipse",
        "category": "shape",
        "bus": "operation",
        "status": "calibrated_dom",
        "summary": "Create an ellipse from four native cubic Bezier segments, convert it to a selection, and fill it as a solid color layer.",
        "modifies_document": True,
        "requires_active_document": True,
        "destructive": False,
        "params_schema": {
            "type": "object",
            "required": ["x", "y"],
            "properties": {
                "x": {"type": "number"},
                "y": {"type": "number"},
                "width": {"type": "number", "minimum": 1, "maximum": 30000},
                "height": {"type": "number", "minimum": 1, "maximum": 30000},
                "diameter": {"type": "number", "minimum": 1, "maximum": 30000},
                "rotation": {"type": "number", "minimum": -3600, "maximum": 3600},
                "fill": {"type": "object"},
                "color": {"type": "object"},
                "name": {"type": "string"},
                "opacity": {"type": "number", "minimum": 0, "maximum": 100},
                "blend_mode": {"type": "string"},
                "feather": {"type": "number", "minimum": 0, "maximum": 500},
                "audit": {"type": "object"},
            },
        },
        "returns": ["layer_id", "layer_name", "implementation", "path", "path_audit", "kappa", "rotation"],
        "failure_codes": ["invalid_path_points", "path_dom_unavailable", "path_dom_create_failed", "path_to_selection_failed", "selection_empty", "modal_busy"],
    },
    {
        "atom_id": "shape.ribbon",
        "category": "shape",
        "bus": "operation",
        "status": "calibrated_batchplay",
        "summary": "Create a ribbon label with pointed tip and optional tail notch.",
        "modifies_document": True,
        "requires_active_document": True,
        "destructive": False,
        "params_schema": {
            "type": "object",
            "required": ["x", "y", "width", "height"],
            "properties": {
                "x": {"type": "number"},
                "y": {"type": "number"},
                "width": {"type": "number", "minimum": 1, "maximum": 30000},
                "height": {"type": "number", "minimum": 1, "maximum": 30000},
                "direction": {"type": "string"},
                "point_depth": {"type": "number", "minimum": 1, "maximum": 30000},
                "tail_width": {"type": "number", "minimum": 0, "maximum": 30000},
                "notch_depth": {"type": "number", "minimum": 0, "maximum": 30000},
                "fill": {"type": "object"},
                "color": {"type": "object"},
                "name": {"type": "string"},
                "opacity": {"type": "number", "minimum": 0, "maximum": 100},
                "blend_mode": {"type": "string"},
                "feather": {"type": "number", "minimum": 0, "maximum": 500},
                "smooth": {"type": "boolean"},
                "smooth_iterations": {"type": "integer", "minimum": 0, "maximum": 5},
                "smoothing_iterations": {"type": "integer", "minimum": 0, "maximum": 5},
                "smooth_ratio": {"type": "number", "minimum": 0.05, "maximum": 0.45},
                "smoothing_ratio": {"type": "number", "minimum": 0.05, "maximum": 0.45},
                "max_points": {"type": "integer", "minimum": 3, "maximum": 256},
            },
        },
        "returns": ["layer_id", "layer_name", "point_count", "implementation", "smoothing"],
        "failure_codes": ["shape_polygon_selection_failed", "selection_empty", "modal_busy"],
    },
    {
        "atom_id": "shape.arc_band",
        "category": "shape",
        "bus": "operation",
        "status": "calibrated_batchplay",
        "summary": "Create a ring segment or orbital arc band from generated contour points.",
        "modifies_document": True,
        "requires_active_document": True,
        "destructive": False,
        "params_schema": {
            "type": "object",
            "properties": {
                "x": {"type": "number"},
                "y": {"type": "number"},
                "center_x": {"type": "number"},
                "center_y": {"type": "number"},
                "radius": {"type": "number", "minimum": 1, "maximum": 30000},
                "outer_radius": {"type": "number", "minimum": 1, "maximum": 30000},
                "thickness": {"type": "number", "minimum": 1, "maximum": 30000},
                "start_angle": {"type": "number"},
                "end_angle": {"type": "number"},
                "arc_span": {"type": "number", "minimum": 1, "maximum": 359.5},
                "rotation": {"type": "number"},
                "fill": {"type": "object"},
                "color": {"type": "object"},
                "name": {"type": "string"},
                "opacity": {"type": "number", "minimum": 0, "maximum": 100},
                "blend_mode": {"type": "string"},
                "feather": {"type": "number", "minimum": 0, "maximum": 500},
            },
        },
        "returns": ["layer_id", "layer_name", "point_count", "implementation", "arc_span", "thickness"],
        "failure_codes": ["shape_polygon_selection_failed", "selection_empty", "modal_busy"],
    },
    {
        "atom_id": "shape.chevron",
        "category": "shape",
        "bus": "operation",
        "status": "calibrated_batchplay",
        "summary": "Create a chevron arrow block for flow direction or section headers.",
        "modifies_document": True,
        "requires_active_document": True,
        "destructive": False,
        "params_schema": {
            "type": "object",
            "required": ["x", "y", "width", "height"],
            "properties": {
                "x": {"type": "number"},
                "y": {"type": "number"},
                "width": {"type": "number", "minimum": 1, "maximum": 30000},
                "height": {"type": "number", "minimum": 1, "maximum": 30000},
                "direction": {"type": "string"},
                "point_depth": {"type": "number", "minimum": 1, "maximum": 30000},
                "notch_depth": {"type": "number", "minimum": 0, "maximum": 30000},
                "fill": {"type": "object"},
                "color": {"type": "object"},
                "name": {"type": "string"},
                "opacity": {"type": "number", "minimum": 0, "maximum": 100},
                "blend_mode": {"type": "string"},
                "feather": {"type": "number", "minimum": 0, "maximum": 500},
                "smooth": {"type": "boolean"},
                "smooth_iterations": {"type": "integer", "minimum": 0, "maximum": 5},
                "smoothing_iterations": {"type": "integer", "minimum": 0, "maximum": 5},
                "smooth_ratio": {"type": "number", "minimum": 0.05, "maximum": 0.45},
                "smoothing_ratio": {"type": "number", "minimum": 0.05, "maximum": 0.45},
                "max_points": {"type": "integer", "minimum": 3, "maximum": 256},
            },
        },
        "returns": ["layer_id", "layer_name", "point_count", "implementation", "smoothing"],
        "failure_codes": ["shape_polygon_selection_failed", "selection_empty", "modal_busy"],
    },
    {
        "atom_id": "shape.bracket",
        "category": "shape",
        "bus": "operation",
        "status": "calibrated_batchplay",
        "summary": "Create a bracket or frame-side glyph for editorial framing and callouts.",
        "modifies_document": True,
        "requires_active_document": True,
        "destructive": False,
        "params_schema": {
            "type": "object",
            "required": ["x", "y", "width", "height"],
            "properties": {
                "x": {"type": "number"},
                "y": {"type": "number"},
                "width": {"type": "number", "minimum": 1, "maximum": 30000},
                "height": {"type": "number", "minimum": 1, "maximum": 30000},
                "side": {"type": "string"},
                "thickness": {"type": "number", "minimum": 1, "maximum": 30000},
                "fill": {"type": "object"},
                "color": {"type": "object"},
                "name": {"type": "string"},
                "opacity": {"type": "number", "minimum": 0, "maximum": 100},
                "blend_mode": {"type": "string"},
                "feather": {"type": "number", "minimum": 0, "maximum": 500},
            },
        },
        "returns": ["layer_id", "layer_name", "point_count", "implementation", "thickness"],
        "failure_codes": ["shape_polygon_selection_failed", "selection_empty", "modal_busy"],
    },
    {
        "atom_id": "shape.scalloped_triangle",
        "category": "shape",
        "bus": "operation",
        "status": "calibrated_batchplay",
        "summary": "Create a triangular illustration layer with a sampled scalloped lower edge.",
        "modifies_document": True,
        "requires_active_document": True,
        "destructive": False,
        "params_schema": {
            "type": "object",
            "required": ["x", "y", "width", "height"],
            "properties": {
                "x": {"type": "number"},
                "y": {"type": "number"},
                "width": {"type": "number", "minimum": 1, "maximum": 30000},
                "height": {"type": "number", "minimum": 1, "maximum": 30000},
                "scallop_count": {"type": "integer", "minimum": 1, "maximum": 24},
                "scallop_depth": {"type": "number", "minimum": 0, "maximum": 30000},
                "tip_x": {"type": "number"},
                "fill": {"type": "object"},
                "color": {"type": "object"},
                "name": {"type": "string"},
                "opacity": {"type": "number", "minimum": 0, "maximum": 100},
                "blend_mode": {"type": "string"},
                "feather": {"type": "number", "minimum": 0, "maximum": 500},
                "smooth": {"type": "boolean"},
                "smooth_iterations": {"type": "integer", "minimum": 0, "maximum": 5},
                "smoothing_iterations": {"type": "integer", "minimum": 0, "maximum": 5},
                "smooth_ratio": {"type": "number", "minimum": 0.05, "maximum": 0.45},
                "smoothing_ratio": {"type": "number", "minimum": 0.05, "maximum": 0.45},
                "max_points": {"type": "integer", "minimum": 3, "maximum": 256},
            },
        },
        "returns": ["layer_id", "layer_name", "point_count", "implementation", "smoothing"],
        "failure_codes": ["shape_polygon_selection_failed", "selection_empty", "modal_busy"],
    },
    {
        "atom_id": "shape.blob",
        "category": "shape",
        "bus": "operation",
        "status": "calibrated_batchplay",
        "summary": "Create an organic blob fill layer from seeded sampled contour points.",
        "modifies_document": True,
        "requires_active_document": True,
        "destructive": False,
        "params_schema": {
            "type": "object",
            "properties": {
                "x": {"type": "number"},
                "y": {"type": "number"},
                "center_x": {"type": "number"},
                "center_y": {"type": "number"},
                "radius_x": {"type": "number", "minimum": 1, "maximum": 30000},
                "radius_y": {"type": "number", "minimum": 1, "maximum": 30000},
                "width": {"type": "number", "minimum": 1, "maximum": 30000},
                "height": {"type": "number", "minimum": 1, "maximum": 30000},
                "seed": {"type": "integer"},
                "point_count": {"type": "integer", "minimum": 8, "maximum": 96},
                "roughness": {"type": "number", "minimum": 0, "maximum": 0.75},
                "fill": {"type": "object"},
                "color": {"type": "object"},
                "name": {"type": "string"},
                "opacity": {"type": "number", "minimum": 0, "maximum": 100},
                "blend_mode": {"type": "string"},
                "feather": {"type": "number", "minimum": 0, "maximum": 500},
                "smooth": {"type": "boolean"},
                "smooth_iterations": {"type": "integer", "minimum": 0, "maximum": 5},
                "smoothing_iterations": {"type": "integer", "minimum": 0, "maximum": 5},
                "smooth_ratio": {"type": "number", "minimum": 0.05, "maximum": 0.45},
                "smoothing_ratio": {"type": "number", "minimum": 0.05, "maximum": 0.45},
                "max_points": {"type": "integer", "minimum": 3, "maximum": 256},
            },
        },
        "returns": ["layer_id", "layer_name", "point_count", "implementation", "seed", "smoothing"],
        "failure_codes": ["shape_polygon_selection_failed", "selection_empty", "modal_busy"],
    },
    {
        "atom_id": "shape.wavy_band",
        "category": "shape",
        "bus": "operation",
        "status": "calibrated_batchplay",
        "summary": "Create a filled band with sampled wave edges.",
        "modifies_document": True,
        "requires_active_document": True,
        "destructive": False,
        "params_schema": {
            "type": "object",
            "required": ["x", "y", "width", "height"],
            "properties": {
                "x": {"type": "number"},
                "y": {"type": "number"},
                "width": {"type": "number", "minimum": 1, "maximum": 30000},
                "height": {"type": "number", "minimum": 1, "maximum": 30000},
                "wave_count": {"type": "integer", "minimum": 1, "maximum": 48},
                "amplitude": {"type": "number", "minimum": 0, "maximum": 30000},
                "phase": {"type": "number"},
                "fill": {"type": "object"},
                "color": {"type": "object"},
                "name": {"type": "string"},
                "opacity": {"type": "number", "minimum": 0, "maximum": 100},
                "blend_mode": {"type": "string"},
                "feather": {"type": "number", "minimum": 0, "maximum": 500},
                "smooth": {"type": "boolean"},
                "smooth_iterations": {"type": "integer", "minimum": 0, "maximum": 5},
                "smoothing_iterations": {"type": "integer", "minimum": 0, "maximum": 5},
                "smooth_ratio": {"type": "number", "minimum": 0.05, "maximum": 0.45},
                "smoothing_ratio": {"type": "number", "minimum": 0.05, "maximum": 0.45},
                "max_points": {"type": "integer", "minimum": 3, "maximum": 256},
            },
        },
        "returns": ["layer_id", "layer_name", "point_count", "implementation", "smoothing"],
        "failure_codes": ["shape_polygon_selection_failed", "selection_empty", "modal_busy"],
    },
    {
        "atom_id": "shape.starburst",
        "category": "shape",
        "bus": "operation",
        "status": "calibrated_batchplay",
        "summary": "Create a many-point starburst badge layer.",
        "modifies_document": True,
        "requires_active_document": True,
        "destructive": False,
        "params_schema": {
            "type": "object",
            "properties": {
                "x": {"type": "number"},
                "y": {"type": "number"},
                "center_x": {"type": "number"},
                "center_y": {"type": "number"},
                "points": {"type": "integer", "minimum": 3, "maximum": 96},
                "point_count": {"type": "integer", "minimum": 3, "maximum": 96},
                "outer_radius": {"type": "number", "minimum": 1, "maximum": 30000},
                "inner_radius": {"type": "number", "minimum": 1, "maximum": 30000},
                "radius": {"type": "number", "minimum": 1, "maximum": 30000},
                "rotation": {"type": "number"},
                "fill": {"type": "object"},
                "color": {"type": "object"},
                "name": {"type": "string"},
                "opacity": {"type": "number", "minimum": 0, "maximum": 100},
                "blend_mode": {"type": "string"},
                "feather": {"type": "number", "minimum": 0, "maximum": 500},
            },
        },
        "returns": ["layer_id", "layer_name", "point_count", "starburst_points", "implementation"],
        "failure_codes": ["shape_polygon_selection_failed", "selection_empty", "modal_busy"],
    },
    {
        "atom_id": "shape.beads_on_path",
        "category": "shape",
        "bus": "operation",
        "status": "calibrated_batchplay",
        "summary": "Place editable bead ellipse layers along a sampled polyline.",
        "modifies_document": True,
        "requires_active_document": True,
        "destructive": False,
        "params_schema": {
            "type": "object",
            "required": ["points", "bead_radius"],
            "properties": {
                "points": {"type": "array", "minItems": 2, "maxItems": 256},
                "bead_radius": {"type": "number", "minimum": 0.5, "maximum": 1000},
                "radius": {"type": "number", "minimum": 0.5, "maximum": 1000},
                "spacing": {"type": "number", "minimum": 1, "maximum": 3000},
                "max_beads": {"type": "integer", "minimum": 1, "maximum": 512},
                "fill": {"type": "object"},
                "highlight_fill": {"type": "object"},
                "highlight_opacity": {"type": "number", "minimum": 0, "maximum": 100},
                "name": {"type": "string"},
                "group": {"type": "boolean"},
            },
        },
        "returns": ["group_id", "group_name", "layer_ids", "bead_count", "sampled_points", "implementation"],
        "failure_codes": ["invalid_shape_polyline_points", "shape_create_failed", "modal_busy"],
    },
    {
        "atom_id": "shape.dashed_path",
        "category": "shape",
        "bus": "operation",
        "status": "calibrated_batchplay",
        "summary": "Place editable dash layers along a sampled polyline.",
        "modifies_document": True,
        "requires_active_document": True,
        "destructive": False,
        "params_schema": {
            "type": "object",
            "required": ["points"],
            "properties": {
                "points": {"type": "array", "minItems": 2, "maxItems": 256},
                "width": {"type": "number", "minimum": 0.5, "maximum": 3000},
                "stroke_width": {"type": "number", "minimum": 0.5, "maximum": 3000},
                "dash_length": {"type": "number", "minimum": 1, "maximum": 10000},
                "gap_length": {"type": "number", "minimum": 0, "maximum": 10000},
                "fill": {"type": "object"},
                "color": {"type": "object"},
                "name": {"type": "string"},
                "group": {"type": "boolean"},
            },
        },
        "returns": ["group_id", "group_name", "layer_ids", "dash_count", "implementation"],
        "failure_codes": ["invalid_shape_polyline_points", "shape_line_selection_failed", "modal_busy"],
    },
    {
        "atom_id": "shape.arrow_path",
        "category": "shape",
        "bus": "operation",
        "status": "calibrated_batchplay",
        "summary": "Create an editable polyline shaft plus polygon arrow head.",
        "modifies_document": True,
        "requires_active_document": True,
        "destructive": False,
        "params_schema": {
            "type": "object",
            "required": ["points"],
            "properties": {
                "points": {"type": "array", "minItems": 2, "maxItems": 256},
                "width": {"type": "number", "minimum": 0.5, "maximum": 3000},
                "stroke_width": {"type": "number", "minimum": 0.5, "maximum": 3000},
                "head_size": {"type": "number", "minimum": 1, "maximum": 30000},
                "head_style": {"type": "string"},
                "fill": {"type": "object"},
                "color": {"type": "object"},
                "name": {"type": "string"},
                "group": {"type": "boolean"},
            },
        },
        "returns": ["group_id", "group_name", "layer_ids", "shaft_layer_id", "head_layer_id", "implementation"],
        "failure_codes": ["invalid_shape_polyline_points", "shape_polyline_selection_failed", "shape_polygon_selection_failed", "modal_busy"],
    },
    {
        "atom_id": "shape.bauble",
        "category": "shape",
        "bus": "operation",
        "status": "calibrated_batchplay",
        "summary": "Create a composite ornament with body, hook, and highlight layers.",
        "modifies_document": True,
        "requires_active_document": True,
        "destructive": False,
        "params_schema": {
            "type": "object",
            "required": ["x", "y", "diameter"],
            "properties": {
                "x": {"type": "number"},
                "y": {"type": "number"},
                "diameter": {"type": "number", "minimum": 1, "maximum": 30000},
                "size": {"type": "number", "minimum": 1, "maximum": 30000},
                "fill": {"type": "object"},
                "color": {"type": "object"},
                "highlight_fill": {"type": "object"},
                "highlight_opacity": {"type": "number", "minimum": 0, "maximum": 100},
                "hook_fill": {"type": "object"},
                "name": {"type": "string"},
                "group": {"type": "boolean"},
            },
        },
        "returns": ["group_id", "group_name", "layer_ids", "body_layer_id", "highlight_layer_id", "hook_layer_id", "implementation"],
        "failure_codes": ["shape_create_failed", "modal_busy"],
    },
    {
        "atom_id": "shape.badge",
        "category": "shape",
        "bus": "operation",
        "status": "calibrated_batchplay",
        "summary": "Create a composite badge using a circle or starburst base plus inner mark.",
        "modifies_document": True,
        "requires_active_document": True,
        "destructive": False,
        "params_schema": {
            "type": "object",
            "required": ["radius"],
            "properties": {
                "x": {"type": "number"},
                "y": {"type": "number"},
                "center_x": {"type": "number"},
                "center_y": {"type": "number"},
                "radius": {"type": "number", "minimum": 1, "maximum": 30000},
                "style": {"type": "string"},
                "points": {"type": "integer", "minimum": 6, "maximum": 96},
                "fill": {"type": "object"},
                "color": {"type": "object"},
                "stroke_fill": {"type": "object"},
                "inner_fill": {"type": "object"},
                "inner_opacity": {"type": "number", "minimum": 0, "maximum": 100},
                "name": {"type": "string"},
                "group": {"type": "boolean"},
            },
        },
        "returns": ["group_id", "group_name", "layer_ids", "implementation"],
        "failure_codes": ["shape_create_failed", "shape_polygon_selection_failed", "modal_busy"],
    },
    {
        "atom_id": "shape.callout",
        "category": "shape",
        "bus": "operation",
        "status": "calibrated_batchplay",
        "summary": "Create a rounded callout bubble with a triangular tail.",
        "modifies_document": True,
        "requires_active_document": True,
        "destructive": False,
        "params_schema": {
            "type": "object",
            "required": ["x", "y", "width", "height"],
            "properties": {
                "x": {"type": "number"},
                "y": {"type": "number"},
                "width": {"type": "number", "minimum": 1, "maximum": 30000},
                "height": {"type": "number", "minimum": 1, "maximum": 30000},
                "radius": {"type": "number", "minimum": 0, "maximum": 30000},
                "tail_side": {"type": "string"},
                "tail_x": {"type": "number"},
                "tail_y": {"type": "number"},
                "tail_width": {"type": "number", "minimum": 1, "maximum": 30000},
                "fill": {"type": "object"},
                "color": {"type": "object"},
                "name": {"type": "string"},
                "opacity": {"type": "number", "minimum": 0, "maximum": 100},
                "blend_mode": {"type": "string"},
                "feather": {"type": "number", "minimum": 0, "maximum": 500},
                "smooth": {"type": "boolean"},
                "smooth_iterations": {"type": "integer", "minimum": 0, "maximum": 5},
                "smoothing_iterations": {"type": "integer", "minimum": 0, "maximum": 5},
                "smooth_ratio": {"type": "number", "minimum": 0.05, "maximum": 0.45},
                "smoothing_ratio": {"type": "number", "minimum": 0.05, "maximum": 0.45},
                "max_points": {"type": "integer", "minimum": 3, "maximum": 256},
            },
        },
        "returns": ["layer_id", "layer_name", "point_count", "implementation", "smoothing"],
        "failure_codes": ["shape_polygon_selection_failed", "selection_empty", "modal_busy"],
    },
    {
        "atom_id": "shape.ticket_card",
        "category": "shape",
        "bus": "operation",
        "status": "calibrated_batchplay",
        "summary": "Create a ticket-like card with sampled perforation-style edge notches.",
        "modifies_document": True,
        "requires_active_document": True,
        "destructive": False,
        "params_schema": {
            "type": "object",
            "required": ["x", "y", "width", "height"],
            "properties": {
                "x": {"type": "number"},
                "y": {"type": "number"},
                "width": {"type": "number", "minimum": 1, "maximum": 30000},
                "height": {"type": "number", "minimum": 1, "maximum": 30000},
                "radius": {"type": "number", "minimum": 0, "maximum": 30000},
                "notch_radius": {"type": "number", "minimum": 0, "maximum": 30000},
                "notch_count": {"type": "integer", "minimum": 1, "maximum": 24},
                "notch_side": {"type": "string"},
                "fill": {"type": "object"},
                "color": {"type": "object"},
                "name": {"type": "string"},
                "opacity": {"type": "number", "minimum": 0, "maximum": 100},
                "blend_mode": {"type": "string"},
                "feather": {"type": "number", "minimum": 0, "maximum": 500},
                "smooth": {"type": "boolean"},
                "smooth_iterations": {"type": "integer", "minimum": 0, "maximum": 5},
                "smoothing_iterations": {"type": "integer", "minimum": 0, "maximum": 5},
                "smooth_ratio": {"type": "number", "minimum": 0.05, "maximum": 0.45},
                "smoothing_ratio": {"type": "number", "minimum": 0.05, "maximum": 0.45},
                "max_points": {"type": "integer", "minimum": 3, "maximum": 256},
            },
        },
        "returns": ["layer_id", "layer_name", "point_count", "implementation", "smoothing"],
        "failure_codes": ["shape_polygon_selection_failed", "selection_empty", "modal_busy"],
    },
    {
        "atom_id": "shape.notched_panel",
        "category": "shape",
        "bus": "operation",
        "status": "calibrated_batchplay",
        "summary": "Create a panel with one or more cut/notched corners.",
        "modifies_document": True,
        "requires_active_document": True,
        "destructive": False,
        "params_schema": {
            "type": "object",
            "required": ["x", "y", "width", "height"],
            "properties": {
                "x": {"type": "number"},
                "y": {"type": "number"},
                "width": {"type": "number", "minimum": 1, "maximum": 30000},
                "height": {"type": "number", "minimum": 1, "maximum": 30000},
                "notch_size": {"type": "number", "minimum": 0, "maximum": 30000},
                "notch_positions": {"type": "array"},
                "fill": {"type": "object"},
                "color": {"type": "object"},
                "name": {"type": "string"},
                "opacity": {"type": "number", "minimum": 0, "maximum": 100},
                "blend_mode": {"type": "string"},
                "feather": {"type": "number", "minimum": 0, "maximum": 500},
                "smooth": {"type": "boolean"},
                "smooth_iterations": {"type": "integer", "minimum": 0, "maximum": 5},
                "smoothing_iterations": {"type": "integer", "minimum": 0, "maximum": 5},
                "smooth_ratio": {"type": "number", "minimum": 0.05, "maximum": 0.45},
                "smoothing_ratio": {"type": "number", "minimum": 0.05, "maximum": 0.45},
                "max_points": {"type": "integer", "minimum": 3, "maximum": 256},
            },
        },
        "returns": ["layer_id", "layer_name", "point_count", "implementation", "smoothing"],
        "failure_codes": ["shape_polygon_selection_failed", "selection_empty", "modal_busy"],
    },
    {
        "atom_id": "shape.folded_corner",
        "category": "shape",
        "bus": "operation",
        "status": "calibrated_batchplay",
        "summary": "Create a composite folded-corner card with body and fold layers.",
        "modifies_document": True,
        "requires_active_document": True,
        "destructive": False,
        "params_schema": {
            "type": "object",
            "required": ["x", "y", "width", "height"],
            "properties": {
                "x": {"type": "number"},
                "y": {"type": "number"},
                "width": {"type": "number", "minimum": 1, "maximum": 30000},
                "height": {"type": "number", "minimum": 1, "maximum": 30000},
                "corner": {"type": "string"},
                "fold_size": {"type": "number", "minimum": 1, "maximum": 30000},
                "fill": {"type": "object"},
                "color": {"type": "object"},
                "fold_fill": {"type": "object"},
                "name": {"type": "string"},
                "group": {"type": "boolean"},
                "opacity": {"type": "number", "minimum": 0, "maximum": 100},
                "blend_mode": {"type": "string"},
                "feather": {"type": "number", "minimum": 0, "maximum": 500},
            },
        },
        "returns": ["group_id", "group_name", "layer_ids", "body_layer_id", "fold_layer_id", "implementation"],
        "failure_codes": ["shape_polygon_selection_failed", "selection_empty", "modal_busy"],
    },
    {
        "atom_id": "shape.polygon",
        "category": "shape",
        "bus": "operation",
        "status": "calibrated_batchplay",
        "summary": "Create a solid-color fill layer constrained by a polygon selection mask.",
        "modifies_document": True,
        "requires_active_document": True,
        "destructive": False,
        "params_schema": {
            "type": "object",
            "required": ["points"],
            "properties": {
                "points": {"type": "array", "minItems": 3, "maxItems": 256},
                "fill": {"type": "object"},
                "color": {"type": "object"},
                "name": {"type": "string"},
                "opacity": {"type": "number", "minimum": 0, "maximum": 100},
                "blend_mode": {"type": "string"},
                "feather": {"type": "number", "minimum": 0, "maximum": 500},
            },
        },
        "returns": ["layer_id", "layer_name", "point_count", "implementation"],
        "failure_codes": ["invalid_polygon_points", "shape_polygon_selection_failed", "selection_empty", "modal_busy"],
    },
    {
        "atom_id": "shape.star",
        "category": "shape",
        "bus": "operation",
        "status": "calibrated_batchplay",
        "summary": "Create a solid-color star fill layer from generated polygon points.",
        "modifies_document": True,
        "requires_active_document": True,
        "destructive": False,
        "params_schema": {
            "type": "object",
            "properties": {
                "x": {"type": "number"},
                "y": {"type": "number"},
                "center_x": {"type": "number"},
                "center_y": {"type": "number"},
                "points": {"type": "integer", "minimum": 3, "maximum": 64},
                "point_count": {"type": "integer", "minimum": 3, "maximum": 64},
                "outer_radius": {"type": "number", "minimum": 1, "maximum": 30000},
                "inner_radius": {"type": "number", "minimum": 1, "maximum": 30000},
                "radius": {"type": "number", "minimum": 1, "maximum": 30000},
                "rotation": {"type": "number"},
                "fill": {"type": "object"},
                "color": {"type": "object"},
                "name": {"type": "string"},
                "opacity": {"type": "number", "minimum": 0, "maximum": 100},
                "blend_mode": {"type": "string"},
                "feather": {"type": "number", "minimum": 0, "maximum": 500},
            },
        },
        "returns": ["layer_id", "layer_name", "point_count", "star_points", "implementation"],
        "failure_codes": ["shape_star_selection_failed", "selection_empty", "modal_busy"],
    },
    {
        "atom_id": "shape.line",
        "category": "shape",
        "bus": "operation",
        "status": "calibrated_batchplay",
        "summary": "Create a thick straight line as a solid-color fill layer constrained by a polygon strip.",
        "modifies_document": True,
        "requires_active_document": True,
        "destructive": False,
        "params_schema": {
            "type": "object",
            "properties": {
                "x1": {"type": "number"},
                "y1": {"type": "number"},
                "x2": {"type": "number"},
                "y2": {"type": "number"},
                "start": {"type": "object"},
                "end": {"type": "object"},
                "width": {"type": "number", "minimum": 0.5, "maximum": 3000},
                "stroke_width": {"type": "number", "minimum": 0.5, "maximum": 3000},
                "fill": {"type": "object"},
                "color": {"type": "object"},
                "name": {"type": "string"},
                "opacity": {"type": "number", "minimum": 0, "maximum": 100},
                "blend_mode": {"type": "string"},
                "feather": {"type": "number", "minimum": 0, "maximum": 500},
            },
        },
        "returns": ["layer_id", "layer_name", "width", "cap", "implementation"],
        "failure_codes": ["invalid_shape_line_points", "shape_line_selection_failed", "selection_empty", "modal_busy"],
    },
    {
        "atom_id": "shape.polyline",
        "category": "shape",
        "bus": "operation",
        "status": "calibrated_batchplay",
        "summary": "Create a thick polyline by unioning polygon-strip selections into one solid-color fill layer.",
        "modifies_document": True,
        "requires_active_document": True,
        "destructive": False,
        "params_schema": {
            "type": "object",
            "required": ["points"],
            "properties": {
                "points": {"type": "array", "minItems": 2, "maxItems": 128},
                "width": {"type": "number", "minimum": 0.5, "maximum": 3000},
                "stroke_width": {"type": "number", "minimum": 0.5, "maximum": 3000},
                "fill": {"type": "object"},
                "color": {"type": "object"},
                "name": {"type": "string"},
                "opacity": {"type": "number", "minimum": 0, "maximum": 100},
                "blend_mode": {"type": "string"},
                "feather": {"type": "number", "minimum": 0, "maximum": 500},
            },
        },
        "returns": ["layer_id", "layer_name", "width", "segment_count", "join", "implementation"],
        "failure_codes": ["invalid_shape_polyline_points", "shape_polyline_selection_failed", "selection_empty", "modal_busy"],
    },
    {
        "atom_id": "path.create_work_path",
        "category": "path",
        "bus": "operation",
        "status": "calibrated_dom",
        "summary": "Create a Photoshop path from points/subpaths; closed polygon paths can use selection -> Make Work Path, while Bezier/open paths use DOM document.pathItems.add.",
        "modifies_document": True,
        "requires_active_document": True,
        "destructive": False,
        "params_schema": {
            "type": "object",
            "properties": {
                "points": {"type": "array", "maxItems": 512},
                "subpaths": {"type": "array", "maxItems": 32},
                "closed": {"type": "boolean"},
                "operation": {"type": "string"},
                "path_mode": {"type": "string"},
                "tolerance": {"type": "number", "minimum": 0.5, "maximum": 100},
                "feather": {"type": "number", "minimum": 0, "maximum": 500},
                "clear_selection": {"type": "boolean"},
            },
        },
        "returns": ["path_kind", "mode", "path_id", "path_name", "subpath_count", "point_count", "closed", "fallback_used", "implementation"],
        "failure_codes": ["invalid_path_points", "path_create_failed", "path_dom_required", "path_dom_unavailable", "path_dom_create_failed", "path_selection_fallback_failed", "modal_busy"],
    },
    {
        "atom_id": "path.bezier_work_path",
        "category": "path",
        "bus": "operation",
        "status": "calibrated_dom",
        "summary": "Create a native Photoshop Bezier PathItem through document.pathItems.add from anchors and handles; supports generated handles and audit feedback.",
        "modifies_document": True,
        "requires_active_document": True,
        "destructive": False,
        "params_schema": {
            "type": "object",
            "properties": {
                "points": {"type": "array", "maxItems": 512},
                "subpaths": {"type": "array", "maxItems": 32},
                "closed": {"type": "boolean"},
                "closed_only": {"type": "boolean"},
                "operation": {"type": "string"},
                "handle_mode": {"type": "string"},
                "handles": {"type": "string"},
                "handle_scale": {"type": "number", "minimum": 0.05, "maximum": 2},
                "corner_angle_threshold": {"type": "number", "minimum": 45, "maximum": 175},
                "min_smooth_segment_length": {"type": "number", "minimum": 0, "maximum": 1000},
                "auto_repair_handles": {"type": "boolean"},
                "audit": {"type": "object"},
                "clear_selection": {"type": "boolean"},
            },
        },
        "returns": ["path_kind", "mode", "path_id", "path_name", "subpath_count", "point_count", "closed", "fallback_used", "implementation", "handle_mode", "path_audit"],
        "failure_codes": ["invalid_path_points", "path_dom_unavailable", "path_dom_create_failed", "modal_busy"],
    },
    {
        "atom_id": "path.dom_runtime_diagnostics",
        "category": "path",
        "bus": "operation",
        "status": "stable",
        "summary": "Return Photoshop UXP path runtime diagnostics without modifying the active document; class constructor route is reported as removed/unavailable.",
        "modifies_document": False,
        "requires_active_document": False,
        "destructive": False,
        "params_schema": {
            "type": "object",
            "properties": {
                "construct_sample": {"type": "boolean"},
            },
        },
        "returns": ["photoshop_path_keys", "class_route_available", "class_route_removed", "plain_dom_route_available", "svg_route_available", "point_kind_keys", "shape_operation_keys"],
        "failure_codes": ["modal_busy", "operation_atom_failed"],
    },
    {
        "atom_id": "path.audit_bezier_handles",
        "category": "path",
        "bus": "operation",
        "status": "stable",
        "summary": "Audit Bezier anchors/handles without modifying Photoshop: direction, ratio, smooth collinearity, and generated-handle mode.",
        "modifies_document": False,
        "requires_active_document": False,
        "destructive": False,
        "params_schema": {
            "type": "object",
            "properties": {
                "points": {"type": "array", "maxItems": 512},
                "subpaths": {"type": "array", "maxItems": 32},
                "closed": {"type": "boolean"},
                "operation": {"type": "string"},
                "handle_mode": {"type": "string"},
                "handles": {"type": "string"},
                "handle_scale": {"type": "number", "minimum": 0.05, "maximum": 2},
                "corner_angle_threshold": {"type": "number", "minimum": 45, "maximum": 175},
                "min_smooth_segment_length": {"type": "number", "minimum": 0, "maximum": 1000},
                "auto_repair_handles": {"type": "boolean"},
                "tolerance": {"type": "number", "minimum": 0.5, "maximum": 180},
                "min_handle_ratio": {"type": "number", "minimum": 0, "maximum": 1},
                "max_handle_ratio": {"type": "number", "minimum": 0.05, "maximum": 3},
            },
        },
        "returns": ["path_audit", "handle_mode", "subpath_count", "point_count"],
        "failure_codes": ["invalid_path_points", "operation_atom_failed"],
    },
    {
        "atom_id": "path.to_selection",
        "category": "path",
        "bus": "operation",
        "status": "calibrated_batchplay",
        "summary": "Convert the active Photoshop work path into the current selection.",
        "modifies_document": True,
        "requires_active_document": True,
        "destructive": False,
        "params_schema": {
            "type": "object",
            "properties": {
                "operation": {"type": "string"},
                "feather": {"type": "number", "minimum": 0, "maximum": 500},
                "anti_alias": {"type": "boolean"},
            },
        },
        "returns": ["path_kind", "has_active_selection", "operation", "feather"],
        "failure_codes": ["path_to_selection_failed", "selection_empty", "modal_busy"],
    },
    {
        "atom_id": "path.stroke",
        "category": "path",
        "bus": "operation",
        "status": "calibrated_batchplay",
        "summary": "Stroke the active work path by converting it to a selection and applying Photoshop Stroke on a target/new pixel layer.",
        "modifies_document": True,
        "requires_active_document": True,
        "destructive": False,
        "params_schema": {
            "type": "object",
            "properties": {
                "layer_id": {},
                "target_layer_id": {},
                "name": {"type": "string"},
                "width": {"type": "number", "minimum": 0.5, "maximum": 3000},
                "stroke_width": {"type": "number", "minimum": 0.5, "maximum": 3000},
                "location": {"type": "string"},
                "color": {"type": "object"},
                "fill": {"type": "object"},
                "opacity": {"type": "number", "minimum": 0, "maximum": 100},
                "blend_mode": {"type": "string"},
                "clear_selection": {"type": "boolean"},
            },
        },
        "returns": ["layer_id", "layer_name", "stroke", "implementation"],
        "failure_codes": ["path_to_selection_failed", "path_stroke_failed", "modal_busy"],
    },
    {
        "atom_id": "shape.path_fill",
        "category": "shape",
        "bus": "operation",
        "status": "calibrated_batchplay",
        "summary": "Fill the active work path as a solid color layer by converting it to a selection.",
        "modifies_document": True,
        "requires_active_document": True,
        "destructive": False,
        "params_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "fill": {"type": "object"},
                "color": {"type": "object"},
                "opacity": {"type": "number", "minimum": 0, "maximum": 100},
                "blend_mode": {"type": "string"},
                "feather": {"type": "number", "minimum": 0, "maximum": 500},
            },
        },
        "returns": ["layer_id", "layer_name", "implementation"],
        "failure_codes": ["path_to_selection_failed", "selection_empty", "modal_busy"],
    },
    {
        "atom_id": "shape.bezier_fill",
        "category": "shape",
        "bus": "operation",
        "status": "calibrated_dom",
        "summary": "Create a native Photoshop Bezier PathItem through document.pathItems.add, optionally generate handles, convert to selection, and fill it as a solid color layer.",
        "modifies_document": True,
        "requires_active_document": True,
        "destructive": False,
        "params_schema": {
            "type": "object",
            "properties": {
                "points": {"type": "array", "maxItems": 512},
                "subpaths": {"type": "array", "maxItems": 32},
                "closed": {"type": "boolean"},
                "closed_only": {"type": "boolean"},
                "operation": {"type": "string"},
                "handle_mode": {"type": "string"},
                "handles": {"type": "string"},
                "handle_scale": {"type": "number", "minimum": 0.05, "maximum": 2},
                "corner_angle_threshold": {"type": "number", "minimum": 45, "maximum": 175},
                "min_smooth_segment_length": {"type": "number", "minimum": 0, "maximum": 1000},
                "auto_repair_handles": {"type": "boolean"},
                "audit": {"type": "object"},
                "fill_strategy": {"type": "string"},
                "name": {"type": "string"},
                "fill": {"type": "object"},
                "color": {"type": "object"},
                "opacity": {"type": "number", "minimum": 0, "maximum": 100},
                "blend_mode": {"type": "string"},
                "feather": {"type": "number", "minimum": 0, "maximum": 500},
            },
        },
        "returns": ["layer_id", "layer_name", "implementation", "path", "handle_mode", "path_audit"],
        "failure_codes": ["invalid_path_points", "path_dom_unavailable", "path_dom_create_failed", "path_to_selection_failed", "selection_empty", "modal_busy"],
    },
    {
        "atom_id": "layer.effect_shadow",
        "category": "effect",
        "bus": "operation",
        "status": "calibrated_batchplay",
        "summary": "Apply a drop shadow layer style.",
        "modifies_document": True,
        "requires_active_document": True,
        "destructive": False,
        "params_schema": {
            "type": "object",
            "properties": {
                "layer_id": {},
                "color": {"type": "object"},
                "opacity": {"type": "number", "minimum": 0, "maximum": 100},
                "distance": {"type": "number"},
                "size": {"type": "number"},
                "spread": {"type": "number", "minimum": 0, "maximum": 100},
                "angle": {"type": "number"},
                "blend_mode": {"type": "string"},
            },
        },
        "returns": ["layer_id"],
        "failure_codes": ["layer_effect_failed", "modal_busy"],
    },
    {
        "atom_id": "layer.effect_outer_glow",
        "category": "effect",
        "bus": "operation",
        "status": "calibrated_batchplay",
        "summary": "Apply an outer glow layer style for light wrap, halo, or poster glow.",
        "modifies_document": True,
        "requires_active_document": True,
        "destructive": False,
        "params_schema": {
            "type": "object",
            "properties": {
                "layer_id": {},
                "color": {"type": "object"},
                "opacity": {"type": "number", "minimum": 0, "maximum": 100},
                "size": {"type": "number"},
                "spread": {"type": "number", "minimum": 0, "maximum": 100},
                "blend_mode": {"type": "string"},
            },
        },
        "returns": ["layer_id", "effect"],
        "failure_codes": ["layer_effect_failed", "modal_busy"],
    },
    {
        "atom_id": "layer.effect_stroke",
        "category": "effect",
        "bus": "operation",
        "status": "calibrated_batchplay",
        "summary": "Apply a Photoshop layer-style stroke to the target layer.",
        "modifies_document": True,
        "requires_active_document": True,
        "destructive": False,
        "params_schema": {
            "type": "object",
            "properties": {
                "layer_id": {},
                "color": {"type": "object"},
                "size": {"type": "number", "minimum": 1, "maximum": 1000},
                "opacity": {"type": "number", "minimum": 0, "maximum": 100},
                "position": {"type": "string"},
                "blend_mode": {"type": "string"},
            },
        },
        "returns": ["layer_id", "effect"],
        "failure_codes": ["layer_effect_failed", "modal_busy"],
    },
    {
        "atom_id": "layer.effect_gradient_overlay",
        "category": "effect",
        "bus": "operation",
        "status": "calibrated_batchplay",
        "summary": "Apply a layer-style gradient overlay to the target layer.",
        "modifies_document": True,
        "requires_active_document": True,
        "destructive": False,
        "params_schema": {
            "type": "object",
            "properties": {
                "layer_id": {},
                "stops": {"type": "array"},
                "opacity": {"type": "number", "minimum": 0, "maximum": 100},
                "angle": {"type": "number"},
                "scale": {"type": "number", "minimum": 1, "maximum": 1000},
                "style": {"type": "string"},
                "blend_mode": {"type": "string"},
            },
        },
        "returns": ["layer_id", "effect"],
        "failure_codes": ["layer_effect_failed", "modal_busy"],
    },
    {
        "atom_id": "gradient.fill",
        "category": "gradient",
        "bus": "operation",
        "status": "calibrated_batchplay",
        "summary": "Create an editable gradient fill layer for atmosphere, background, or design overlays.",
        "modifies_document": True,
        "requires_active_document": True,
        "destructive": False,
        "params_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "stops": {"type": "array"},
                "style": {"type": "string"},
                "angle": {"type": "number"},
                "scale": {"type": "number", "minimum": 1, "maximum": 1000},
                "opacity": {"type": "number", "minimum": 0, "maximum": 100},
                "blend_mode": {"type": "string"},
                "reverse": {"type": "boolean"},
                "dither": {"type": "boolean"},
            },
        },
        "returns": ["layer_id", "layer_name", "gradient"],
        "failure_codes": ["gradient_fill_failed", "modal_busy"],
    },
    {
        "atom_id": "layer.extract_luminosity_range",
        "category": "effect",
        "bus": "operation",
        "status": "calibrated_batchplay",
        "summary": "Duplicate a layer and mask it to highlights, midtones, or shadows for local tone/effect building.",
        "modifies_document": True,
        "requires_active_document": True,
        "destructive": False,
        "params_schema": {
            "type": "object",
            "properties": {
                "source_layer_id": {},
                "name": {"type": "string"},
                "range": {"type": "string"},
                "fuzziness": {"type": "number"},
                "feather": {"type": "number"},
                "expand": {"type": "number"},
                "opacity": {"type": "number", "minimum": 0, "maximum": 100},
                "blend_mode": {"type": "string"},
                "invert": {"type": "boolean"},
            },
        },
        "returns": ["layer_id", "layer_name", "range", "mask_applied"],
        "failure_codes": ["luminosity_extract_failed", "selection_empty", "modal_busy"],
    },
    {
        "atom_id": "effect.bloom_layer",
        "category": "effect",
        "bus": "operation",
        "status": "calibrated_batchplay",
        "summary": "Create a masked blur blend layer for high-light bloom, soft glow, or diffusion.",
        "modifies_document": True,
        "requires_active_document": True,
        "destructive": False,
        "params_schema": {
            "type": "object",
            "properties": {
                "source_layer_id": {},
                "name": {"type": "string"},
                "range": {"type": "string"},
                "fuzziness": {"type": "number"},
                "blur_radius": {"type": "number", "minimum": 0.1, "maximum": 500},
                "feather": {"type": "number"},
                "opacity": {"type": "number", "minimum": 0, "maximum": 100},
                "blend_mode": {"type": "string"},
            },
        },
        "returns": ["layer_id", "layer_name", "range", "blur_radius"],
        "failure_codes": ["bloom_layer_failed", "selection_empty", "modal_busy"],
    },
    {
        "atom_id": "effect.light_rays",
        "category": "effect",
        "bus": "operation",
        "status": "calibrated_batchplay",
        "summary": "Create polygon-based directional light ray layers for Tyndall or poster light beams.",
        "modifies_document": True,
        "requires_active_document": True,
        "destructive": False,
        "params_schema": {
            "type": "object",
            "properties": {
                "origin": {"type": "object"},
                "rays": {"type": "array"},
                "count": {"type": "integer", "minimum": 1, "maximum": 24},
                "length": {"type": "number"},
                "spread": {"type": "number"},
                "width": {"type": "number"},
                "blur_radius": {"type": "number"},
                "color": {"type": "object"},
                "opacity": {"type": "number", "minimum": 0, "maximum": 100},
                "blend_mode": {"type": "string"},
                "name": {"type": "string"},
            },
        },
        "returns": ["group_id", "group_name", "ray_count", "layer_ids"],
        "failure_codes": ["light_rays_failed", "modal_busy"],
    },
    {
        "atom_id": "preview.export_global",
        "category": "preview",
        "bus": "operation",
        "status": "existing_tool",
        "summary": "Use ps_export_preview for full-document review.",
        "modifies_document": False,
        "requires_active_document": True,
        "destructive": False,
        "params_schema": {"type": "object", "additionalProperties": True},
        "returns": ["asset_path", "asset_url"],
        "failure_codes": ["no_active_document"],
    },
    {
        "atom_id": "preview.export_regions",
        "category": "preview",
        "bus": "operation",
        "status": "existing_tool",
        "summary": "Use ps_export_regions for local review crops.",
        "modifies_document": False,
        "requires_active_document": True,
        "destructive": False,
        "params_schema": {"type": "object", "additionalProperties": True},
        "returns": ["regions"],
        "failure_codes": ["no_active_document"],
    },
]

ATOM_BY_ID = {atom["atom_id"]: atom for atom in OPERATION_ATOMS}


def _error(code: str, message: str, details: Any | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "status": "error",
        "schema_version": SCHEMA_VERSION,
        "error": {"code": code, "message": message},
    }
    if details is not None:
        payload["error"]["details"] = details
    return payload


def list_operation_atoms(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = payload or {}
    category = payload.get("category")
    include_details = bool(payload.get("include_details", True))
    atoms = [deepcopy(atom) for atom in OPERATION_ATOMS if not category or atom.get("category") == category]
    if not include_details:
        atoms = [
            {
                "atom_id": atom["atom_id"],
                "category": atom["category"],
                "status": atom["status"],
                "summary": atom["summary"],
                "modifies_document": atom["modifies_document"],
            }
            for atom in atoms
        ]
    categories: dict[str, int] = {}
    for atom in OPERATION_ATOMS:
        categories[atom["category"]] = categories.get(atom["category"], 0) + 1
    return {
        "status": "ok",
        "schema_version": SCHEMA_VERSION,
        "atom_count": len(OPERATION_ATOMS),
        "returned_count": len(atoms),
        "categories": categories,
        "atoms": atoms,
    }


def probe_operation_atom(payload: dict[str, Any]) -> dict[str, Any]:
    atom_id = str(payload.get("atom_id") or payload.get("id") or "")
    atom = ATOM_BY_ID.get(atom_id)
    if not atom:
        return _error("unknown_operation_atom", f"Unknown operation atom: {atom_id}")
    return {"status": "ok", "schema_version": SCHEMA_VERSION, "atom": deepcopy(atom), "available": atom.get("status") != "planned"}


def _is_step_ref(value: Any) -> bool:
    return isinstance(value, str) and bool(TARGET_REF_RE.match(value))


def _validate_step_target(value: Any, path: str, errors: list[str]) -> None:
    if value is None:
        return
    if isinstance(value, int):
        return
    if isinstance(value, str):
        if value.strip() and (value.isdigit() or _is_step_ref(value)):
            return
        errors.append(f"{path} must be a layer id or $steps.<step_id_or_index>.<field> reference")
        return
    errors.append(f"{path} must be a layer id or step reference")


def _is_point(value: Any) -> bool:
    if isinstance(value, dict):
        return isinstance(value.get("x"), (int, float)) and isinstance(value.get("y"), (int, float))
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        return isinstance(value[0], (int, float)) and isinstance(value[1], (int, float))
    return False


def _point_xy(value: Any) -> tuple[float, float] | None:
    if isinstance(value, dict) and isinstance(value.get("x"), (int, float)) and isinstance(value.get("y"), (int, float)):
        return float(value["x"]), float(value["y"])
    if isinstance(value, (list, tuple)) and len(value) >= 2 and isinstance(value[0], (int, float)) and isinstance(value[1], (int, float)):
        return float(value[0]), float(value[1])
    return None


def _path_handle(point: dict[str, Any], keys: tuple[str, ...]) -> tuple[float, float] | None:
    for key in keys:
        if key in point:
            return _point_xy(point.get(key))
    return None


def _validate_path_handle(value: Any, path: str, errors: list[str]) -> None:
    if value is not None and not _is_point(value):
        errors.append(f"{path} must contain numeric x/y coordinates")


def _validate_smooth_handle_geometry(point: dict[str, Any], path: str, errors: list[str]) -> None:
    anchor = _point_xy(point)
    if anchor is None:
        return
    kind = str(point.get("kind") or ("smooth" if point.get("smooth") is True else "corner")).lower()
    if kind == "corner" and point.get("smooth") is True:
        errors.append(f"{path} cannot set kind=corner and smooth=true at the same time")
        return
    if kind != "smooth":
        return
    backward = _path_handle(point, ("backward", "back", "in", "in_handle"))
    forward = _path_handle(point, ("forward", "out", "out_handle"))
    if backward is None or forward is None:
        errors.append(f"{path} kind=smooth requires both in/backward and out/forward handles")
        return
    in_vec = (anchor[0] - backward[0], anchor[1] - backward[1])
    out_vec = (forward[0] - anchor[0], forward[1] - anchor[1])
    in_len = math.hypot(*in_vec)
    out_len = math.hypot(*out_vec)
    if in_len <= 0.001 or out_len <= 0.001:
        errors.append(f"{path} kind=smooth requires non-zero handles")
        return
    angle = math.degrees(math.asin(min(1.0, abs(in_vec[0] * out_vec[1] - in_vec[1] * out_vec[0]) / max(in_len * out_len, 0.001))))
    if angle > 12:
        errors.append(f"{path} kind=smooth handles must be approximately collinear; error={angle:.2f}deg")


def _validate_path_points(points: Any, path: str, errors: list[str], *, closed: bool = True) -> None:
    min_points = 3 if closed else 2
    if not isinstance(points, list) or not min_points <= len(points) <= 512:
        errors.append(f"{path} must contain {min_points}..512 points")
        return
    for index, point in enumerate(points):
        if not _is_point(point):
            errors.append(f"{path}[{index}] must contain numeric x/y coordinates")
            continue
        if isinstance(point, dict):
            for key in ("backward", "back", "in", "in_handle", "forward", "out", "out_handle"):
                if key in point:
                    _validate_path_handle(point.get(key), f"{path}[{index}].{key}", errors)
            kind = point.get("kind")
            if kind is not None and str(kind).lower() not in {"smooth", "corner"}:
                errors.append(f"{path}[{index}].kind must be smooth or corner when provided")
            _validate_smooth_handle_geometry(point, f"{path}[{index}]", errors)


def _validate_bezier_handle_options(params: dict[str, Any], path: str, errors: list[str]) -> None:
    mode = params.get("handle_mode", params.get("handles"))
    if mode is not None:
        normalized = str(mode).lower().replace("-", "_").replace(" ", "_")
        if normalized not in {"manual", "auto", "auto_smooth", "catmull_rom", "catmullrom", "geometric", "geometry"}:
            errors.append(f"{path}.handle_mode must be manual, auto_smooth/catmull_rom, or geometric")
    handle_scale = params.get("handle_scale")
    if handle_scale is not None and not (isinstance(handle_scale, (int, float)) and 0.05 <= float(handle_scale) <= 2):
        errors.append(f"{path}.handle_scale must be 0.05..2 when provided")
    for key, minimum, maximum in (("tolerance", 0.5, 180), ("min_handle_ratio", 0, 1), ("max_handle_ratio", 0.05, 3), ("corner_angle_threshold", 45, 175), ("min_smooth_segment_length", 0, 1000)):
        value = params.get(key)
        if value is not None and not (isinstance(value, (int, float)) and minimum <= float(value) <= maximum):
            errors.append(f"{path}.{key} must be {minimum}..{maximum} when provided")
    audit = params.get("audit")
    if audit is not None and not isinstance(audit, dict):
        errors.append(f"{path}.audit must be an object when provided")


def validate_operation_recipe(payload: dict[str, Any]) -> dict[str, Any]:
    recipe = payload.get("operation_recipe") or payload.get("recipe")
    errors: list[str] = []
    warnings: list[str] = []
    step_ids: set[str] = set()
    created_group = False

    if not isinstance(recipe, dict):
        errors.append("operation_recipe must be an object")
    else:
        if recipe.get("schema_version") != SCHEMA_VERSION:
            errors.append("operation_recipe.schema_version must be ps-agent/v1")
        if not str(recipe.get("goal") or "").strip():
            errors.append("operation_recipe.goal must be a non-empty string")
        steps = recipe.get("steps")
        if not isinstance(steps, list) or not steps:
            errors.append("operation_recipe.steps must be a non-empty array")
        elif len(steps) > MAX_OPERATION_STEPS:
            errors.append(f"operation_recipe.steps must contain at most {MAX_OPERATION_STEPS} steps")
        else:
            for index, step in enumerate(steps):
                path = f"operation_recipe.steps[{index}]"
                if not isinstance(step, dict):
                    errors.append(f"{path} must be an object")
                    continue
                step_id = str(step.get("step_id") or f"step_{index + 1}")
                if not STEP_ID_RE.match(step_id):
                    errors.append(f"{path}.step_id must match {STEP_ID_RE.pattern}")
                if step_id in step_ids:
                    errors.append(f"Duplicate step_id: {step_id}")
                step_ids.add(step_id)
                atom_id = str(step.get("atom_id") or "")
                atom = ATOM_BY_ID.get(atom_id)
                if not atom:
                    errors.append(f"{path}.atom_id is unknown: {atom_id}")
                    continue
                if atom.get("status") == "planned":
                    errors.append(f"{path}.atom_id is planned but not executable yet: {atom_id}")
                    continue
                params = step.get("params")
                if params is not None and not isinstance(params, dict):
                    errors.append(f"{path}.params must be an object when provided")
                _validate_step_target(step.get("target"), f"{path}.target", errors)
                if atom_id in {"layer.group", "layer.create_group"}:
                    created_group = True
                if atom_id == "filter.gaussian_blur":
                    radius = (params or {}).get("radius")
                    if not isinstance(radius, (int, float)) or not 0.1 <= float(radius) <= 500:
                        errors.append(f"{path}.params.radius must be 0.1..500")
                if atom_id == "adjustment.hue_saturation":
                    adjustment_params = params or {}
                    ranges = {"master", "reds", "yellows", "greens", "cyans", "blues", "magentas"}
                    range_name = str(adjustment_params.get("range", "master")).lower()
                    if range_name not in ranges:
                        errors.append(f"{path}.params.range must be one of: {', '.join(sorted(ranges))}")
                    for key, minimum, maximum in (
                        ("hue", -180, 180),
                        ("saturation", -100, 100),
                        ("lightness", -100, 100),
                        ("opacity", 0, 100),
                    ):
                        value = adjustment_params.get(key)
                        if value is not None and not (
                            isinstance(value, (int, float)) and minimum <= float(value) <= maximum
                        ):
                            errors.append(f"{path}.params.{key} must be {minimum}..{maximum}")
                    if adjustment_params.get("clipping_mask") is True:
                        has_target = any(
                            value is not None
                            for value in (
                                step.get("target"),
                                adjustment_params.get("target_layer_id"),
                                adjustment_params.get("layer_id"),
                            )
                        )
                        if not has_target:
                            errors.append(f"{path}.params.target_layer_id or step.target is required when clipping_mask=true")
                if atom_id == "adjustment.create":
                    adjustment_params = params or {}
                    wants_clip = adjustment_params.get("clipping_mask") is True or adjustment_params.get("clip_to_target") is True
                    if wants_clip:
                        has_target = any(
                            value is not None
                            for value in (
                                adjustment_params.get("target_layer_id"),
                                adjustment_params.get("clip_to_layer_id"),
                            )
                        )
                        if not has_target:
                            errors.append(f"{path}.params.target_layer_id is required when clipping_mask=true")
                if atom_id in {"retouch.spot_heal_points", "retouch.healing_brush_points"}:
                    points = (params or {}).get("points")
                    if not isinstance(points, list) or not points:
                        errors.append(f"{path}.params.points must be a non-empty array")
                    elif len(points) > 80:
                        errors.append(f"{path}.params.points must contain at most 80 points")
                    else:
                        for point_index, point in enumerate(points):
                            point_path = f"{path}.params.points[{point_index}]"
                            if not isinstance(point, dict):
                                errors.append(f"{point_path} must be an object")
                                continue
                            if not isinstance(point.get("x"), (int, float)) or not isinstance(point.get("y"), (int, float)):
                                errors.append(f"{point_path}.x and .y must be numbers")
                            radius = point.get("radius", point.get("r"))
                            width = point.get("width")
                            height = point.get("height")
                            has_radius = isinstance(radius, (int, float)) and 1 <= float(radius) <= 500
                            has_size = isinstance(width, (int, float)) and isinstance(height, (int, float)) and 1 <= float(width) <= 1000 and 1 <= float(height) <= 1000
                            if not has_radius and not has_size:
                                errors.append(f"{point_path} must include radius 1..500 or width/height 1..1000")
                    feather = (params or {}).get("feather")
                    if feather is not None and not (isinstance(feather, (int, float)) and 0 <= float(feather) <= 200):
                        errors.append(f"{path}.params.feather must be 0..200")
                    expand = (params or {}).get("expand")
                    if expand is not None and not (isinstance(expand, (int, float)) and 0 <= float(expand) <= 200):
                        errors.append(f"{path}.params.expand must be 0..200")
                if atom_id == "retouch.content_aware_fill_selection":
                    feather = (params or {}).get("feather")
                    if feather is not None and not (isinstance(feather, (int, float)) and 0 <= float(feather) <= 200):
                        errors.append(f"{path}.params.feather must be 0..200")
                    expand = (params or {}).get("expand")
                    if expand is not None and not (isinstance(expand, (int, float)) and 0 <= float(expand) <= 200):
                        errors.append(f"{path}.params.expand must be 0..200")
                if atom_id == "retouch.clone_patch":
                    patches = (params or {}).get("patches")
                    has_single = isinstance((params or {}).get("source"), dict) and isinstance((params or {}).get("target"), dict)
                    if patches is None and not has_single:
                        errors.append(f"{path}.params must include patches[] or source + target")
                    if patches is not None:
                        if not isinstance(patches, list) or not patches:
                            errors.append(f"{path}.params.patches must be a non-empty array")
                        elif len(patches) > 40:
                            errors.append(f"{path}.params.patches must contain at most 40 items")
                    feather = (params or {}).get("feather")
                    if feather is not None and not (isinstance(feather, (int, float)) and 0 <= float(feather) <= 200):
                        errors.append(f"{path}.params.feather must be 0..200")
                    opacity = (params or {}).get("opacity")
                    if opacity is not None and not (isinstance(opacity, (int, float)) and 0 <= float(opacity) <= 100):
                        errors.append(f"{path}.params.opacity must be 0..100")
                if atom_id == "layer.set_properties":
                    opacity = (params or {}).get("opacity")
                    if opacity is not None and not (isinstance(opacity, (int, float)) and 0 <= float(opacity) <= 100):
                        errors.append(f"{path}.params.opacity must be 0..100")
                if atom_id == "document.set_canvas_size":
                    if (params or {}).get("width") is None and (params or {}).get("height") is None:
                        errors.append(f"{path}.params.width or params.height is required")
                if atom_id in {"asset.place_embedded", "asset.replace_contents"}:
                    if not ((params or {}).get("asset_uri") or (params or {}).get("uri") or (params or {}).get("asset_path")):
                        errors.append(f"{path}.params.asset_uri or params.asset_path is required")
                if atom_id in {"layer.move_above", "layer.move_below"}:
                    if (params or {}).get("reference_layer_id") is None and (params or {}).get("above_layer_id") is None and (params or {}).get("below_layer_id") is None:
                        errors.append(f"{path}.params.reference_layer_id is required")
                if atom_id == "layer.reorder":
                    position = str((params or {}).get("position") or (params or {}).get("to") or (params or {}).get("order") or "front").lower().replace("-", "_")
                    if position not in {"front", "top", "move_to_top", "bring_to_front", "back", "bottom", "move_to_back", "send_to_back", "above", "before", "move_above", "below", "after", "move_below"}:
                        errors.append(f"{path}.params.position must be front/top, back/bottom, above, or below")
                    if position in {"above", "before", "move_above", "below", "after", "move_below"}:
                        if (params or {}).get("reference_layer_id") is None and (params or {}).get("above_layer_id") is None and (params or {}).get("below_layer_id") is None:
                            errors.append(f"{path}.params.reference_layer_id is required for relative reorder")
                if atom_id == "layer.transform":
                    for key in ("scale_x", "scale_y"):
                        value = (params or {}).get(key)
                        if value is not None and not (isinstance(value, (int, float)) and 0.1 <= float(value) <= 10000):
                            errors.append(f"{path}.params.{key} must be 0.1..10000")
                    for key in ("width", "height"):
                        value = (params or {}).get(key)
                        if value is not None and not (isinstance(value, (int, float)) and 1 <= float(value) <= 30000):
                            errors.append(f"{path}.params.{key} must be 1..30000")
                    rotation = (params or {}).get("rotation")
                    if rotation is not None and not (isinstance(rotation, (int, float)) and -360 <= float(rotation) <= 360):
                        errors.append(f"{path}.params.rotation must be -360..360")
                if atom_id == "layer.align":
                    if not ((params or {}).get("align") or (params or {}).get("mode") or (params or {}).get("alignments")):
                        errors.append(f"{path}.params.align or params.alignments is required")
                if atom_id == "layer.distribute":
                    layer_ids = (params or {}).get("layer_ids")
                    if not isinstance(layer_ids, list) or len(layer_ids) < 2:
                        errors.append(f"{path}.params.layer_ids must contain at least two layer ids")
                if atom_id == "text.fit_to_box":
                    p = params or {}
                    if p.get("box_layer_id") is None and p.get("container_layer_id") is None and p.get("reference_layer_id") is None:
                        errors.append(f"{path}.params.box_layer_id is required")
                    align_x = p.get("align_x")
                    if align_x is not None and str(align_x).lower() not in {"left", "center", "right"}:
                        errors.append(f"{path}.params.align_x must be left, center, or right")
                    align_y = p.get("align_y")
                    if align_y is not None and str(align_y).lower() not in {"top", "center", "bottom"}:
                        errors.append(f"{path}.params.align_y must be top, center, or bottom")
                    fit_mode = p.get("fit_mode")
                    if fit_mode is not None and str(fit_mode).lower().replace("-", "_") not in {"position", "position_only", "move", "shrink", "shrink_to_fit", "scale_down_to_fit", "fit", "scale_to_fit"}:
                        errors.append(f"{path}.params.fit_mode must be position, shrink_to_fit, or fit")
                    max_iterations = p.get("max_iterations")
                    if max_iterations is not None and not (isinstance(max_iterations, int) and 1 <= int(max_iterations) <= 8):
                        errors.append(f"{path}.params.max_iterations must be an integer 1..8")
                    damping = p.get("damping")
                    if damping is not None and not (isinstance(damping, (int, float)) and 0.05 <= float(damping) <= 1):
                        errors.append(f"{path}.params.damping must be 0.05..1")
                    tolerance = p.get("tolerance", p.get("tolerance_px"))
                    if tolerance is not None and not (isinstance(tolerance, (int, float)) and 0.5 <= float(tolerance) <= 100):
                        errors.append(f"{path}.params.tolerance must be 0.5..100")
                if atom_id in {"shape.rectangle", "shape.ellipse", "shape.bezier_ellipse", "shape.rounded_rectangle", "shape.capsule", "shape.cut_corner_rect", "shape.ribbon", "shape.chevron", "shape.bracket", "shape.scalloped_triangle", "shape.wavy_band", "shape.callout", "shape.ticket_card", "shape.notched_panel", "shape.folded_corner", "shape.bauble"}:
                    if (params or {}).get("x") is None or (params or {}).get("y") is None:
                        errors.append(f"{path}.params.x and params.y are required")
                if atom_id == "shape.svg_asset_place":
                    p = params or {}
                    has_svg = isinstance(p.get("svg"), str) and bool(p.get("svg", "").strip())
                    has_path = any(isinstance(p.get(key), str) and bool(p.get(key, "").strip()) for key in ("path_data", "svg_path", "d"))
                    if not has_svg and not has_path:
                        errors.append(f"{path}.params.svg or params.path_data/svg_path/d is required")
                    if has_svg and not str(p.get("svg", "")).lstrip().lower().startswith("<svg"):
                        errors.append(f"{path}.params.svg must be a complete <svg> document")
                    for key in ("width", "height", "svg_width", "svg_height", "view_width", "view_height"):
                        value = p.get(key)
                        if value is not None and not (isinstance(value, (int, float)) and 1 <= float(value) <= 30000):
                            errors.append(f"{path}.params.{key} must be 1..30000 when provided")
                    for key in ("scale", "scale_x", "scale_y"):
                        value = p.get(key)
                        if value is not None and not (isinstance(value, (int, float)) and 0.1 <= float(value) <= 10000):
                            errors.append(f"{path}.params.{key} must be 0.1..10000 when provided")
                    rotation = p.get("rotation")
                    if rotation is not None and not (isinstance(rotation, (int, float)) and -360 <= float(rotation) <= 360):
                        errors.append(f"{path}.params.rotation must be -360..360 when provided")
                    opacity = p.get("opacity")
                    if opacity is not None and not (isinstance(opacity, (int, float)) and 0 <= float(opacity) <= 100):
                        errors.append(f"{path}.params.opacity must be 0..100 when provided")
                    path_opacity = p.get("path_opacity")
                    if path_opacity is not None and not (isinstance(path_opacity, (int, float)) and 0 <= float(path_opacity) <= 1):
                        errors.append(f"{path}.params.path_opacity must be 0..1 when provided")
                    stroke_width = p.get("stroke_width")
                    if stroke_width is not None and not (isinstance(stroke_width, (int, float)) and 0 <= float(stroke_width) <= 3000):
                        errors.append(f"{path}.params.stroke_width must be 0..3000 when provided")
                if atom_id in {"shape.rounded_rectangle", "shape.capsule", "shape.cut_corner_rect", "shape.ribbon", "shape.chevron", "shape.bracket", "shape.scalloped_triangle", "shape.wavy_band", "shape.callout", "shape.ticket_card", "shape.notched_panel", "shape.folded_corner"}:
                    for key in ("width", "height"):
                        value = (params or {}).get(key)
                        if value is None or not (isinstance(value, (int, float)) and 1 <= float(value) <= 30000):
                            errors.append(f"{path}.params.{key} must be 1..30000")
                if atom_id in {"shape.scalloped_triangle", "shape.blob", "shape.wavy_band", "shape.callout", "shape.ticket_card", "shape.notched_panel"}:
                    p = params or {}
                    smooth_iterations = p.get("smooth_iterations", p.get("smoothing_iterations", p.get("smoothness")))
                    if smooth_iterations is not None and not (isinstance(smooth_iterations, int) and 0 <= int(smooth_iterations) <= 5):
                        errors.append(f"{path}.params.smooth_iterations must be an integer 0..5")
                    smooth_ratio = p.get("smooth_ratio", p.get("smoothing_ratio"))
                    if smooth_ratio is not None and not (isinstance(smooth_ratio, (int, float)) and 0.05 <= float(smooth_ratio) <= 0.45):
                        errors.append(f"{path}.params.smooth_ratio must be 0.05..0.45")
                    max_points = p.get("max_points", p.get("max_point_count"))
                    if max_points is not None and not (isinstance(max_points, int) and 3 <= int(max_points) <= 256):
                        errors.append(f"{path}.params.max_points must be an integer 3..256")
                if atom_id == "shape.bauble":
                    diameter = (params or {}).get("diameter", (params or {}).get("size"))
                    if diameter is None or not (isinstance(diameter, (int, float)) and 1 <= float(diameter) <= 30000):
                        errors.append(f"{path}.params.diameter must be 1..30000")
                if atom_id == "shape.bezier_ellipse":
                    p = params or {}
                    width = p.get("width", p.get("diameter"))
                    height = p.get("height", p.get("diameter"))
                    if width is None or not (isinstance(width, (int, float)) and 1 <= float(width) <= 30000):
                        errors.append(f"{path}.params.width or params.diameter must be 1..30000")
                    if height is None or not (isinstance(height, (int, float)) and 1 <= float(height) <= 30000):
                        errors.append(f"{path}.params.height or params.diameter must be 1..30000")
                    rotation = p.get("rotation")
                    if rotation is not None and not (isinstance(rotation, (int, float)) and -3600 <= float(rotation) <= 3600):
                        errors.append(f"{path}.params.rotation must be -3600..3600 when provided")
                if atom_id == "shape.blob":
                    p = params or {}
                    has_center = p.get("center_x") is not None and p.get("center_y") is not None
                    has_xy = p.get("x") is not None and p.get("y") is not None
                    if not has_center and not has_xy:
                        errors.append(f"{path}.params.center_x/center_y or x/y is required")
                    for key in ("radius_x", "radius_y"):
                        value = p.get(key)
                        if value is not None and not (isinstance(value, (int, float)) and 1 <= float(value) <= 30000):
                            errors.append(f"{path}.params.{key} must be 1..30000 when provided")
                    point_count = p.get("point_count")
                    if point_count is not None and not (isinstance(point_count, int) and 8 <= int(point_count) <= 96):
                        errors.append(f"{path}.params.point_count must be an integer 8..96")
                    roughness = p.get("roughness")
                    if roughness is not None and not (isinstance(roughness, (int, float)) and 0 <= float(roughness) <= 0.75):
                        errors.append(f"{path}.params.roughness must be 0..0.75")
                if atom_id == "shape.arc_band":
                    p = params or {}
                    has_xy = p.get("x") is not None and p.get("y") is not None
                    has_center = p.get("center_x") is not None and p.get("center_y") is not None
                    if not has_xy and not has_center:
                        errors.append(f"{path}.params.x/y or center_x/center_y is required")
                    if p.get("radius") is None and p.get("outer_radius") is None:
                        errors.append(f"{path}.params.radius or params.outer_radius is required")
                    thickness = p.get("thickness")
                    if thickness is not None and not (isinstance(thickness, (int, float)) and 1 <= float(thickness) <= 30000):
                        errors.append(f"{path}.params.thickness must be 1..30000 when provided")
                    arc_span = p.get("arc_span", p.get("span"))
                    if arc_span is not None and not (isinstance(arc_span, (int, float)) and 1 <= float(arc_span) <= 359.5):
                        errors.append(f"{path}.params.arc_span must be 1..359.5 when provided")
                if atom_id == "shape.polygon":
                    points = (params or {}).get("points")
                    if not isinstance(points, list) or not 3 <= len(points) <= 256:
                        errors.append(f"{path}.params.points must contain 3..256 points")
                if atom_id == "shape.star":
                    star_points = (params or {}).get("points", (params or {}).get("point_count"))
                    if star_points is not None and not (isinstance(star_points, int) and 3 <= int(star_points) <= 64):
                        errors.append(f"{path}.params.points must be an integer 3..64 when provided")
                    outer_radius = (params or {}).get("outer_radius", (params or {}).get("radius"))
                    if outer_radius is not None and not (isinstance(outer_radius, (int, float)) and 1 <= float(outer_radius) <= 30000):
                        errors.append(f"{path}.params.outer_radius/radius must be 1..30000 when provided")
                    inner_radius = (params or {}).get("inner_radius")
                    if inner_radius is not None and not (isinstance(inner_radius, (int, float)) and 1 <= float(inner_radius) <= 30000):
                        errors.append(f"{path}.params.inner_radius must be 1..30000 when provided")
                if atom_id == "shape.starburst":
                    starburst_points = (params or {}).get("points", (params or {}).get("point_count"))
                    if starburst_points is not None and not (isinstance(starburst_points, int) and 3 <= int(starburst_points) <= 96):
                        errors.append(f"{path}.params.points must be an integer 3..96 when provided")
                    outer_radius = (params or {}).get("outer_radius", (params or {}).get("radius"))
                    if outer_radius is not None and not (isinstance(outer_radius, (int, float)) and 1 <= float(outer_radius) <= 30000):
                        errors.append(f"{path}.params.outer_radius/radius must be 1..30000 when provided")
                    inner_radius = (params or {}).get("inner_radius")
                    if inner_radius is not None and not (isinstance(inner_radius, (int, float)) and 1 <= float(inner_radius) <= 30000):
                        errors.append(f"{path}.params.inner_radius must be 1..30000 when provided")
                if atom_id == "shape.badge":
                    p = params or {}
                    has_center = p.get("center_x") is not None and p.get("center_y") is not None
                    has_xy = p.get("x") is not None and p.get("y") is not None
                    if not has_center and not has_xy:
                        errors.append(f"{path}.params.center_x/center_y or x/y is required")
                    radius = p.get("radius")
                    if radius is None or not (isinstance(radius, (int, float)) and 1 <= float(radius) <= 30000):
                        errors.append(f"{path}.params.radius must be 1..30000")
                if atom_id == "shape.line":
                    p = params or {}
                    has_xy = all(p.get(key) is not None for key in ("x1", "y1", "x2", "y2"))
                    has_objects = isinstance(p.get("start"), dict) and isinstance(p.get("end"), dict)
                    if not has_xy and not has_objects:
                        errors.append(f"{path}.params must include x1/y1/x2/y2 or start/end")
                    width = p.get("width", p.get("stroke_width"))
                    if width is not None and not (isinstance(width, (int, float)) and 0.5 <= float(width) <= 3000):
                        errors.append(f"{path}.params.width/stroke_width must be 0.5..3000 when provided")
                if atom_id == "shape.polyline":
                    points = (params or {}).get("points")
                    if not isinstance(points, list) or not 2 <= len(points) <= 128:
                        errors.append(f"{path}.params.points must contain 2..128 points")
                    width = (params or {}).get("width", (params or {}).get("stroke_width"))
                    if width is not None and not (isinstance(width, (int, float)) and 0.5 <= float(width) <= 3000):
                        errors.append(f"{path}.params.width/stroke_width must be 0.5..3000 when provided")
                if atom_id in {"shape.beads_on_path", "shape.dashed_path", "shape.arrow_path"}:
                    points = (params or {}).get("points")
                    if not isinstance(points, list) or not 2 <= len(points) <= 256:
                        errors.append(f"{path}.params.points must contain 2..256 points")
                    width = (params or {}).get("width", (params or {}).get("stroke_width"))
                    if width is not None and not (isinstance(width, (int, float)) and 0.5 <= float(width) <= 3000):
                        errors.append(f"{path}.params.width/stroke_width must be 0.5..3000 when provided")
                    if atom_id == "shape.beads_on_path":
                        radius = (params or {}).get("bead_radius", (params or {}).get("radius"))
                        if radius is None or not (isinstance(radius, (int, float)) and 0.5 <= float(radius) <= 1000):
                            errors.append(f"{path}.params.bead_radius must be 0.5..1000")
                        max_beads = (params or {}).get("max_beads")
                        if max_beads is not None and not (isinstance(max_beads, int) and 1 <= int(max_beads) <= 512):
                            errors.append(f"{path}.params.max_beads must be an integer 1..512")
                    if atom_id == "shape.dashed_path":
                        for key in ("dash_length", "gap_length"):
                            value = (params or {}).get(key)
                            if value is not None and not (isinstance(value, (int, float)) and 0 <= float(value) <= 10000):
                                errors.append(f"{path}.params.{key} must be 0..10000 when provided")
                    if atom_id == "shape.arrow_path":
                        head_size = (params or {}).get("head_size")
                        if head_size is not None and not (isinstance(head_size, (int, float)) and 1 <= float(head_size) <= 30000):
                            errors.append(f"{path}.params.head_size must be 1..30000 when provided")
                if atom_id in {"path.create_work_path", "path.bezier_work_path", "path.audit_bezier_handles", "shape.bezier_fill"}:
                    p = params or {}
                    if isinstance(p.get("subpaths"), list):
                        if not 1 <= len(p["subpaths"]) <= 32:
                            errors.append(f"{path}.params.subpaths must contain 1..32 subpaths")
                        for subpath_index, subpath in enumerate(p["subpaths"]):
                            if not isinstance(subpath, dict):
                                errors.append(f"{path}.params.subpaths[{subpath_index}] must be an object")
                                continue
                            _validate_path_points(
                                subpath.get("points"),
                                f"{path}.params.subpaths[{subpath_index}].points",
                                errors,
                                closed=subpath.get("closed") is not False,
                            )
                            if atom_id in {"path.bezier_work_path", "path.audit_bezier_handles", "shape.bezier_fill"}:
                                _validate_bezier_handle_options(subpath, f"{path}.params.subpaths[{subpath_index}]", errors)
                    else:
                        _validate_path_points(p.get("points"), f"{path}.params.points", errors, closed=p.get("closed") is not False)
                    if atom_id in {"path.bezier_work_path", "path.audit_bezier_handles", "shape.bezier_fill"}:
                        _validate_bezier_handle_options(p, f"{path}.params", errors)
                        if atom_id == "shape.bezier_fill":
                            fill_strategy = p.get("fill_strategy")
                            if fill_strategy is not None and str(fill_strategy).lower().replace("-", "_") not in {"selection", "selection_fill", "selection_fill_layer"}:
                                errors.append(f"{path}.params.fill_strategy currently supports selection_fill_layer only")
                    if atom_id == "path.create_work_path":
                        tolerance = p.get("tolerance")
                        if tolerance is not None and not (isinstance(tolerance, (int, float)) and 0.5 <= float(tolerance) <= 100):
                            errors.append(f"{path}.params.tolerance must be 0.5..100 when provided")
                        path_mode = p.get("path_mode")
                        if path_mode is not None and str(path_mode).lower().replace("-", "_") not in {"auto", "stable", "calibrated_bezier", "bezier", "direct", "dom"}:
                            errors.append(f"{path}.params.path_mode must be auto, stable, calibrated_bezier, bezier, direct, or dom when provided")
                if atom_id in {"path.to_selection", "shape.path_fill", "shape.bezier_fill", "shape.bezier_ellipse"}:
                    feather = (params or {}).get("feather")
                    if feather is not None and not (isinstance(feather, (int, float)) and 0 <= float(feather) <= 500):
                        errors.append(f"{path}.params.feather must be 0..500")
                if atom_id == "path.stroke":
                    width = (params or {}).get("width", (params or {}).get("stroke_width"))
                    if width is not None and not (isinstance(width, (int, float)) and 0.5 <= float(width) <= 3000):
                        errors.append(f"{path}.params.width/stroke_width must be 0.5..3000 when provided")
                    opacity = (params or {}).get("opacity")
                    if opacity is not None and not (isinstance(opacity, (int, float)) and 0 <= float(opacity) <= 100):
                        errors.append(f"{path}.params.opacity must be 0..100")
                if atom_id == "layer.effect_shadow":
                    opacity = (params or {}).get("opacity")
                    spread = (params or {}).get("spread")
                    if opacity is not None and not (isinstance(opacity, (int, float)) and 0 <= float(opacity) <= 100):
                        errors.append(f"{path}.params.opacity must be 0..100")
                    if spread is not None and not (isinstance(spread, (int, float)) and 0 <= float(spread) <= 100):
                        errors.append(f"{path}.params.spread must be 0..100")
                if atom_id in {"layer.effect_outer_glow", "layer.effect_stroke", "layer.effect_gradient_overlay", "gradient.fill"}:
                    opacity = (params or {}).get("opacity")
                    if opacity is not None and not (isinstance(opacity, (int, float)) and 0 <= float(opacity) <= 100):
                        errors.append(f"{path}.params.opacity must be 0..100")
                    if atom_id == "gradient.fill":
                        stops = (params or {}).get("stops")
                        if stops is not None and not (isinstance(stops, list) and 2 <= len(stops) <= 16):
                            errors.append(f"{path}.params.stops must contain 2..16 stops when provided")
                if atom_id in {"layer.extract_luminosity_range", "effect.bloom_layer"}:
                    range_name = str((params or {}).get("range", (params or {}).get("preset", "highlights"))).lower()
                    if range_name not in {"highlight", "highlights", "lights", "midtone", "midtones", "shadow", "shadows", "darks"}:
                        errors.append(f"{path}.params.range must be highlights, midtones, or shadows")
                    feather = (params or {}).get("feather")
                    if feather is not None and not (isinstance(feather, (int, float)) and 0 <= float(feather) <= 500):
                        errors.append(f"{path}.params.feather must be 0..500")
                    opacity = (params or {}).get("opacity")
                    if opacity is not None and not (isinstance(opacity, (int, float)) and 0 <= float(opacity) <= 100):
                        errors.append(f"{path}.params.opacity must be 0..100")
                    if atom_id == "effect.bloom_layer":
                        radius = (params or {}).get("blur_radius", (params or {}).get("radius"))
                        if radius is not None and not (isinstance(radius, (int, float)) and 0.1 <= float(radius) <= 500):
                            errors.append(f"{path}.params.blur_radius must be 0.1..500")
                if atom_id == "effect.light_rays":
                    count = (params or {}).get("count")
                    rays = (params or {}).get("rays")
                    if rays is not None and not isinstance(rays, list):
                        errors.append(f"{path}.params.rays must be an array when provided")
                    if count is not None and not (isinstance(count, int) and 1 <= count <= 24):
                        errors.append(f"{path}.params.count must be an integer 1..24")
                    opacity = (params or {}).get("opacity")
                    if opacity is not None and not (isinstance(opacity, (int, float)) and 0 <= float(opacity) <= 100):
                        errors.append(f"{path}.params.opacity must be 0..100")

        safety = recipe.get("safety") if isinstance(recipe.get("safety"), dict) else {}
        modifies = any(
            isinstance(step, dict)
            and ATOM_BY_ID.get(str(step.get("atom_id") or ""), {}).get("modifies_document")
            for step in recipe.get("steps", []) if isinstance(recipe.get("steps"), list)
        )
        if modifies and safety.get("allow_destructive") is True:
            errors.append("operation_recipe.safety.allow_destructive must be false for atom recipes")
        if modifies and not created_group:
            warnings.append("Modification recipes should create or group stage layers so rollback can delete one group.")
        review = recipe.get("review") if isinstance(recipe.get("review"), dict) else {}
        regions = review.get("regions")
        if modifies and regions is not None and not isinstance(regions, list):
            errors.append("operation_recipe.review.regions must be an array when provided")

    return {
        "status": "ok",
        "schema_version": SCHEMA_VERSION,
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "summary": summarize_operation_recipe(recipe) if isinstance(recipe, dict) else None,
    }


def summarize_operation_recipe(recipe: dict[str, Any]) -> dict[str, Any]:
    steps = recipe.get("steps") if isinstance(recipe.get("steps"), list) else []
    return {
        "recipe_id": recipe.get("recipe_id"),
        "goal": recipe.get("goal"),
        "step_count": len(steps),
        "atom_ids": [step.get("atom_id") for step in steps if isinstance(step, dict)],
        "stage_id": recipe.get("stage_id"),
        "workflow_id": recipe.get("workflow_id"),
    }


def review_operation_recipe(payload: dict[str, Any]) -> dict[str, Any]:
    recipe = payload.get("operation_recipe") or payload.get("recipe")
    validation = validate_operation_recipe({"operation_recipe": recipe})
    if not validation["valid"]:
        return {
            "status": "error",
            "schema_version": SCHEMA_VERSION,
            "error": {
                "code": "invalid_operation_recipe",
                "message": "Operation recipe validation failed.",
                "details": validation["errors"],
            },
        }
    return {
        "status": "ok",
        "schema_version": SCHEMA_VERSION,
        "recipe_id": recipe.get("recipe_id") or f"oprec-{uuid.uuid4().hex[:8]}",
        "reviews": [
            {
                "step_id": step.get("step_id"),
                "atom_id": step.get("atom_id"),
                "status": "needs_visual_review" if ATOM_BY_ID.get(str(step.get("atom_id") or ""), {}).get("modifies_document") else "informational",
                "suggested_feedback_mapping": {
                    "workflow_id": recipe.get("workflow_id"),
                    "stage_id": recipe.get("stage_id"),
                    "step_id": step.get("step_id"),
                    "atom_id": step.get("atom_id"),
                },
            }
            for step in recipe.get("steps", [])
            if isinstance(step, dict)
        ],
    }


