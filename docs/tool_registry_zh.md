# PS UXP Agent 工具注册与调用说明

## Retouch / 瑕疵修复

| Tool | 功能 | 推荐用法 |
|---|---|---|
| `ps_retouch_spot_heal_points` | 按 Codex 识别出的文档像素点位批量修小瑕疵、灰尘、痘印 | 先用 `ps_export_regions` 导出局部预览，由 Codex 判断 `x/y/radius`，再小批量执行；默认复制源层，失败时删除 retouch 层 |
| `ps_retouch_content_aware_fill_selection` | 对当前已复核选区执行内容识别填充 | 适合先用 bbox/polygon/alpha/channel/color range 等方式创建选区，再对较大污点、划痕或杂物局部填充 |

详细参数、边界和建议流程见 `docs/retouch_tools_zh.md`。

本文记录 `ps-agent/v1` 当前工具、默认调用顺序、选区/蒙版策略，以及新增的开放式 Effect Primitives 工作流。

## 启动与诊断

主后端：

```powershell
python backend\cli.py daemon start
python backend\cli.py daemon status
python backend\cli.py daemon stop
python backend\cli.py doctor
```

SAM 2.1 worker：

```powershell
python backend\cli.py sam start
python backend\cli.py sam status
python backend\cli.py sam stop
python backend\cli.py sam restart
```

SAM worker 使用独立环境 `D:\Photo_sontrol\.venv-sam`，模型路径为 `D:\Photo_sontrol\backend\models\sam2\sam2.1_hiera_base_plus.pt`。

## 默认修图工作流

```text
ps_ping_backend
-> ps_get_state
-> ps_export_preview
-> 必要时 ps_export_regions
-> Codex 写 visual_brief
-> 修改前联网检索当前攻略/参考案例，并调用本地 RAG 记录策略要点
-> 复杂任务先 ps_create_workflow_plan / ps_validate_workflow_plan
-> ps_retrieve_effect_primitives 或 ps_retrieve_style_guidance
-> Codex 写 mask_strategy
-> ps_generate_layer_recipe 或手写 ps-agent/v1 plan
-> 将 recipe/plan 放入某个 workflow stage
-> 后续 stage 应先复核前序 stage 是否仍达标；若前序不到位，回退修正前序 stage
-> ps_apply_workflow_stage 或低风险单步 ps_apply_layer_recipe / ps_apply_plan
-> ps_export_preview + ps_export_regions
-> ps_review_workflow_stage / 视觉复核
-> 失败则 ps_delete_workflow_stage 或 ps_delete_agent_group(job_id)
-> 全部 stage 通过后 ps_finalize_workflow_review
```

## 系统底层规则：Staged Workflow

任何复杂修图、风格化、图层结构构建、选区/蒙版流程，都必须先拆成阶段，再逐阶段执行和复核。

```text
大目标 -> 联网检索/RAG 获取当前攻略与约束 -> Codex 拆解阶段 -> 每阶段定义预期结果
-> 执行一个阶段 -> 导出全图/局部预览
-> 阶段评测 -> 通过则进入下一阶段
-> 后续阶段执行前复核依赖阶段
-> 如果后续阶段暴露前序不到位，则回退修正前序 stage，再重放受影响后续 stage
-> 不通过则局部修正或删除该阶段 group
-> 全部阶段完成后做最终整体评测
```

新增工具：

| 工具 | 修改 PS | 用途 |
|---|---:|---|
| `ps_create_workflow_plan` | 否 | 创建分阶段 workflow 骨架 |
| `ps_validate_workflow_plan` | 否 | 校验 stage 目标、预期、评测区域、通过标准、回退策略 |
| `ps_apply_workflow_stage` | 是 | 只执行一个 stage |
| `ps_review_workflow_stage` | 否 | 将阶段问题定位到 stage/step/primitive/capability |
| `ps_finalize_workflow_review` | 否 | 最终检查跨阶段副作用 |
| `ps_delete_workflow_stage` | 是 | 删除指定 stage group，不影响其他阶段 |

规则：

