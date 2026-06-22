from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


DEFAULT_BASE_URL = "http://127.0.0.1:17860"
DEFAULT_SAM_URL = "http://127.0.0.1:17861"
DEFAULT_GROUNDING_HQ_URL = "http://127.0.0.1:17862"
CURRENT_DIR = Path(__file__).resolve().parent
WORKSPACE_ROOT = CURRENT_DIR.parent
RUNTIME_DIR = CURRENT_DIR / "runtime"
PID_PATH = RUNTIME_DIR / "backend.pid"
LOCK_PATH = RUNTIME_DIR / "backend.lock"
TOKEN_PATH = RUNTIME_DIR / "backend.token"
LOG_PATH = RUNTIME_DIR / "ps-agent-backend.log"
APP_PATH = CURRENT_DIR / "app.py"
BACKEND_START_SCRIPT = CURRENT_DIR / "start_backend.ps1"
VENV_PYTHON = WORKSPACE_ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
SAM_WORKER_PATH = CURRENT_DIR / "sam_worker.py"
SAM_START_SCRIPT = CURRENT_DIR / "start_sam_worker.ps1"
SAM_PID_PATH = RUNTIME_DIR / "sam-worker.pid"
SAM_LOG_PATH = RUNTIME_DIR / "sam-worker.log"
SAM_VENV_PYTHON = WORKSPACE_ROOT / ".venv-sam" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
SAM_MODEL_PATH = CURRENT_DIR / "models" / "sam2" / "sam2.1_hiera_base_plus.pt"
SAM_CONFIG = "configs/sam2.1/sam2.1_hiera_b+.yaml"
GROUNDING_HQ_WORKER_PATH = CURRENT_DIR / "grounding_hq_worker.py"
GROUNDING_HQ_START_SCRIPT = CURRENT_DIR / "start_grounding_hq_worker.ps1"
GROUNDING_HQ_PID_PATH = RUNTIME_DIR / "grounding-hq-worker.pid"
GROUNDING_HQ_LOG_PATH = RUNTIME_DIR / "grounding-hq-worker.log"
GROUNDING_DINO_MODEL_PATH = CURRENT_DIR / "models" / "grounding_dino" / "groundingdino_swint_ogc.pth"
GROUNDING_DINO_CONFIG_PATH = CURRENT_DIR / "models" / "grounding_dino" / "GroundingDINO_SwinT_OGC.py"
HQSAM_MODEL_PATH = CURRENT_DIR / "models" / "sam_hq" / "sam_hq_vit_l.pth"
HQSAM_MODEL_TYPE = "vit_l"


def request_json(
    base_url: str,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    timeout: float = 35,
) -> dict[str, Any]:
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(base_url.rstrip("/") + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"status": "error", "error": {"code": "http_error", "message": raw or str(exc)}}
    except urllib.error.URLError as exc:
        return {
            "status": "error",
            "error": {
                "code": "backend_unreachable",
                "message": f"Could not reach {base_url.rstrip('/')}: {exc.reason}",
            },
        }
    except OSError as exc:
        return {
            "status": "error",
            "error": {
                "code": "backend_unreachable",
                "message": f"Could not reach {base_url.rstrip('/')}: {exc}",
            },
        }


def print_json(data: dict[str, Any]) -> None:
    text = json.dumps(data, ensure_ascii=False, indent=2)
    try:
        print(text)
    except UnicodeEncodeError:
        encoding = sys.stdout.encoding or "utf-8"
        sys.stdout.buffer.write((text + "\n").encode(encoding, errors="replace"))
        sys.stdout.buffer.flush()


