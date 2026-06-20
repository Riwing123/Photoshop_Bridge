---
name: ps-uxp-agent
description: Use when the user wants Codex to plan, validate, apply, or evaluate Adobe Photoshop edits through the local PS UXP Agent bridge, including open-ended layer effects, direct visual color grading, reference-image matching, masks/selections, previews, local crops, and non-destructive feedback loops.
metadata:
  short-description: Control Photoshop through PS UXP Agent
---

# PS UXP Agent

Use this skill for Photoshop editing workflows that should go through the local `ps-agent/v1` tool registry and MCP bridge.

## Default Workflow

1. Check backend connectivity with `ps_ping_backend`.
2. If the backend is unreachable, start it with `python backend/cli.py daemon start`, then re-check connectivity.
3. If UXP heartbeat, queue, or result delivery looks stale, call `ps_bridge_diagnostics` or run `python backend/cli.py doctor`.
4. Read the active Photoshop document with `ps_get_state`.
5. Export a full preview with `ps_export_preview`.
6. Export local crops with `ps_export_regions` when local evaluation or mask planning is needed.
7. Before any Photoshop modification, search the web for current editing references, tutorials, style examples, or technique notes relevant to the user's requested change; also call local RAG when available. Record the findings in the workflow or stage notes.
8. For any complex edit, create a staged workflow with `ps_create_workflow_plan`, then validate it with `ps_validate_workflow_plan`.
9. For broad style, reference-image grading, or stylized layer effects, use `effect_primitive_recipe` inside a workflow stage.
10. For precise manual plans, use `direct_visual_grade` inside a workflow stage.
11. For complex non-face local masks, prefer `ps_detect_grounding_boxes` plus `ps_generate_grounded_hq_mask` when the target is a semantic object class; use `ps_generate_sam_mask` or `ps_generate_hqsam_mask` when you already have a precise bbox/point prompt.
12. For complex masks, create and validate a flexible selection strategy with `ps_create_selection_strategy` and `ps_validate_selection_strategy` before running selection trials.
13. Treat Grounding DINO and HQ-SAM device status as part of mask planning. The default worker policy is GroundingDINO `auto` with CPU fallback when the CUDA extension is unavailable, and HQ-SAM `auto` with GPU-first execution when CUDA is available.
14. When Face Landmarker, SAM/HQ-SAM, Grounding, alpha masks, bbox, or Codex polygons produce a reusable local region, normalize it through `ps_create_region_artifact` before deciding whether it should become a layer mask, polygon lasso, or path representation.
15. Validate every edit plan with `ps_validate_plan`, every recipe with `ps_validate_layer_recipe`, and every workflow with `ps_validate_workflow_plan`.
16. Before applying a later stage, re-check the accepted result of all dependency stages. If an earlier stage is not good enough, pause the current stage and revise or delete the earlier stage first.
17. Apply only one stage at a time with `ps_apply_workflow_stage`.
18. Evaluate returned global preview and local regions with `ps_review_workflow_stage`.
19. If a specific stage result must be removed, call `ps_delete_workflow_stage`; if only an apply job id is known, call `ps_delete_agent_group`.
20. After all stages pass, call `ps_finalize_workflow_review`.
21. Iterate at most three times, then stop and summarize the result.

## staged_workflow Rules

Use this as a system-level rule for any complex Photoshop task, including color grading, layer effects, masks/selections, capability calls, and raw descriptor work.