- Codex 是策略主体，必须先写阶段目标，再选 primitives/capabilities。
- 每次用户提出 Photoshop 修改时，默认先联网检索相关修图攻略、风格案例、Photoshop 功能限制或图层技法；同时调用本地 RAG。用户明确要求不联网时才跳过。
- 检索/RAG 只提供策略参考，不能直接当作可执行 descriptor；Codex 仍负责视觉判断和阶段规划。
- workflow 顶层必须包含 `research_policy`，并记录可执行的发现：视觉机制、参数倾向、风险点、参考来源。
- Primitives 只服务于某个 stage，不代表完整风格预设。
- 每个会修改画面的 stage 都应有 `expected_result`、`review_regions`、`pass_criteria`、`rollback_target`、`pre_stage_checks`、`previous_stage_recheck`。
- 后一个 stage 不能默认建立在“前一个 stage 一定正确”的假设上；它必须复核依赖阶段。若发现前序曝光、颜色、mask、氛围或细节不到位，应先删除/修正前序 stage，再继续。
- 失败反馈必须定位到 `workflow_id -> stage_id -> step_id/primitive_id/capability_id`。
- 简单低风险单步任务可跳过 workflow，例如读取状态、导出预览、单个选区命令、删除指定 group。

## Photoshop Capability Registry

PS 能力仍然通过现有 `tool_registry.json` 暴露给 Codex/MCP。新增的 capabilities registry 是后端内部能力目录，用于避免每个低频 PS 功能都变成一个独立 MCP tool。

新增工具：

| 工具 | 修改 PS | 用途 |
|---|---:|---|
| `ps_list_capabilities` | 否 | 列出当前可用 PS capability |
| `ps_probe_capability` | 否 | 查询单个 capability 的可用性、安全等级和执行方式 |
| `ps_validate_capability_call` | 否 | 校验 capability 参数、破坏性确认、raw descriptor 权限 |
| `ps_execute_capability` | 可能 | 执行低频 capability；高频功能仍优先走专用 tool |

当前已登记能力包括：文档状态、预览导出、局部导出、选区/蒙版、原生选区命令、apply plan、apply layer recipe、创建图层组、复制活动层、复制后 Gaussian Blur、高级 raw batchPlay。

安全规则：

- `tool_registry.json` 是对外入口；capability registry 是内部能力目录。
- 高频能力保留专用 tool，低频能力走 `ps_execute_capability`。
- raw batchPlay 仅高级用户路径可用，必须 `user_confirmed=true` 且 `risk_acknowledged=true`。
- 删除、裁切、栅格化、合并、直接改像素等破坏性操作必须先询问用户。

## 开放式 Effect Primitives

`recipe cards` 不再是封闭预设，而是示例组合。默认思路是：理解用户目标，拆成多个视觉机制，从 primitives 组合 layer recipe，再分阶段执行和评测。

新增工具：

| 工具 | 修改 PS | 用途 |
|---|---:|---|
| `ps_list_effect_primitives` | 否 | 列出当前可用效果积木、参数范围、风险、示例 recipe |
| `ps_retrieve_effect_primitives` | 否 | 根据用户目标、visual brief、场景标签检索候选 primitives |
| `ps_generate_layer_recipe` | 否 | 把选中的 primitives 转成可执行 layer recipe |
| `ps_validate_layer_recipe` | 否 | 校验 recipe 是否只使用白名单 op 和安全参数 |
| `ps_apply_layer_recipe` | 是 | 将 recipe 降级为 `ps_apply_plan` 并创建 Photoshop 调整层 |
| `ps_review_layer_recipe` | 否 | 把复核问题映射回具体 primitive 和参数 |

第一版 primitive 类别：

| 类别 | 例子 |
|---|---|
| Tone / 明暗 | `exposure_lift`, `black_lift`, `white_rolloff`, `contrast_compress`, `contrast_pop` |
| Color / 色彩 | `warm_highlights`, `cool_shadows`, `green_to_yellow`, `pastel_desaturate`, `selective_saturation` |
| Glow / 光感 | `highlight_bloom`, `soft_bloom`, `white_soft`, `halation`, `edge_glow` |
| Atmosphere / 氛围 | `mist_layer`, `haze_lift`, `airy_background`, `vintage_fog`, `night_haze` |
| Detail / 质感 | `clarity_boost`, `texture_soften`, `skin_soften`, `micro_contrast`, `grain` |
| Lens / 镜头感 | `vignette`, `light_leak`, `cinematic_glare`, `streak_glow` |
| Local / 局部控制 | `subject_protect`, `skin_protect`, `eye_protect`, `logo_protect`, `background_only` |
| Composition / 视线引导 | `center_lift`, `subject_spotlight`, `edge_darken`, `background_deemphasis` |

示例：用户说“清晨、柔和、轻微胶片感，但不要太白”，系统应拆成 `warm_highlights + black_lift + soft_bloom + grain + white_rolloff` 等机制，而不是匹配一个固定预设。

注意：