def ok(data: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = {"status": "ok"}
    if data:
        payload.update(data)
    return payload


def err(code: str, message: str, details: Any | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"status": "error", "error": {"code": code, "message": message}}
    if details is not None:
        payload["error"]["details"] = details
    return payload


def tail_text(path: Path, lines: int) -> str:
    if not path.is_file():
        return ""
    content = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(content[-max(1, min(lines, 1000)):])


def read_pid() -> int | None:
    try:
        raw = PID_PATH.read_text(encoding="utf-8").strip()
        return int(raw) if raw else None
    except (OSError, ValueError):
        return None


def read_pid_file(path: Path) -> int | None:
    try:
        raw = path.read_text(encoding="utf-8").strip()
        return int(raw) if raw else None
    except (OSError, ValueError):
        return None


def read_token() -> str | None:
    try:
        token = TOKEN_PATH.read_text(encoding="utf-8").strip()
        return token or None
    except OSError:
        return None


def ensure_token() -> str:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    token = read_token()
    if token:
        return token
    import secrets

    token = secrets.token_urlsafe(32)
    TOKEN_PATH.write_text(token, encoding="utf-8")
    return token


def process_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        if os.name == "nt":
            completed = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            return str(pid) in completed.stdout
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def backend_python_executable() -> Path:
    return VENV_PYTHON if VENV_PYTHON.is_file() else Path(sys.executable)


def start_powershell_script(script_path: Path, script_args: list[str]) -> int:
    def ps_quote(value: str) -> str:
        return "'" + str(value).replace("'", "''") + "'"

    def win_quote(value: str) -> str:
        return '"' + str(value).replace('"', '\\"') + '"'

    if os.name == "nt":
        argument_text = " ".join(
            [
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                win_quote(str(script_path)),
                *[win_quote(arg) if any(ch.isspace() for ch in str(arg)) else str(arg) for arg in script_args],
            ]
        )
        command = (
            "$shell = New-Object -ComObject Shell.Application; "
            f"$shell.ShellExecute('powershell.exe', {ps_quote(argument_text)}, "
            f"{ps_quote(str(WORKSPACE_ROOT))}, 'open', 0)"
        )
        completed = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                command,
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError((completed.stderr or completed.stdout or "ShellExecute failed").strip())
        return 0

    script_args_literal = "@(" + ",".join(ps_quote(arg) for arg in script_args) + ")"
    command = (
        f"$script = {ps_quote(str(script_path))}; "
        f"$workdir = {ps_quote(str(WORKSPACE_ROOT))}; "
        f"$scriptArgs = {script_args_literal}; "
        "$proc = Start-Process -FilePath 'powershell.exe' "
        "-ArgumentList (@('-NoProfile','-ExecutionPolicy','Bypass','-File',$script) + $scriptArgs) "
        "-WorkingDirectory $workdir -WindowStyle Hidden -PassThru; "
        "$proc.Id"
    )
    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            command,
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError((completed.stderr or completed.stdout or "Start-Process failed").strip())
    for token in completed.stdout.split():
        try:
            return int(token)
        except ValueError:
            continue
    return 0


def backend_health(base_url: str, timeout: float = 2) -> dict[str, Any]:
    return request_json(base_url, "GET", "/health", timeout=timeout)


def daemon_status(base_url: str) -> dict[str, Any]:
    health = backend_health(base_url, timeout=2)
    pid = read_pid()
    lock = None
    if LOCK_PATH.is_file():
        try:
            lock = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            lock = {"raw": LOCK_PATH.read_text(encoding="utf-8", errors="replace")}
    if health.get("status") == "ok":
        return ok(
            {
                "state": "running",
                "base_url": base_url,
                "pid": health.get("process", {}).get("pid") or pid,
                "health": health,
                "pid_file": str(PID_PATH),
                "lock": lock,
            }
        )
    return ok(
        {
            "state": "stopped",
            "base_url": base_url,
            "pid": pid,
            "pid_alive": process_exists(pid) if pid else False,
            "health": health,
            "pid_file": str(PID_PATH),
            "lock": lock,
        }
    )


def start_backend_process() -> int:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    ensure_token()
    (RUNTIME_DIR / "matplotlib").mkdir(parents=True, exist_ok=True)
    python_exe = backend_python_executable()
    if os.name == "nt" and BACKEND_START_SCRIPT.is_file() and os.environ.get("PS_AGENT_USE_PS_BACKEND_LAUNCHER") == "1":
        return start_powershell_script(BACKEND_START_SCRIPT, ["-PythonExe", str(python_exe)])

    log_handle = LOG_PATH.open("ab", buffering=0)
    env = os.environ.copy()
    env.setdefault("MPLCONFIGDIR", str(RUNTIME_DIR / "matplotlib"))
    env.setdefault("GLOG_minloglevel", "2")
    env.setdefault("ABSL_LOGGING_MIN_LOG_LEVEL", "2")
    env.setdefault("TF_CPP_MIN_LOG_LEVEL", "2")
    flags = 0
    if os.name == "nt":
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW
    try:
        process = subprocess.Popen(
            [str(python_exe), str(APP_PATH)],
            cwd=str(WORKSPACE_ROOT),
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            env=env,
            creationflags=flags,
        )
    finally:
        log_handle.close()
    return int(process.pid)


def daemon_start(base_url: str, wait_seconds: float = 10) -> dict[str, Any]:
    current = backend_health(base_url, timeout=1)
    if current.get("status") == "ok":
        return ok(
            {
                "state": "already_running",
                "base_url": base_url,
                "pid": current.get("process", {}).get("pid") or read_pid(),
                "health": current,
            }
        )
    pid = read_pid()
    if pid and process_exists(pid):
        return err(
            "backend_start_blocked_by_unreachable_pid",
            "A backend PID is still alive but /health is unreachable; refusing to start a second backend.",
            {"pid": pid, "pid_file": str(PID_PATH), "health": current, "log_path": str(LOG_PATH)},
        )

    pid = start_backend_process()
    deadline = time.monotonic() + max(1, wait_seconds)
    last_health: dict[str, Any] = {}
    while time.monotonic() < deadline:
        time.sleep(0.35)
        last_health = backend_health(base_url, timeout=1)
        if last_health.get("status") == "ok":
            return ok(
                {
                    "state": "started",
                    "base_url": base_url,
                    "pid": last_health.get("process", {}).get("pid") or pid,
                    "health": last_health,
                    "log_path": str(LOG_PATH),
                }
            )
    return err(
        "backend_start_timeout",
        "Backend process was started but /health did not become ready in time.",
        {"pid": pid, "last_health": last_health, "log_path": str(LOG_PATH), "log_tail": tail_text(LOG_PATH, 80)},
    )


def daemon_stop(base_url: str, timeout_seconds: float = 10) -> dict[str, Any]:
    health = backend_health(base_url, timeout=2)
    if health.get("status") != "ok":
        pid = read_pid()
        if pid and process_exists(pid):
            return err(
                "backend_unreachable_cannot_confirm",
                "A PID file exists, but /health is unreachable, so the CLI will not stop an unconfirmed process.",
                {"pid": pid, "health": health, "pid_file": str(PID_PATH)},
            )
        for path in (PID_PATH, LOCK_PATH):
            try:
                if path.is_file():
                    path.unlink()
            except OSError:
                pass
        return ok({"state": "already_stopped", "health": health})

    pid_from_health = health.get("process", {}).get("pid")
    pid_from_file = read_pid()
    if pid_from_file and pid_from_health and int(pid_from_file) != int(pid_from_health):
        return err(
            "pid_mismatch",
            "PID file does not match the running backend, refusing to stop.",
            {"pid_file": pid_from_file, "pid_health": pid_from_health},
        )

    token = read_token()
    if not token:
        return err("shutdown_token_missing", "Backend shutdown token file is missing.", {"token_path": str(TOKEN_PATH)})

    result = request_json(base_url, "POST", "/api/shutdown", {"token": token}, timeout=3)
    if result.get("status") != "ok":
        return result

    deadline = time.monotonic() + max(1, timeout_seconds)
    while time.monotonic() < deadline:
        time.sleep(0.35)
        probe = backend_health(base_url, timeout=1)
        if probe.get("status") != "ok":
            return ok({"state": "stopped", "shutdown": result})
    return err(
        "backend_stop_timeout",
        "Shutdown was requested but the backend is still reachable.",
        {"shutdown": result, "health": backend_health(base_url, timeout=1)},
    )


def daemon_restart(base_url: str) -> dict[str, Any]:
    stopped = daemon_stop(base_url)
    if stopped.get("status") != "ok":
        return stopped
    started = daemon_start(base_url)
    return ok({"stop": stopped, "start": started}) if started.get("status") == "ok" else started


def daemon_logs(tail: int) -> dict[str, Any]:
    return ok({"log_path": str(LOG_PATH), "tail": tail_text(LOG_PATH, tail)})


def sam_health(base_url: str = DEFAULT_SAM_URL, timeout: float = 2) -> dict[str, Any]:
    return request_json(base_url, "GET", "/health", timeout=timeout)


def sam_status(base_url: str = DEFAULT_SAM_URL) -> dict[str, Any]:
    health = sam_health(base_url, timeout=2)
    pid = read_pid_file(SAM_PID_PATH)
    paths = {
        "venv_python": {"path": str(SAM_VENV_PYTHON), "exists": SAM_VENV_PYTHON.is_file()},
        "worker_script": {"path": str(SAM_WORKER_PATH), "exists": SAM_WORKER_PATH.is_file()},
        "model": {
            "path": str(SAM_MODEL_PATH),
            "exists": SAM_MODEL_PATH.is_file(),
            "size_bytes": SAM_MODEL_PATH.stat().st_size if SAM_MODEL_PATH.is_file() else None,
        },
        "pid_file": {"path": str(SAM_PID_PATH), "exists": SAM_PID_PATH.is_file()},
        "log": {"path": str(SAM_LOG_PATH), "exists": SAM_LOG_PATH.is_file()},
    }
    state = "running" if health.get("status") == "ok" else "stopped"
    resolved_pid = health.get("process", {}).get("pid") or pid
    return ok(
        {
            "state": state,
            "base_url": base_url,
            "pid": resolved_pid,
            "pid_alive": True if health.get("status") == "ok" else (process_exists(pid) if pid else False),
            "health": health,
            "paths": paths,
            "start_command": "python backend/cli.py sam start",
        }
    )


def start_sam_worker_process() -> int:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    log_handle = SAM_LOG_PATH.open("ab", buffering=0)
    env = os.environ.copy()
    env.setdefault("PYTHONUNBUFFERED", "1")
    env.setdefault("PS_AGENT_SAM_MODEL_PATH", str(SAM_MODEL_PATH))
    env.setdefault("PS_AGENT_SAM_CONFIG", SAM_CONFIG)
    env.setdefault("PS_AGENT_SAM_HOST", "127.0.0.1")
    env.setdefault("PS_AGENT_SAM_PORT", "17861")
    flags = 0
    if os.name == "nt":
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    try:
        process = subprocess.Popen(
            [str(SAM_VENV_PYTHON), str(SAM_WORKER_PATH)],
            cwd=str(WORKSPACE_ROOT),
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            env=env,
            creationflags=flags,
        )
    finally:
        log_handle.close()
    return int(process.pid)


def sam_start(base_url: str = DEFAULT_SAM_URL, wait_seconds: float = 20) -> dict[str, Any]:
    current = sam_health(base_url, timeout=1)
    if current.get("status") == "ok":
        return ok(
            {
                "state": "already_running",
                "base_url": base_url,
                "pid": current.get("process", {}).get("pid") or read_pid_file(SAM_PID_PATH),
                "health": current,
            }
        )

    missing = []
    if not SAM_VENV_PYTHON.is_file():
        missing.append({"code": "sam_venv_missing", "path": str(SAM_VENV_PYTHON)})
    if not SAM_WORKER_PATH.is_file():
        missing.append({"code": "sam_worker_missing", "path": str(SAM_WORKER_PATH)})
    if not SAM_MODEL_PATH.is_file():
        missing.append({"code": "sam_model_missing", "path": str(SAM_MODEL_PATH)})
    if missing:
        return err(
            "sam_start_prerequisites_missing",
            "SAM worker prerequisites are missing.",
            {
                "missing": missing,
                "venv_path": str(WORKSPACE_ROOT / ".venv-sam"),
                "model_path": str(SAM_MODEL_PATH),
                "note": "Install SAM dependencies in .venv-sam and place the SAM 2.1 Base+ checkpoint before starting.",
            },
        )

    pid = read_pid_file(SAM_PID_PATH)
    if pid and process_exists(pid):
        return err(
            "sam_start_blocked_by_unreachable_pid",
            "A SAM worker PID is still alive but /health is unreachable; refusing to start a second worker.",
            {"pid": pid, "pid_file": str(SAM_PID_PATH), "health": current, "log_path": str(SAM_LOG_PATH)},
        )

    pid = start_sam_worker_process()
    deadline = time.monotonic() + max(1, wait_seconds)
    last_health: dict[str, Any] = {}
    while time.monotonic() < deadline:
        time.sleep(0.35)
        last_health = sam_health(base_url, timeout=1)
        if last_health.get("status") == "ok":
            return ok(
                {
                    "state": "started",
                    "base_url": base_url,
                    "pid": last_health.get("process", {}).get("pid") or pid,
                    "health": last_health,
                    "log_path": str(SAM_LOG_PATH),
                }
            )
    return err(
        "sam_start_timeout",
        "SAM worker process was started but /health did not become ready in time.",
        {"pid": pid, "last_health": last_health, "log_path": str(SAM_LOG_PATH), "log_tail": tail_text(SAM_LOG_PATH, 80)},
    )


def sam_stop(base_url: str = DEFAULT_SAM_URL, timeout_seconds: float = 10) -> dict[str, Any]:
    health = sam_health(base_url, timeout=2)
    if health.get("status") != "ok":
        pid = read_pid_file(SAM_PID_PATH)
        if pid and process_exists(pid):
            return err(
                "sam_unreachable_cannot_confirm",
                "A SAM worker PID file exists, but /health is unreachable, so the CLI will not stop an unconfirmed process.",
                {"pid": pid, "health": health, "pid_file": str(SAM_PID_PATH)},
            )
        try:
            if SAM_PID_PATH.is_file():
                SAM_PID_PATH.unlink()
        except OSError:
            pass
        return ok({"state": "already_stopped", "health": health})

    result = request_json(base_url, "POST", "/shutdown", {}, timeout=3)
    if result.get("status") != "ok":
        return result

    deadline = time.monotonic() + max(1, timeout_seconds)
    while time.monotonic() < deadline:
        time.sleep(0.35)
        probe = sam_health(base_url, timeout=1)
        if probe.get("status") != "ok":
            return ok({"state": "stopped", "shutdown": result})
    return err(
        "sam_stop_timeout",
        "Shutdown was requested but the SAM worker is still reachable.",
        {"shutdown": result, "health": sam_health(base_url, timeout=1)},
    )


def sam_restart(base_url: str = DEFAULT_SAM_URL) -> dict[str, Any]:
    stopped = sam_stop(base_url)
    if stopped.get("status") != "ok":
        return stopped
    started = sam_start(base_url)
    return ok({"stop": stopped, "start": started}) if started.get("status") == "ok" else started


def sam_logs(tail: int) -> dict[str, Any]:
    return ok({"log_path": str(SAM_LOG_PATH), "tail": tail_text(SAM_LOG_PATH, tail)})


def grounding_hq_health(base_url: str = DEFAULT_GROUNDING_HQ_URL, timeout: float = 2) -> dict[str, Any]:
    return request_json(base_url, "GET", "/health", timeout=timeout)


def grounding_hq_status(base_url: str = DEFAULT_GROUNDING_HQ_URL) -> dict[str, Any]:
    health = grounding_hq_health(base_url, timeout=2)
    pid = read_pid_file(GROUNDING_HQ_PID_PATH)
    paths = {
        "venv_python": {"path": str(SAM_VENV_PYTHON), "exists": SAM_VENV_PYTHON.is_file()},
        "worker_script": {"path": str(GROUNDING_HQ_WORKER_PATH), "exists": GROUNDING_HQ_WORKER_PATH.is_file()},
        "grounding_dino_model": {
            "path": str(GROUNDING_DINO_MODEL_PATH),
            "exists": GROUNDING_DINO_MODEL_PATH.is_file(),
            "size_bytes": GROUNDING_DINO_MODEL_PATH.stat().st_size if GROUNDING_DINO_MODEL_PATH.is_file() else None,
        },
        "grounding_dino_config": {
            "path": str(GROUNDING_DINO_CONFIG_PATH),
            "exists": GROUNDING_DINO_CONFIG_PATH.is_file(),
        },
        "hqsam_model": {
            "path": str(HQSAM_MODEL_PATH),
            "exists": HQSAM_MODEL_PATH.is_file(),
            "size_bytes": HQSAM_MODEL_PATH.stat().st_size if HQSAM_MODEL_PATH.is_file() else None,
        },
        "pid_file": {"path": str(GROUNDING_HQ_PID_PATH), "exists": GROUNDING_HQ_PID_PATH.is_file()},
        "log": {"path": str(GROUNDING_HQ_LOG_PATH), "exists": GROUNDING_HQ_LOG_PATH.is_file()},
    }
    state = "running" if health.get("status") == "ok" else "stopped"
    resolved_pid = health.get("process", {}).get("pid") or pid
    return ok(
        {
            "state": state,
            "base_url": base_url,
            "pid": resolved_pid,
            "pid_alive": True if health.get("status") == "ok" else (process_exists(pid) if pid else False),
            "health": health,
            "paths": paths,
            "start_command": "python backend/cli.py grounding start",
        }
    )


def start_grounding_hq_worker_process() -> int:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    if os.name == "nt" and GROUNDING_HQ_START_SCRIPT.is_file():
        return start_powershell_script(
            GROUNDING_HQ_START_SCRIPT,
            [
                "-PythonExe",
                str(SAM_VENV_PYTHON),
                "-GroundingDinoModelPath",
                str(GROUNDING_DINO_MODEL_PATH),
                "-GroundingDinoConfigPath",
                str(GROUNDING_DINO_CONFIG_PATH),
                "-HQSamModelPath",
                str(HQSAM_MODEL_PATH),
                "-HQSamModelType",
                HQSAM_MODEL_TYPE,
            ],
        )

    log_handle = GROUNDING_HQ_LOG_PATH.open("ab", buffering=0)
    env = os.environ.copy()
    env.setdefault("PYTHONUNBUFFERED", "1")
    env.setdefault("PS_AGENT_GROUNDING_HQ_HOST", "127.0.0.1")
    env.setdefault("PS_AGENT_GROUNDING_HQ_PORT", "17862")
    env.setdefault("PS_AGENT_GROUNDING_DINO_MODEL_PATH", str(GROUNDING_DINO_MODEL_PATH))
    env.setdefault("PS_AGENT_GROUNDING_DINO_CONFIG_PATH", str(GROUNDING_DINO_CONFIG_PATH))
    env.setdefault("PS_AGENT_GROUNDING_DEVICE", "auto")
    env.setdefault("PS_AGENT_HQSAM_MODEL_PATH", str(HQSAM_MODEL_PATH))
    env.setdefault("PS_AGENT_HQSAM_MODEL_TYPE", HQSAM_MODEL_TYPE)
    env.setdefault("PS_AGENT_HQSAM_DEVICE", "auto")
    flags = 0
    if os.name == "nt":
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    try:
        process = subprocess.Popen(
            [str(SAM_VENV_PYTHON), str(GROUNDING_HQ_WORKER_PATH)],
            cwd=str(WORKSPACE_ROOT),
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            env=env,
            creationflags=flags,
        )
    finally:
        log_handle.close()
    return int(process.pid)


def grounding_hq_start(base_url: str = DEFAULT_GROUNDING_HQ_URL, wait_seconds: float = 20) -> dict[str, Any]:
    current = grounding_hq_health(base_url, timeout=1)
    if current.get("status") == "ok":
        return ok(
            {
                "state": "already_running",
                "base_url": base_url,
                "pid": current.get("process", {}).get("pid") or read_pid_file(GROUNDING_HQ_PID_PATH),
                "health": current,
            }
        )

    missing = []
    if not SAM_VENV_PYTHON.is_file():
        missing.append({"code": "grounding_hq_venv_missing", "path": str(SAM_VENV_PYTHON)})
    if not GROUNDING_HQ_WORKER_PATH.is_file():
        missing.append({"code": "grounding_hq_worker_missing", "path": str(GROUNDING_HQ_WORKER_PATH)})
    if not GROUNDING_DINO_MODEL_PATH.is_file():
        missing.append({"code": "grounding_dino_model_missing", "path": str(GROUNDING_DINO_MODEL_PATH)})
    if not GROUNDING_DINO_CONFIG_PATH.is_file():
        missing.append({"code": "grounding_dino_config_missing", "path": str(GROUNDING_DINO_CONFIG_PATH)})
    if not HQSAM_MODEL_PATH.is_file():
        missing.append({"code": "hqsam_model_missing", "path": str(HQSAM_MODEL_PATH)})
    if missing:
        return err(
            "grounding_hq_start_prerequisites_missing",
            "Grounding HQ worker prerequisites are missing.",
            {
                "missing": missing,
                "venv_path": str(WORKSPACE_ROOT / ".venv-sam"),
                "note": "Install Grounding DINO and HQ-SAM dependencies in .venv-sam and place the local checkpoints before starting.",
            },
        )

    pid = read_pid_file(GROUNDING_HQ_PID_PATH)
    if pid and process_exists(pid):
        return err(
            "grounding_hq_start_blocked_by_unreachable_pid",
            "A Grounding HQ worker PID is still alive but /health is unreachable; refusing to start a second worker.",
            {"pid": pid, "pid_file": str(GROUNDING_HQ_PID_PATH), "health": current, "log_path": str(GROUNDING_HQ_LOG_PATH)},
        )

    pid = start_grounding_hq_worker_process()
    deadline = time.monotonic() + max(1, wait_seconds)
    last_health: dict[str, Any] = {}
    while time.monotonic() < deadline:
        time.sleep(0.35)
        last_health = grounding_hq_health(base_url, timeout=1)
        if last_health.get("status") == "ok":
            return ok(
                {
                    "state": "started",
                    "base_url": base_url,
                    "pid": last_health.get("process", {}).get("pid") or pid,
                    "health": last_health,
                    "log_path": str(GROUNDING_HQ_LOG_PATH),
                }
            )
    return err(
        "grounding_hq_start_timeout",
        "Grounding HQ worker process was started but /health did not become ready in time.",
        {"pid": pid, "last_health": last_health, "log_path": str(GROUNDING_HQ_LOG_PATH), "log_tail": tail_text(GROUNDING_HQ_LOG_PATH, 80)},
    )


def grounding_hq_stop(base_url: str = DEFAULT_GROUNDING_HQ_URL, timeout_seconds: float = 10) -> dict[str, Any]:
    health = grounding_hq_health(base_url, timeout=2)
    if health.get("status") != "ok":
        pid = read_pid_file(GROUNDING_HQ_PID_PATH)
        if pid and process_exists(pid):
            return err(
                "grounding_hq_unreachable_cannot_confirm",
                "A Grounding HQ worker PID file exists, but /health is unreachable, so the CLI will not stop an unconfirmed process.",
                {"pid": pid, "health": health, "pid_file": str(GROUNDING_HQ_PID_PATH)},
            )
        try:
            if GROUNDING_HQ_PID_PATH.is_file():
                GROUNDING_HQ_PID_PATH.unlink()
        except OSError:
            pass
        return ok({"state": "already_stopped", "health": health})

    result = request_json(base_url, "POST", "/shutdown", {}, timeout=3)
    if result.get("status") != "ok":
        return result

    deadline = time.monotonic() + max(1, timeout_seconds)
    while time.monotonic() < deadline:
        time.sleep(0.35)
        probe = grounding_hq_health(base_url, timeout=1)
        if probe.get("status") != "ok":
            return ok({"state": "stopped", "shutdown": result})
    return err(
        "grounding_hq_stop_timeout",
        "Shutdown was requested but the Grounding HQ worker is still reachable.",
        {"shutdown": result, "health": grounding_hq_health(base_url, timeout=1)},
    )


def grounding_hq_restart(base_url: str = DEFAULT_GROUNDING_HQ_URL) -> dict[str, Any]:
    stopped = grounding_hq_stop(base_url)
    if stopped.get("status") != "ok":
        return stopped
    started = grounding_hq_start(base_url)
    return ok({"stop": stopped, "start": started}) if started.get("status") == "ok" else started


def grounding_hq_logs(tail: int) -> dict[str, Any]:
    return ok({"log_path": str(GROUNDING_HQ_LOG_PATH), "tail": tail_text(GROUNDING_HQ_LOG_PATH, tail)})


def doctor(base_url: str) -> dict[str, Any]:
    health = backend_health(base_url, timeout=2)
    checks = {
        "workspace_root": {"path": str(WORKSPACE_ROOT), "exists": WORKSPACE_ROOT.is_dir()},
        "app": {"path": str(APP_PATH), "exists": APP_PATH.is_file()},
        "sam_worker": {"path": str(SAM_WORKER_PATH), "exists": SAM_WORKER_PATH.is_file()},
        "sam_venv_python": {"path": str(SAM_VENV_PYTHON), "exists": SAM_VENV_PYTHON.is_file()},
        "sam_model": {"path": str(SAM_MODEL_PATH), "exists": SAM_MODEL_PATH.is_file()},
        "grounding_hq_worker": {"path": str(GROUNDING_HQ_WORKER_PATH), "exists": GROUNDING_HQ_WORKER_PATH.is_file()},
        "grounding_dino_model": {"path": str(GROUNDING_DINO_MODEL_PATH), "exists": GROUNDING_DINO_MODEL_PATH.is_file()},
        "grounding_dino_config": {"path": str(GROUNDING_DINO_CONFIG_PATH), "exists": GROUNDING_DINO_CONFIG_PATH.is_file()},
        "hqsam_model": {"path": str(HQSAM_MODEL_PATH), "exists": HQSAM_MODEL_PATH.is_file()},
        "runtime": {"path": str(RUNTIME_DIR), "exists": RUNTIME_DIR.exists()},
        "pid": {"path": str(PID_PATH), "pid": read_pid()},
        "sam_pid": {"path": str(SAM_PID_PATH), "pid": read_pid_file(SAM_PID_PATH)},
        "grounding_hq_pid": {"path": str(GROUNDING_HQ_PID_PATH), "pid": read_pid_file(GROUNDING_HQ_PID_PATH)},
        "log": {"path": str(LOG_PATH), "exists": LOG_PATH.is_file()},
        "sam_log": {"path": str(SAM_LOG_PATH), "exists": SAM_LOG_PATH.is_file()},
        "grounding_hq_log": {"path": str(GROUNDING_HQ_LOG_PATH), "exists": GROUNDING_HQ_LOG_PATH.is_file()},
        "token": {"path": str(TOKEN_PATH), "exists": TOKEN_PATH.is_file()},
    }
    sam = sam_status(DEFAULT_SAM_URL)
    grounding_hq = grounding_hq_status(DEFAULT_GROUNDING_HQ_URL)
    if health.get("status") == "ok":
        diagnostics = request_json(base_url, "GET", "/api/diagnostics", timeout=5)
        return ok({"state": "running", "checks": checks, "health": health, "diagnostics": diagnostics, "sam": sam, "grounding_hq": grounding_hq})
    return ok(
        {
            "state": "stopped",
            "checks": checks,
            "health": health,
            "sam": sam,
            "grounding_hq": grounding_hq,
            "start_command": "python backend/cli.py daemon start",
        }
    )


def load_plan(args: argparse.Namespace) -> dict[str, Any]:
    if getattr(args, "plan_json", None):
        data = json.loads(args.plan_json)
    else:
        data = json.loads(Path(args.plan_file).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Plan must be a JSON object.")
    return data


def load_selection_mask(args: argparse.Namespace) -> dict[str, Any]:
    if getattr(args, "mask_json", None):
        data = json.loads(args.mask_json)
    else:
        data = json.loads(Path(args.mask_file).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Selection mask must be a JSON object.")
    return data


def parse_region(value: str, padding: int) -> dict[str, Any]:
    try:
        region_id, coords = value.split(":", 1)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("region must use id:x,y,width,height") from exc

    parts = [part.strip() for part in coords.split(",")]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("region must use id:x,y,width,height")
    try:
        x, y, width, height = [float(part) for part in parts]
    except ValueError as exc:
        raise argparse.ArgumentTypeError("region coordinates must be numbers") from exc
    if not region_id.strip():
        raise argparse.ArgumentTypeError("region id cannot be empty")
    return {
        "id": region_id.strip(),
        "type": "bbox",
        "bbox": {
            "x": x,
            "y": y,
            "width": width,
            "height": height,
        },
        "padding": max(0, min(int(padding), 512)),
    }


def parse_bbox_arg(value: str) -> dict[str, Any]:
    parts = [part.strip() for part in value.split(",")]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("bbox must use x,y,width,height")
    try:
        x, y, width, height = [float(part) for part in parts]
    except ValueError as exc:
        raise argparse.ArgumentTypeError("bbox coordinates must be numbers") from exc
    if width <= 0 or height <= 0 or x < 0 or y < 0:
        raise argparse.ArgumentTypeError("bbox coordinates must be non-negative with positive width/height")
    return {"x": x, "y": y, "width": width, "height": height}


def parse_size_arg(value: str) -> dict[str, Any]:
    parts = [part.strip() for part in value.lower().replace("x", ",").split(",")]
    if len(parts) != 2:
        raise argparse.ArgumentTypeError("size must use width,height or widthxheight")
    try:
        width, height = [int(round(float(part))) for part in parts]
    except ValueError as exc:
        raise argparse.ArgumentTypeError("size values must be numbers") from exc
    if width <= 0 or height <= 0:
        raise argparse.ArgumentTypeError("size values must be positive")
    return {"width": width, "height": height}


def parse_point_arg(value: str) -> dict[str, Any]:
    parts = [part.strip() for part in value.split(",")]
    if len(parts) != 2:
        raise argparse.ArgumentTypeError("point must use x,y")
    try:
        x, y = [float(part) for part in parts]
    except ValueError as exc:
        raise argparse.ArgumentTypeError("point coordinates must be numbers") from exc
    if x < 0 or y < 0:
        raise argparse.ArgumentTypeError("point coordinates must be non-negative")
    return {"x": x, "y": y}


def parse_rgb_arg(value: str) -> dict[str, Any]:
    parts = [part.strip() for part in value.split(",")]
    if len(parts) != 3:
        raise argparse.ArgumentTypeError("rgb must use r,g,b")
    try:
        r, g, b = [float(part) for part in parts]
    except ValueError as exc:
        raise argparse.ArgumentTypeError("rgb values must be numbers") from exc
    if any(channel < 0 or channel > 255 for channel in (r, g, b)):
        raise argparse.ArgumentTypeError("rgb values must be in 0..255")
    return {"r": r, "g": g, "b": b}


def parse_parts_arg(value: str) -> list[str]:
    parts = [part.strip() for part in value.split(",") if part.strip()]
    if not parts:
        raise argparse.ArgumentTypeError("parts cannot be empty")
    return parts


def parse_query_arg(value: str) -> dict[str, Any]:
    raw = str(value).strip()
    if not raw:
        raise argparse.ArgumentTypeError("query cannot be empty")
    if ":" in raw:
        query_id, prompt = raw.split(":", 1)
        query_id = query_id.strip() or f"query_{abs(hash(raw)) % 10000}"
        prompt = prompt.strip()
    else:
        query_id = f"query_{abs(hash(raw)) % 10000}"
        prompt = raw
    if not prompt:
        raise argparse.ArgumentTypeError("query prompt cannot be empty")
    return {"id": query_id, "prompt_en": prompt}


def main() -> int:
    parser = argparse.ArgumentParser(description="Debug CLI for the local PS UXP Agent backend.")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    subparsers = parser.add_subparsers(dest="command", required=True)

    health_parser = subparsers.add_parser("health", help="Show backend and UXP heartbeat status.")
    health_parser.add_argument("--json", action="store_true", help="Keep machine-readable JSON output.")

    daemon_parser = subparsers.add_parser("daemon", help="Manage the local backend daemon.")
    daemon_subparsers = daemon_parser.add_subparsers(dest="daemon_command", required=True)
    daemon_subparsers.add_parser("start", help="Start the backend daemon if needed.")
    daemon_subparsers.add_parser("stop", help="Stop the backend daemon using the local shutdown token.")
    daemon_subparsers.add_parser("restart", help="Restart the backend daemon.")
    daemon_subparsers.add_parser("status", help="Show daemon status.")
    logs_parser = daemon_subparsers.add_parser("logs", help="Show backend log tail.")
    logs_parser.add_argument("--tail", type=int, default=80)

    sam_parser = subparsers.add_parser("sam", help="Manage the optional SAM 2.1 mask worker.")
    sam_subparsers = sam_parser.add_subparsers(dest="sam_command", required=True)
    sam_subparsers.add_parser("start", help="Start the SAM worker if prerequisites exist.")
    sam_subparsers.add_parser("stop", help="Stop the SAM worker through its local shutdown endpoint.")
    sam_subparsers.add_parser("restart", help="Restart the SAM worker.")
    sam_subparsers.add_parser("status", help="Show SAM worker status.")
    sam_logs_parser = sam_subparsers.add_parser("logs", help="Show SAM worker log tail.")
    sam_logs_parser.add_argument("--tail", type=int, default=80)

    grounding_parser = subparsers.add_parser("grounding", help="Manage the optional Grounding DINO + HQ-SAM worker.")
    grounding_subparsers = grounding_parser.add_subparsers(dest="grounding_command", required=True)
    grounding_subparsers.add_parser("start", help="Start the Grounding HQ worker if prerequisites exist.")
    grounding_subparsers.add_parser("stop", help="Stop the Grounding HQ worker through its local shutdown endpoint.")
    grounding_subparsers.add_parser("restart", help="Restart the Grounding HQ worker.")
    grounding_subparsers.add_parser("status", help="Show Grounding HQ worker status.")
    grounding_logs_parser = grounding_subparsers.add_parser("logs", help="Show Grounding HQ worker log tail.")
    grounding_logs_parser.add_argument("--tail", type=int, default=80)

    sam_mask_parser = subparsers.add_parser("sam-mask", help="Generate a SAM alpha mask through the backend.")
    sam_mask_parser.add_argument("--asset-path", required=True)
    sam_mask_parser.add_argument("--document-size", required=True, type=parse_size_arg, help="Original document size: width,height")
    sam_mask_parser.add_argument("--document-bbox", required=True, type=parse_bbox_arg, help="Crop bbox in original document pixels: x,y,width,height")
    sam_mask_parser.add_argument("--scale-factor", required=True, type=float, help="Scale factor from document pixels to the exported asset.")
    sam_mask_parser.add_argument("--prompt-bbox", type=parse_bbox_arg, default=None, help="SAM bbox prompt in document pixels by default.")
    sam_mask_parser.add_argument("--positive-point", action="append", type=parse_point_arg, default=[])
    sam_mask_parser.add_argument("--negative-point", action="append", type=parse_point_arg, default=[])
    sam_mask_parser.add_argument("--coord-space", choices=["document", "asset"], default="document")
    sam_mask_parser.add_argument("--label", default="sam_alpha_mask")
    sam_mask_parser.add_argument("--threshold", type=float, default=0.5)
    sam_mask_parser.add_argument("--feather", type=float, default=0)
    sam_mask_parser.add_argument("--invert", action="store_true")
    sam_mask_parser.add_argument("--show-marching-ants", action="store_true")
    sam_mask_parser.add_argument("--timeout-ms", type=int, default=120000)

    grounded_detect_parser = subparsers.add_parser("grounded-detect", help="Run Grounding DINO detection through the backend.")
    grounded_detect_parser.add_argument("--asset-path", required=True)
    grounded_detect_parser.add_argument("--document-size", required=True, type=parse_size_arg, help="Original document size: width,height")
    grounded_detect_parser.add_argument("--document-bbox", required=True, type=parse_bbox_arg, help="Crop bbox in original document pixels: x,y,width,height")
    grounded_detect_parser.add_argument("--scale-factor", required=True, type=float)
    grounded_detect_parser.add_argument("--query", action="append", required=True, type=parse_query_arg, help="Detection query as id:english prompt or just english prompt.")
    grounded_detect_parser.add_argument("--box-threshold", type=float, default=0.35)
    grounded_detect_parser.add_argument("--text-threshold", type=float, default=0.25)
    grounded_detect_parser.add_argument("--max-candidates", type=int, default=32)
    grounded_detect_parser.add_argument("--dedupe-iou", type=float, default=0.85)
    grounded_detect_parser.add_argument("--timeout-ms", type=int, default=120000)

    grounded_mask_parser = subparsers.add_parser("grounded-mask", help="Run Grounding DINO + HQ-SAM alpha-mask generation through the backend.")
    grounded_mask_parser.add_argument("--asset-path", required=True)
    grounded_mask_parser.add_argument("--document-size", required=True, type=parse_size_arg, help="Original document size: width,height")
    grounded_mask_parser.add_argument("--document-bbox", required=True, type=parse_bbox_arg, help="Crop bbox in original document pixels: x,y,width,height")
    grounded_mask_parser.add_argument("--scale-factor", required=True, type=float)
    grounded_mask_parser.add_argument("--include-query", action="append", required=True, type=parse_query_arg, help="Include query as id:english prompt or just english prompt.")
    grounded_mask_parser.add_argument("--exclude-query", action="append", type=parse_query_arg, default=[], help="Exclude query as id:english prompt or just english prompt.")
    grounded_mask_parser.add_argument("--box-threshold", type=float, default=0.35)
    grounded_mask_parser.add_argument("--text-threshold", type=float, default=0.25)
    grounded_mask_parser.add_argument("--max-candidates", type=int, default=32)
    grounded_mask_parser.add_argument("--dedupe-iou", type=float, default=0.85)
    grounded_mask_parser.add_argument("--merge-mode", choices=["union", "subtract_excludes", "intersect"], default=None)
    grounded_mask_parser.add_argument("--threshold", type=float, default=0.5)
    grounded_mask_parser.add_argument("--feather", type=float, default=0)
    grounded_mask_parser.add_argument("--show-marching-ants", action="store_true")
    grounded_mask_parser.add_argument("--timeout-ms", type=int, default=180000)

    subparsers.add_parser("doctor", help="Run bridge diagnostics.")

    state_parser = subparsers.add_parser("state", help="Create a get_state job.")
    state_parser.add_argument("--no-wait", action="store_true", help="Return immediately after queueing the job.")
    state_parser.add_argument("--timeout-ms", type=int, default=25000)
    state_parser.add_argument("--no-layers", action="store_true")

    preview_parser = subparsers.add_parser("preview", help="Create an export_preview job.")
    preview_parser.add_argument("--no-wait", action="store_true", help="Return immediately after queueing the job.")
    preview_parser.add_argument("--timeout-ms", type=int, default=30000)
    preview_parser.add_argument("--format", choices=["png", "jpeg"], default="jpeg")
    preview_parser.add_argument("--max-side", type=int, default=None)
    preview_parser.add_argument("--mode", choices=["quick", "standard", "detail"], default="standard")
    preview_parser.add_argument("--quality", type=int, default=8)

    regions_parser = subparsers.add_parser("regions", help="Create an export_regions job.")
    regions_parser.add_argument("--no-wait", action="store_true", help="Return immediately after queueing the job.")
    regions_parser.add_argument("--timeout-ms", type=int, default=30000)
    regions_parser.add_argument("--format", choices=["png", "jpeg"], default="jpeg")
    regions_parser.add_argument("--max-side", type=int, default=1536)
    regions_parser.add_argument("--quality", type=int, default=8)
    regions_parser.add_argument("--padding", type=int, default=0)
    regions_parser.add_argument("--no-upscale", action="store_true", help="Do not upscale regions smaller than --max-side.")
    regions_parser.add_argument(
        "--region",
        action="append",
        required=True,
        help="Region bbox in document pixels, e.g. face:800,360,460,560. Repeat for multiple regions.",
    )

    validate_parser = subparsers.add_parser("validate", help="Validate a ps-agent/v1 edit plan.")
    validate_source = validate_parser.add_mutually_exclusive_group(required=True)
    validate_source.add_argument("--plan-file")
    validate_source.add_argument("--plan-json")

    face_parser = subparsers.add_parser("face-selection", help="Generate polygon selection masks from a face crop.")
    face_parser.add_argument("--asset-path", required=True)
    face_parser.add_argument("--document-bbox", required=True, type=parse_bbox_arg, help="Original document bbox: x,y,width,height")
    face_parser.add_argument("--scale-factor", required=True, type=float)
    face_parser.add_argument(
        "--parts",
        required=True,
        type=parse_parts_arg,
        help="Comma-separated parts: left_eye,right_eye,both_eyes,lips_outer,face_oval,left_cheek,right_cheek",
    )
    face_parser.add_argument("--face-index", type=int, default=None)
    face_parser.add_argument("--max-faces", type=int, default=4)
    face_parser.add_argument("--expand-px", type=float, default=0)
    face_parser.add_argument("--smooth", type=float, default=0)
    face_parser.add_argument("--feather", type=float, default=12)

    selection_parser = subparsers.add_parser("selection", help="Create an active Photoshop selection from a selection_mask.")
    selection_source = selection_parser.add_mutually_exclusive_group(required=True)
    selection_source.add_argument("--mask-file")
    selection_source.add_argument("--mask-json")
    selection_parser.add_argument("--no-wait", action="store_true", help="Return immediately after queueing the job.")
    selection_parser.add_argument("--timeout-ms", type=int, default=60000)

    selection_command_parser = subparsers.add_parser("selection-command", help="Run a current-selection Photoshop command.")
    selection_command_parser.add_argument(
        "action",
        choices=[
            "select_all",
            "deselect",
            "inverse",
            "modify",
            "save_selection",
            "load_selection",
        ],
    )
    selection_command_parser.add_argument("--operation", choices=["feather", "expand", "contract", "smooth", "border"])
    selection_command_parser.add_argument("--amount", type=float)
    selection_command_parser.add_argument("--channel-name", default=None)
    selection_command_parser.add_argument("--no-wait", action="store_true")
    selection_command_parser.add_argument("--timeout-ms", type=int, default=60000)

    native_selection_parser = subparsers.add_parser("native-selection", help="Run a Photoshop native selector and capture it as a reusable alpha mask.")
    native_selection_parser.add_argument(
        "action",
        choices=["select_subject", "select_sky", "color_range", "focus_area"],
    )
    native_selection_parser.add_argument("--rgb", type=parse_rgb_arg, default=None, help="Color range sampled RGB, e.g. 32,40,38.")
    native_selection_parser.add_argument(
        "--preset",
        choices=["sampled", "reds", "yellows", "greens", "cyans", "blues", "magentas", "skin_tones", "highlights", "midtones", "shadows"],
    )
    native_selection_parser.add_argument("--fuzziness", type=float, default=None)
    native_selection_parser.add_argument("--localized-color-clusters", action="store_true")
    native_selection_parser.add_argument("--in-focus-range", type=float, default=None)
    native_selection_parser.add_argument("--noise-level", type=float, default=None)
    native_selection_parser.add_argument("--feather", type=float, default=None)
    native_selection_parser.add_argument("--invert", action="store_true")
    native_selection_parser.add_argument("--no-wait", action="store_true")
    native_selection_parser.add_argument("--timeout-ms", type=int, default=60000)

    apply_parser = subparsers.add_parser("apply", help="Validate and optionally apply a ps-agent/v1 edit plan.")
    apply_source = apply_parser.add_mutually_exclusive_group(required=True)
    apply_source.add_argument("--plan-file")
    apply_source.add_argument("--plan-json")
    apply_parser.add_argument("--dry-run", action="store_true")
    apply_parser.add_argument("--no-wait", action="store_true", help="Return immediately after queueing the job.")
    apply_parser.add_argument("--timeout-ms", type=int, default=60000)

    undo_parser = subparsers.add_parser("undo", help="Undo the latest confirmed Codex Agent edit.")
    undo_parser.add_argument("--job-id", default=None)
    undo_parser.add_argument("--history-name-prefix", default="Codex Agent")
    undo_parser.add_argument("--no-wait", action="store_true")
    undo_parser.add_argument("--timeout-ms", type=int, default=60000)

    delete_group_parser = subparsers.add_parser("delete-agent-group", help="Delete a specific Codex Agent layer group by apply job id.")
    delete_group_parser.add_argument("job_id")
    delete_group_parser.add_argument("--dry-run", action="store_true")
    delete_group_parser.add_argument("--no-wait", action="store_true")
    delete_group_parser.add_argument("--timeout-ms", type=int, default=60000)

    job_parser = subparsers.add_parser("job", help="Show a job by id.")
    job_parser.add_argument("job_id")

    args = parser.parse_args()

    if args.command == "health":
        print_json(request_json(args.base_url, "GET", "/health"))
        return 0

    if args.command == "daemon":
        if args.daemon_command == "start":
            print_json(daemon_start(args.base_url))
            return 0
        if args.daemon_command == "stop":
            print_json(daemon_stop(args.base_url))
            return 0
        if args.daemon_command == "restart":
            print_json(daemon_restart(args.base_url))
            return 0
        if args.daemon_command == "status":
            print_json(daemon_status(args.base_url))
            return 0
        if args.daemon_command == "logs":
            print_json(daemon_logs(args.tail))
            return 0

    if args.command == "sam":
        if args.sam_command == "start":
            print_json(sam_start(DEFAULT_SAM_URL))
            return 0
        if args.sam_command == "stop":
            print_json(sam_stop(DEFAULT_SAM_URL))
            return 0
        if args.sam_command == "restart":
            print_json(sam_restart(DEFAULT_SAM_URL))
            return 0
        if args.sam_command == "status":
            print_json(sam_status(DEFAULT_SAM_URL))
            return 0
        if args.sam_command == "logs":
            print_json(sam_logs(args.tail))
            return 0

    if args.command == "grounding":
        if args.grounding_command == "start":
            print_json(grounding_hq_start(DEFAULT_GROUNDING_HQ_URL))
            return 0
        if args.grounding_command == "stop":
            print_json(grounding_hq_stop(DEFAULT_GROUNDING_HQ_URL))
            return 0
        if args.grounding_command == "restart":
            print_json(grounding_hq_restart(DEFAULT_GROUNDING_HQ_URL))
            return 0
        if args.grounding_command == "status":
            print_json(grounding_hq_status(DEFAULT_GROUNDING_HQ_URL))
            return 0
        if args.grounding_command == "logs":
            print_json(grounding_hq_logs(args.tail))
            return 0

    if args.command == "sam-mask":
        if not args.prompt_bbox and not args.positive_point:
            print_json(
                err(
                    "sam_prompt_missing",
                    "Provide --prompt-bbox or at least one --positive-point for SAM mask generation.",
                )
            )
            return 2
        prompt: dict[str, Any] = {"coord_space": args.coord_space}
        if args.prompt_bbox:
            prompt["bbox"] = args.prompt_bbox
        if args.positive_point:
            prompt["positive_points"] = args.positive_point
        if args.negative_point:
            prompt["negative_points"] = args.negative_point
        payload = {
            "asset_path": args.asset_path,
            "document_size": args.document_size,
            "document_bbox": args.document_bbox,
            "scale_factor": args.scale_factor,
            "prompt": prompt,
            "label": args.label,
            "threshold": args.threshold,
            "feather": args.feather,
            "invert": args.invert,
            "show_marching_ants": args.show_marching_ants,
            "timeout_ms": args.timeout_ms,
        }
        print_json(request_json(args.base_url, "POST", "/api/sam-mask", payload, timeout=max(35, args.timeout_ms / 1000 + 5)))
        return 0

    if args.command == "grounded-detect":
        print_json(
            request_json(
                args.base_url,
                "POST",
                "/api/grounding/detect-boxes",
                {
                    "asset_path": args.asset_path,
                    "document_size": args.document_size,
                    "document_bbox": args.document_bbox,
                    "scale_factor": args.scale_factor,
                    "queries": args.query,
                    "box_threshold": args.box_threshold,
                    "text_threshold": args.text_threshold,
                    "max_candidates": args.max_candidates,
                    "dedupe_iou": args.dedupe_iou,
                    "timeout_ms": args.timeout_ms,
                },
                timeout=max(35, args.timeout_ms / 1000 + 5),
            )
        )
        return 0

    if args.command == "grounded-mask":
        print_json(
            request_json(
                args.base_url,
                "POST",
                "/api/grounding/grounded-mask",
                {
                    "asset_path": args.asset_path,
                    "document_size": args.document_size,
                    "document_bbox": args.document_bbox,
                    "scale_factor": args.scale_factor,
                    "include_queries": args.include_query,
                    "exclude_queries": args.exclude_query,
                    "box_threshold": args.box_threshold,
                    "text_threshold": args.text_threshold,
                    "max_candidates": args.max_candidates,
                    "dedupe_iou": args.dedupe_iou,
                    "merge_mode": args.merge_mode,
                    "threshold": args.threshold,
                    "feather": args.feather,
                    "show_marching_ants": args.show_marching_ants,
                    "timeout_ms": args.timeout_ms,
                },
                timeout=max(35, args.timeout_ms / 1000 + 5),
            )
        )
        return 0

    if args.command == "doctor":
        print_json(doctor(args.base_url))
        return 0

    if args.command == "state":
        print_json(
            request_json(
                args.base_url,
                "POST",
                "/api/state",
                {
                    "include_layers": not args.no_layers,
                    "include_history": False,
                    "wait": not args.no_wait,
                    "timeout_ms": args.timeout_ms,
                },
            )
        )
        return 0

    if args.command == "preview":
        max_side_defaults = {
            "quick": 768,
            "standard": 1600,
            "detail": 2400,
        }
        print_json(
            request_json(
                args.base_url,
                "POST",
                "/api/export-preview",
                {
                    "format": args.format,
                    "max_side": args.max_side or max_side_defaults[args.mode],
                    "preview_mode": args.mode,
                    "quality": max(1, min(args.quality, 12)),
                    "include_document_state": True,
                    "return_before_after": False,
                    "wait": not args.no_wait,
                    "timeout_ms": args.timeout_ms,
                },
            )
        )
        return 0

    if args.command == "regions":
        regions = [parse_region(region, args.padding) for region in args.region]
        print_json(
            request_json(
                args.base_url,
                "POST",
                "/api/export-regions",
                {
                    "regions": regions,
                    "format": args.format,
                    "max_side": max(128, min(args.max_side, 2048)),
                    "upscale_small_regions": not args.no_upscale,
                    "quality": max(1, min(args.quality, 12)),
                    "return_before_after": False,
                    "wait": not args.no_wait,
                    "timeout_ms": args.timeout_ms,
                },
            )
        )
        return 0

    if args.command == "validate":
        print_json(
            request_json(
                args.base_url,
                "POST",
                "/api/validate-plan",
                {"plan": load_plan(args)},
            )
        )
        return 0

    if args.command == "face-selection":
        payload = {
            "asset_path": args.asset_path,
            "document_bbox": args.document_bbox,
            "scale_factor": args.scale_factor,
            "parts": args.parts,
            "face_index": args.face_index,
            "max_faces": args.max_faces,
            "expand_px": args.expand_px,
            "smooth": args.smooth,
            "feather": args.feather,
        }
        print_json(request_json(args.base_url, "POST", "/api/face-selection", payload))
        return 0

    if args.command == "selection":
        print_json(
            request_json(
                args.base_url,
                "POST",
                "/api/make-selection",
                {
                    "selection_mask": load_selection_mask(args),
                    "wait": not args.no_wait,
                    "timeout_ms": args.timeout_ms,
                },
            )
        )
        return 0

    if args.command == "selection-command":
        payload: dict[str, Any] = {
            "action": args.action,
            "wait": not args.no_wait,
            "timeout_ms": args.timeout_ms,
        }
        optional_fields = {
            "operation": args.operation,
            "amount": args.amount,
            "channel_name": args.channel_name,
        }
        payload.update({key: value for key, value in optional_fields.items() if value is not None})
        print_json(request_json(args.base_url, "POST", "/api/selection-command", payload))
        return 0

    if args.command == "native-selection":
        payload: dict[str, Any] = {
            "action": args.action,
            "wait": not args.no_wait,
            "timeout_ms": args.timeout_ms,
        }
        optional_fields = {
            "color": args.rgb,
            "preset": args.preset,
            "fuzziness": args.fuzziness,
            "localized_color_clusters": args.localized_color_clusters if args.localized_color_clusters else None,
            "in_focus_range": args.in_focus_range,
            "noise_level": args.noise_level,
            "feather": args.feather,
            "invert": args.invert if args.invert else None,
        }
        payload.update({key: value for key, value in optional_fields.items() if value is not None})
        route_map = {
            "select_subject": "/api/select-subject",
            "select_sky": "/api/select-sky",
            "color_range": "/api/select-color-range",
            "focus_area": "/api/select-focus-area",
        }
        print_json(request_json(args.base_url, "POST", route_map[args.action], payload))
        return 0

    if args.command == "apply":
        print_json(
            request_json(
                args.base_url,
                "POST",
                "/api/apply-plan",
                {
                    "plan": load_plan(args),
                    "dry_run": args.dry_run,
                    "wait": not args.no_wait,
                    "timeout_ms": args.timeout_ms,
                },
            )
        )
        return 0

    if args.command == "undo":
        print_json(
            request_json(
                args.base_url,
                "POST",
                "/api/undo-last",
                {
                    "job_id": args.job_id,
                    "history_name_prefix": args.history_name_prefix,
                    "wait": not args.no_wait,
                    "timeout_ms": args.timeout_ms,
                },
            )
        )
        return 0

    if args.command == "delete-agent-group":
        print_json(
            request_json(
                args.base_url,
                "POST",
                "/api/delete-agent-group",
                {
                    "job_id": args.job_id,
                    "dry_run": args.dry_run,
                    "wait": not args.no_wait,
                    "timeout_ms": args.timeout_ms,
                },
            )
        )
        return 0

    if args.command == "job":
        print_json(request_json(args.base_url, "GET", f"/api/jobs/{args.job_id}"))
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
