# PS Agent 原子组件清单与可靠性说明

本文档列出现有 `operation_atoms` 与 `selection_atoms` 的传参方式、执行方式和可靠性状态。它只描述当前工程已注册的原子组件，不包含 effect primitives、style cards 或普通 MCP tools。

## 可靠性等级

| 状态 | 是否可直接执行 | 含义 |
|---|---:|---|
| `stable` | 是 | 已接入 UXP/后端执行链路，通常是 DOM、已验证 descriptor 或现有稳定工具封装。仍可能因无活动文档、无选区、图层 id 错误、Photoshop modal busy 失败。 |
| `calibrated_batchplay` | 是 | 使用受控 batchPlay descriptor，当前 PS/UXP 版本下已校准。比 `stable` 更依赖 Photoshop 版本和 descriptor 细节。 |
| `existing_tool` | 间接可用 | 不是在 `operation_recipe` 内直接执行，而是调用已有工具，例如 `ps_export_preview`、`ps_generate_sam_mask`、`ps_generate_face_selection`。可靠性继承底层工具。 |
| `planned` | 否 | 只进入能力目录，方便 Codex 规划和识别缺口。当前 `ps_validate_operation_recipe` 会拒绝这些 atom，不应进入执行 recipe。 |

## 执行入口

| 原子类型 | 默认入口 | 执行位置 |
|---|---|---|
| 操作原子 | `ps_validate_operation_recipe` -> `ps_apply_operation_recipe` / `ps_apply_design_stage` | UXP `executeAsModal` |
| 选区原子 | `ps_validate_selection_recipe` -> `ps_apply_selection_recipe` | hard selection 在 UXP；soft alpha 先在后端合成 |
| 现有工具原子 | 对应 `ps_*` tool | 后端或 UXP job queue |

## Operation Atoms

