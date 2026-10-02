#!/usr/bin/env python3
"""MCP stdio server exposing jev_drive / jev_read to any MCP-capable agent.

Stdlib only. The Hermes adapter under plugin/ is untouched: this imports its
tool schemas and subprocess handlers (never jev_driver directly) and serves
the same CLI contract over MCP. Core stays in scripts/drive.py + scripts/read.py.

Run:
    uv run --directory <repo> python scripts/mcp.py
    # debug overlay on:
    JEV_DEBUG=1 uv run --directory <repo> python scripts/mcp.py

Client config (Claude Code / OpenCode / Crush via MCP):
    {"command": "uv", "args": ["run", "--directory", "<repo>",
     "python", "scripts/mcp.py"]}

Like the Hermes plugin, a client-supplied `debug` flag is ignored: only the
JEV_DEBUG env var controls the overlay.
"""

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from plugin import (  # noqa: E402
    DESCRIPTION,
    PARAMETERS,
    READ_DESCRIPTION,
    READ_PARAMETERS,
    apply_debug_setting,  # noqa: E402
)
from plugin import handler as h  # noqa: E402

SERVER_VERSION = "0.1.0"  # synced with plugin.yaml version
SUPPORTED_PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05", "2024-10-07")

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


def debug_default() -> bool:
    return os.environ.get("JEV_DEBUG", "").strip().lower() in {"1", "true", "yes"}


def _tool_list() -> list[dict]:
    return [
        {"name": "jev_drive", "description": DESCRIPTION, "inputSchema": PARAMETERS},
        {"name": "jev_read", "description": READ_DESCRIPTION, "inputSchema": READ_PARAMETERS},
    ]


def _call_tool(name: str, arguments: dict) -> dict:
    if name == "jev_drive":
        payload = apply_debug_setting(arguments, debug_default())
        result = h.run_drive(payload)
        # "blocked" is a normal outcome (page state), not a protocol error.
        # Only "error" marks the call failed for MCP clients.
        return {
            "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
            "isError": result.get("status") == "error",
        }
    if name == "jev_read":
        payload = apply_debug_setting(arguments, debug_default())
        result = h.run_read(payload)
        return {
            "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
            "isError": not result.get("success", False),
        }
    raise ValueError(f"unknown tool: {name}")


def dispatch(msg: dict):
    """Handle one JSON-RPC message. Returns a response dict, or None for
    notifications (no id) which must not be answered."""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        return {"jsonrpc": "2.0", "id": None, "error": {"code": INVALID_REQUEST, "message": "invalid request"}}
    method = msg.get("method")
    msg_id = msg.get("id")
    params = msg.get("params") or {}
    if not isinstance(method, str):
        return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": INVALID_REQUEST, "message": "missing method"}}
    if msg_id is None:
        # Notification. MCP only sends notifications/initialized (and
        # notifications/cancelled); neither needs a reply.
        return None
    try:
        if method == "initialize":
            requested = params.get("protocolVersion") if isinstance(params, dict) else None
            version = requested if requested in SUPPORTED_PROTOCOLS else SUPPORTED_PROTOCOLS[0]
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "protocolVersion": version,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "jev-driver", "version": SERVER_VERSION},
                },
            }
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": _tool_list()}}
        if method == "tools/call":
            if not isinstance(params, dict) or not isinstance(params.get("name"), str):
                return {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "error": {"code": INVALID_PARAMS, "message": "tools/call needs {name, arguments}"},
                }
            args = params.get("arguments") or {}
            if not isinstance(args, dict):
                return {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "error": {"code": INVALID_PARAMS, "message": "arguments must be an object"},
                }
            try:
                result = _call_tool(params["name"], args)
            except ValueError as exc:
                return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": INVALID_PARAMS, "message": str(exc)}}
            return {"jsonrpc": "2.0", "id": msg_id, "result": result}
        if method == "ping":
            return {"jsonrpc": "2.0", "id": msg_id, "result": {}}
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {"code": METHOD_NOT_FOUND, "message": f"unknown method: {method}"},
        }
    except Exception as exc:  # never let one call kill the server
        h.log_handler_event("mcp", f"{method} failed: {exc}")
        return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": INTERNAL_ERROR, "message": "internal error"}}


def serve() -> int:
    stdin = sys.stdin
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            err = {"jsonrpc": "2.0", "id": None, "error": {"code": PARSE_ERROR, "message": "parse error"}}
            sys.stdout.write(json.dumps(err))
            sys.stdout.write("\n")
            sys.stdout.flush()
            continue
        response = dispatch(msg)
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(serve())
