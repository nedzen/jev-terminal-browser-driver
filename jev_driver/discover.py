"""CDP discovery, TUI-only: explicit → terminal-browser → visible provision.

No headless or loopback scan. Provision via cmux socket when available, else
``terminal-browser open --split right`` with herdr env scrubbed so the pane
does not nest inside the agent's herdr surface.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from . import instances as _instances
from .cdp import TB, browser_websocket_url, list_browsers

POST_LAUNCH_TRIES = 5
POST_LAUNCH_DELAY_S = 5
PROVISION_TIMEOUT_S = 60

WATCH_INSTALL = (
    "watch requested but terminal-browser is not installed. Install it "
    "(see https://terminal-browser.dev) or retry without watch. "
    "terminal-browser needs an existing kitty-graphics terminal "
    "(kitty, ghostty, wezterm, tmux, vscode, cmux, supacode, herdr — not iTerm2/Terminal.app). "
    "Installing TB does not install a terminal."
)
WATCH_TERMINAL_NOTE = (
    "terminal-browser needs an existing kitty-graphics terminal "
    "(kitty, ghostty, wezterm, tmux, vscode, cmux, supacode, herdr — not iTerm2/Terminal.app)."
)
CMUX_UNAVAILABLE = (
    "A cmux control socket was found but the cmux CLI is not on PATH. "
    "Install cmux or run where terminal-browser's own terminal detection works."
)


class WatchUnavailable(RuntimeError):
    """watch=true could not open a visible terminal-browser pane."""


@dataclass
class Discovery:
    ws_url: str
    http_origin: str
    source: str
    auto_launched: bool = False
    visibility: str = "terminal-browser-pane"


LAST: Discovery | None = None

# Per-cmux-session instance reuse (keyed by socket digest).
LEDGER_PATH = Path.home() / ".cache" / "wwwdrive" / "cmux-instance.json"


def _ledger_key() -> str:
    """Digest of CMUX_SOCKET_PATH for this session's ledger records."""
    path = (os.environ.get("CMUX_SOCKET_PATH") or "").strip()
    return hashlib.sha256(path.encode("utf-8")).hexdigest()[:16] if path else ""


