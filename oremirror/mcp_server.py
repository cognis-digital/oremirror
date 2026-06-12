"""oremirror MCP server.

Exposes the safe, read-only slice of oremirror — transfer planning and OCI
layout verification — as an MCP capability over stdio using newline-delimited
JSON-RPC 2.0. Standard library only, so it runs anywhere Python does and can be
wired into Cognis.Studio, Claude Desktop, or Cursor as a local MCP server:

    {"command": "python", "args": ["-m", "oremirror", "mcp"]}

Implemented methods:
  * initialize  — handshake, advertises the tools capability
  * tools/list  — describes the `plan` and `verify` tools
  * tools/call  — runs a tool and returns its report as JSON text

`plan` operates over offline fixtures by default (or a real registry when no
fixtures are given and egress exists). `verify` recomputes digests of a local
OCI layout. Neither mutates a registry, so the MCP surface is non-destructive.
"""

from __future__ import annotations

import json
import sys
from typing import Any, Dict, Optional

from . import TOOL_NAME, TOOL_VERSION
from .core import (
    OreError,
    build_plan,
    parse_image_list,
    verify_layout,
)

PROTOCOL_VERSION = "2024-11-05"

_TOOLS = [
    {
        "name": "plan",
        "description": "Resolve a list of OCI image references to their "
                       "manifests and compute a transfer plan (layers, distinct "
                       "blobs, total size). Accepts offline fixtures for "
                       "disconnected planning.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "images": {
                    "type": "string",
                    "description": "Newline-separated image references "
                                   "(or simple YAML 'images:' block).",
                },
                "fixtures": {
                    "type": "object",
                    "description": "Optional offline manifest fixtures map.",
                },
            },
            "required": ["images"],
            "additionalProperties": False,
        },
    },
    {
        "name": "verify",
        "description": "Recompute every digest in a local OCI image-layout "
                       "directory and confirm it is intact and untampered.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "layout": {
                    "type": "string",
                    "description": "Path to an OCI image-layout directory.",
                }
            },
            "required": ["layout"],
            "additionalProperties": False,
        },
    },
]


def _result(req_id: Any, result: Dict[str, Any]) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _error(req_id: Any, code: int, message: str) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def _call_tool(name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    if name == "plan":
        images = arguments.get("images")
        if not isinstance(images, str) or not images.strip():
            raise ValueError("`images` (string) is required")
        refs = parse_image_list(images)
        fixtures = arguments.get("fixtures")
        if fixtures is not None and not isinstance(fixtures, dict):
            raise ValueError("`fixtures` must be an object")
        payload = build_plan(refs, fixtures=fixtures).to_dict()
        is_error = bool(payload.get("failed"))
    elif name == "verify":
        layout = arguments.get("layout")
        if not isinstance(layout, str) or not layout:
            raise ValueError("`layout` (string path) is required")
        payload = verify_layout(layout).to_dict()
        is_error = not payload.get("passed")
    else:
        raise ValueError(f"unknown tool: {name}")

    return {
        "content": [{"type": "text", "text": json.dumps(payload, indent=2)}],
        "isError": is_error,
    }


def handle_request(req: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Dispatch a single JSON-RPC request. Returns None for notifications."""
    method = req.get("method")
    req_id = req.get("id")
    params = req.get("params") or {}
    is_notification = "id" not in req

    if method == "initialize":
        res = _result(req_id, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": TOOL_NAME, "version": TOOL_VERSION},
        })
        return None if is_notification else res

    if method in ("notifications/initialized", "initialized"):
        return None

    if method == "ping":
        return None if is_notification else _result(req_id, {})

    if method == "tools/list":
        return _result(req_id, {"tools": _TOOLS})

    if method == "tools/call":
        name = params.get("name", "")
        arguments = params.get("arguments") or {}
        try:
            return _result(req_id, _call_tool(name, arguments))
        except (ValueError, OSError, OreError) as exc:
            return _error(req_id, -32602, str(exc))
        except Exception as exc:  # pragma: no cover - defensive
            return _error(req_id, -32603, f"internal error: {exc}")

    if is_notification:
        return None
    return _error(req_id, -32601, f"method not found: {method}")


def run_mcp_server(stdin=None, stdout=None) -> None:
    """Read newline-delimited JSON-RPC from stdin, write responses to stdout."""
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            stdout.write(json.dumps(_error(None, -32700, "parse error")) + "\n")
            stdout.flush()
            continue
        response = handle_request(req)
        if response is not None:
            stdout.write(json.dumps(response) + "\n")
            stdout.flush()


if __name__ == "__main__":
    run_mcp_server()