- 每个 recipe 至少应包含 3 个不同 primitive。
- 每个 primitive 都必须能解释选择理由、参数范围和失败模式。
- 评测反馈要定位到 primitive，例如“泛光过强”应反馈到 `highlight_bloom`，而不是只说整体不好。
- 第一版执行层会降级到现有稳定 op：`camera_raw_filter`、`adjust_hue_saturation`、`adjust_exposure`、`adjust_vibrance`、`adjust_color_balance`。
- 真实 stamp layer、模糊层、光晕层、颗粒层等高级图层结构在后续阶段扩展；当前 primitive 会先用稳定调整层近似表达。

## 主要桥接工具

| 工具 | 修改 PS | 用途 |
|---|---:|---|
| `ps_ping_backend` | 否 | 检查 Python 后端与 UXP 心跳 |
| `ps_bridge_diagnostics` | 否 | 诊断端口、SQLite、UXP、SAM、日志路径 |
| `ps_get_state` | 否 | 读取当前文档、图层和活动图层 |
| `ps_export_preview` | 否 | 导出全图预览 |
| `ps_export_regions` | 否 | 导出局部 crop，用于复核和 mask 提示 |
| `ps_analyze_image_metrics` | 否 | 硬编码确定性量化图像明暗和色彩 |
| `ps_retrieve_style_guidance` | 否 | 本地 RAG 风格约束和失败陷阱 |
| `ps_generate_face_selection` | 否 | MediaPipe Face Landmarker 生成面部 polygon |
| `ps_generate_sam_mask` | 否 | SAM 2.1 根据 bbox/点提示生成全尺寸 alpha mask |
| `ps_create_selection_strategy` | 否 | 为复杂选区生成候选工具、试选顺序、加减法细化和评测标准 |
| `ps_validate_selection_strategy` | 否 | 校验选区策略是否避免死板路由、是否包含候选试选和 review |
| `ps_make_selection` | 是 | 按 `selection_mask` 建立活动选区 |
| `ps_validate_plan` | 否 | 校验 plan 安全性和字段 |
| `ps_apply_plan` | 是 | 执行非破坏性调整层/智能滤镜 |
| `ps_delete_agent_group` | 是 | 按 apply job_id 删除 `Codex Agent - {job_id}` 图层组 |
| `ps_undo_last` | 是 | 历史回退兜底，不作为常规清理路径 |

## Photoshop 原生选区工具

这些工具是对 Photoshop 菜单能力的安全封装，全部走 job queue，由 UXP 在 `executeAsModal` 内执行。

| 工具 | 对应能力 | 说明 |
|---|---|---|
| `ps_select_subject` | 选择 > 主体 | 适合人物/产品/主体整体选区 |
| `ps_select_sky` | 选择 > 天空 | 适合风光、户外图天空区域 |
| `ps_select_color_range` | 选择 > 颜色范围 | 可按 RGB 采样色或 reds/greens/highlights 等 preset 建选区 |
| `ps_select_focus_area` | 选择 > 焦点区域 | 实验性 descriptor，可能需要按 PS 版本校准 |
| `ps_modify_selection` | 选择 > 修改 | 支持 `feather / expand / contract / smooth / border` |
| `ps_save_selection_channel` | 选择 > 存储选区 | 保存当前选区为 alpha channel |
| `ps_load_selection_channel` | 选择 > 载入选区 | 从 alpha channel 载入选区 |
| `ps_selection_command` | 通用白名单命令 | 支持 `select_all / deselect / inverse` 等菜单动作 |

## 选区与蒙版策略

底层原则：不要把目标类别死板映射到单个工具。天空不一定优先 `ps_select_sky`，主体不一定优先 `ps_select_subject`，复杂物体也不一定只走 SAM。Codex 应根据画面、用户目标、保护区域、联网/RAG 结论，先生成抽象 `selection_strategy`，再试选和评测。

重要分层：

- `selection_strategy` 阶段只描述平权候选方法、为什么尝试、试选参数范围、评测方式和降级路径。
- `selection_strategy` 不应直接输出 `selection_mask.source: "color_range"`、`"select_sky"`、`"alpha_mask"` 等执行层表达。
- 只有候选经过 RAG/联网依据、overlay / crop 试选评测后，才把胜出的候选 lower 成具体 `selection_mask`，交给 `ps_make_selection` 或 `ps_apply_plan`。

复杂选区默认流程：

```text
用户目标 / visual brief / protected regions
-> 联网检索 + 本地 RAG 记录选区经验和风险
-> ps_create_selection_strategy
-> ps_validate_selection_strategy
-> 逐个试选候选工具
-> 导出 overlay / selection preview / 局部 crops
-> 比较 coverage / leakage / edge_quality / softness / edit_safety / latency
-> 选择最佳 mask 或用 add/subtract/intersect 合成
-> 只有通过 review 的 mask 才能进入 ps_apply_plan
```

