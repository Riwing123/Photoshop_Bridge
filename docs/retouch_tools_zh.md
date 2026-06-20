# Retouch Tools / 瑕疵修复工具

本文记录当前 PS Agent 已落地的瑕疵修复能力。这里的“瑕疵位置”默认由 Codex 通过局部预览观察后给出，不依赖自动检测模型。

## 已落地工具

| Tool | 状态 | 作用 | 关键输入 | 执行方式 |
|---|---|---|---|---|
| `ps_retouch_spot_heal_points` | calibrated_batchplay | 按点位批量修小瑕疵、灰尘、痘印 | `points[{x,y,radius,width,height,label}]`, `source_layer_id?`, `feather?`, `expand?` | 后端 lower 为 `retouch.spot_heal_points` operation atom，UXP 在 `executeAsModal` 内复制源层，逐点建立椭圆选区并执行内容识别填充 |
| `ps_retouch_content_aware_fill_selection` | calibrated_batchplay | 对当前选区做内容识别填充 | 当前 Photoshop active selection，`source_layer_id?`, `feather?`, `expand?` | 后端 lower 为 `retouch.content_aware_fill_selection` atom，UXP 复制源层后对当前选区执行内容识别填充 |

## Operation Atoms

| Atom | 状态 | 说明 |
|---|---|---|
| `retouch.spot_heal_points` | calibrated_batchplay | Codex 给文档像素坐标点位；每个点会转成软边椭圆选区。默认复制源层并尝试栅格化复制层，保护原图。 |
| `retouch.content_aware_fill_selection` | calibrated_batchplay | 使用已有选区。适合先用 bbox/polygon/alpha/channel/颜色范围创建并复核选区，再对较大污点或划痕填充。 |
| `retouch.clone_patch` | planned | 源点到目标点的仿制修补接口已登记为规划项，但尚未校准可执行 descriptor，不会通过 recipe 校验。 |

## 推荐工作流

1. `ps_export_regions` 导出脸部或局部高分辨率 crop，建议 `max_side >= 1536` 且 `upscale_small_regions=true`。
2. Codex 观察 crop，列出疑似瑕疵点位和半径。坐标必须换算回 Photoshop 文档像素坐标。
3. 调用 `ps_retouch_spot_heal_points`，每轮点数控制在 3-20 个，避免一次性过修。
4. 立即导出同一局部 crop 做 before/after 复核。
5. 若修坏纹理，只隐藏或删除该 retouch layer/stage，不改原始层。

## 坐标规则

- 坐标系是 Photoshop 文档像素坐标，左上角为 `(0, 0)`。
- `x/y` 表示瑕疵中心。
- 小痘印建议 `radius=6..18`。
- 明显斑块或灰尘可用 `width/height` 建椭圆。
- `feather` 默认 2，皮肤可用 2-8，硬边物体可用 0-2。
- `expand` 默认 0；当选区太贴边时再加 1-4。

## 当前边界

- 当前没有自动瑕疵检测模型；`ps_detect_blemishes` 尚未落地。
- 内容识别填充依赖 Photoshop 当前版本的 `fill/contentAware` descriptor；若 Photoshop 拒绝，会返回 `content_aware_fill_failed`。
- 对智能对象、文字层、形状层，工具默认复制后尝试栅格化复制层。若栅格化失败，可能需要先指定像素层或导出/合成可编辑像素层。
- 这不是磨皮工具。它用于点状或小区域修复；大面积皮肤质感调整应使用独立的频率分离/柔化/纹理保护 workflow。