| Atom | 状态 | 执行方式 | 关键参数 | 返回 | 可靠性备注 |
|---|---|---|---|---|---|
| `layer.create_group` | `stable` | UXP 创建图层组 | `name` | `group_id`, `group_name` | 可用；要求活动文档。 |
| `layer.duplicate` | `stable` | UXP 选择源图层并复制 | `source_layer_id?`, `name?` | `layer_id`, `layer_name` | 可用；源图层缺失会失败。 |
| `layer.select` | `stable` | UXP 按 id 单选/多选图层 | `layer_id` 或 `layer_ids` | `active_layer_ids` | 可用；多选依赖 Photoshop 连续选择行为。 |
| `layer.set_properties` | `stable` | UXP 设置当前/目标图层属性 | `name?`, `opacity? 0..100`, `blend_mode?`, `visible?` | `layer_id` | 可用；blend mode 仅支持当前映射表，未知值会回落或失败。 |
| `layer.group` | `stable` | UXP 多选图层后建组 | `name`, `layer_ids[]` | `group_id`, `group_name` | 可用；图层顺序/选择状态会影响组内顺序。 |
| `layer.delete` | `stable` | UXP 删除指定图层/组 | `layer_id` | `deleted_layer_id` | 可用但有破坏性结果；仅应删除 Codex 可追踪产物。 |
| `filter.gaussian_blur` | `calibrated_batchplay` | UXP 对目标层执行高斯模糊 | `radius 0.1..500` | `layer_id`, `radius` | 可用；会直接修改目标层像素/智能对象内容状态，建议作用于复制层。 |
| `adjustment.create` | `stable` | 复用 `ps_apply_plan` 调整层描述符 | `op`, `params`, `target?`, `layer?` | `layer_id`, `op` | 可用；仅支持已白名单调整操作。 |
| `mask.apply_current_selection` | `stable` | 当前选区转目标图层蒙版 | `target_layer_id?` | `layer_id`, `mask_applied` | 可用；必须已有活动选区。 |
| `mask.apply_alpha` | `stable` | 下载 alpha mask 并转图层蒙版 | `selection_mask`, `target_layer_id?` | `layer_id`, `mask_applied`, `selection` | 可用；要求全尺寸 alpha 或可解析 runtime asset。 |
| `selection.clear` | `stable` | 清除当前 Photoshop 选区 | 无特殊参数 | `has_active_selection` | 可用。 |
| `document.create` | `calibrated_batchplay` | UXP 新建 Photoshop 文档 | `width`, `height`, `name?`, `resolution?`, `background?` | `document` | 可用；第一步可在无活动文档时执行。背景矩形移动到底部做了容错。 |
| `document.set_canvas_size` | `calibrated_batchplay` | UXP 调用 canvas resize descriptor | `width?`, `height?`, `anchor?` | `document` | 可用；缩小画布可能裁掉画布外内容，需阶段复核。 |
| `document.export` | `existing_tool` | 使用 `ps_export_preview`/`ps_export_design_package` | 任意导出参数 | `asset_path`, `asset_url` | 间接可用；不在 operation recipe 内直接导出。 |
| `asset.place_embedded` | `calibrated_batchplay` | 后端 asset 下载到 UXP 临时文件后 place embedded | `asset_uri` 或 `asset_path`, `name?`, `x?`, `y?`, `width?`, `height?`, `scale_x?`, `scale_y?`, `rotation?` | `layer_id`, `layer_name`, `asset_uri` | 可用；素材需在 `backend/runtime/assets` 或可访问 URL；坐标变换基于当前 layer bounds。 |
| `asset.replace_contents` | `calibrated_batchplay` | UXP 下载 asset 后替换智能对象内容 | `layer_id?`, `asset_uri?`, `asset_path?` | `layer_id`, `asset_uri` | 可用但仅适合智能对象；普通像素层会被 Photoshop 拒绝。 |
| `layer.move_to_top` | `calibrated_batchplay` | UXP 将图层移到顶层 | `layer_id?` | `layer_id` | 可用；跨组移动边界仍需更多实测。 |
| `layer.move_above` | `calibrated_batchplay` | DOM moveAbove 优先，失败后 batchPlay 相对移动 | `layer_id`, `reference_layer_id` | `layer_id` | 可用；跨组移动仍需 Photoshop 实测复核。 |
| `layer.move_below` | `calibrated_batchplay` | DOM moveBelow 优先，失败后 batchPlay 相对移动 | `layer_id`, `reference_layer_id` | `layer_id` | 可用；跨组移动仍需 Photoshop 实测复核。 |
| `layer.transform` | `calibrated_batchplay` | UXP transform 当前/目标图层 | `layer_id?`, `x?`, `y?`, `width?`, `height?`, `scale_x?`, `scale_y?`, `offset_x?`, `offset_y?`, `rotation?` | `layer_id`, `transform` | 可用；普通图层的 `x/y/width/height` 按 bounds 计算；文字层在无旋转时优先按 `textClickPoint` 锚点定位，复杂 smart object/空 bounds 需复核。 |
| `layer.align` | `calibrated_batchplay` | 读取 bounds 后用 transform 位移对齐到画布 | `layer_id?`, `layer_ids?`, `align`, `to=canvas` | `layer_ids`, `applied` | 可用；第一版只支持对齐到画布。 |
| `layer.distribute` | `calibrated_batchplay` | 读取 bounds 后用 transform 做水平/垂直分布 | `layer_ids`, `axis?`, `spacing?` | `layer_ids`, `applied` | 可用；无 spacing 时至少 3 层，按中心等距。 |
| `layer.create_clipping_mask` | `calibrated_batchplay` | UXP 调用 clipping/groupEvent descriptor | `layer_id?` | `layer_id`, `clipping_mask` | 可用；要求目标层下方存在可剪贴基底层。 |
| `text.create` | `calibrated_batchplay` | UXP 创建可编辑文字层；支持原生段落文本框、真实行高/段落/内边距控制 | `text`, `x?`, `y?`, `font_size?`, `font?`, `color?`, `name?`, `text_kind?/paragraph?/native_paragraph?`, `box_layer_id?/box_x?/box_y?/box_width?/box_height?`, `padding*?/inset*?`, `align_x?`, `align_y?`, `line_height_*?`, `first_line_indent?/left_indent?/right_indent?/space_before?/space_after?`, `wrap_text?`, `wrap_mode?`, `auto_fit?`, `fit_mode?` | `layer_id`, `layer_name` | 默认仍可创建点文字；当显式请求段落框或提供容器 bounds 时，会优先走 Photoshop 原生 paragraph text。旧的轻量预换行/贴合仍可用于点文字兼容路径。 |
| `text.fit_to_box` | `calibrated_batchplay` | 用文字实际渲染 bounds 迭代校正到目标容器层内部，可选缩放适配 | `box_layer_id`, `text_layer_id?/layer_id?`, `align_x?`, `align_y?`, `fit_mode?`, `padding*?`, `max_iterations?`, `tolerance?`, `damping?` | `layer_id`, `box_layer_id`, `converged`, `final_bounds`, `final_error`, `iterations` | 可用；轻量版闭环，不做像素识别，直接基于 Photoshop 返回的文字可见 bounds 做 1-8 轮校正。 |
| `shape.rectangle` | `calibrated_batchplay` | UXP 创建纯色矩形 shape layer | `x`, `y`, `width`, `height`, `fill?`, `name?` | `layer_id`, `layer_name` | 可用；第一版只支持纯色填充。 |
| `shape.rounded_rectangle` | `calibrated_batchplay` | 用生成轮廓点创建圆角矩形填充形状 | `x`, `y`, `width`, `height`, `radius?`, `radius_*?`, `fill?`, `name?` | `layer_id`, `layer_name`, `point_count`, `radii` | 可用；当前实现是高密度轮廓点 + solid color fill，不承诺原生矢量圆角 shape。 |
| `shape.capsule` | `calibrated_batchplay` | 用生成轮廓点创建胶囊/药丸标签 | `x`, `y`, `width`, `height`, `fill?`, `name?` | `layer_id`, `layer_name`, `point_count`, `radius` | 可用；适合标签、按钮、状态 chip。 |
| `shape.cut_corner_rect` | `calibrated_batchplay` | 创建切角矩形/切角面板 | `x`, `y`, `width`, `height`, `corner_cut?`, `cut_*?`, `fill?`, `name?` | `layer_id`, `layer_name`, `point_count` | 可用；适合编辑感信息卡、标题条、装饰框。 |
| `shape.ellipse` | `calibrated_batchplay` | UXP 创建纯色椭圆 shape layer | `x`, `y`, `width?`, `height?`, `diameter?`, `fill?`, `name?` | `layer_id`, `layer_name` | 可用；第一版只支持纯色填充。 |
| `shape.ribbon` | `calibrated_batchplay` | 创建带尖角和尾口的丝带标签 | `x`, `y`, `width`, `height`, `direction?`, `point_depth?`, `tail_width?`, `notch_depth?`, `fill?`, `name?` | `layer_id`, `layer_name`, `point_count` | 可用；适合社媒卡小标签、章节头。 |
| `shape.arc_band` | `calibrated_batchplay` | 创建圆弧带/环形段 | `x/y` 或 `center_x/center_y`, `radius?/outer_radius?`, `thickness`, `start_angle?/end_angle?`, `arc_span?`, `rotation?`, `fill?`, `name?` | `layer_id`, `layer_name`, `point_count`, `arc_span`, `thickness` | 可用；适合核心节点外环、轨道、仪表盘式装饰。 |
| `shape.chevron` | `calibrated_batchplay` | 创建箭头块/流程导向块 | `x`, `y`, `width`, `height`, `direction?`, `point_depth?`, `notch_depth?`, `fill?`, `name?` | `layer_id`, `layer_name`, `point_count` | 可用；适合流程步骤、导向标签。 |
| `shape.bracket` | `calibrated_batchplay` | 创建括号/侧边框构件 | `x`, `y`, `width`, `height`, `side?`, `thickness?`, `fill?`, `name?` | `layer_id`, `layer_name`, `point_count`, `thickness` | 可用；适合编辑感页框、包裹式注释。 |
| `shape.polygon` | `calibrated_batchplay` | UXP 用 polygon 选区创建纯色填充层 | `points[]`, `fill?`, `name?`, `opacity?`, `blend_mode?`, `feather?` | `layer_id`, `layer_name`, `point_count`, `implementation` | 可用；实现为 solid color fill layer + selection mask，不是可编辑钢笔路径。 |
| `shape.star` | `calibrated_batchplay` | UXP 生成星形点位并创建纯色填充层 | `x/y` 或 `center_x/center_y`, `points?`, `outer_radius?`, `inner_radius?`, `rotation?`, `fill?`, `name?` | `layer_id`, `layer_name`, `star_points`, `implementation` | 可用；适合海报、节日装饰、徽章。第一版不是 path shape。 |
| `shape.line` | `calibrated_batchplay` | UXP 把线段转为厚度多边形条带并创建纯色填充层 | `x1/y1/x2/y2` 或 `start/end`, `width?`, `fill?`, `name?` | `layer_id`, `layer_name`, `width`, `cap` | 可用；第一版为 butt cap，不支持圆头线帽。 |
| `shape.polyline` | `calibrated_batchplay` | UXP 把折线段 union 为一个厚度选区并创建纯色填充层 | `points[]`, `width?`, `fill?`, `name?` | `layer_id`, `layer_name`, `segment_count`, `join` | 可用；第一版用 hard selection union，复杂曲线应先采样为 polyline。 |
| `layer.effect_shadow` | `calibrated_batchplay` | UXP 设置 drop shadow layer style | `layer_id?`, `color?`, `opacity?`, `distance?`, `size?`, `spread?`, `angle?` | `layer_id`, `effect` | 可用；会设置/覆盖当前 drop shadow 样式，需视觉复核。 |
| `layer.effect_outer_glow` | `calibrated_batchplay` | UXP 设置 outer glow layer style | `layer_id?`, `color?`, `opacity?`, `size?`, `spread?`, `blend_mode?` | `layer_id`, `effect` | 可用；适合核心节点、标签高亮、霓虹辅助层。 |
| `layer.effect_stroke` | `calibrated_batchplay` | UXP 设置 layer-style stroke | `layer_id?`, `color?`, `size?`, `position?`, `opacity?`, `blend_mode?` | `layer_id`, `effect` | 可用；适合细边框、双层边界、panel 轮廓。 |
| `layer.effect_gradient_overlay` | `calibrated_batchplay` | UXP 设置 gradient overlay layer style | `layer_id?`, `stops?`, `opacity?`, `angle?`, `scale?`, `style?`, `blend_mode?` | `layer_id`, `effect` | 可用；适合低对比面板层次、亮部染色、轨道节点高光。 |
| `gradient.fill` | `calibrated_batchplay` | 创建可编辑渐变填充层 | `name?`, `stops?`, `style?`, `angle?`, `scale?`, `opacity?`, `blend_mode?` | `layer_id`, `layer_name`, `gradient` | 可用；适合背景氛围、区域过渡和图形底色。 |
| `preview.export_global` | `existing_tool` | 调用 `ps_export_preview` | `max_side?`, `format?`, `quality?` | `asset_path`, `asset_url` | 间接可用。 |
| `preview.export_regions` | `existing_tool` | 调用 `ps_export_regions` | `regions[]`, `max_side?`, `upscale_small_regions?` | `regions` | 间接可用。 |

