# Photoshop_bridge 配置与调用

这份文档按“新机器从 0 跑通”的顺序写。已经把 `UXP_plugin/com.yfy25.psuxpagent_PS.ccx` 放进 UXP Developer Tool 的，也可以从第 3 步开始。

## 0. 准备

需要：

- Windows
- Adobe Photoshop 26.10+
- UXP Developer Tool
- Python 3.11+
- 本仓库路径：`D:\Photo_sontrol`

可选模型：

- `backend/models/face_landmarker.task`
- `backend/models/sam2/sam2.1_hiera_base_plus.pt`
- `backend/models/grounding_dino/groundingdino_swint_ogc.pth`
- `backend/models/sam_hq/sam_hq_vit_l.pth`

没有可选模型也能跑通桥接、预览、状态读取和多数 Photoshop 原生命令。

## 1. 创建 Python 环境

在仓库根目录执行：

```powershell
cd D:\Photo_sontrol
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r backend\requirements-ml.txt
```

如果需要 SAM / Grounding DINO / HQ-SAM，再单独建模型环境：

```powershell
python -m venv .venv-sam
```

模型依赖和权重比较重，先不装也可以；等确实要用语义分割时再补。

## 2. 启动后端

```powershell
cd D:\Photo_sontrol
.\.venv\Scripts\python.exe backend\cli.py daemon start
.\.venv\Scripts\python.exe backend\cli.py daemon status
```

默认后端地址：

```text
http://127.0.0.1:17860
```

常用管理命令：

```powershell
.\.venv\Scripts\python.exe backend\cli.py daemon stop
.\.venv\Scripts\python.exe backend\cli.py daemon restart
.\.venv\Scripts\python.exe backend\cli.py daemon logs
.\.venv\Scripts\python.exe backend\cli.py health
```

## 3. 加载 Photoshop UXP 插件

### 方式 A：使用已打包的 CCX

直接把 `.ccx` 放入 Photoshop plugin 文件夹，或在 UXP Developer Tool 里加载 `UXP_plugin/com.yfy25.psuxpagent_PS.ccx`。

确认插件名是 `PS UXP Agent`，Photoshop 打开后点击 Load / Reload，然后在 Photoshop 菜单或插件面板中打开它。

### 方式 B：开发模式加载源码

如果 CCX 不方便加载，直接添加 manifest：

```text
D:\Photo_sontrol\UXP_plugin\com.yfy25.psuxpagent\manifest.json
```

然后在 UXP Developer Tool 里 Load / Reload。

## 4. 验证桥接

先打开 Photoshop，并打开任意 PSD、PNG 或 JPG 文件，再执行：

```powershell
cd D:\Photo_sontrol
.\.venv\Scripts\python.exe backend\cli.py doctor
.\.venv\Scripts\python.exe backend\cli.py state
.\.venv\Scripts\python.exe backend\cli.py preview
```

期望结果：

- `health` / `daemon status` 返回 `status: ok`
- `doctor` 不再提示 UXP 未连接
- `state` 能返回当前 Photoshop 文档信息
- `preview` 能导出当前文档预览

如果 `state` 或 `preview` 一直等待，通常是 Photoshop 面板没打开、UXP 插件没 Reload，或后端地址不是 `127.0.0.1:17860`。

## 5. 接入 Codex / MCP

把下面配置加入 MCP 客户端配置。路径按本机仓库路径写死即可：

```json
{
  "mcpServers": {
    "ps-uxp-agent": {
      "command": "python",
      "args": ["D:\\Photo_sontrol\\backend\\mcp_server.py"],
      "env": {
        "PS_AGENT_BACKEND_URL": "http://127.0.0.1:17860"
      }
    }
  }
}
```

Codex TOML 示例：

```toml
[mcp_servers.ps-uxp-agent]
command = "python"
args = ["D:\\Photo_sontrol\\backend\\mcp_server.py"]
env = { PS_AGENT_BACKEND_URL = "http://127.0.0.1:17860" }
```

仓库里已有示例：

- `mcp/mcp-client.example.json`
- `mcp/codex.example.toml`

## 6. 调用方式

### CLI 调用

读取 Photoshop 状态：

```powershell
.\.venv\Scripts\python.exe backend\cli.py state
```

导出预览：

```powershell
.\.venv\Scripts\python.exe backend\cli.py preview --mode standard --format jpeg
```

创建原生主体选区：

```powershell
.\.venv\Scripts\python.exe backend\cli.py native-selection select_subject
```

取消选区：

```powershell
.\.venv\Scripts\python.exe backend\cli.py selection-command deselect
```

查看任务：

```powershell
.\.venv\Scripts\python.exe backend\cli.py job <job_id>
```

### MCP 工具调用

MCP 接好后，客户端会看到这些常用工具：

```text
ps_ping_backend
ps_bridge_diagnostics
ps_get_state
ps_export_preview
ps_select_subject
ps_select_sky
ps_select_color_range
ps_make_selection
ps_validate_plan
ps_apply_plan
ps_undo_last
ps_get_job
```

最小调用顺序：

1. `ps_ping_backend`
2. `ps_bridge_diagnostics`
3. `ps_get_state`
4. `ps_export_preview`
5. 需要修改时先 `ps_validate_plan`
6. 再 `ps_apply_plan`

不要跳过 `ps_validate_plan`。它是便宜的刹车。

## 7. 可选模型 worker

SAM：

```powershell
.\.venv\Scripts\python.exe backend\cli.py sam start
.\.venv\Scripts\python.exe backend\cli.py sam status
```

Grounding DINO + HQ-SAM：

```powershell
.\.venv\Scripts\python.exe backend\cli.py grounding start
.\.venv\Scripts\python.exe backend\cli.py grounding status
```

只有调用这些工具时才需要启动：

- `ps_generate_sam_mask`
- `ps_detect_grounding_boxes`
- `ps_generate_hqsam_mask`
- `ps_generate_grounded_hq_mask`

## 8. 常见问题

### 后端连不上

```powershell
.\.venv\Scripts\python.exe backend\cli.py daemon status
.\.venv\Scripts\python.exe backend\cli.py daemon logs
```

确认端口是 `17860`，并且没有旧进程占用。

### Photoshop 没响应任务

按这个顺序查：

1. Photoshop 是否已经打开文档
2. `PS UXP Agent` 面板是否已经打开
3. UXP Developer Tool 是否点过 Reload
4. 后端是否是 `http://127.0.0.1:17860`
5. `backend\cli.py doctor` 输出的错误是什么

### MCP 能列工具，但调用失败

通常是后端没启动或 Photoshop 面板没连上：

```powershell
.\.venv\Scripts\python.exe backend\cli.py health
.\.venv\Scripts\python.exe backend\cli.py doctor
```

### 模型工具失败

先确认模型文件存在，再确认 worker 已启动：

```powershell
.\.venv\Scripts\python.exe backend\cli.py sam status
.\.venv\Scripts\python.exe backend\cli.py grounding status
```

核心桥接不依赖这些模型；缺模型时只影响对应的分割/检测工具。
