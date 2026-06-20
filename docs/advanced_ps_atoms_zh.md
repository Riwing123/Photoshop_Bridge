# Advanced PS Atoms / 高级 Photoshop 原子能力

本文记录 2026-06-17 新增的高级调色、光效、渐变、图层样式与修复原子能力。它们不是固定预设，必须由 Codex 在 staged workflow 中按目标组合成 `ps-agent/v1` plan 或 `operation_recipe`。

## Apply Plan 调整层

新增可通过 `ps_validate_plan` / `ps_apply_plan` 执行的非破坏性调整层：

| Op | 作用 | 典型用途 |
|---|---|---|
| `adjust_curves` | Photoshop Curves 调整层 | S 曲线、抬黑、压高光、通透对比 |
| `adjust_levels` | Photoshop Levels 调整层 | 黑白场、gamma、中间调控制 |
| `adjust_selective_color` | Photoshop Selective Color 调整层 | 按颜色族微调 CMYK 倾向 |
| `adjust_gradient_map` | Photoshop Gradient Map 调整层 | 胶片分色、双色调、统一影调 |
| `adjust_color_lookup` | Photoshop Color Lookup 调整层 | 调用本机可用 LUT 名称或 profile |

## Operation Atoms

新增可通过 `ps_validate_operation_recipe` / `ps_apply_operation_recipe` 执行的原子组件：

| Atom | 状态 | 作用 |
|---|---|---|
| `layer.extract_luminosity_range` | `calibrated_batchplay` | 复制图层并用 highlights/midtones/shadows 亮度范围生成图层蒙版 |
| `effect.bloom_layer` | `calibrated_batchplay` | 创建带亮度蒙版的模糊混合泛光层 |
| `effect.light_rays` | `calibrated_batchplay` | 创建多边形光束层并模糊，用于丁达尔光/海报光束 |
| `gradient.fill` | `calibrated_batchplay` | 创建渐变填充层 |
| `layer.effect_outer_glow` | `calibrated_batchplay` | 添加外发光图层样式 |
| `layer.effect_stroke` | `calibrated_batchplay` | 添加描边图层样式 |
| `layer.effect_gradient_overlay` | `calibrated_batchplay` | 添加渐变叠加图层样式 |
| `retouch.clone_patch` | `calibrated_batchplay` | 复制源点选区为 patch layer，移动到目标点 |
| `retouch.healing_brush_points` | `calibrated_batchplay` | 点状 healing 入口，复用内容识别填充路径 |

## 使用规则

- 白柔、泛光、丁达尔、胶片雾等仍然只是 `effect_intent`，不能作为 atom id。
- 高光泛光应优先用 `layer.extract_luminosity_range` 或 `effect.bloom_layer`，再配合低透明度 `screen` / `linear_dodge`。
- 光束应作为独立 stage 执行，并导出全图和光源边缘局部 crop 复核。
- 修复类 atom 必须先导出局部 crop，由 Codex 或用户确认点位、半径、源点/目标点，不做自动瑕疵检测承诺。
- 这些 batchPlay 能力已进入校验与 UXP 执行分支，但不同 Photoshop build 仍可能拒绝 descriptor；失败时必须返回结构化错误并回退 stage。