## 新增通用图形 Atom（0.10）

这些 atom 进入 `operation_recipe`，默认走稳定的采样点/selection/fill 路线，不依赖未校准的开放 Bezier path。

| Atom | 用途 | 关键参数 | 返回 |
|---|---|---|---|
| `shape.scalloped_triangle` | 波浪底边三角层、树冠、云状插画层 | `x`, `y`, `width`, `height`, `scallop_count?`, `scallop_depth?`, `tip_x?`, `smooth_iterations?`, `smooth_ratio?`, `fill?` | `layer_id`, `point_count`, `smoothing` |
| `shape.blob` | 不规则有机色块、背景斑块 | `center_x/center_y` 或 `x/y`, `radius_x?`, `radius_y?`, `seed?`, `roughness?`, `smooth_iterations?`, `smooth_ratio?` | `layer_id`, `point_count`, `seed`, `smoothing` |
| `shape.wavy_band` | 波浪条、分割带、流动背景 | `x`, `y`, `width`, `height`, `wave_count?`, `amplitude?`, `phase?`, `smooth_iterations?` | `layer_id`, `point_count`, `smoothing` |
| `shape.starburst` | 放射徽章、爆炸贴纸 | `center_x/center_y`, `outer_radius`, `inner_radius?`, `points?` | `layer_id`, `starburst_points` |
| `shape.beads_on_path` | 灯串、珠串、节点链 | `points[]`, `bead_radius`, `spacing?`, `max_beads?`, `highlight_fill?`, `group?` | `group_id?`, `layer_ids`, `bead_count`, `sampled_points` |
| `shape.dashed_path` | 虚线、流程路径 | `points[]`, `width?`, `dash_length?`, `gap_length?`, `group?` | `group_id?`, `layer_ids`, `dash_count` |
| `shape.arrow_path` | 折线箭头连接器 | `points[]`, `width?`, `head_size?`, `group?` | `layer_ids`, `shaft_layer_id`, `head_layer_id` |
| `shape.bauble` | 圆形挂饰、带高光装饰物 | `x`, `y`, `diameter`, `fill?`, `highlight_fill?`, `hook_fill?`, `group?` | `group_id?`, `layer_ids`, `body_layer_id` |
| `shape.badge` | 徽章、编号标、状态标 | `center_x`, `center_y`, `radius`, `style?`, `fill?`, `group?` | `group_id?`, `layer_ids` |
| `shape.callout` | 气泡、尖角标注框 | `x`, `y`, `width`, `height`, `tail_side?`, `tail_x?`, `tail_y?`, `smooth_iterations?` | `layer_id`, `point_count`, `smoothing` |
| `shape.ticket_card` | 票券边、齿孔感容器 | `x`, `y`, `width`, `height`, `notch_radius?`, `notch_count?`, `notch_side?`, `smooth_iterations?` | `layer_id`, `point_count`, `smoothing` |
| `shape.notched_panel` | 缺口面板、科技感容器 | `x`, `y`, `width`, `height`, `notch_size?`, `notch_positions?`, `smooth_iterations?` | `layer_id`, `point_count`, `smoothing` |
| `shape.folded_corner` | 折角卡片 | `x`, `y`, `width`, `height`, `corner?`, `fold_size?`, `fold_fill?`, `group?` | `group_id?`, `layer_ids`, `body_layer_id`, `fold_layer_id` |

