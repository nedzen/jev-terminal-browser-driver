"""CDP discovery ladder: explicit → terminal-browser → agent-browser daemon → loopback → headless launch."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from .cdp import browser_websocket_url, list_browsers

SESSION = "jev-driver"
BUNDLED_AGENT_BROWSER = (
    Path.home() / ".local" / "share" / "terminal-browser" / "app" / "agent-browser" / "bin" / "agent-browser"
)
LOOPBACK_PORTS = range(9222, 9331)


@dataclass
class Discovery:
    ws_url: str
    http_origin: str
    source: str
    auto_launched: bool = False
    session: str | None = None


LAST: Discovery | None = None


def ws_to_http_origin(ws_url: str) -> str:
    parsed = urlparse(ws_url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port
    scheme = "https" if parsed.scheme in {"wss", "https"} else "http"
    if port:
        return f"{scheme}://{host}:{port}"
    return f"{scheme}://{host}"


def json_version_ws(origin: str, timeout: float = 2.0) -> str | None:
    url = origin.rstrip("/") + "/json/version"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            info = json.loads(resp.read())
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None
    ws = (info.get("webSocketDebuggerUrl") or "").strip()
    return ws or None


def normalize_cdp_url(raw: str) -> str:
    value = (raw or "").strip()
    if not value:
        raise RuntimeError("Empty CDP URL")
    lowered = value.lower()
    if "/devtools/browser/" in lowered:
        return value
    if lowered.startswith(("ws://", "wss://")):
        rest = value.split("://", 1)[1]
        if "/" not in rest and rest.rsplit(":", 1)[-1].isdigit():
            origin = ("http://" if lowered.startswith("ws://") else "https://") + rest
            ws = json_version_ws(origin, timeout=5)
            if ws:
                return ws
        return value
    origin = value if lowered.endswith("/json/version") else value.rstrip("/")
    if origin.endswith("/json/version"):
        origin = origin[: -len("/json/version")]
    if not origin.startswith(("http://", "https://")):
        origin = "http://" + origin
    ws = json_version_ws(origin, timeout=5)
    if not ws:
        raise RuntimeError(f"No webSocketDebuggerUrl at {origin}/json/version")
    return ws


def agent_browser_argv(binary: str) -> list[str]:
    if binary == "npx agent-browser" or binary.startswith("npx "):
        return ["npx", "--yes", "agent-browser"]
    return [binary]


def resolve_agent_browser() -> str | None:
    found = shutil.which("agent-browser")
    if found:
        return found
    if BUNDLED_AGENT_BROWSER.is_file() and os.access(BUNDLED_AGENT_BROWSER, os.X_OK):
        return str(BUNDLED_AGENT_BROWSER)
    if shutil.which("npx"):
        return "npx agent-browser"
    return None


def _run_agent_browser(binary: str, extra: list[str], timeout: float = 15) -> str:
    return subprocess.check_output(
        agent_browser_argv(binary) + extra,
        text=True,
        timeout=timeout,
        stderr=subprocess.DEVNULL,
    )


def agent_browser_cdp_url(binary: str, session: str = SESSION) -> str | None:
    try:
        out = _run_agent_browser(binary, ["--session", session, "get", "cdp-url"], timeout=8).strip()
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if not out:
        return None
    if out.startswith("{"):
        try:
            payload = json.loads(out)
            data = payload.get("data") if isinstance(payload, dict) else None
            if isinstance(data, str) and data.startswith("ws"):
                return data
            if isinstance(payload, dict):
                for key in ("cdpUrl", "cdp_url", "url"):
                    val = payload.get(key)
                    if isinstance(val, str) and val.startswith("ws"):
                        return val
        except json.JSONDecodeError:
            return None
        return None
    if out.startswith("ws"):
        return out.split()[0]
    return None


def _terminal_browser_discovery() -> Discovery | None:
    try:
        data = list_browsers()
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
        return None
    browsers = data.get("browsers") or []
    ports = {b["cdpPort"] for b in browsers if b.get("cdpPort")}
    if not ports:
        return None
    port = next(iter(ports))
    try:
        ws = browser_websocket_url(port)
    except (RuntimeError, urllib.error.URLError, TimeoutError, OSError):
        return None
    return Discovery(ws_url=ws, http_origin=f"http://127.0.0.1:{port}", source="terminal-browser")


def _loopback_discovery() -> Discovery | None:
    for port in LOOPBACK_PORTS:
        ws = json_version_ws(f"http://127.0.0.1:{port}", timeout=0.4)
        if ws:
            return Discovery(ws_url=ws, http_origin=f"http://127.0.0.1:{port}", source="loopback")
    return None


def _launch_headless(binary: str, url: str) -> None:
    subprocess.run(
        agent_browser_argv(binary) + ["--session", SESSION, "open", url],
        check=False,
        timeout=60,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def discover(*, explicit: str | None = None, launch_url: str = "about:blank", auto_provision: bool = True) -> Discovery:
    """Walk the HQ ladder. Stores the result on LAST for cdp_port / tab-open."""
    global LAST
    raw = (explicit or os.environ.get("JEV_CDP_URL") or os.environ.get("BROWSER_CDP_URL") or "").strip()
    if raw:
        ws = normalize_cdp_url(raw)
        LAST = Discovery(ws_url=ws, http_origin=ws_to_http_origin(ws), source="explicit")
        return LAST
    found = _terminal_browser_discovery()
    if found:
        LAST = found
        return LAST
    binary = resolve_agent_browser()
    if binary:
        ws = agent_browser_cdp_url(binary)
        if ws:
            LAST = Discovery(
                ws_url=ws, http_origin=ws_to_http_origin(ws), source="agent-browser-daemon", session=SESSION
            )
            return LAST
    found = _loopback_discovery()
    if found:
        LAST = found
        return LAST
    if auto_provision and binary:
        _launch_headless(binary, launch_url)
        ws = agent_browser_cdp_url(binary)
        if ws:
            LAST = Discovery(
                ws_url=ws,
                http_origin=ws_to_http_origin(ws),
                source="headless-launched",
                auto_launched=True,
                session=SESSION,
            )
            return LAST
    raise RuntimeError(
        "No CDP browser found. Open terminal-browser, set JEV_CDP_URL, "
        "or install agent-browser for headless auto-provision."
    )
