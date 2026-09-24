"""CDP discovery, TUI-only: explicit → terminal-browser → visible provision.

No headless, no agent-browser, no loopback scanning. If no terminal-browser
pane exists we PROVISION one: `terminal-browser open <url> --split right` with
all HERDR_* env vars scrubbed so terminal-browser's terminal detection skips
the herdr adapter (it matches on HERDR_PANE_ID alone and would otherwise nest
the browser inside the calling herdr pane) and opens a visible split in the
real terminal window (ghostty/kitty/cmux/...).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

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
    """Child env with every HERDR_* variable stripped.

    terminal-browser detects the terminal via an adapter chain whose FIRST
    entry is the herdr adapter, matched on `HERDR_PANE_ID` alone. Every
    process spawned from inside a herdr pane inherits that variable, so an
    unscrubbed launch makes terminal-browser ask herdr to split — nesting the
    browser in a herdr pane of the agent's tab instead of a visible split in
    the real terminal window. Scrubbing lets detection fall through to the
    actual terminal (ghostty/kitty/cmux/...).
    """
    return {k: v for k, v in os.environ.items() if not k.startswith("HERDR_")}


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
    """Find live terminal-browser instances via the daemon SQLite record.

    `terminal-browser ls` only sees browsers attached to the calling terminal
    (its adapter chain is env/TTY-based). From a no-TTY context — e.g. a
    subprocess of a herdr pane — a visible ghostty-split browser is invisible
    to `ls`. The daemon DB at ~/.local/share/terminal-browser-*/terminal-browser.db
    records every instance with its cdp_port; verify the port before trusting it.
    """
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
    """`terminal-browser open` prints its instance record (with cdpPort) as JSON."""
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


def _provision_terminal_browser(
    url: str, *, tries: int = POST_LAUNCH_TRIES, delay: float = POST_LAUNCH_DELAY_S
) -> Discovery:
    """Open a VISIBLE terminal-browser pane split right in the real terminal.

    Never headless. Raises (WatchUnavailable for the watch path) when a
    visible pane cannot be created — there is no silent background fallback.
    """
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

    # From a no-TTY caller `ls` cannot see tty-associated browsers; the
    # instance record printed by `open` carries the cdpPort directly.
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

    # Real-TTY caller: poll the normal discovery path until the pane is ready.
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


def discover(
    *,
    explicit: str | None = None,
    launch_url: str = "about:blank",
    auto_provision: bool = True,
    watch: bool = False,
    background: bool = False,
) -> Discovery:
    """Visible terminal-browser pane, unless background=True.

    Default order: a running terminal-browser pane, then `terminal-browser open
    --split right`. Environment CDP URLs are ignored on that path so a leftover
    `JEV_CDP_URL` cannot hide the browser. background=True does not launch a
    hidden browser; it only attaches to an explicit CDP URL the caller supplies.
    """
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
        LAST = _provision_terminal_browser(launch_url)
        return LAST
    raise WatchUnavailable(
        "No terminal-browser pane found and auto-provision is disabled. "
        "Open one with `terminal-browser open <url>`, or retry with auto-provision. "
        + WATCH_TERMINAL_NOTE
    )