评测 helper 不修改 Photoshop 文档：

| Helper | 调用方式 | 用途 |
|---|---|---|
| `preview.compare_reference` | `POST /api/preview/compare-reference` | 对导出预览与参考图做 deterministic 尺寸、颜色、主体 bbox、边缘密度、占比和简单相似度评分。 |
| `layout.detect_overflow` | `POST /api/layout/detect-overflow` | 检测内容贴边、主体 bbox 过满等风险。 |
| `layout.visual_score` | `POST /api/layout/visual-score` | 根据预览图和可选 recipe metadata 给出非矩形丰富度、构图和边缘安全评分。 |

轮廓圆化参数：

- `smooth_iterations` / `smoothing_iterations`：闭合轮廓 Chaikin 平滑轮数，范围 `0..5`。
- `smooth_ratio` / `smoothing_ratio`：切角比例，范围 `0.05..0.45`，默认约 `0.18..0.25`。
- `max_points`：平滑后的最大输出点数，范围 `3..256`，避免超过 Photoshop polygon selection 稳定边界。
- `smooth: false`：关闭默认圆化。当前 `shape.blob` 默认 2 轮，`shape.scalloped_triangle / shape.ticket_card / shape.notched_panel` 默认 1 轮，`shape.wavy_band / shape.callout` 默认 0 轮但可显式开启。