| 场景 | 推荐方式 |
|---|---|
| 整体基调 | `camera_raw_filter` + `target.type: "global"` |
| 天空 | 候选比较：`ps_select_sky`、`ps_select_color_range` 的 blues/cyans/highlights 或 sampled colors，必要时 subtract foreground |
| Photoshop 可用主体 | 候选比较：`ps_select_subject`、Object Selection/subject 当前选区、Grounding/HQ-SAM 或 polygon refine |
| 脸、眼、唇、脸颊 | `ps_generate_face_selection` |
| 黑板、衣服、手、花、建筑、产品、背景等复杂非人脸区域 | 候选比较：Grounding DINO + HQ-SAM、HQ-SAM bbox/points、SAM bbox/points、Color Range、polygon/bbox refine |
| 简单矩形 | `bbox` |
| 可解释硬边局部 | `polygon` |
| 多个硬边选区加减 | `selection_mask.source: "composite"` |
| 用户手动选好的区域 | `current_selection` |

加减法规则：

- `replace` 必须作为 base selection。
- `add / subtract / intersect` 用于 Photoshop 原生选区、Color Range、bbox、polygon、current selection 等硬边路径。
- 同一个方法族可以在最终 lowered composite 中多次出现。例如天空候选胜出后，可先 Color Range blues fuzziness=45 建 base，再 Color Range cyans fuzziness=35 add，再 Color Range highlights fuzziness=25 add。
- 不同工具可以互相细化。例如天空/背景候选胜出后，可用多个 Color Range 建 base，再 subtract 主体选择、Face Landmarker/polygon、foreground bbox，避免污染人物、粉笔、Logo 或前景物体。
- `alpha_mask` 保持 soft edge，不在 Photoshop 内做硬布尔；多个 soft mask 的 union/subtract/intersect 先在 backend 合成为单个 alpha PNG。
- 每个 refinement 都要记录目的，例如“subtract face/eyes/logo/text”“intersect with sky color range”“add missed flower edge”。
- 后续调色 stage 如果发现 mask 不到位，必须回退到 `mask_preparation` stage 修改，而不是继续叠调色层。

最终 lower 后的执行示例：天空多色域 + 主体规避

注意：下面是试选评测通过后的执行层 `selection_mask`，不是 `selection_strategy` 的直接输出。

```json
{
  "source": "composite",
  "items": [
    { "source": "color_range", "operation": "replace", "preset": "blues", "fuzziness": 45, "localized_color_clusters": true },
    { "source": "color_range", "operation": "add", "preset": "cyans", "fuzziness": 35, "localized_color_clusters": true },
    { "source": "color_range", "operation": "add", "preset": "highlights", "fuzziness": 25, "localized_color_clusters": true },
    { "source": "select_subject", "operation": "subtract", "feather": 2 }
  ],
  "feather": 1
}
```

## SAM Alpha Mask 主路径

`ps_generate_sam_mask` 输入 preview/crop、原图尺寸、crop 对应原图 bbox、scale factor，以及 bbox/正负点提示。输出包括：

- `alpha_mask`：全尺寸透明 PNG，尺寸与 Photoshop 原始文档一致。
- `overlay_preview`：叠加预览图，用于判断 mask 是否偏移或漏选。
- `mask_preview`：局部 mask 预览。
- `selection_mask`：可直接传给 `ps_make_selection` 或 `ps_apply_plan`。

规则：

- `alpha_mask` 第一版只支持 `operation: "replace"`。
- 修图时优先用 alpha mask 创建调整层蒙版，保留软边。
- 蚂蚁线只用于可视验证；真实非破坏性修图不依赖 polygon。
- 多个软蒙版加/减/交集应在后端合成新的 alpha mask，不建议让 Photoshop 选区布尔运算硬化边缘。

## 清理策略

已知 apply job id 时优先：

```text
ps_delete_agent_group(job_id)
```

它只删除名称精确为 `Codex Agent - {job_id}` 的图层组，不依赖 Photoshop history。仅当用户明确要求历史回退时，才使用 `ps_undo_last`。
## Grounding DINO + HQ-SAM

新增 3 个语义选区工具，适合“非主主体、多实例、可文本描述”的复杂目标：

