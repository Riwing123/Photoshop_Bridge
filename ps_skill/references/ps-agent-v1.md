# ps-agent/v1 Reference

This reference is intentionally thin. The canonical protocol files live in the workspace root:

- Tool registry: `backend/tool_registry.json`
- Tool registry docs: `docs/tool_registry_zh.md`
- JSON Schemas: `backend/schemas/`
- MCP config examples: `mcp/`

## Required Call Order

1. `ps_ping_backend`
2. `ps_get_state`
3. `ps_export_preview`
4. `ps_export_regions` when local evaluation, mask planning, or region review is needed
5. Before any Photoshop modification, search the web for relevant current editing references/tutorials/style examples and call local RAG when available; record actionable findings
6. For complex work, create and validate a staged workflow with `ps_create_workflow_plan` and `ps_validate_workflow_plan`
7. For broad style, reference-image grading, or stylized layer effects, use `effect_primitive_recipe` inside one workflow stage
8. For precise manual color grading, use `direct_visual_grade` inside one workflow stage
9. `ps_generate_face_selection` when precise face-part selection is needed
10. `ps_detect_grounding_boxes` plus `ps_generate_grounded_hq_mask` when a semantic non-face object needs text-guided detection and a high-fidelity alpha mask
11. `ps_generate_sam_mask` or `ps_generate_hqsam_mask` when a complex non-face region already has a reliable bbox/point prompt
12. `ps_create_selection_strategy` and `ps_validate_selection_strategy` before complex mask trials
13. Check Grounding DINO + HQ-SAM device diagnostics when text-guided masks are used. Default policy is GroundingDINO `auto` with CPU fallback when `groundingdino._C` is unavailable, and HQ-SAM `auto` with CUDA-first execution.
14. Use `ps_list_selection_atoms`, `ps_validate_selection_recipe`, and `ps_apply_selection_recipe` for executable complex mask trials
15. Use `ps_list_operation_atoms`, `ps_validate_operation_recipe`, and `ps_apply_operation_recipe` for open-ended layer stacks such as duplicate/blur/blend/group/mask
16. Use `ps_apply_mask_to_layer` when a reviewed current selection or alpha mask must become a layer mask
17. `ps_validate_layer_recipe` or `ps_validate_plan`
18. Before applying a later stage, re-check whether earlier accepted stages still satisfy their pass criteria
19. `ps_apply_workflow_stage` for staged complex work
20. `ps_review_workflow_stage` after each stage, then `ps_finalize_workflow_review`
21. `ps_get_job` when the operation is asynchronous
22. `ps_delete_workflow_stage` or `ps_delete_agent_group` when a specific output must be removed

## Safety Rules