## Selection Atoms

| Atom | 状态 | Bus | 执行方式 | 关键参数 | 返回 | 可靠性备注 |
|---|---|---|---|---|---|---|
| `selection.select_subject` | `stable` | `hard_selection` | Photoshop 原生 Select Subject | 允许附加参数 | `channel_name`, `has_active_selection` | 可用但受当前图像/PS AI 状态影响；可能提示不可用或选区为空。 |
| `selection.select_sky` | `stable` | `hard_selection` | Photoshop 原生 Select Sky | 允许附加参数 | `channel_name`, `has_active_selection` | 可用但天空识别不总是优于颜色范围。 |
| `selection.color_range` | `calibrated_batchplay` | `hard_selection` | Photoshop Color Range descriptor | `preset?`, `color?`, `fuzziness?` | `channel_name`, `has_active_selection` | 可用；应从 seed ladder 开始并根据 overlay 微调。 |
| `selection.tonal_range` | `stable` | `hard_selection` | Color Range tonal seed | `preset: highlights/midtones/shadows`, `fuzziness?` | `channel_name`, `has_active_selection` | 可用；不是固定 Lights 1-5 体系，需反馈微调。 |
| `selection.focus_area` | `calibrated_batchplay` | `hard_selection` | Photoshop Focus Area descriptor | `in_focus_range?`, `noise_level?` | `channel_name`, `has_active_selection` | 可用性依赖当前 PS descriptor；可能不可用。 |
| `selection.current` | `stable` | `hard_selection` | 保存当前活动选区 | 任意附加参数 | `channel_name`, `has_active_selection` | 可用；必须已有活动选区。 |
| `selection.bbox` | `stable` | `hard_selection` | 文档坐标矩形选区 | `bbox: {x,y,width,height}` | `channel_name`, `has_active_selection` | 可用；边缘硬，适合粗选/遮挡/保护。 |
| `selection.polygon` | `stable` | `hard_selection` | 文档坐标多边形选区 | `points[]` | `channel_name`, `has_active_selection` | 可用；点位质量决定结果，边缘仍是 hard selection。 |
| `selection.alpha_mask` | `stable` | `soft_alpha` | 使用 full-document alpha PNG | `asset_path?`, `asset_uri?`, `threshold?`, `feather?` | `alpha_mask`, `mask_asset`, `area_ratio` | 作为 soft mask 候选可靠；add/subtract/intersect 应先在后端合成，不能直接硬布尔。 |
| `selection.face_landmarker` | `existing_tool` | `hard_selection` | 调用 `ps_generate_face_selection`，再 lower 为 polygon | 透传 face selection 参数 | `points`, `bbox` | 依赖 MediaPipe 模型文件和人脸可见性；适合五官/脸部结构。 |
| `selection.sam_mask` | `existing_tool` | `soft_alpha` | 调用 `ps_generate_sam_mask` | 透传 bbox/points 等参数 | `alpha_mask`, `overlay_preview` | 依赖 SAM worker 和提示质量；需 overlay 复核。 |
| `selection.hqsam_mask` | `existing_tool` | `soft_alpha` | 调用 `ps_generate_hqsam_mask` | 透传 bbox/points 等参数 | `alpha_mask`, `overlay_preview` | 依赖 Grounding/HQ worker 和模型；需 overlay 复核。 |
| `selection.grounding_dino_boxes` | `existing_tool` | `detector` | 调用 `ps_detect_grounding_boxes` | 透传英文 prompt/阈值参数 | `detections`, `overlay_preview` | 只是检测框，不是最终 mask；CPU fallback 会慢。 |
| `selection.grounded_hqsam_mask` | `existing_tool` | `soft_alpha` | Grounding DINO 检测 + HQ-SAM 分割 | 透传 include/exclude prompt、阈值、合成策略 | `alpha_mask`, `detections`, `overlay_preview` | 适合语义物体；可靠性取决于 prompt、检测框和模型状态。 |
| `selection.channel_load` | `stable` | `hard_selection` | 加载已有 alpha channel 为选区 | `channel_name` | `channel_name`, `has_active_selection` | 可用；channel 名必须存在。 |
| `selection.refine` | `stable` | `hard_selection` | 对当前选区 feather/expand/contract/smooth/inverse | `operation?`, `amount?`, `invert?` | `has_active_selection` | 可用；必须已有活动选区。 |

## 当前结论

1. 当前共有 `29` 个 operation atoms：`stable` 10 个、`calibrated_batchplay` 16 个、`existing_tool` 3 个、`planned` 0 个。
2. 当前共有 `16` 个 selection atoms：`stable` 9 个、`calibrated_batchplay` 2 个、`existing_tool` 5 个。本地模型类可靠性依赖 worker、模型文件、提示质量和 overlay 复核。
3. 设计功能第一版可稳定依赖的核心链路是：
   - `document.create`
   - `asset.place_embedded`
   - `layer.transform`
   - `layer.move_to_top`
   - `shape.rectangle`
   - `text.create`
   - `layer.group`
   - `preview.export_global`
