# PS Agent 复杂设计工作流

设计类任务使用 `design_workflow`：先读取素材、理解素材，再由 Codex 规划布局和图层结构，最后分阶段调用原子组件执行。它不是模板匹配系统。

## 默认目录

- 素材目录：`D:\Photo_sontrol\design_assets\inbox`
- 环境变量覆盖：`PS_AGENT_DESIGN_ASSET_ROOT`
- 运行时产物：`D:\Photo_sontrol\backend\runtime\assets`
- 导出目录提示：`D:\Photo_sontrol\backend\runtime\design_exports`

## 新增工具

| Tool | 是否修改 PS | 功能 |
|---|---:|---|
| `ps_scan_asset_library` | 否 | 扫描素材目录，生成缩略图和 contact sheet |
| `ps_analyze_design_assets` | 否 | 计算素材尺寸、比例、透明通道、主色和建议角色 |
| `ps_prepare_asset_variant` | 否 | 将源素材裁剪/缩放/复制到 runtime asset，供 Photoshop 置入 |
| `ps_create_design_plan` | 否 | 创建海报、面板、拼接、卡通、商业物料等设计工作流容器 |
| `ps_validate_design_plan` | 否 | 校验画布、阶段、rollback target 和可执行性 |
| `ps_apply_design_stage` | 是 | 只执行一个设计 stage，内部下沉到 `operation_recipe` |
| `ps_review_design_stage` | 否 | 返回设计阶段复核清单和反馈定位 |
| `ps_export_design_package` | 否 | 返回导出目录和 `ps_export_preview` 参数 |

## 新增设计操作原子

| 类别 | Atom |
|---|---|
| document | `document.create`, `document.set_canvas_size`, `document.export` |
| asset | `asset.place_embedded`, `asset.replace_contents` |
| layer | `layer.move_to_top`, `layer.move_above`, `layer.move_below`, `layer.reorder`, `layer.transform`, `layer.align`, `layer.distribute`, `layer.create_clipping_mask`, `layer.release_clipping_mask` |
| text | `text.create` |
| shape/path | `shape.rectangle`, `shape.rounded_rectangle`, `shape.capsule`, `shape.cut_corner_rect`, `shape.ellipse`, `shape.ribbon`, `shape.arc_band`, `shape.chevron`, `shape.bracket`, `shape.polygon`, `shape.star`, `shape.line`, `shape.polyline`, `path.create_work_path`, `path.to_selection`, `shape.path_fill`, `path.stroke` |
| effect | `layer.effect_shadow`, `layer.effect_outer_glow`, `layer.effect_stroke`, `layer.effect_gradient_overlay`, `gradient.fill` |

第一版已执行原子：

- `document.create`
- `asset.place_embedded`
- `layer.transform`
- `layer.reorder`
- `layer.create_clipping_mask`
- `layer.move_to_top`
- `text.create`
- `shape.rectangle`
- `shape.rounded_rectangle`
- `shape.capsule`
- `shape.cut_corner_rect`
- `shape.ellipse`
- `shape.ribbon`
- `shape.arc_band`
- `shape.chevron`
- `shape.bracket`
- `path.create_work_path`
- `shape.path_fill`
- `path.stroke`
- `shape.polygon`
- `shape.star`
- `shape.line`
- `shape.polyline`

## 推荐视觉母版

- 知识卡 / 社媒科普卡：
  优先 `capsule + cut_corner_rect + chevron + gradient.fill`，少量 `outer_glow / gradient_overlay` 做层次。
- 流程图 / 图解卡：
  优先 `arc_band + polyline + ribbon + path.stroke`，让关系线和主节点更有结构感，而不是只靠矩形。
- 编辑感页面 / 复刻页：
  优先 `bracket + cut_corner_rect + stroke + 低强度 shadow`，让边框和按钮更有路径语言。

## 复杂路径推荐顺序

1. 先用语义 shape atom：`rounded_rectangle / capsule / cut_corner_rect / ribbon / arc_band / chevron / bracket`
2. 不够时再用 `path.create_work_path + shape.path_fill / path.stroke`
3. 只有曲线要求高时才直接写带 handles 的 Bezier `subpaths`

其中：

- `path_mode=stable`：只用于闭合多边形、简单 subpath，优先走 selection fallback
- `path_mode=calibrated_bezier`：用于开放路径、带 handle 的曲线；需要接受 Photoshop build 差异和 direct descriptor 风险

第一版 `planned` 原子只作为能力目录暴露，不应进入执行 recipe，直到对应 UXP descriptor 校准完成。

## 默认流程

1. 调用 `ps_scan_asset_library`，用 contact sheet 做视觉理解。
2. 调用 `ps_analyze_design_assets` 获取稳定指标。
3. Codex 写 `asset_brief`：每张素材适合做主视觉、背景、装饰、Logo、纹理、信息块或不可用。
4. 结合用户目标、联网检索和本地 RAG 写 `design_plan`。
5. 每个可执行 stage 填写 `operation_recipe`，再调用 `ps_validate_design_plan` 和 `ps_validate_operation_recipe`。
6. 一次只调用 `ps_apply_design_stage` 执行一个 stage。
7. 每阶段导出 preview/crops，并用 `ps_review_design_stage` + Codex 视觉复核。
8. 后续 stage 发现前序布局或素材选择不合格时，回退前序 stage，不继续叠图层。

## 布局坐标约定

- 单位：document pixels。
- 原点：左上角。
- `asset.place_embedded` 可传 `x/y/width/height`，UXP 会按当前 layer bounds 计算缩放和偏移。
- `layer.transform` 可传 `x/y/width/height` 或 `scale_x/scale_y/offset_x/offset_y/rotation`。

## 最小海报 stage 示例

```json
{
  "schema_version": "ps-agent/v1",
  "recipe_id": "oprec-poster-minimal",
  "goal": "创建商业海报基础画布、背景块、主视觉和标题",
  "steps": [
    {
      "step_id": "canvas",
      "atom_id": "document.create",
      "params": {
        "name": "Codex Poster",
        "width": 1080,
        "height": 1350,
        "background": { "rgb": [248, 248, 246] }
      }
    },
    {
      "step_id": "hero",
      "atom_id": "asset.place_embedded",
      "params": {
        "asset_uri": "/assets/design-variant-demo/product.png",
        "name": "Hero Product",
        "x": 180,
        "y": 260,
        "width": 720,
        "height": 720
      }
    },
    {
      "step_id": "title",
      "atom_id": "text.create",
      "params": {
        "text": "NEW ARRIVAL",
        "name": "Title",
        "x": 96,
        "y": 120,
        "font_size": 72,
        "color": { "rgb": [20, 20, 20] }
      }
    }
  ],
  "safety": {
    "non_destructive": true,
    "allow_destructive": false,
    "create_history_state": true
  }
}
```

## 生成式小组件规则

- 默认优先使用 Photoshop 内部原子组件创建可编辑图层结构。
- 当原子组件无法高效表达复杂小图案、贴纸、纹理、图标、插画细节，或用户明确要求时，可以由 Codex / image generation 先生成小型 raster component。
- 生成的小组件必须通过 `asset.place_embedded` 放入 Photoshop，并继续参与正常的 `layer.transform`、`layer.move_*`、`mask.apply_*`、`layer.group` 和 staged review。
- 不允许用一张整图贴片替代本应由文本、版式、基础形状完成的可编辑设计；生成组件主要用于装饰性或复杂视觉局部。