def _read_ledger() -> dict | None:
    """Recorded instance for this cmux session, or None. Never raises."""
    if not LEDGER_PATH.is_file():
        return None
    try:
        data = json.loads(LEDGER_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    required = ("ledger", "key", "port", "pid", "workspace")
    if any(field not in data for field in required):
        return None
    if not isinstance(data["port"], int) or not isinstance(data["pid"], int):
        return None
    if data["ledger"] != _ledger_key():
        return None
    return data


def _write_ledger(record: dict) -> None:
    """Record the provisioned instance. Never raises."""
    try:
        LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
        LEDGER_PATH.write_text(json.dumps({**_ledger_stamp(), **record}))
    except OSError:
        return


def _ledger_stamp() -> dict:
    context = cmux_context() or {}
    return {
        "ledger": _ledger_key(),
        "workspace": context.get("workspace") or "",
        "socket_path": context.get("socket_path") or "",
    }


def _instance_in_record(record: dict) -> Discovery | None:
    """Discovery if key/port/pid still match a live instance."""
    try:
        data = list_browsers()
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
        return None
    for browser in data.get("browsers") or []:
        if browser.get("key") != record["key"]:
            continue
        if browser.get("cdpPort") != record["port"] or browser.get("pid") != record["pid"]:
            continue
        try:
            ws = browser_websocket_url(record["port"])
        except (RuntimeError, urllib.error.URLError, TimeoutError, OSError):
            return None
        return Discovery(
            ws_url=ws,
            http_origin=f"http://127.0.0.1:{record['port']}",
            source="terminal-browser",
            auto_launched=False,
            visibility="terminal-browser-pane",
        )
    return None


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


def resolve_terminal_browser() -> str | None:
    found = shutil.which("terminal-browser")
    if found:
        return found
    path = Path(TB)
    if path.is_file() and os.access(path, os.X_OK):
        return str(path)
    return None


def _provision_env() -> dict:
    """Child env with herdr traces stripped; CMUX_* kept for the socket route."""
    return _instances.scrubbed_env()


def cmux_context() -> dict | None:
    """Socket (+ workspace) for cmux control, or None. No credentials returned."""
    path = (os.environ.get("CMUX_SOCKET_PATH") or "").strip()
    if not path:
        return None
    if not Path(path).exists():
        return None  # stale socket path → fall through to adapter path
    context = {"socket_path": path}
    workspace = (os.environ.get("CMUX_WORKSPACE_ID") or "").strip()
    if workspace:
        context["workspace"] = workspace
    return context


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


def _daemon_db_discovery() -> Discovery | None:
    """Find a live instance via the daemon DB when ``ls`` is TTY-blind."""
    import sqlite3

    roots = sorted(
        Path.home().glob(".local/share/terminal-browser-*/terminal-browser.db"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for db_path in roots:
        try:
            with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2) as conn:
                rows = conn.execute(
                    "SELECT cdp_port FROM instances WHERE cdp_port IS NOT NULL ORDER BY started_at DESC"
                ).fetchall()
        except (sqlite3.Error, OSError):
            continue
        for (port,) in rows:
            try:
                ws = browser_websocket_url(int(port))
            except (RuntimeError, urllib.error.URLError, TimeoutError, OSError, ValueError):
                continue
            return Discovery(ws_url=ws, http_origin=f"http://127.0.0.1:{int(port)}", source="terminal-browser")
    return None


def _instance_record_port(text: str) -> int | None:
    """Parse cdpPort from ``terminal-browser open`` JSON stdout."""
    for line in (text or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict) and isinstance(rec.get("cdpPort"), int):
            return rec["cdpPort"]
    return None


def _resolve_cmux() -> str | None:
    """Path to the ``cmux`` CLI, or None."""
    found = shutil.which("cmux")
    if found:
        return found
    bundled = os.environ.get("CMUX_BUNDLED_CLI_PATH", "").strip()
    if bundled and Path(bundled).is_file() and os.access(bundled, os.X_OK):
        return bundled
    return None


def _provisioned_instance_discovery() -> Discovery | None:
    """Reuse a prior cmux-provisioned instance when still alive; else None."""
    if cmux_context() is None:
        return None
    record = _read_ledger()
    if record is None:
        return None
    return _instance_in_record(record)


def _provision_via_cmux(
    url: str, context: dict, *, tries: int = POST_LAUNCH_TRIES, delay: float = POST_LAUNCH_DELAY_S
) -> Discovery:
    """Ask cmux for a sibling pane; poll until terminal-browser answers CDP."""
    binary = resolve_terminal_browser()
    if not binary:
        raise WatchUnavailable(WATCH_INSTALL)
    cmux = _resolve_cmux()
    if not cmux:
        raise WatchUnavailable(CMUX_UNAVAILABLE)

    argv = [cmux, "new-split", "right"]
    if context.get("workspace"):
        argv += ["--workspace", context["workspace"]]
    argv += ["--command", f"{shlex.quote(binary)} open {shlex.quote(url)}"]
    try:
        completed = subprocess.run(
            argv,
            env=_provision_env(),
            capture_output=True,
            text=True,
            timeout=PROVISION_TIMEOUT_S,
        )
    except FileNotFoundError as exc:
        raise WatchUnavailable(f"cmux binary vanished: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise WatchUnavailable(
            f"cmux new-split timed out after {PROVISION_TIMEOUT_S}s. {WATCH_TERMINAL_NOTE}"
        ) from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()[-800:]
        raise WatchUnavailable(
            "cmux could not open a pane for terminal-browser"
            + (f" — it said: {detail}" if detail else "")
            + f" {WATCH_TERMINAL_NOTE}"
        )

    for attempt in range(1, tries + 1):
        found = _terminal_browser_discovery() or _daemon_db_discovery()
        if found:
            found.auto_launched = True
            found.visibility = "terminal-browser-pane"
            _remember_instance(found)
            return found
        if attempt < tries:
            time.sleep(delay)
    raise WatchUnavailable(
        f"terminal-browser in the cmux pane did not become ready within {int(tries * delay)}s. "
        f"{WATCH_TERMINAL_NOTE}"
    )


def _remember_instance(found: Discovery) -> None:
    """Ledger key/port/pid for the provisioned instance (reuse on next drive)."""
    try:
        port = int(found.http_origin.rsplit(":", 1)[-1])
    except (ValueError, AttributeError):
        return
    try:
        data = list_browsers()
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
        return
    for browser in data.get("browsers") or []:
        if browser.get("cdpPort") != port:
            continue
        _write_ledger(
            {
                "key": str(browser.get("key") or ""),
                "port": port,
                "pid": int(browser.get("pid") or 0),
                "socket": str(browser.get("socket") or ""),
            }
        )
        return


def _provision_terminal_browser(
    url: str, *, tries: int = POST_LAUNCH_TRIES, delay: float = POST_LAUNCH_DELAY_S
) -> Discovery:
    """Open a visible split-right pane (cmux route when available). Never headless."""
    context = cmux_context()
    if context is not None:
        return _provision_via_cmux(url, context, tries=tries, delay=delay)
    binary = resolve_terminal_browser()
    if not binary:
        raise WatchUnavailable(WATCH_INSTALL)
    try:
        completed = subprocess.run(
            [binary, "open", url, "--split", "right", "--no-merge"],
            env=_provision_env(),
            capture_output=True,
            text=True,
            timeout=PROVISION_TIMEOUT_S,
        )
    except FileNotFoundError as exc:
        raise WatchUnavailable(f"terminal-browser binary vanished: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise WatchUnavailable(
            f"terminal-browser open timed out after {PROVISION_TIMEOUT_S}s. {WATCH_TERMINAL_NOTE}"
        ) from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()[-800:]
        raise WatchUnavailable(
            "terminal-browser could not open a visible pane"
            + (f" — it said: {detail}" if detail else "")
            + f" {WATCH_TERMINAL_NOTE} If you run inside herdr, ensure the outer terminal"
            " (ghostty/kitty/cmux/...) is supported."
        )

    port = _instance_record_port(completed.stdout)
    if port:
        try:
            ws = browser_websocket_url(port)
            return Discovery(
                ws_url=ws,
                http_origin=f"http://127.0.0.1:{port}",
                source="terminal-browser",
                auto_launched=True,
                visibility="terminal-browser-pane",
            )
        except (RuntimeError, urllib.error.URLError, TimeoutError, OSError):
            pass

    for attempt in range(1, tries + 1):
        found = _terminal_browser_discovery()
        if found:
            found.auto_launched = True
            found.visibility = "terminal-browser-pane"
            return found
        if attempt < tries:
            time.sleep(delay)
    raise WatchUnavailable(
        f"terminal-browser pane did not become ready within {int(tries * delay)}s. {WATCH_TERMINAL_NOTE}"
    )


def _in_cmux_context(env=None) -> bool:
    """True when both CMUX_SOCKET_PATH and CMUX_WORKSPACE_ID are set."""
    env = os.environ if env is None else env
    return bool(env.get("CMUX_SOCKET_PATH") and env.get("CMUX_WORKSPACE_ID"))


def _provision_cmux_split(url: str, *, env=None) -> Discovery:
    """Env-form wrapper around ``_provision_via_cmux`` (one spawner)."""
    env = os.environ if env is None else env
    context: dict = {}
    socket_path = (env.get("CMUX_SOCKET_PATH") or "").strip()
    if socket_path:
        context["socket_path"] = socket_path
    workspace = (env.get("CMUX_WORKSPACE_ID") or "").strip()
    if workspace:
        context["workspace"] = workspace
    return _provision_via_cmux(url, context, tries=POST_LAUNCH_TRIES, delay=POST_LAUNCH_DELAY_S)


def discover(
    *,
    explicit: str | None = None,
    launch_url: str = "about:blank",
    auto_provision: bool = True,
    watch: bool = False,
    background: bool = False,
    env: dict | None = None,
) -> Discovery:
    """Find or provision a visible pane; background=True only attaches to explicit CDP."""
    global LAST
    raw = ""
    if background:
        raw = (explicit or os.environ.get("JEV_CDP_URL") or os.environ.get("BROWSER_CDP_URL") or "").strip()
        if not raw:
            raise WatchUnavailable(
                "background=true does not launch a hidden browser. "
                "Pass cdp_url for a browser you already started, or leave background off "
                "to open a visible terminal-browser pane."
            )
    if raw:
        ws = normalize_cdp_url(raw)
        LAST = Discovery(
            ws_url=ws,
            http_origin=ws_to_http_origin(ws),
            source="explicit",
            visibility="background",
        )
        return LAST
    found = _terminal_browser_discovery() or _daemon_db_discovery()
    if found:
        found.visibility = "terminal-browser-pane"
        LAST = found
        return LAST
    if auto_provision:
        # Readers → reuse → cmux socket → herdr refuse → adapter provision.
        reused = _provisioned_instance_discovery()
        if reused is not None:
            LAST = reused
            return LAST
        if _in_cmux_context(env):
            LAST = _provision_cmux_split(launch_url, env=env)
            return LAST
        blocked = _instances.root_terminal_blocker(env)
        if blocked:
            raise WatchUnavailable(blocked)
        LAST = _provision_terminal_browser(launch_url)
        return LAST
    raise WatchUnavailable(
        "No terminal-browser pane found and auto-provision is disabled. "
        "Open one with `terminal-browser open <url>`, or retry with auto-provision. "
        + WATCH_TERMINAL_NOTE
    )