4. 当前 operation atom 中已没有 `planned` 项；新增设计/布局能力多为 `calibrated_batchplay`，需要通过 Photoshop reload 后实测确认 descriptor 兼容性。
5. 对视觉质量有影响的 atom，即使技术执行成功，也必须通过 preview/crop 做阶段复核。

## 不可执行或不可直接执行详情

## 0.11 原生 Bezier Path Atom

这两个曲线路径 atom 已切换到 Photoshop UXP DOM 路线：`document.pathItems.add(name, SubPathInfo[])`。旧的 batchPlay 低层路径探针已从默认能力中移除，避免再次触发 Photoshop “程序错误”模态框。

| Atom | 用途 | 关键参数 | 返回 |
|---|---|---|---|
| `path.bezier_work_path` | 创建 Photoshop 原生 Bezier PathItem | `points[]` 或 `subpaths[]`；点支持 `in/out/backward/forward` handles；`closed?`, `closed_only?` | `path_kind`, `path_id`, `path_name`, `subpath_count`, `point_count`, `fallback_used=false` |
| `shape.bezier_fill` | 创建原生 Bezier PathItem 后转选区并填充成图层 | 同上，另加 `fill/color`, `opacity?`, `blend_mode?`, `feather?`, `name?` | `layer_id`, `layer_name`, `path`, `implementation` |

注意：
- 默认不再组装低层 path descriptor，也不再暴露旧探针参数。
- 闭合 Bezier 路径可通过 `PathItem.makeSelection()` 转选区后填充。
- 开放 Bezier 路径可先稳定生成 PathItem；可控线宽描边仍需后续单独校准 `strokePath()`/画笔状态路线。
### A. 本轮已补齐为可执行的原 `planned` atom

这些 atom 已从 `planned` 升级为 `calibrated_batchplay`，现在可以进入 `operation_recipe`。它们不是“绝对稳定 DOM”，而是有真实 UXP/batchPlay 执行分支，并在失败时返回结构化错误。

| Atom | 当前执行能力 | 仍需注意 |
|---|---|---|
| `document.set_canvas_size` | `canvasSize` descriptor，支持 `width/height/anchor`。 | 缩小画布可能裁切画布外内容；先用于设计画布扩展/留白。 |
| `asset.replace_contents` | 下载 backend asset，调用 `placedLayerReplaceContents`。 | 只适合智能对象；普通图层会失败。 |
| `layer.move_above` | DOM `moveAbove` 优先，失败后 batchPlay `move`。 | 跨组移动和背景层附近仍需实测。 |
| `layer.move_below` | DOM `moveBelow` 优先，失败后 batchPlay `move`。 | 跨组移动和背景层附近仍需实测。 |
| `layer.align` | 读取 layer bounds，用 transform 对齐到 canvas。 | 第一版只支持 `to=canvas`，不支持 selection/reference layer。 |
| `layer.distribute` | 读取 layer bounds，用 transform 做水平/垂直分布。 | 无 `spacing` 时至少 3 层；按中心等距。 |
| `layer.create_clipping_mask` | 调用 clipping/groupEvent descriptor。 | 要求目标层下方存在可剪贴基底层。 |
| `shape.ellipse` | 创建纯色椭圆 shape layer。 | 第一版无 stroke/path 布尔编辑。 |
| `layer.effect_shadow` | 设置 drop shadow layer style。 | 会设置当前 drop shadow；复杂多重样式后续再扩展。 |

### B. `existing_tool`：不在 atom recipe 内直接执行，但可通过工具执行

这些不是“不能用”，而是不能作为普通 `operation_recipe` / `selection_recipe` 内的一步直接执行。Codex 应调用对应 `ps_*` 工具，拿到结果后再 lower 成可执行 atom 或 plan。

| Atom | 对应工具 | 使用方式 | 注意事项 |
|---|---|---|---|
| `document.export` | `ps_export_preview` / `ps_export_design_package` | 作为导出阶段独立调用。 | 不应放进 `operation_recipe`；导出不修改 PS 文档。 |
| `preview.export_global` | `ps_export_preview` | 每个 stage 后导出全图。 | 用于评测，不是设计图层操作。 |
| `preview.export_regions` | `ps_export_regions` | 导出局部 crop。 | 每个关键主体/文字/边缘区域都应有 review crop。 |
| `selection.face_landmarker` | `ps_generate_face_selection` | 先生成 face polygon/bbox，再 lower 为 `selection.polygon` 或保护 mask。 | 依赖 MediaPipe 模型和人脸可见性。 |
| `selection.sam_mask` | `ps_generate_sam_mask` | 先生成 alpha mask，再作为 `selection.alpha_mask` 使用。 | 依赖 SAM worker；必须看 overlay。 |
| `selection.hqsam_mask` | `ps_generate_hqsam_mask` | 先生成 alpha mask，再作为 `selection.alpha_mask` 使用。 | 依赖 Grounding/HQ worker 和 bbox/点提示。 |
| `selection.grounding_dino_boxes` | `ps_detect_grounding_boxes` | 只返回检测框；检测后再分割或转 bbox。 | 它不是最终选区。 |
| `selection.grounded_hqsam_mask` | `ps_generate_grounded_hq_mask` | 生成语义 alpha mask，再作为 `selection.alpha_mask` 使用。 | 适合物体/建筑/衣服等语义目标。 |

