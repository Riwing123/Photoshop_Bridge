# 图形语言升级指南

目标：让设计结果不再自然滑回“直来直去的矩形 + 单个阴影”，而是优先使用可复用的路径语言、非矩形标签、结构化连接件和低强度效果组合。

## 推荐结构

### 1. 标签层

- `shape.capsule`
- `shape.ribbon`
- `shape.chevron`

适合：栏目标签、状态 chip、流程步骤名、亮点提示。

### 2. 面板层

- `shape.rounded_rectangle`
- `shape.cut_corner_rect`
- `shape.bracket`

适合：说明卡、信息面板、复刻页外框、编辑感容器。

### 3. 关系层

- `shape.line`
- `shape.polyline`
- `path.create_work_path + path.stroke`

适合：流程关系、箭头、连接结构、轨迹示意。

### 4. 强调层

- `shape.arc_band`
- `shape.star`
- `gradient.fill`
- `layer.effect_outer_glow`

适合：核心节点、轨道环、主视觉焦点、辅助高亮。

## 推荐组合

### 社媒知识卡

- `cut_corner_rect + capsule + ribbon + arc_band`
- 背景优先 `gradient.fill`
- 面板优先 `soft_card`

### 流程图 / 图解卡

- `arc_band + polyline + chevron + path.stroke`
- 节点高亮用 `outer_glow`
- 连线尽量少阴影，多结构

### 编辑感页面

- `bracket + cut_corner_rect + stroke`
- 按钮优先 `ribbon / capsule`
- 页框优先路径和描边，不用厚重卡片感

## 可复用片段

运行时示例片段位于：

- `backend/runtime/fragment_label_chip_set.json`
- `backend/runtime/fragment_connector_set.json`
- `backend/runtime/fragment_panel_set.json`
- `backend/runtime/fragment_accent_shapes.json`

它们不是固定模板，而是开放积木，可直接拼进新的 `operation_recipe`。

## 开放示范 Recipe

- `backend/runtime/social_knowledge_card_rich_shapes.json`
- `backend/runtime/diagram_card_orbital_layout.json`
- `backend/runtime/editorial_border_page.json`

## 0.10 通用 Atom 使用建议

### 插画轮廓
- 树冠、云状层、节日插画优先用 `shape.scalloped_triangle`，不要用 `shape.chevron` 代替自然轮廓。
- 背景柔性色块、非矩形内容托底优先用 `shape.blob`，固定 `seed` 以保证 recipe 可复现。
- 流动分割、装饰横条优先用 `shape.wavy_band`。
- 促销贴、重点提示、徽章底优先用 `shape.starburst` 或 `shape.badge`。

### 路径排布
- 灯串、珠串、节点链优先用 `shape.beads_on_path`，不要用单根 `shape.polyline` 假装珠子。
- 虚线流程和路径提示用 `shape.dashed_path`。
- 方向连接件用 `shape.arrow_path`，箭头方向由最后一段路径决定。

### 复合物件与容器
- 圆形挂饰、带高光装饰物用 `shape.bauble`，一次生成 body/hook/highlight 并默认分组。
- 徽章类用 `shape.badge`，说明类气泡用 `shape.callout`。
- 票券边用 `shape.ticket_card`，缺口面板用 `shape.notched_panel`，折角卡片用 `shape.folded_corner`。

### 评测闭环
- 每次复杂绘制后先导出 preview，再调用 `POST /api/layout/visual-score` 看非矩形丰富度、构图和边缘安全。
- 有参考图时调用 `POST /api/preview/compare-reference`，重点看 `occupancy_delta`、`edge_density_delta` 和 `suggestions`。
- 文本或主体贴边时调用 `POST /api/layout/detect-overflow`，把结果转成下一轮微调 recipe。

