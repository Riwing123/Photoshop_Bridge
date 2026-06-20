from __future__ import annotations

import json
import os
import sys
import traceback
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

from ps_backend.tool_registry import get_tool, list_mcp_tools, load_registry, registry_summary


REGISTRY = load_registry()
BACKEND_URL = os.environ.get(
    "PS_AGENT_BACKEND_URL",
    REGISTRY["backend"]["default_base_url"],
).rstrip("/")


def respond(message_id: Any, result: Any = None, error: dict[str, Any] | None = None) -> None:
    payload: dict[str, Any] = {"jsonrpc": "2.0", "id": message_id}
    if error is not None:
        payload["error"] = error
    else:
        payload["result"] = result
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def notify(method: str, params: dict[str, Any] | None = None) -> None:
    payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        payload["params"] = params
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def tool_result(data: Any, is_error: bool = False) -> dict[str, Any]:
    text = json.dumps(data, ensure_ascii=False, indent=2)
    return {
        "content": [{"type": "text", "text": text}],
        "structuredContent": data if isinstance(data, dict) else {"value": data},
        "isError": is_error,
    }


def http_call(tool: dict[str, Any], arguments: dict[str, Any]) -> dict[str, Any]:
    endpoint = tool["endpoint"]
    path = endpoint["path"]
    if "{job_id}" in path:
        job_id = arguments.get("job_id")
        if not job_id:
            return tool_result(
                {"error": {"code": "missing_job_id", "message": "job_id is required"}},
                is_error=True,
            )
        path = path.replace("{job_id}", urllib.parse.quote(str(job_id), safe=""))

    url = BACKEND_URL + path
    method = endpoint["method"]
    body = None
    headers = {"Accept": "application/json"}

    if method == "GET":
        query_arguments = {
            key: value
            for key, value in arguments.items()
            if key != "job_id" and value is not None
        }
        if query_arguments:
            url = url + "?" + urllib.parse.urlencode(query_arguments)
    else:
        body = json.dumps(arguments, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read().decode("utf-8")
            data = json.loads(raw) if raw else {}
            return tool_result(data)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = {"error": {"code": "http_error", "message": raw or str(exc)}}
        return tool_result(data, is_error=True)
    except Exception as exc:
        return tool_result(
            {
                "error": {
                    "code": "backend_unreachable",
                    "message": f"Could not reach Photoshop agent backend at {BACKEND_URL}: {exc}",
                    "details": {
                        "start_command": "python backend/cli.py daemon start",
                        "status_command": "python backend/cli.py daemon status",
                        "diagnostics_command": "python backend/cli.py doctor",
                        "log_path": str(CURRENT_DIR / "runtime" / "ps-agent-backend.log"),
                    },
                }
            },
            is_error=True,
        )


def call_tool(name: str, arguments: dict[str, Any] | None) -> dict[str, Any]:
    arguments = arguments or {}
    if name == "ps_list_registered_tools":
        return tool_result(registry_summary(REGISTRY))

    try:
        tool = get_tool(name, REGISTRY)
    except KeyError as exc:
        return tool_result({"error": {"code": "unknown_tool", "message": str(exc)}}, True)

    if tool["transport"] == "local":
        return tool_result({"error": {"code": "unsupported_local_tool", "message": name}}, True)

    return http_call(tool, arguments)


def handle(message: dict[str, Any]) -> None:
    method = message.get("method")
    message_id = message.get("id")
    params = message.get("params") or {}

    if method == "initialize":
        respond(
            message_id,
            {
                "protocolVersion": params.get("protocolVersion", "2025-06-18"),
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "ps-uxp-agent", "version": "0.7.0"},
            },
        )
        return

    if method == "notifications/initialized":
        return

    if method == "tools/list":
        respond(message_id, {"tools": list_mcp_tools(REGISTRY)})
        return

    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        respond(message_id, call_tool(name, arguments))
        return

    respond(
        message_id,
        error={"code": -32601, "message": f"Method not found: {method}"},
    )


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            handle(json.loads(line))
        except Exception as exc:
            respond(
                None,
                error={
                    "code": -32603,
                    "message": str(exc),
                    "data": traceback.format_exc(),
                },
            )


if __name__ == "__main__":
    main()