## 2026-06-17 新增：布局、路径、描边与剪贴原子

本轮把海报、面板、插画编排常用的底层能力继续拆成可校验 atom。所有 atom 仍通过
`ps_validate_operation_recipe -> ps_apply_operation_recipe` 执行，不能直接暴露 raw descriptor。

| Atom | 当前状态 | 作用 | 关键参数 | 可靠性说明 |
|---|---|---|---|---|
| `layer.reorder` | `calibrated_batchplay` | 统一移动图层顺序，覆盖置顶、置底、移到参考层上方/下方 | `layer_id?`, `position`, `reference_layer_id?` | 是 `layer.move_to_top/move_above/move_below` 的统一入口；跨组移动仍需视觉复核。 |
| `layer.transform` | `calibrated_batchplay` | 缩放、位移、按左上角定位、旋转图层 | `x/y`, `width/height`, `scale_x/scale_y`, `offset_x/offset_y`, `rotation` | 可用于文字、素材、形状层；复杂 smart object 或空 bounds 图层需要复核。 |
| `layer.create_clipping_mask` | `calibrated_batchplay` | 将目标层剪贴到下方基底层 | `layer_id?` | 已可执行；要求目标层下方存在可剪贴基底层。 |
| `layer.release_clipping_mask` | `calibrated_batchplay` | 释放目标层剪贴关系 | `layer_id?` | 新增；优先 DOM `isClippingMask=false`，失败时走 `ungroupEvent` descriptor。 |
| `path.create_work_path` | `calibrated_batchplay` | 从点位或 subpath 创建 Photoshop work path | `points[]` 或 `subpaths[]`, `closed`, `tolerance`, `direct?`, `path_mode?` | 默认闭合点位走“选区 -> Make Work Path”，更稳；`path_mode=stable` 只走稳定闭合轮廓；开放路径/贝塞尔 handle 走 `path_mode=dom` / DOM PathItem。 |
| `path.to_selection` | `calibrated_batchplay` | 将当前 work path 转为当前选区 | `operation`, `feather`, `anti_alias` | 用于 path 后续变成蒙版、填充、描边；依赖当前文档存在 work path。 |
| `path.stroke` | `calibrated_batchplay` | 对当前 work path 做描边 | `width`, `location`, `color`, `opacity`, `blend_mode`, `name?` | 当前实现为 `work path -> selection -> Photoshop Stroke`，比画笔 stroke 更可控。 |
| `shape.path_fill` | `calibrated_batchplay` | 用当前 work path 创建纯色填充层 | `fill/color`, `opacity`, `blend_mode`, `name?` | 当前实现为 `work path -> selection -> solid color fill layer`，不是直接矢量 shape path。 |

注意：`path.create_work_path` 的默认路线已经避免直接点位 descriptor，因为该 descriptor 在当前 Photoshop build 上可能弹出“程序错误”模态框。需要真正开放贝塞尔 pen path 时，应先用 Adobe Action Recording / Copy As JavaScript 捕获并校准 descriptor，再升为默认路径。

## 2026-06-19 Bezier 输入管线升级

### 两阶段落地

1. `path.audit_bezier_handles`：只审计 Bezier 锚点与手柄，不修改 Photoshop 文档。检查项包括 smooth 点手柄共线性、in/out 方向、手柄长度比例和点数结构。
2. `path.bezier_work_path` / `shape.bezier_fill`：接入同一套归一化逻辑，支持 `handle_mode` 自动生成手柄，并在返回值中带上 `path_audit`。

### 新增参数

- `handle_mode`: `manual`、`auto_smooth` / `catmull_rom`、`geometric`。
- `handle_scale`: 自动手柄缩放，范围 `0.05..2`，默认 `1`。
- `auto_repair_handles`: 在 `manual` 模式下为缺失的 in/out 手柄补全自动手柄。
- `audit`: 可选审计配置，也可直接传 `tolerance`、`min_handle_ratio`、`max_handle_ratio`。

### 推荐用法

- 写复杂轮廓时优先只给 anchors，再设置 `handle_mode: "catmull_rom"`，避免手写绝对手柄导致轮廓塌陷。
- 真要手写手柄时，先运行 `path.audit_bezier_handles`，看 `path_audit.warnings`，再执行填充。
- 当前 `shape.bezier_fill` 的稳定填充路线仍是 `PathItem -> selection -> solid color layer`，不是 compound vector shape；负空间仍需单独设计或后续 compound atom。
### 2026-06-19 防打结更新