| 工具 | 修改 PS | 用途 |
|---|---:|---|
| `ps_detect_grounding_boxes` | 否 | 用英文语义提示词做本地检测，返回候选框和 overlay 预览 |
| `ps_generate_hqsam_mask` | 否 | 已知 bbox / 正负点提示时，用 HQ-SAM 生成全尺寸 alpha mask |
| `ps_generate_grounded_hq_mask` | 否 | 默认主路径：Grounding DINO 检测 -> HQ-SAM 精分 -> backend 合成 soft alpha mask |

推荐场景：

- 建筑、招牌、产品、黑板、衣服、花、背景道具等“语义明确但不是主主体”的区域
- 先检测 `"building"`、再排除 `"person"` 这类 include / exclude 场景
- 多实例目标需要先 union，再 soft subtract 排除区

默认规则：

- Grounding DINO 只吃 `prompt_en`
- 中文目标由 Codex 先翻成英文短语，例如 `building . apartment building . residential tower`
- `alpha_mask` 仍是 Photoshop 的 replace-only 输入
- 多个 soft mask 的 add / subtract / intersect 一律先在 backend 合成，再交给 Photoshop
- 设备策略默认是 `PS_AGENT_GROUNDING_DEVICE=auto`、`PS_AGENT_HQSAM_DEVICE=auto`
- Grounding DINO 只有在 `torch.cuda.is_available()` 和 `groundingdino._C` CUDA 扩展都可用时才走 GPU；否则自动 CPU fallback
- HQ-SAM 在 `auto` 下优先使用 GPU；CUDA 不可用时才退回 CPU
- 工具返回会包含 `devices` 字段；若出现 `grounding_cpu_fallback_no_cuda_extension`，表示检测仍可运行，但应优先缩小 ROI、减少候选框、收紧英文 prompt

CLI / 诊断：

```powershell
python backend\cli.py grounding start
python backend\cli.py grounding status
python backend\cli.py grounding stop
python backend\cli.py grounding logs --tail 80
python backend\cli.py grounded-detect ...
python backend\cli.py grounded-mask ...
```
# 操作与选区蒙版原子组件（新增）

当前系统新增两套平权组件目录，均通过 `backend/tool_registry.json` 暴露：

| 工具 | 修改 PS | 用途 |
|---|---:|---|
| `ps_list_operation_atoms` | 否 | 列出图层、滤镜、调整层、蒙版、预览等操作原子组件 |
| `ps_list_selection_atoms` | 否 | 列出原生选区、几何选区、本地模型、通道/Alpha、细化组件 |
| `ps_validate_operation_recipe` | 否 | 校验一串图层/滤镜/蒙版操作是否安全可执行 |
| `ps_apply_operation_recipe` | 是 | 在一次 `executeAsModal` 中顺序执行操作组件 recipe |
| `ps_validate_selection_recipe` | 否 | 校验候选选区、merge plan、hard/soft bus 和 review 区域 |
| `ps_apply_selection_recipe` | 可能 | hard selection 走 UXP；soft alpha 在后端合成一张最终 Alpha |
| `ps_review_selection_recipe` | 否 | 将 mask 复核问题定位到 candidate_id / atom_id |
| `ps_apply_mask_to_layer` | 是 | 将通过复核的 `selection_mask` 或 Alpha mask 应用为图层蒙版 |

核心规则：

- 白柔、泛光、胶片雾、日系浅柔等不是固定工具；它们应由 Codex 临时规划为 `operation_recipe`。
- `operation_recipe` 的真实工具是 `layer.duplicate`、`filter.gaussian_blur`、`layer.set_properties`、`layer.group`、`mask.apply_alpha` 等原子组件。
- 复杂局部任务必须先进入 `selection_recipe`：试选多个候选，导出 overlay/crop 复核，再 lower 到最终 mask。
- hard selection bus 支持 Photoshop `replace/add/subtract/intersect`、feather、expand、contract、smooth、inverse、channel save/load。
- soft alpha bus 不在 Photoshop 内做硬布尔；由后端先按 union/subtract/intersect 合成单张 Alpha，再交给 `ps_apply_mask_to_layer` 或 `ps_apply_plan`。
- `color_range` 和 `tonal_range` 必须从 seed profiles / seed ladder 出发，再根据 overlay 反馈微调 `fuzziness`、sampled color、localized clusters 等参数。

示例：白柔背景不是调用“白柔工具”，而是：

```text
mask_preparation:
  selection_recipe 生成并复核 background alpha
atmosphere_effects:
  operation_recipe:
    layer.duplicate -> filter.gaussian_blur -> layer.set_properties(screen/opacity)
    -> mask.apply_alpha(background_alpha) -> layer.group
```
