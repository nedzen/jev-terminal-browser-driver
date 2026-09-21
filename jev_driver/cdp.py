"""Minimal websocket CDP client matching browser_harness.helpers.cdp's contract."""

from __future__ import annotations

import json
import os
import subprocess
import threading
import urllib.request
from pathlib import Path

import websocket

TB = os.environ.get("TERMINAL_BROWSER", str(Path.home() / ".local" / "bin" / "terminal-browser"))

_lock = threading.Lock()
_ws = None
_next_id = 0
_discover_enabled = False


def list_browsers() -> dict:
    out = subprocess.check_output([TB, "ls", "--all", "--json"], text=True, timeout=15)
    return json.loads(out)


def _tb_cdp_port() -> int:
    data = list_browsers()
    browsers = data.get("browsers") or []
    if not browsers:
        raise RuntimeError("No terminal-browser instance. Open one before driving.")
    ports = {b["cdpPort"] for b in browsers if b.get("cdpPort")}
    if not ports:
        raise RuntimeError("terminal-browser ls --json had no cdpPort")
    return next(iter(ports))


def http_origin() -> str:
    from .discover import LAST

    if LAST and LAST.http_origin:
        return LAST.http_origin
    return f"http://127.0.0.1:{_tb_cdp_port()}"


def cdp_port() -> int:
    from urllib.parse import urlparse

    parsed = urlparse(http_origin())
    if parsed.port:
        return parsed.port
    return _tb_cdp_port()


def browser_websocket_url(port: int | None = None) -> str:
    port = port if port is not None else cdp_port()
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=5) as resp:
        info = json.loads(resp.read())
    url = info.get("webSocketDebuggerUrl")
    if not url:
        raise RuntimeError("CDP /json/version had no webSocketDebuggerUrl")
    return url


def _send_locked(method, session_id=None, **params) -> dict:
    global _next_id
    if method.startswith("Target."):
        session_id = None
    _next_id += 1
    message_id = _next_id
    payload = {"id": message_id, "method": method, "params": params}
    if session_id:
        payload["sessionId"] = session_id
    _ws.send(json.dumps(payload))
    return _recv_until(_ws, message_id)


def connect(url: str | None = None) -> None:
    global _ws, _discover_enabled
    if url is None and _ws is None:
        from .discover import LAST, discover

        url = (LAST or discover()).ws_url
    with _lock:
        if _ws is None:
            _ws = websocket.create_connection(url, timeout=30, suppress_origin=True)
        if not _discover_enabled:
            reply = _send_locked("Target.setDiscoverTargets", discover=True)
            if reply.get("error"):
                raise RuntimeError(f"CDP Target.setDiscoverTargets failed: {reply['error']}")
            _discover_enabled = True


def disconnect() -> None:
    global _ws, _next_id, _discover_enabled
    with _lock:
        if _ws is not None:
            try:
                _ws.close()
            except Exception:
                pass
            _ws = None
        _next_id = 0
        _discover_enabled = False


def _recv_until(ws, message_id: int) -> dict:
    while True:
        raw = ws.recv()
        msg = json.loads(raw)
        if msg.get("id") == message_id:
            return msg


def cdp(method, session_id=None, **params):
    """Raw CDP. Returns the unwrapped result dict. Target.* never carries sessionId."""
    if _ws is None:
        connect()
    with _lock:
        reply = _send_locked(method, session_id=session_id, **params)
    if reply.get("error"):
        err = reply["error"]
        message = err.get("message", err) if isinstance(err, dict) else err
        raise RuntimeError(f"CDP {method} failed: {message}") from None
    return reply.get("result", {})