`handle_mode: "catmull_rom"` 现在使用角度感知手柄：高转角锚点会自动降级为 corner，普通曲线点会按转角缩短 in/out handle，避免尖点附近出现小环、反折和打结。

新增可调参数：

- `corner_angle_threshold`: 转角超过该值时保留为尖角，默认 `118`，更小会保留更多尖角。
- `min_smooth_segment_length`: 邻边过短时不强行平滑，默认 `12`。

如果预览里出现顶点打结，优先把 `corner_angle_threshold` 调到 `100..112`，同时把 `handle_scale` 控制在 `0.75..0.95`。

## Bezier ??????

- `shape.bezier_fill` ??? `kind` ??????`kind: "smooth"` ???????????????? `in/backward` ? `out/forward` ?????????????`kind: "corner"` ????? cusp?????????
- `smooth: true` ?????? recipe ??????? `kind: "corner"` ?????
- `shape.bezier_ellipse` ????? 4 ? cubic Bezier ?? atom??? `x/y/width/height/diameter/rotation/fill/opacity/name`?????????Logo ???????????????
- `path.audit_bezier_handles` ??? `not_smooth_collinear`?`smooth_missing_handle`?`self_intersection` ? warning???????????????????????





## SVG 装饰资产 Atom

### `shape.svg_asset_place`

定位：把 SVG 作为画面设计的重要补充层，用于复杂曲线装饰、贴纸、星芒、徽章、手绘箭头、光束、Logo 类元素和背景纹样。它不替代 `shape.rectangle / shape.polygon / text.create` 等基础结构 atom。

常用参数：
- `svg`：完整 `<svg>` 字符串。
- `path_data` / `svg_path` / `d`：单个 SVG path 的 `d` 数据，系统会包装成完整 SVG。
- `svg_width` / `svg_height` / `viewBox`：SVG 自身坐标系。
- `x` / `y` / `width` / `height` / `scale` / `rotation` / `opacity`：作为 Photoshop placed asset 后的整体变换。
- `fill` / `stroke` / `stroke_width`：当使用 `path_data` 时用于包装 `<path>`。

返回：`layer_id`, `layer_name`, `asset_kind: "svg"`, `implementation: "svg_place_embedded"`, `bounds`。

编辑边界：SVG v1 以 placed asset / smart object 风格图层为准，可整体移动、缩放、旋转、加阴影、外发光、描边、编组；不承诺 Photoshop 内部锚点级可编辑。

推荐组合：基础布局用 Photoshop shape/text；复杂曲线点缀用 SVG；最终通过 `layer.effect_*` 增强层次。

## SVG 复合对象编译

复杂曲线对象优先在后端表示为 `vector_object`，再通过 `ps_compile_svg_object` 或 Design Overlay lowering 编译成多个 `shape.svg_asset_place` 步骤。低层 SVG atom 只负责放置一个资产，不负责拆分策略。

- 所有 part 共用 `view_box` 和目标 `bbox`，保证 Photoshop 图层对齐。
- 默认按 `shadow / base_fill / secondary_fill / texture / outline / highlight / accent` 视觉样式层拆分。
- 单层超过 96 条 path 或 96 KB 时自动分片；单对象最多生成 24 个 SVG 资产。
- 每个放置结果返回 `object_id / part_id / style_role / asset_hash`，供日志、失败清理和视觉评测使用。
- 结构化路径支持 `M/L/C/Q/A/Z`；raw `d` 仅作兼容入口，无法获得完整采样审计。
- 连续身体、飘带和流体轮廓应使用闭合 SVG contour；`shape.beads_on_path` 只用于真正离散的珠子或节点。

## 2026-06-20 Clipped Adjustment Atom

### `adjustment.hue_saturation`

Creates a non-destructive Hue/Saturation adjustment layer for a pixel layer, placed asset,
Smart Object, or Smart Filter result. With `clipping_mask: true`, the adjustment is moved
directly above `target_layer_id` and clipped to that layer.

Parameters:

- `target_layer_id` or step `target`: base layer affected by the adjustment.
- `range`: `master / reds / yellows / greens / cyans / blues / magentas`.
- `hue / saturation / lightness`: Photoshop Hue/Saturation values.
- `opacity / blend_mode / name`: adjustment-layer properties.
- `clipping_mask`: restrict the adjustment to the target layer.

The implementation prefers Adobe UXP DOM `Layer.isClippingMask`. `layer.create_clipping_mask`
now rejects group layers with `clipping_mask_target_is_group` before invoking Photoshop. Its
fallback uses `groupEvent` with `dialogOptions: "silent"`, so unsupported states return an error
without opening a modal dialog. Generic `adjustment.create` also supports
`target_layer_id / clip_to_layer_id / clipping_mask`. Use this route for Smart Object
desaturation instead of the pixel-level `desaturate` command.
