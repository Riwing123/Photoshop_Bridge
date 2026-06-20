from __future__ import annotations

import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = ROOT / "tool_registry.json"


def load_registry(path: Path = REGISTRY_PATH) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def list_mcp_tools(registry: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    registry = registry or load_registry()
    tools = []
    for tool in registry["tools"]:
        tools.append(
            {
                "name": tool["name"],
                "title": tool.get("title", tool["name"]),
                "description": tool["description"],
                "inputSchema": tool["inputSchema"],
                "outputSchema": tool.get("outputSchema"),
            }
        )
    return tools


def get_tool(name: str, registry: dict[str, Any] | None = None) -> dict[str, Any]:
    registry = registry or load_registry()
    for tool in registry["tools"]:
        if tool["name"] == name:
            return tool
    raise KeyError(f"Unknown Photoshop agent tool: {name}")


def registry_summary(registry: dict[str, Any] | None = None) -> dict[str, Any]:
    registry = registry or load_registry()
    return {
        "protocol_version": registry["protocol_version"],
        "backend": registry["backend"],
        "default_timeout_ms": registry["default_timeout_ms"],
        "tool_count": len(registry["tools"]),
        "tools": [
            {
                "name": tool["name"],
                "title": tool.get("title", tool["name"]),
                "transport": tool["transport"],
                "job_type": tool.get("job_type"),
                "modifies_document": tool["safety"]["modifies_document"],
                "returns_images": tool["safety"]["returns_images"],
            }
            for tool in registry["tools"]
        ],
    }