1. Split the user goal into stages before executing.
2. Every modification workflow must include a `research_policy`: web search when possible, local RAG when available, direct preview/reference-image inspection, and recorded actionable findings.
3. Each stage must define `objective`, `expected_result`, `recipe_or_plan`, `review_regions`, `pass_criteria`, `rollback_target`, `pre_stage_checks`, and `previous_stage_recheck`.
4. Execute exactly one stage, then export preview/crops and review it.
5. Before executing a later stage, re-check whether previous accepted stages still satisfy their pass criteria under the current image state.
6. If a later stage reveals that an earlier stage was not good enough, do a backward correction: delete or revise the responsible earlier stage, then replay dependent stages only as needed.
7. If a stage fails, revise or delete that stage only. Do not blindly redo the whole image.
8. Feedback must map to `workflow_id -> stage_id -> step_id/primitive_id/capability_id`.
9. Simple low-risk single-step tasks may skip workflow: state read, preview export, one selection command, or deleting a known group.

## Stage Route And Effect Intent Rules

- `photo_effect` is a stage route only. It is not an executable Photoshop operation.
- White-soft, bloom, halation, haze, film mist, Tyndall light, night neon, and similar named looks are `effect_intent` values. They are not atoms, tools, descriptors, or presets.
- A `photo_effect` stage may carry `effect_intent` to explain the visual goal and mechanisms, but it must be lowered into `operation_recipe`, `selection_recipe`, `layer_recipe`, or a validated `ps-agent/v1` plan before execution.
- `effect_intent.mechanisms` may contain strategy terms such as `highlight_isolation`, `blurred_screen_glow`, `soft_light_diffusion`, or `subject_protection`; these terms guide Codex planning only.
- The executable layer must contain concrete atoms or plan ops, such as `layer.duplicate`, `filter.gaussian_blur`, `layer.set_properties`, `mask.apply_alpha`, `adjustment.create`, or whitelisted `ps-agent/v1` ops.
- Do not put `white_soft`, `highlight_bloom`, `soft_bloom`, `halation`, or similar concept names into `atom_id`, `op`, or raw descriptor payloads unless a future real atom with that exact id is registered and validated.
- If a `photo_effect` stage has only `effect_intent` and no lowered recipe, it is still a planning stage and must not be applied.

## Research And RAG Rules

- For every user-requested Photoshop modification, search the web for relevant current technique/style guidance unless the user explicitly says not to browse.
- Use web search for mutable or taste-sensitive material: named styles, retouching recipes, Photoshop feature behavior, plugin effects, layer-stack techniques, and examples of a requested look.
- Use local RAG for house rules, known descriptor limitations, style cards, effect primitives, failure traps, and tool-specific constraints.
- Treat web/RAG as guidance, not authority. Codex still writes the visual brief, mask strategy, stage objectives, and executable plan.
- Record concise findings: source idea, intended visual mechanism, useful parameter direction, and risk/failure trap.
- If search/RAG contradicts the current image needs, favor the image and explain the deviation in the stage notes.

## effect_primitive_recipe

Use this path for open-ended effect requests such as 白柔, 泛光, 胶片雾, 日系浅柔, 电影眩光, 商业通透, 夜景霓虹, 梦幻, 冷清, 复古, 清晨, or any request where the result should be built from composable visual mechanisms instead of a fixed preset.

1. Look at the target preview and any reference image directly.
2. Write a `visual_brief` covering source problems, desired visual mechanisms, protected areas, and forbidden artifacts.
3. Call `ps_retrieve_effect_primitives` with the user goal, visual brief, scene tags, and explicit avoid terms.
4. Select or remove primitives intentionally. Do not accept the retrieved list as a preset.
5. Call `ps_generate_layer_recipe` with selected primitives, strength, and review regions.
6. Validate with `ps_validate_layer_recipe`.
7. Apply with `ps_apply_layer_recipe`.
8. Export global preview and local crops.
9. Call `ps_review_layer_recipe` or manually map visual failures back to primitive ids.
10. If the result is wrong, remove the job group with `ps_delete_agent_group` and revise only the responsible primitive parameters.

Rules:

