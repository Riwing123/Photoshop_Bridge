# Region Artifact：区域资产统一协议

`region_artifact` 是 PS Agent 的区域中间层。它把 Face Landmarker、SAM/HQ-SAM、Grounding、alpha mask、bbox、polygon、landmarks 统一成一个区域资产包，然后再 lower 到不同 Photoshop 表达。

核心原则：

```text
区域理解来源
-> region_artifact
-> alpha mask / polygon lasso / hard selection / pen path / brush mask / layer mask
```

## 为什么需要它

同一个“眼睛”“脸部”“衣服”“建筑”“天空”区域，不应该只绑定到一种输出。

| 来源 | 以前 | 现在 |
|---|---|---|
| Face Landmarker | 只能生成脸部 polygon 选区 | 可生成 polygon、selection_recipe、bezier_path、未来钢笔路径 |
| SAM/HQ-SAM | 只能生成 alpha mask | 可保留 alpha，也可提取 contour 变成 polygon/路径 |
| Grounding + HQ-SAM | 只能语义 alpha mask | 可变成 selection_recipe，也可转路径/套索规划 |
| Codex 坐标 | bbox/polygon 临时输入 | 可封装成可复用 region_artifact |

## Region Artifact 结构

```json
{
  "schema_version": "ps-agent/v1",
  "artifact_type": "region_artifact",
  "region_id": "face-left-eye",
  "label": "left_eye",
  "source": "face_landmarker",
  "quality": {
    "confidence": 1.0,
    "area_ratio": 0.012,
    "bbox": {"x": 100, "y": 120, "width": 80, "height": 40}
  },
  "representations": {
    "alpha_mask": {},
    "polygon": {},
    "landmarks": {},
    "bezier_path": {},
    "selection_mask": {}
  }
}
```

## 新增工具

| Tool | 作用 | 是否修改 PS |
|---|---|---:|
| `ps_create_region_artifact` | 把 Face/SAM/HQ-SAM/bbox/polygon/landmarks 统一成 region artifact | 否 |
| `ps_extract_region_contour` | 从 alpha mask 提取近似 polygon contour 和 bezier path | 否 |
| `ps_lower_region_to_selection_recipe` | 把 region artifact lower 成可执行 `selection_recipe` | 否 |
| `ps_lower_region_to_path` | 把 region artifact lower 成钢笔/路径数据表示 | 否 |

执行 Photoshop 选区时继续走：

```text
ps_lower_region_to_selection_recipe
-> ps_validate_selection_recipe
-> ps_apply_selection_recipe
```

应用软边蒙版时继续走：

```text
region_artifact.representations.alpha_mask
-> ps_apply_mask_to_layer
```

路径/钢笔第一版只输出 path data：

```text
ps_lower_region_to_path
-> bezier_path / path_recipe
```

原生 Photoshop work path 创建会在后续绑定 `path.create_work_path` atom。当前版本先不伪装成已经能创建 PS 钢笔路径。

## 推荐用法

### Face Landmarker 到多边形套索

1. 调 `ps_generate_face_selection` 得到脸部/眼睛/嘴唇 polygon。
2. 调 `ps_create_region_artifact`，传入 `source_result`。
3. 调 `ps_lower_region_to_selection_recipe`，`prefer="polygon"`。
4. 调 `ps_apply_selection_recipe`，得到 Photoshop 当前选区/alpha channel。

### SAM/HQ-SAM alpha 到套索

1. 调 `ps_generate_sam_mask` 或 `ps_generate_grounded_hq_mask`。
2. 调 `ps_create_region_artifact`，传入 `source_result`，并设置 `extract_polygon=true`。
3. 若要硬选区/蚂蚁线，调 `ps_lower_region_to_selection_recipe`，`prefer="polygon"`。
4. 若要软边修图，直接使用 artifact 内 `alpha_mask`，不要转硬选区。

### Alpha 到钢笔路径规划

1. 调 `ps_extract_region_contour`，从 alpha mask 提取 contour。
2. 调 `ps_create_region_artifact` 封装 contour。
3. 调 `ps_lower_region_to_path` 得到 `bezier_path`。

注意：当前 `bezier_path` 是路径数据，不会直接创建 Photoshop work path。它用于后续 path atom、描边、矢量蒙版、路径文字规划。

## 质量边界

- `alpha_mask` 是精修主路径，适合柔边调色、局部效果、图层蒙版。
- `polygon` 是硬边路径，适合套索、蚂蚁线确认、粗选、遮挡保护。
- `bezier_path` 当前是数据表示，适合规划钢笔路径；原生 PS path 创建待后续 atom。
- 从 alpha 提取 polygon 是近似轮廓，默认使用 radial boundary approximation；复杂发丝、薄纱、树叶不应丢弃 alpha。

