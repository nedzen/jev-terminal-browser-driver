#!/usr/bin/env python3
"""MCP stdio server exposing drive / read / status to any MCP-capable agent.

Server name `wwwdrive`; the tools are bare names under that namespace, so
clients that namespace MCP tools see `mcp__wwwdrive__drive` and friends.

Stdlib only. The Hermes adapter under plugin/ is untouched: this imports its
tool schemas and subprocess handlers (never jev_driver's browser loop) and
serves the same CLI contract over MCP. The one jev_driver import is the
stdlib-only preflight leaf behind status. Core stays in scripts/drive.py +
scripts/read.py.

Run:
    uv run --directory <repo> python scripts/mcp.py
    # debug overlay on:
    WWWDRIVE_DEBUG=1 uv run --directory <repo> python scripts/mcp.py

Client config (Claude Code / OpenCode / Crush / Hermes / anything speaking MCP):
    {"command": "uv", "args": ["run", "--directory", "<repo>",
     "python", "scripts/mcp.py"]}

Like the Hermes plugin, a client-supplied `debug` flag is ignored: only the
WWWDRIVE_DEBUG env var controls the overlay (JEV_DEBUG still works as a
fallback). The ranked operations/targets insight trace is stricter still: it
is only in the result when WWWDRIVE_DEBUG explicitly turns debug on, since an
unset var is a default rather than a request to spend the caller's context.

Serial by design: one tools/call runs at a time; while a 300-900s drive is
in flight the server cannot read stdin, so notifications/cancelled is only
seen after the call finishes. Clients that need out-of-band cancellation
should SIGTERM the drive subprocess (started in its own session) or use the
per-call timeout_s. A client SIGKILL of this server orphans the running
drive subprocess.
"""

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from jev_driver.preflight import preflight  # noqa: E402  (stdlib-only leaf: no browser, no network)
from plugin import (  # noqa: E402
    DESCRIPTION,
    PARAMETERS,
    READ_DESCRIPTION,
    READ_PARAMETERS,
    STATUS_DESCRIPTION,
    STATUS_PARAMETERS,
    apply_debug_setting,  # noqa: E402
)
from plugin import handler as h  # noqa: E402

SERVER_NAME = "wwwdrive"
SERVER_VERSION = "1.1.0"  # synced with plugin.yaml version
SUPPORTED_PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05", "2024-10-07")

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


def debug_default() -> bool:
    """Overlay on unless explicitly disabled: WWWDRIVE_DEBUG=0/false/no opts out.

    JEV_DEBUG is still read, so pre-1.0 client configs keep the same behavior.
    """
    raw = os.environ.get("WWWDRIVE_DEBUG", "").strip() or os.environ.get("JEV_DEBUG", "").strip()
    return raw.lower() not in {"0", "false", "no"}


def insights_default() -> bool:
    """Whether the ranked operations/targets trace rides along in the result.

    Explicit opt-in only, and deliberately stricter than :func:`debug_default`:
    the overlay is a panel a human asked to watch, while the trace is payload
    the calling agent pays for on every drive. An unset env var is a default,
    not a request, so the trace waits for WWWDRIVE_DEBUG=1 (or JEV_DEBUG=1).
    Turning it off is unchanged -- WWWDRIVE_DEBUG=0 still drops both.
    """
    raw = os.environ.get("WWWDRIVE_DEBUG", "").strip() or os.environ.get("JEV_DEBUG", "").strip()
    return raw.lower() in {"1", "true", "yes", "on"}


def _tool_list() -> list[dict]:
    return [
        {"name": "drive", "description": DESCRIPTION, "inputSchema": PARAMETERS},
        {"name": "read", "description": READ_DESCRIPTION, "inputSchema": READ_PARAMETERS},
        {"name": "status", "description": STATUS_DESCRIPTION, "inputSchema": STATUS_PARAMETERS},
    ]