### 圆化边缘
- `shape.blob`、`shape.scalloped_triangle`、`shape.wavy_band`、`shape.callout`、`shape.ticket_card`、`shape.notched_panel` 支持 `smooth_iterations`、`smooth_ratio`、`max_points`。
- 边缘有折线感时，优先设置 `smooth_iterations: 2`、`smooth_ratio: 0.22`、`max_points: 180..240`。
- 当前默认策略：`blob` 默认 2 轮圆化；`scalloped_triangle / ticket_card / notched_panel` 默认 1 轮；`wavy_band / callout` 默认不额外圆化但可显式开启。
- Bezier 默认路线已改为 Photoshop DOM `document.pathItems.add(name, SubPathInfo[])`；不再使用会触发模态错误的直接 descriptor 探针。开放路径的可控描边仍需单独校准。

这些示范用来说明“ richer shape language 怎么搭”，不是要求后续项目照抄版式。

## Bezier 曲线图形建议

- 自然物体、Logo 复刻、卡通轮廓不要再直接用密集多边形锚点硬折，优先使用 `shape.bezier_fill` + `handle_mode: "catmull_rom"`。
- 手写 `in/out` 只适合少量精调点；大规模手写绝对 handle 前必须先用 `path.audit_bezier_handles` 检查方向和 smooth 共线性。
- 如果预览出现盾牌形、尖刺、边缘反折，工程上优先检查 `path_audit.warnings` 中的 `in_wrong_direction`、`out_wrong_direction`、`not_smooth_collinear`。
- `handle_scale` 用于控制圆滑强度：Logo/柔性图形建议 `0.8..1.05`，更硬朗的装饰图形建议 `0.45..0.75`。
### 曲线打结排查

- 尖顶、叶片、龙角、树冠边缘这类高转角轮廓，使用 `handle_mode: "catmull_rom"` 时建议加 `corner_angle_threshold: 100..112`。
- 如果顶点附近出现小环，先降低 `handle_scale`，再降低 `corner_angle_threshold`；不要通过堆更多锚点硬修。
- 需要真正锐利的点，可以在该点显式写 `kind: "corner"`，让 backward/forward 留在 anchor。

## ????????

- ????????????? `kind: "corner"`???????/????????? smooth?
- ?????????????????????? `kind: "smooth"`?????/????????
- ???????? `shape.bezier_ellipse`?? `shape.ellipse` ?????????????????????? selection/transform ????????
- ???? recipe ???????? `path.audit_bezier_handles`????? `self_intersection`?????????? no-kind fallback????? Photoshop build ??????





## SVG 作为图形语言补充

SVG 路线用于补齐 Photoshop Agent 在复杂曲线、自由形装饰、贴纸化元素和 Logo/插画类素材上的表达力。它不是默认布局系统，也不建议承担正文容器、文本排版或严格信息结构。

推荐使用场景：
- 曲线装饰：波浪线、弧线、高光线、手绘箭头。
- 贴纸与徽章：星芒、爆炸贴、奖章、角标。
- 背景纹样：光束、轨道、流体斑块、重复装饰。
- 复杂轮廓：Logo 临摹、卡通轮廓、难以用 polygon 稳定表达的图形。

不推荐使用场景：
- 大量正文排版。
- 需要 Photoshop 内部逐锚点编辑的生产路径。
- 可由现有 rectangle/polygon/ellipse/text 稳定完成的基础结构。

当前 Bezier class constructor 路线已从默认绘制候选中移除。原因是当前 Photoshop UXP runtime 未暴露可用的 `PathPointInfo` / `SubPathInfo` constructor；继续探测会增加弹窗和 job 卡死风险。legacy plain DOM Bezier 保留，用于已有 smoke 和受控路径；新设计中的复杂曲线装饰优先走 `shape.svg_asset_place`。

## 工程级 SVG 制图路由

- 文本与规则布局继续使用 Photoshop 原生 text/shape atom。
- 连续自然曲线、Logo、贴纸和插画轮廓使用 `vector_object`，由后端编译为多个视觉样式级 SVG 图层。
- 不把整个页面封装为单个 SVG；复合对象按阴影、填色、纹理、描边、高光和装饰拆层。
- 所有子层先以同一 viewBox 放置，再统一编组；旋转和整体缩放只作用于组。
- 结构化 SVG 在执行前检查非法数据、自交、viewBox 越界、单体体积和分片数量。
- Photoshop live 结果应将 operation result 一并交给 `layout.visual_score`，检查子层 bounds 偏差和巨型 SVG 风险。