- Never call `ps_apply_plan` before `ps_validate_plan`.
- Keep all edits non-destructive.
- For broad aesthetic grading and stylized layer effects, do not match fixed presets. Use effect primitives as composable visual mechanisms, then generate or hand-write a constrained layer recipe.
- For real Photoshop layer structures, use operation atoms. White-soft, bloom, haze, glare, and film mist are Codex-planned `operation_recipe` stacks, not named preset tools.
- `photo_effect` is only a stage route. Named effects such as white-soft, bloom, halation, Tyndall light, film mist, and night neon must live in `effect_intent`, then be lowered to concrete `operation_recipe`, `selection_recipe`, `layer_recipe`, or validated `ps-agent/v1` plan payloads before execution.
- `effect_intent` and primitive names are never executable by themselves. Do not put concept names into `atom_id`, `op`, or descriptor payloads unless that exact executable atom/op is registered and validated.
- For complex masks, use selection atoms and `selection_recipe`. Hard-selection recipes may use Photoshop replace/add/subtract/intersect; soft-alpha masks must be composited in the backend before Photoshop receives one final `alpha_mask`.
- Complex tasks must be split into staged workflows. Apply and review one stage at a time.
- Every modification workflow must include `research_policy` findings from web search when possible, local RAG when available, and direct image/reference inspection.
- Later stages must re-check dependency stages before execution. If a later stage exposes that an earlier stage is not good enough, revise or delete the earlier stage first, then replay only dependent stages as needed.
- Feedback must map to `workflow_id -> stage_id -> step_id/primitive_id/capability_id`.
- PS capabilities are exposed through the existing `tool_registry.json`; the internal capability registry is only a catalog behind `ps_list_capabilities` and `ps_execute_capability`.
- For design drawing, prefer editable Photoshop atoms before raster assets: `shape.rectangle`, `shape.ellipse`, `shape.polygon`, `shape.star`, `shape.line`, `shape.polyline`, `path.create_work_path`, `path.to_selection`, `shape.path_fill`, `path.stroke`, and `text.create`. In v1, polygon/star/line/polyline lower to solid-color fill layers constrained by Photoshop selections; closed path fill/stroke uses a Photoshop work path converted to selection/fill/stroke. Open Bezier pen paths still need descriptor calibration before use.
- Use `layer.transform`, `layer.reorder`, and `layer.create_clipping_mask` as first-class layout atoms for posters, panels, collages, and object-over-text designs. Prefer `layer.reorder` over ad hoc `move_to_top` when the intended stack relation matters.
- Generated raster components are allowed when necessary or explicitly requested, especially for small illustrations, stickers, textures, icons, and decorative parts that Photoshop atoms cannot express efficiently. Place them with `asset.place_embedded`, keep them in named stage groups, and continue using normal layer/order/mask/review atoms around them.
- `recipe cards` are examples only; they are not closed enum presets.
- Use `ps_retrieve_effect_primitives` to retrieve candidate mechanisms, but Codex must still select/remove primitives intentionally.
- Use `ps_generate_layer_recipe`, `ps_validate_layer_recipe`, `ps_apply_layer_recipe`, and `ps_review_layer_recipe` for the open-ended recipe route.
- Use `ps_validate_operation_recipe` before `ps_apply_operation_recipe`, and `ps_validate_selection_recipe` before `ps_apply_selection_recipe`.
- For broad aesthetic grading, do not use automatic grade candidate generation. Use Codex visual judgment plus RAG/primitive guidance, then generate a layer recipe or hand-write one constrained plan.
- `ps_retrieve_style_guidance` is strategy only and must not be treated as an executable Photoshop descriptor.
- `ps_analyze_image_metrics` is evidence only: tone/color percentiles, compact histograms, Lab/HSV summaries, hue bands, dominant colors, and risk flags help Codex reason about the image, but they must not directly decide Photoshop parameters.
- `ps_score_grade_preview` is an auxiliary review helper and is not the default route. The previous automatic grade candidate generator has been removed.
- Use only whitelisted operations: `adjust_exposure`, `adjust_vibrance`, `adjust_color_balance`, `adjust_hue_saturation`, and `camera_raw_filter`.
- Use `camera_raw_filter` as the preferred global main color-grading path.
- Use `adjust_hue_saturation` for stable Photoshop Hue/Saturation adjustment layers. It supports `range: "master" | "reds" | "yellows" | "greens" | "cyans" | "blues" | "magentas"`, plus `hue`, `saturation`, and `lightness`.
- Use `target.type: "selection_mask"` for non-destructive local masks sourced from `current_selection`, `bbox`, `polygon`, `alpha_mask`, `color_range`, `select_subject`, `select_sky`, or `composite`.
- Do not hard-route target classes to one tool. Use `ps_create_selection_strategy` to compare native Photoshop selectors, Color Range, Face Landmarker, Grounding/HQ-SAM, SAM/HQ-SAM bbox or points, and Codex polygon/bbox when the mask is important.
- Selection strategy output must stay abstract and must not directly emit executable `selection_mask.source` values. Concrete `selection_mask` objects are allowed only after a candidate method wins RAG/web-guided trial review.
- For sky, background, and color/tone-separated regions, consider `ps_select_color_range` alongside semantic tools. Color Range can be better when the target is separated by hue/tone rather than object semantics.
- For Color Range and tonal-range selectors, do not anchor on one fixed threshold. Start with several seed profiles, review the closest hit, then micro-tune only the responsible axes such as `fuzziness`, sampled/negative color points, tonal band, or localized clusters.
- The same method family may appear multiple times in one final lowered composite. For example, after trial review, a sky mask can use Color Range blues with one fuzziness, add cyans with another fuzziness, add highlights if needed, then subtract subject or face/foreground masks.
- Every candidate trial needs overlay or selection preview plus local crops around target edges and protected regions before the mask is used for an edit.
- Simple bbox/polygon masks and generated Photoshop selections such as `color_range`, `select_subject`, and `select_sky` may set `selection_mask.operation` to `replace`, `add`, `subtract`, or `intersect`; non-replace operations intentionally combine with the current Photoshop selection.
- Prefer `selection_mask.source: "composite"` for deterministic multi-part masks. Composite item 0 must use `operation: "replace"`; later items may use `add`, `subtract`, or `intersect`.
- Prefer `selection_mask.source: "alpha_mask"` for high-fidelity masks generated by SAM or another mask generator. It is replace-only in the first implementation and preserves soft edges for adjustment layer masks.
- Prefer `ps_generate_face_selection` for face parts. It emits `selection_mask.source: "polygon"` masks from local MediaPipe Face Landmarker output.
- Prefer `ps_detect_grounding_boxes` plus `ps_generate_grounded_hq_mask` for semantic non-face objects such as buildings, signs, products, blackboards, clothes, flowers, and background props.
- If Grounding DINO reports `grounding_cpu_fallback_no_cuda_extension` or `grounding_fallback_reason: "grounding_cuda_extension_missing"`, continue with CPU detection but constrain the task with cropped ROI, tighter English prompts, fewer candidates, and explicit exclude prompts.
- HQ-SAM remains GPU-first under `PS_AGENT_HQSAM_DEVICE=auto`; if it falls back to CPU, reduce crop size or instance count before running large masks.
- Prefer `ps_generate_sam_mask` or `ps_generate_hqsam_mask` when a complex non-face region already has a reliable bbox/point prompt.
- Codex-generated polygon masks should primarily cover non-face regions, and should be used for face regions only as fallback.
- Do not silently degrade failed polygon selections to bbox selections.
- Camera Raw edits may use `target.type: "global"` for normal ACR smart-filter grading.
- Experimental ACR AI mask edits may use `target.type: "acr_ai_mask"` only with `plan.safety.allow_experimental_acr_masks: true`.
- `acr_ai_mask.engine: "camera_raw_internal"` is reserved for true Camera Raw internal AI masks and currently returns `acr_ai_mask_internal_unavailable` until a calibrated descriptor is available.
- `acr_ai_mask.engine: "photoshop_selection_fallback"` is implemented for `subject`, `background`, and `sky`: it applies ACR as a smart filter and constrains the smart-object layer with a Photoshop AI-generated mask.
- Prefer `ps_delete_agent_group` for cleanup when the apply `job_id` is known; it deletes only the matching `Codex Agent - {job_id}` layer group.
- Use `ps_undo_last` only when Photoshop history rollback is explicitly needed.
