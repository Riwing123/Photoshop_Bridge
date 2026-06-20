# 亮度范围工具说明

这组工具是对 Photoshop 原生“颜色范围 / 存储选区 / 载入选区 / 修改选区”的高层封装，目的是让 Codex 和 MCP 直接用“高光 / 中间调 / 阴影”这类修图语义工作。

## 已注册工具

| 工具 | 作用 | 接口 |
|---|---|---|
| `ps_extract_tonal_range` | 通用亮度范围提取入口，支持 `highlights / midtones / shadows` | `POST /api/extract-tonal-range` |
| `ps_select_highlights` | 直接提取高光选区 | `POST /api/select-highlights` |
| `ps_select_midtones` | 直接提取中间调选区 | `POST /api/select-midtones` |
| `ps_select_shadows` | 直接提取阴影选区 | `POST /api/select-shadows` |
| `ps_build_luminosity_mask` | 提取亮度范围并保存成 Alpha 通道 | `POST /api/build-luminosity-mask` |

## 核心规则

- 不要把亮度范围或颜色范围当成“一次性固定阈值”工具。
- 默认应先给出几档起步值，再看 overlay、局部 crop、保护区域泄漏情况，由 agent 做微调。
- 也就是说，流程应是：

```text
seed profiles -> review -> micro tune -> approve -> apply
```

- 当前系统已经把这条规则落到了 `ps_create_selection_strategy`：
  - 对 `ps_select_color_range` 候选，会输出 `seed_trials + feedback_loop`
  - 对 `ps_extract_tonal_range` 候选，也会输出 `seed_trials + feedback_loop`

## 推荐起步方式

### 亮度范围

第一轮不要只试一个 `fuzziness`，建议至少试三档：

| 档位 | 含义 | 推荐起步值 |
|---|---|---|
| `tight` | 保守，先看边界是否干净 | `22` |
| `balanced` | 默认主试档 | `40` |
| `broad` | 宽一些，补漏选 | `62` |

推荐微调逻辑：

- 如果漏选明显：从 `tight -> balanced -> broad`
- 如果污染明显：从 `broad -> balanced -> tight`
- 如果亮度带打偏了：先换 `highlights / midtones / shadows`，再调 `fuzziness`
- 如果范围对了但边缘生硬：优先后加 `feather / smooth`，不要一开始就盲目放宽范围

## `Lights 1-5 / Darks 1-5 / Midtones 1-3` 的关系

这是更细的亮度蒙版体系，本质上也是“从宽到窄、从粗到细”的分层。

- `Lights 1`：最宽的高光
- `Lights 5`：最窄、最亮的高光
- `Darks 1`：最宽的阴影
- `Darks 5`：最深、最窄的阴影
- `Midtones 1-3`：中间调的不同宽窄层

当前项目还没有把这整套编号体系做成专门工具，但现有 `ps_extract_tonal_range` / `ps_build_luminosity_mask` 已经可以作为第一层能力，后续可以在此基础上继续扩展。

## 颜色范围同样遵循“三档起步”

对于 `ps_select_color_range`，也不要只给一个固定 `fuzziness`。

常见推荐：

| 档位 | 含义 |
|---|---|
| `tight` | 优先保护边缘和主体 |
| `balanced` | 默认主试档 |
| `broad` | 用于补齐色相变化大、雾气重、过渡软的区域 |

典型微调轴：

- `fuzziness`
- `localized_color_clusters`
- `preset` 组合，如 `blues + cyans + highlights`
- `sampled_color_points`
- `negative_color_points`

## 适用场景

- `ps_select_highlights`
  - 白柔
  - 泛光
  - 高光保护
  - 局部提亮

- `ps_select_midtones`
  - 主体层次微调
  - 中间调柔化
  - 低对比氛围调整

- `ps_select_shadows`
  - 阴影雾化
  - 抬黑
  - 夜景气氛
  - 暗部压制

- `ps_build_luminosity_mask`
  - 想复用同一组选区到多个调整层
  - 想把一次试选保存成长期可调用的 Alpha 通道

## 参数示例

### `ps_extract_tonal_range`

```json
{
  "tonal_range": "highlights",
  "fuzziness": 40,
  "localized_color_clusters": false,
  "feather": 0,
  "invert": false,
  "wait": true,
  "timeout_ms": 60000
}
```

### `ps_build_luminosity_mask`

```json
{
  "tonal_range": "highlights",
  "channel_name": "Lum Highlights 1",
  "fuzziness": 40,
  "modify": [
    { "operation": "feather", "amount": 6 },
    { "operation": "smooth", "amount": 2 }
  ],
  "wait": true,
  "timeout_ms": 60000
}
```

## 返回逻辑

- `ps_extract_tonal_range`
  - 返回当前选区结果
  - 本质是对 Photoshop `Color Range` 的亮度提取封装

- `ps_build_luminosity_mask`
  - 顺序执行：
    1. 提取亮度范围选区
    2. 可选执行 `modify`
    3. 保存为指定 Alpha 通道
  - 返回每一步的 `job_id` 和状态

## 注意事项

- `ps_build_luminosity_mask` 是多步工具，必须 `wait=true`
- 它要求 UXP 插件处于已连接状态，否则不能保证“提取后立刻保存”这一串动作连续完成
- 更底层的 RGB 采样色选仍继续走 `ps_select_color_range`
- 真正复杂的亮度/颜色选区，不要跳过 `ps_create_selection_strategy`

## 推荐调用顺序

```text
ps_get_state
-> ps_create_selection_strategy
-> ps_validate_selection_strategy
-> 试选亮度种子档
-> 必要时 ps_modify_selection
-> 通过 review 后再 ps_apply_plan
```

如果想一步到位保存通道：

```text
ps_build_luminosity_mask
-> ps_load_selection_channel
-> ps_apply_plan
```