- Effects are not presets; effects are composable layer strategies.
- Every recipe should usually include at least three distinct primitive ids.
- Every primitive must have a reason, tunable parameters, review regions, and known failure modes.
- If glow is too strong, revise `highlight_bloom`, `soft_bloom`, `halation`, or related opacity/threshold parameters.
- If color is polluted, revise the responsible color primitive or add a local protection primitive.
- If the image is too gray, revise `black_lift`, `matte_fade`, or `contrast_compress`.
- If details are too soft, revise `soft_bloom`, `mist_layer`, `texture_soften`, or add `eye_protect` / `logo_protect`.

## direct_visual_grade

Use this lower-level path for requests like “日系清新”, “模仿参考图色调”, “电影感”, “通透风光”, “产品干净低饱和”, or any aesthetic color-grading request where Codex must make a visual judgment and hand-write one constrained `ps-agent/v1` plan.

1. Look at the target preview and any reference image directly.
2. Optionally search the web for current style examples or terminology when the user asks for a named style and no reference image is provided.
3. Call `ps_retrieve_style_guidance` for local RAG constraints and failure traps.
4. Write a `visual_brief` covering source problems, target look, protected areas, and forbidden artifacts.
5. Write a `mask_strategy` before writing the plan.
6. Hand-write one constrained `ps-agent/v1` plan. Do not use automatic grade candidate generation.
7. Include `review.regions` for every important mask or protected area.
8. After apply, inspect the after preview and local crops visually.
9. If the result is wrong, remove the job group with `ps_delete_agent_group` instead of keeping a bad edit.

## mask_strategy Rules

