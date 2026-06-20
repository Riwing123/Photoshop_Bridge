# PS UXP Agent 桥接鲁棒化说明

## 启动入口

推荐统一使用 CLI 管理后端：

```powershell
python backend\cli.py daemon start
python backend\cli.py daemon status
python backend\cli.py daemon stop
python backend\cli.py daemon restart
python backend\cli.py daemon logs --tail 80
python backend\cli.py doctor
```

后端默认监听 `http://127.0.0.1:17860`。运行时文件写入 `backend/runtime/`，包括：

- `backend.pid`：当前后端 PID
- `backend.lock`：后端启动信息
- `backend.token`：本机 shutdown token
- `ps-agent-backend.log`：后端日志
- `ps-agent.sqlite3`：持久化 job 队列

## 健康检查与诊断

`GET /health` 返回后端版本、进程 PID、运行时长、SQLite 路径、UXP 心跳、插件版本和队列统计。

`GET /api/diagnostics` 返回更完整的桥接诊断：

- 后端进程与路径权限
- SQLite schema 与队列状态
- 最近事件
- active jobs
- 最新 UXP heartbeat
- Face Landmarker 模型是否存在

MCP 新增 `ps_bridge_diagnostics`，用于 Codex 在 job 卡住、UXP 断连或 result 回传异常时排查。

## 队列持久化

job 队列使用标准库 `sqlite3`，数据库位于 `backend/runtime/ps-agent.sqlite3`。状态包括：

- `pending`
- `running`
- `done`
- `error`
- `cancelled`
- `expired`

UXP 领取 job 时会写入 `claimed_by`、`claimed_at`、`lease_expires_at` 和 `attempts`。后端重启后，已完成 job 仍可通过 `GET /api/jobs/{job_id}` 查询。

## UXP 面板

UXP 面板只负责状态显示和诊断，不直接启动后端进程。面板提供：

- `Refresh`：立即 heartbeat + polling
- `Diagnostics`：读取 `/api/diagnostics`
- `Copy Start`：复制 `python D:\Photo_sontrol\backend\cli.py daemon start`

连接失败时，面板会对 polling 做指数退避；如果 Photoshop 已执行完 job 但 result 回传失败，会缓存 pending result 并优先重试回传，避免重复领取新 job。
