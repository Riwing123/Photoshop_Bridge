# 图层效果组合预设

本文档不新增黑盒 effect atom，而是约定一组可复用的视觉预设。实现时继续组合现有：

- `layer.effect_shadow`
- `layer.effect_outer_glow`
- `layer.effect_stroke`
- `layer.effect_gradient_overlay`
- `gradient.fill`

## 预设映射

### `soft_card`

适用：信息卡主容器、说明面板、白底内容块

- `layer.effect_shadow`
  - `opacity: 10-18`
  - `distance: 8-18`
  - `size: 18-34`
- `layer.effect_stroke`
  - `size: 1-2`
  - `position: inside`
  - `opacity: 18-36`

### `neon_accent`

适用：核心节点、重点标签、状态 chip、标题强调块

- `layer.effect_outer_glow`
  - `opacity: 32-58`
  - `size: 18-42`
- `layer.effect_gradient_overlay`
  - `opacity: 24-48`
  - `blend_mode: screen / soft_light`

### `editorial_frame`

适用：边框页、复刻页、标题容器、页角结构

- `layer.effect_stroke`
  - `size: 1-3`
  - `position: inside`
- `layer.effect_shadow`
  - `opacity: 6-12`
  - `distance: 4-10`
  - `size: 10-18`

### `orbital_node`

适用：核心图解节点、轨道环、示意图中心

- `gradient.fill`
  - radial / linear 低对比底色
- `layer.effect_outer_glow`
  - 强度低于 `neon_accent`
- `layer.effect_gradient_overlay`
  - 轻微高光过渡

## 使用边界

- 主容器：优先 `soft_card`，避免过重 glow
- 强调标签：优先 `capsule/ribbon + neon_accent`
- 边框页：优先 `bracket/cut_corner_rect + editorial_frame`
- 连线和辅助构件：优先 `shape.line / shape.polyline / path.stroke`，少用 shadow

## 不推荐

- 同一张知识卡内大面积同时使用高强度 glow、重阴影、强渐变
- 把所有 panel 都做成高光+描边+发光叠加
- 用图层效果替代结构本身；优先先做出更好的 shape/path 关系