def _call_tool(name: str, arguments: dict) -> dict:
    if name == "drive":
        # Overlay and insight trace are separate switches: the pane keeps its
        # on-by-default overlay, the result only carries the trace when debug
        # was explicitly turned on.
        payload = apply_debug_setting({**arguments, "insights": insights_default()}, debug_default())
        result = h.run_drive(payload)
        # "blocked" is a normal outcome (page state), not a protocol error.
        # Only "error" marks the call failed for MCP clients.
        return {
            "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
            "isError": result.get("status") == "error",
        }
    if name == "read":
        payload = apply_debug_setting(arguments, debug_default())
        result = h.run_read(payload)
        return {
            "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
            "isError": not result.get("success", False),
        }
    if name == "status":
        # A missing dependency is the answer, not a failure: no browser, no model call.
        return {
            "content": [{"type": "text", "text": json.dumps(preflight(), ensure_ascii=False)}],
            "isError": False,
        }
    raise ValueError(f"unknown tool: {name}")  # unreachable: dispatch checks first


def _known_tool(name: str) -> bool:
    """The tool surface lives in _tool_list(); ask it, never a second list."""
    return name in {tool["name"] for tool in _tool_list()}


def _log_fault(method, note: str) -> None:
    """Record a server fault in the run log and swallow any logging failure.

    Only the exception class reaches the log, never the message: a message can
    carry a URL with a key in it, and that log is append-only.
    """
    try:
        h.log_handler_event("mcp", f"{method}: {note}")
    except Exception:
        pass


def _internal_error(msg_id, method, exc: BaseException) -> dict:
    """JSON-RPC internal error for anything the server did not expect."""
    _log_fault(method, f"unhandled {type(exc).__name__}")
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": INTERNAL_ERROR, "message": "internal error"}}


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
                    "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
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
            # Unknown tool is the client's mistake: reject it here, before a
            # handler runs, and do not log it as a server fault.
            name = params["name"]
            if not _known_tool(name):
                message = f"unknown tool: {name}"
                return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": INVALID_PARAMS, "message": message}}
            # Anything the handler raises is ours, not the client's: report it
            # as an internal error and let the stdio loop keep serving.
            try:
                result = _call_tool(name, args)
            except Exception as exc:
                return _internal_error(msg_id, method, exc)
            return {"jsonrpc": "2.0", "id": msg_id, "result": result}
        if method == "ping":
            return {"jsonrpc": "2.0", "id": msg_id, "result": {}}
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {"code": METHOD_NOT_FOUND, "message": f"unknown method: {method}"},
        }
    except Exception as exc:  # never let one call kill the server
        return _internal_error(msg_id, method, exc)


def _emit(out, payload: dict) -> bool:
    """Write one response line. False means it could not be serialized."""
    try:
        line = json.dumps(payload, ensure_ascii=False)
    except (TypeError, ValueError):
        return False
    out.write(line + "\n")
    out.flush()
    return True


def serve(stdin=None, stdout=None) -> int:
    """One line in, one JSON-RPC response out. Malformed input, unknown
    methods and handler crashes each answer with an error object; the loop
    keeps serving the next request."""
    stdin = sys.stdin if stdin is None else stdin
    out = sys.stdout if stdout is None else stdout
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            _emit(out, {"jsonrpc": "2.0", "id": None, "error": {"code": PARSE_ERROR, "message": "parse error"}})
            continue
        msg_id = msg.get("id") if isinstance(msg, dict) else None
        method = msg.get("method") if isinstance(msg, dict) else "?"
        try:
            response = dispatch(msg)
        except Exception as exc:  # dispatch traps its own errors; belt and braces
            response = _internal_error(msg_id, method, exc)
        if response is None:
            continue  # notification: no reply
        if not _emit(out, response):
            # A result the server cannot serialize is our fault, not the
            # client's: log it, answer with an error, keep the loop alive.
            _log_fault(method, "unserializable response")
            _emit(
                out,
                {"jsonrpc": "2.0", "id": msg_id, "error": {"code": INTERNAL_ERROR, "message": "internal error"}},
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(serve())
