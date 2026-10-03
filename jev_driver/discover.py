"""CDP discovery, TUI-only: explicit → terminal-browser → visible provision.

No headless, no agent-browser, no loopback scanning. If no terminal-browser
pane exists we PROVISION one, by one of two routes:

- Inside cmux, via the cmux control socket: `cmux new-split right --command
  "terminal-browser open <url>"`. The pane is a sibling of the agent's own pane
  at the level the user is looking at.
- Everywhere else, via terminal-browser's own adapter detection:
  `terminal-browser open <url> --split right`, with all HERDR_* env vars
  scrubbed so that detection skips the herdr adapter (it matches on
  HERDR_PANE_ID alone and would otherwise nest the browser inside the calling
  herdr pane).

The split matters because of the nesting: when the agent runs as Hermes-TUI in
cmux, itself inside herdr panes, the adapter chain resolves to herdr and the
browser lands in the agent's own pane tree — visible to nobody. cmux's socket
is consulted first precisely so that case never reaches the adapter guess.
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

# The instance a cmux-provisioned pane is running, so the next drive opens a TAB in
# it instead of provisioning a second pane. Keyed by the cmux socket, because a
# pane belongs to the cmux session that made it: the same box with two cmux
# sessions has two sets of panes, and a ledger from one must not satisfy a drive
# that was launched from the other.
LEDGER_PATH = Path.home() / ".cache" / "wwwdrive" / "cmux-instance.json"


def _ledger_key() -> str:
    """Identity of the cmux session a record belongs to.

    The socket path, hashed rather than stored: the ledger lives in a cache
    directory and the socket path is a filesystem location belonging to the user's
    account, so a digest of it is what the record needs to carry.
    """
    path = (os.environ.get("CMUX_SOCKET_PATH") or "").strip()
    return hashlib.sha256(path.encode("utf-8")).hexdigest()[:16] if path else ""


def _read_ledger() -> dict | None:
    """The recorded instance for this cmux session, or None.

    Every failure reads as "no record": a missing file, unreadable JSON, a record
    written by a different cmux session, or a record missing any field this
    module needs. Re-provisioning is always a safe answer to an unreadable
    ledger, so nothing here is allowed to raise.
    """
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
    """Record the provisioned instance. Never raises: a lost record costs one pane."""
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
    """A Discovery for the recorded instance, if that exact instance is still alive.

    Identity is the triple terminal-browser itself reports — key, cdpPort and pid.
    The pid is what makes this an instance check rather than a port check: a port
    can be reused by a different process after the original exits, and attaching
    to whatever now holds it would drive a browser nobody asked for. All three must
    match, and the port must still answer /json/version, so a dead record
    provisions a fresh pane instead of attaching to a corpse.
    """
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
    """Child env with every herdr trace stripped.

    Delegates to `instances.scrubbed_env`, which strips the `HERDR_*` prefix *and* the
    variables that carry a herdr trace without it — verified live: `SSH_AUTH_SOCK`
    points at ~/.config/herdr/herdr.sock.agent and `TERM_PROGRAM` is "herdr".

    This is necessary but NOT sufficient, and the reason matters: terminal-browser's
    adapter chain picks cmux correctly once the prefix is gone, but its `open` only
    offers `--split`, which always splits the *current surface*. Under a herdr pane
    that surface is the agent's own pane, so a correct adapter still nests. See
    `instances.root_terminal_blocker`.

    CMUX_* is deliberately KEPT. On the cmux path the caller is a cmux process
    that has to be told which workspace and surface to split; scrub that and the
    split lands wherever cmux's own default points, which is not necessarily the
    workspace the user is looking at.
    """
    return _instances.scrubbed_env()


def cmux_context() -> dict | None:
    """The cmux control-socket context, or None when this is not a cmux session.

    Presence of the socket path is the whole test. The path is a filesystem
    location cmux exports to every process it spawns, so a child can talk to the
    control socket directly without going through terminal-browser's adapter
    detection at all — which is the point: that detection is what nests the
    browser inside a herdr pane when the agent runs Hermes-TUI-in-cmux.

    Returns only what the provisioner needs to address a target. The socket
    capability token and the password are deliberately not returned: they are
    credentials and nothing here needs them, because the `cmux` CLI reads them
    from the environment itself.
    """
    path = (os.environ.get("CMUX_SOCKET_PATH") or "").strip()
    if not path:
        return None
    if not Path(path).exists():
        # A stale export from a cmux that has since exited. Treating this as "no
        # cmux" falls through to the adapter path, which is the correct answer:
        # there is no control socket to provision through.
        return None
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


def _resolve_cmux() -> str | None:
    """The `cmux` CLI, or None. Same lookup contract as resolve_terminal_browser."""
    found = shutil.which("cmux")
    if found:
        return found
    bundled = os.environ.get("CMUX_BUNDLED_CLI_PATH", "").strip()
    if bundled and Path(bundled).is_file() and os.access(bundled, os.X_OK):
        return bundled
    return None


def _provisioned_instance_discovery() -> Discovery | None:
    """The still-running instance a previous drive provisioned, or None.

    Only consulted inside cmux, and only after the ordinary discovery readers have
    come back empty — the readers stay first because they answer for an instance
    nobody ledgered (one the user opened by hand, say), and the ledger is the
    fallback for the one this module opened itself.

    Outside cmux this returns None without reading anything: the ledger is keyed by
    cmux socket, so it cannot describe an instance in another terminal, and the
    adapter path keeps its current behaviour of provisioning when nothing is found.
    """
    if cmux_context() is None:
        return None
    record = _read_ledger()
    if record is None:
        return None
    return _instance_in_record(record)


def _provision_via_cmux(
    url: str, context: dict, *, tries: int = POST_LAUNCH_TRIES, delay: float = POST_LAUNCH_DELAY_S
) -> Discovery:
    """Open the browser by asking cmux to make the pane, never terminal-browser.

    The failure this fixes: terminal-browser's adapter chain is walked to decide
    which terminal to split, and when the agent runs Hermes-TUI-in-cmux nested in
    herdr panes, that chain resolves to herdr and the browser lands inside the
    agent's own pane tree instead of at the cmux root. Asking cmux directly makes
    the pane a sibling of the agent's pane at the level the user is actually
    looking at, so there is no adapter to guess wrong.

    `--command` runs terminal-browser INSIDE the new pane, so its own adapter
    detection is irrelevant: it is already the pane's command and needs no split
    of its own. That is why this path passes no `--split`.

    The new pane's terminal-browser is not our child, so its instance record is
    not in our stdout. Discovery is by poll instead.
    """
    binary = resolve_terminal_browser()
    if not binary:
        raise WatchUnavailable(WATCH_INSTALL)
    cmux = _resolve_cmux()
    if not cmux:
        raise WatchUnavailable(CMUX_UNAVAILABLE)

    argv = [cmux, "new-split", "right"]
    if context.get("workspace"):
        argv += ["--workspace", context["workspace"]]
    # Quoted as one argument: cmux passes this string to a shell in the new pane,
    # so the URL must not be able to break out of it into a second command.
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

    # The pane's terminal-browser is not our child, so there is no instance record
    # to parse. Both readers are tried because neither sees the other terminal's
    # instances: `ls` is env/TTY-scoped to this cmux surface, and the daemon DB
    # records the new instance regardless of who launched it.
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
    """Ledger the instance a provisioned pane is running, so the next drive reuses it.

    The port alone would be enough to find it again; key and pid are stored beside
    it so the next drive can prove it is the same instance and not a different
    process that inherited the port. Nothing is written when no instance matches
    the port, so a ledger never claims an instance it could not identify.
    """
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
    """Open a VISIBLE terminal-browser pane split right in the real terminal.

    Never headless. Raises (WatchUnavailable for the watch path) when a
    visible pane cannot be created — there is no silent background fallback.

    The root-terminal refusal lives in `discover`, not here: attaching to a pane that
    already exists is fine from inside a herdr pane, and only *provisioning* nests.
    Inside cmux this delegates to `_provision_via_cmux`, which asks the control
    socket for a pane instead of letting terminal-browser guess a terminal. The
    adapter path below remains the fallback everywhere else.
    """
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


def _in_cmux_context(env=None) -> bool:
    """Whether cmux can be addressed directly from here.

    Both the socket and the workspace are required. The socket alone is not enough: a
    stale `CMUX_SOCKET_PATH` left in the environment would claim a cmux context that
    no longer exists, and the route would then fail at the socket instead of falling
    back to the adapter ladder. The workspace id is what the split is created in, so
    without it there is nowhere correct to create it.
    """
    env = os.environ if env is None else env
    return bool(env.get("CMUX_SOCKET_PATH") and env.get("CMUX_WORKSPACE_ID"))


def _provision_cmux_split(url: str, *, env=None) -> Discovery:
    """Provision a visible pane by asking cmux for the split, allowed from anywhere.

    This is the route that makes the herdr refusal necessary rather than sufficient.
    terminal-browser can only split the surface it is already inside, which under a
    herdr pane is the agent's own pane. cmux's own `new-split` addresses the workspace
    directly and is handed a command to run in the new surface, so the split lands at
    cmux level and terminal-browser then attaches to *that* surface. No adapter is
    consulted, so there is no adapter to get wrong.

    Single implementation lives in `_provision_via_cmux`; this adapts the env-form
    caller to its context-form contract so there is exactly one spawner.
    """
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
    """Visible terminal-browser pane, unless background=True.

    Default order: a running terminal-browser pane, then `terminal-browser open
    --split right`. Environment CDP URLs are ignored on that path so a leftover
    `JEV_CDP_URL` cannot hide the browser. background=True does not launch a
    hidden browser; it only attaches to an explicit CDP URL the caller supplies.

    `env` is the environment the placement guard reads. It is a parameter rather than
    a direct `os.environ` read so the placement policy is testable without a herdr
    pane, and so a caller can state its own environment deliberately.
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
        # ROUTE ORDER IS THE SAFETY ORDER. Readers first (a hand-opened pane
        # wins), then reuse of this module's own previous provision (one pane
        # per cmux session, one tab per drive — reuse stops the window filling
        # with one browser per run). Then the cmux socket route, allowed from
        # anywhere including inside a herdr pane: it addresses cmux directly
        # and creates the split at cmux's own level, so there is no adapter
        # to guess wrong and nothing to nest inside. The herdr refusal below
        # therefore guards only the adapter-guess fallback, which is the one
        # route that cannot escape the calling pane. Refuse rather than nest:
        # a nesting provision is indistinguishable from a correct one after
        # the fact, which is what makes this a refusal and not a warning.
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
