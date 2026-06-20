# Design Overlay v2

Design Overlay 是 Codex 的设计规划层增强器，不是独立执行路线。所有 Photoshop 执行仍走共享 `operation_recipe` / `selection_recipe` 和现有原子组件。

## 触发规则

- `design_overlay.enabled=true`：海报、封面、卡片、面板、拼图、排版、加标题、杂志风、商业物料、素材编排。
- `design_overlay.enabled=false`：普通调色、白柔、丁达尔光、修肤、去瑕疵、局部修图。
- 长任务可以分阶段混合：简单图片处理 -> 调色 -> 设计排版 -> final review。

## 核心结构

- `design_brief`：由 Codex 编写，描述目标、受众、主信息、风格参考、禁忌、素材角色和输出用途。
- `design_lock`：固定可执行约束，包括画布、安全边距、主视觉区域、文字层级、色板、字体策略、素材清单、遮挡关系和导出规格。
- `layer_graph`：表达图层节点、层级、遮挡、剪贴、蒙版和对齐关系。

## Layer Graph

节点类型：

`canvas / group / image / cutout / text / shape / path / adjustment / mask / generated_asset`

边关系：

`parent / above / below / clip_to / mask_of / align_to / overlap_protect`

每个节点必须包含：

`node_id / node_type / role / bbox / z_order / stage_id / review_regions`

复杂遮挡，例如“人物挡住标题”，应表达为：

- 标题层：`text` node，z_order 较低。
- 人物层：`cutout` node，z_order 较高。
- 必要时使用 `mask_of` 或 `clip_to` 边关系修边。

## Lowering

使用 `ps_lower_design_stage_to_operation_recipe` 检查某个 stage 会下沉成什么共享 recipe。该工具不修改 Photoshop，不做审美选择，只做结构转换。

可 lower 的常见节点：

- `canvas` -> `document.create`
- `image / cutout / generated_asset` -> `asset.place_embedded` + `layer.transform`
- `text` -> `text.create`
- `shape` -> `shape.rectangle / shape.ellipse / shape.polygon / shape.star / shape.line / shape.polyline`
- `path` -> `path.create_work_path`，可选 `shape.path_fill` / `path.stroke`
- `adjustment` -> `adjustment.create`
- `mask` -> `mask.apply_alpha` 或 `mask.apply_current_selection`

可 lower 的常见边：

- `above / below` -> `layer.reorder`
- `clip_to` -> `layer.create_clipping_mask`
- `mask_of` -> `mask.apply_alpha`

`parent / align_to / overlap_protect` 是规划约束；如果必须修改 Photoshop，需要 Codex 显式写成具体 atom 步骤。

## 执行规则

- `ps_apply_design_stage` 每次只执行一个 stage。
- stage 可以来自已有 `operation_recipe`、`selection_recipe`，或由 `layer_graph` lower 得到。
- 禁止 raw Photoshop JavaScript。
- 禁止不可执行或 `planned` atom 进入执行 recipe。
- 每阶段执行后必须导出 preview/crops，并用 `ps_review_design_stage` 与 Codex 视觉复核。