- Do not hard-route a target class to one selection tool. Sky does not always mean `ps_select_sky`; subject does not always mean `ps_select_subject`; semantic objects do not always mean SAM/Grounding. Choose based on visible edges, color separability, target ambiguity, protected regions, and current web/RAG findings.
- Treat Face Landmarker, SAM/HQ-SAM, Grounding, alpha mask, bbox, and Codex polygon outputs as region sources, not final destinations. Use `ps_create_region_artifact` to preserve all reusable representations.
- A region artifact can lower to different Photoshop expressions: `alpha_mask` for soft layer masks, `polygon` for polygon lasso/hard selection, and `bezier_path` for pen/path planning. Choose the expression based on the task.
- For polygon lasso/marching ants, call `ps_lower_region_to_selection_recipe` with `prefer="polygon"`, validate the recipe, then apply it.
- For high-fidelity local edits, keep `alpha_mask` and apply it as a layer mask. Do not convert alpha to polygon unless the user needs lasso/path-like behavior.
- For pen/path planning, call `ps_lower_region_to_path` when converting an alpha/polygon region into path data. Closed point paths can now be executed through `path.create_work_path`, then reused by `path.to_selection`, `shape.path_fill`, or `path.stroke`; open Bezier paths still require explicit descriptor calibration before implying native execution.
- For complex masks, call `ps_create_selection_strategy` before trials. The strategy must list plausible native Photoshop tools and local model tools with equal decision status, explain why each is worth trying, and define overlay/crop review criteria.
- The strategy stage must stay abstract. Do not emit executable `selection_mask.source: "color_range"` or any other concrete `selection_mask.source` as the strategy answer. Only lower a chosen candidate to executable `selection_mask` after RAG/web findings, trial overlay, and local crop review support that candidate.
- For `color_range` and tonal-range style selectors, do not start from one fixed threshold. Start from several seed values or seed profiles, review which one lands closest, then let the agent micro-tune only the relevant axes such as `fuzziness`, sampled colors, negative colors, tonal band choice, or localized clusters.
- When ambiguity is high, try at least two candidates before applying edits. Example: for sky, compare `ps_select_sky` with `ps_select_color_range` using blues/cyans/highlights or sampled colors; choose the one with less leakage and better boundary softness.
- A candidate may use the same method family multiple times. For sky/background color separation, it is valid after selection review to lower several Color Range trials with different presets, sampled colors, fuzziness values, and localized color cluster settings into one composite.
- Use different tools together when they solve different failure modes. Example: build a sky mask with multiple Color Range items, then subtract `select_subject`, face polygons, foreground bbox/polygon, or a model-generated alpha mask to protect the person.
- Evaluate every trial mask using global overlay and local crops around target edges and protected regions. Do not use a mask for `ps_apply_plan` until the trial passes.
- Use `replace` for the base selection, then `add`, `subtract`, or `intersect` to refine hard selections. `color_range`, `select_subject`, and `select_sky` may participate in composite add/subtract/intersect via temporary alpha channels. For soft alpha masks, composite add/subtract/intersect in the backend and pass one final `alpha_mask` to Photoshop.
- Prefer `ps_list_selection_atoms`, `ps_validate_selection_recipe`, and `ps_apply_selection_recipe` for executable mask trials. A `selection_recipe` is the execution-layer version of a reviewed strategy; it must contain candidates, merge policy, and review regions.
- Treat hard-selection and soft-alpha as separate buses. Hard selection can use Photoshop replace/add/subtract/intersect; soft alpha must be composited in backend and then passed as one `alpha_mask`.
- Use `global_base` only for overall exposure, white balance, contrast, highlight recovery, black floor, and broad atmosphere.
- Use `subject_protect` when the subject, product, logo, sky, whites, eyes, lips, or clothing must not follow the background grade.
- Use `background_grade` when the background target and subject target conflict.
- Prefer `select_subject`, `select_sky`, and `selection_mask.source: "composite"` for large semantic regions.
- Prefer `ps_generate_face_selection` for face parts: `left_eye`, `right_eye`, `both_eyes`, `lips_outer`, `face_oval`, `left_cheek`, and `right_cheek`.
- Prefer `ps_detect_grounding_boxes` plus `ps_generate_grounded_hq_mask` for semantic non-face objects such as buildings, signs, products, blackboards, flowers, clothing, and background props when text-guided detection is likely to be more stable than blind segmentation.
- If Grounding DINO reports `grounding_cpu_fallback_no_cuda_extension` or `grounding_fallback_reason: "grounding_cuda_extension_missing"`, keep the route valid but prefer cropped ROI, tighter prompts, fewer candidates, and explicit include/exclude prompts to control CPU latency.
- HQ-SAM should remain GPU-first under `PS_AGENT_HQSAM_DEVICE=auto`; if it falls back to CPU, reduce mask count or crop size before attempting large multi-instance masks.
- Prefer `ps_generate_sam_mask` or `ps_generate_hqsam_mask` for complex non-face regions when you already have a reliable bbox/point prompt and do not need text grounding.
- Use `selection_mask.source: "alpha_mask"` as the main high-fidelity mask path after SAM generation; use `ps_make_selection` only when marching ants verification is useful.
- Use Codex-generated polygon coordinates primarily for non-face regions such as boards, foliage, clothes, products, buildings, water, and sky details.
- Use bbox masks for simple rectangular areas and review crops.
- Do not silently degrade polygon masks to bbox masks. If polygon execution fails, report the structured error.
- Every local mask that affects the result must have a matching review crop.

## Tool Rules

- Do not generate arbitrary Photoshop JavaScript.
- Do not generate raw `batchPlay` unless a future developer-only tool explicitly enables it.
- PS capabilities must be exposed through the existing `backend/tool_registry.json`; the internal capability registry is only a catalog behind `ps_list_capabilities` and `ps_execute_capability`.
- For open-ended Photoshop layer construction, prefer `ps_list_operation_atoms`, `ps_validate_operation_recipe`, and `ps_apply_operation_recipe`. Effects such as white-soft, bloom, haze, or glare must be Codex-planned operation recipes, not fixed preset tools.
- Use `ps_apply_mask_to_layer` when a reviewed `selection_mask` or composited alpha mask should become a layer mask on a specific layer.
- Prefer dedicated tools for high-frequency capabilities; use `ps_execute_capability` for low-frequency registered capabilities.
- Raw batchPlay may be used only through `descriptor.raw_batchplay` with `user_confirmed=true` and `risk_acknowledged=true`.
- Destructive capabilities must be presented to the user before execution.
- Use `python backend/cli.py daemon status/start/stop/restart` for backend process management; do not ask UXP to launch the backend process.
- Use `python backend/cli.py sam status/start/stop/restart` for the optional SAM worker. If SAM is unavailable, report the structured diagnostics instead of silently falling back to a weak mask.
- Use `python backend/cli.py grounding status/start/stop/restart` for the optional Grounding DINO + HQ-SAM worker. Its stable default is `PS_AGENT_GROUNDING_DEVICE=auto` and `PS_AGENT_HQSAM_DEVICE=auto`: GroundingDINO falls back to CPU if `groundingdino._C` is missing, while HQ-SAM uses CUDA when available.
- Prefer non-destructive adjustment layers, selection masks, smart-object layers, and smart filters.
- Use only whitelisted apply ops: `adjust_exposure`, `adjust_vibrance`, `adjust_color_balance`, `adjust_hue_saturation`, `adjust_curves`, `adjust_levels`, `adjust_selective_color`, `adjust_gradient_map`, `adjust_color_lookup`, and `camera_raw_filter`.
- Prefer `camera_raw_filter` for global main color grading.
- Use `adjust_hue_saturation` for stable Photoshop Hue/Saturation adjustment layers, especially selective color-range refinements for skin, lips, clothing, or background color casts.
- Use `adjust_curves`, `adjust_levels`, `adjust_selective_color`, and `adjust_gradient_map` when Codex needs precise tonal shaping, black/white point control, color-family CMYK correction, or tone-mapped color styling. These are plan ops, not fixed style presets.
- Use operation atoms such as `layer.extract_luminosity_range`, `effect.bloom_layer`, `effect.light_rays`, `gradient.fill`, `layer.effect_outer_glow`, `layer.effect_stroke`, and `layer.effect_gradient_overlay` for composable light/effect/design layer stacks.
- Use `retouch.healing_brush_points`, `retouch.spot_heal_points`, `retouch.content_aware_fill_selection`, or `retouch.clone_patch` only after Codex has reviewed local crops and chosen explicit points, selections, or source/target patches.
- Use `target.type: "selection_mask"` for local non-destructive edits sourced from `current_selection`, `bbox`, `polygon`, `alpha_mask`, `color_range`, `select_subject`, `select_sky`, or `composite`.
- Use `selection_mask.operation: "replace" | "add" | "subtract" | "intersect"` for simple bbox/polygon masks when a current selection base is intentional.
- Prefer `selection_mask.source: "composite"` for deterministic multi-part selections. Composite items must be ordered, with the first item using `operation: "replace"` and later items using `add`, `subtract`, or `intersect`.
- Treat `selection_mask.source: "alpha_mask"` as replace-only. For add/subtract/intersect soft masks, generate a new composited alpha mask rather than hardening it through Photoshop selection booleans.
- Use document-pixel coordinates with top-left origin for all local regions.
- Export local regions with `max_side=1536` and `upscale_small_regions=true` by default so small crops are enlarged for inspection.
- For blemish retouching, first export a local crop, let Codex identify document-pixel blemish points/radii, then call `ps_retouch_spot_heal_points` in small batches on a duplicated retouch layer.
- Use `ps_retouch_content_aware_fill_selection` only after a reviewed active selection exists. Do not claim automatic blemish detection unless a future `ps_detect_blemishes` tool is implemented.
- Prefer `ps_delete_agent_group` over `ps_undo_last` when the apply `job_id` is known; it removes only the matching `Codex Agent - {job_id}` layer group.
- Use `ps_undo_last` only as a fallback when the user explicitly wants a Photoshop history rollback.

## Experimental Tools

- `ps_analyze_image_metrics` is the read-only observation layer for tone/color percentiles, compact histograms, Lab/HSV summaries, hue bands, dominant colors, and risk flags. Use it as evidence for Codex planning, never as a plan generator.
- `ps_score_grade_preview` is an auxiliary review helper.
- The previous automatic grade candidate generator has been removed. Broad style color grading must be planned by Codex as staged visual intent plus explicit recipes or constrained plans.
- `ps_retrieve_style_guidance` is safe as local RAG guidance only. It provides style constraints, aliases, mask guidance, and failure traps; it does not generate executable Photoshop descriptors.
- `ps_list_effect_primitives`, `ps_retrieve_effect_primitives`, `ps_generate_layer_recipe`, `ps_validate_layer_recipe`, `ps_apply_layer_recipe`, and `ps_review_layer_recipe` remain useful for adjustment-layer style planning. For real layer stacks such as duplicate+blur+Screen/Soft Light, use operation atoms instead.

## design_workflow

Use this path when the user asks for posters, commercial graphics, panels, collages, simple image stitching, cartoon-style layouts, or any design task that combines multiple assets and editable Photoshop layers.

Default asset folder:

`D:\Photo_sontrol\design_assets\inbox`

Design overlay rule:

- Design is a planning overlay, not a separate execution route. Enable `design_overlay.enabled=true` for posters, covers, cards, panels, collages, layout, titles, magazine looks, commercial graphics, or asset composition.
- Keep `design_overlay.enabled=false` for ordinary photo grading, skin retouching, white-soft effects, Tyndall light, blemish cleanup, or local photo edits unless a later stage explicitly needs design/layout constraints.
- Long tasks may mix routes stage by stage: simple image adjustment -> photo grading -> design layout -> final review.
- Codex must write `design_brief`, lock constraints in `design_lock`, and represent layout/depth as `layer_graph` before executable design stages.
- `layer_graph` nodes and edges are planning structure. They must lower into shared `operation_recipe` or `selection_recipe` before execution.

Rules:

1. Call `ps_scan_asset_library` before planning the layout.
2. Inspect the returned contact sheet directly and write an `asset_brief`.
3. Call `ps_analyze_design_assets` for deterministic asset metrics.
4. Create a staged plan with `ps_create_design_plan`; Codex must fill `design_brief`, `design_lock`, `layer_graph`, and executable `operation_recipe` or `selection_recipe` stages.
5. Use `ps_lower_design_stage_to_operation_recipe` to inspect the lowered recipe when the stage comes from `layer_graph`.
6. Use design operation atoms through `ps_apply_design_stage`; do not generate arbitrary Photoshop JavaScript.
7. Use `asset.place_embedded`, `layer.transform`, `layer.reorder`, `layer.create_clipping_mask`, `text.create`, `shape.rectangle`, `shape.ellipse`, `shape.polygon`, `shape.star`, `shape.line`, `shape.polyline`, `path.create_work_path`, `path.to_selection`, `shape.path_fill`, and `path.stroke` for the v1 executable path.
8. When Photoshop atoms cannot efficiently express a small visual component, or when the user explicitly asks for generated art, Codex may generate a small raster component and place it with `asset.place_embedded`; the component must still be treated as a layer asset inside the staged design workflow.
9. Treat planned atoms as unavailable for execution until calibrated.
10. Execute one stage at a time, export preview/crops, then review with `ps_review_design_stage`.
11. If layout, crop, or visual hierarchy fails, revise that stage instead of continuing to later effects.

## References

- Tool registry and current tool docs: `docs/tool_registry_zh.md`
- Retouch tools docs: `docs/retouch_tools_zh.md`
- Advanced Photoshop atom docs: `docs/advanced_ps_atoms_zh.md`
- Design workflow docs: `docs/design_workflow_zh.md`
- Design Overlay v2 docs: `docs/design_overlay_v2_zh.md`
- Protocol schemas: `backend/schemas/`
- MCP examples: `mcp/`
