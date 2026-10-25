"""Startup gate: can this machine drive a browser at all?

Answers before the first tick, so a missing key or a missing terminal-browser
surfaces as a named status instead of a failure halfway through a paid run.

Stdlib only and side-effect free: no browser launch, no CDP probe, no model
call, nothing written to disk, and no import of the package being checked —
`find_spec` reads the loader spec without executing the module. The result is
statuses and a fix hint, never key material — a key is reported as present or
missing, and its value is never read into the result.

Three checks, one owner each:

- `decision_key` — a key is reachable from env or the Hermes env file.
- `terminal_browser` — the binary is on PATH or named by TERMINAL_BROWSER.
- `package` — the driver is importable in this interpreter (installed wheel) or
  an explicit WWWDRIVE_HOME/JEV_DRIVER_HOME checkout holds scripts/drive.py.

The Hermes gate (`plugin/core/env.check_drive`) asks the same three questions;
`tests/test_preflight.py` pins that they agree.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
from pathlib import Path

KEY_VARS = ("DECISION_GATE_API_KEY", "TYPESAFE_API_KEY", "OPENROUTER_API_KEY")
_KEY_PREFIXES = tuple(f"{var}=" for var in KEY_VARS)

# The importable pieces an installed driver needs. `find_spec` does not execute them.
PACKAGE_MODULES = ("jev_driver", "httpx", "websocket")

# Report order. Every field is a status the caller can act on.
CHECKS = ("decision_key", "terminal_browser", "package")

FIXES = {
    "decision_key": "set DECISION_GATE_API_KEY or TYPESAFE_API_KEY in env, or OPENROUTER_API_KEY",
    "terminal_browser": "install terminal-browser, or point TERMINAL_BROWSER at the binary",
    "package": "install wwwdrive (pipx install wwwdrive), or set WWWDRIVE_HOME to a checkout",
}


def _status(found: bool) -> str:
    return "ok" if found else "missing"


def _spec(name: str) -> bool:
    """True when `name` is importable here, without importing it."""
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        return False


def terminal_browser_binary() -> Path | None:
    """Where the driver would spawn terminal-browser from, or None.

    Same order as discover.resolve_terminal_browser: PATH first, then
    TERMINAL_BROWSER, then ~/.local/bin. Nothing is executed.
    """
    found = shutil.which("terminal-browser")
    if found:
        return Path(found)
    override = os.environ.get("TERMINAL_BROWSER", "").strip()
    candidates = [Path(override).expanduser()] if override else []
    candidates.append(Path.home() / ".local" / "bin" / "terminal-browser")
    for path in candidates:
        if path.is_file() and os.access(path, os.X_OK):
            return path
    return None


def terminal_browser_installed() -> bool:
    return terminal_browser_binary() is not None


def _key_in_env_file(path: Path) -> bool:
    """True when the file holds a non-empty key assignment. The value is dropped here."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return False
    for line in lines:
        line = line.strip()
        if not line.startswith(_KEY_PREFIXES):
            continue
        if line.split("=", 1)[1].strip().strip('"').strip("'"):
            return True
    return False


def has_decision_key() -> bool:
    """True when a decision key is reachable from env or the Hermes env file."""
    if any(os.environ.get(var, "").strip() for var in KEY_VARS):
        return True
    homes = [Path.home() / ".hermes" / ".env"]
    hermes_home = os.environ.get("HERMES_HOME", "").strip()
    if hermes_home:
        homes.append(Path(hermes_home).expanduser() / ".env")
    return any(_key_in_env_file(path) for path in homes)


def driver_home() -> Path:
    """Directory holding a checkout's scripts/drive.py, or this package's parent.

    Walks up from this file so a development checkout resolves without env.
    WWWDRIVE_HOME overrides it; JEV_DRIVER_HOME is the pre-1.0 fallback.
    """
    env = os.environ.get("WWWDRIVE_HOME", "").strip() or os.environ.get("JEV_DRIVER_HOME", "").strip()
    if env:
        return Path(env).expanduser()
    here = Path(__file__).resolve().parent
    for candidate in (here, *here.parents):
        if (candidate / "scripts" / "drive.py").is_file():
            return candidate
    return here


def package_available() -> bool:
    """True when this interpreter can run the driver.

    An explicit home wins, so pointing WWWDRIVE_HOME at a broken tree reports
    missing instead of silently falling back to an installed wheel. Without an
    override, the installed distribution plus its two runtime deps answer it.
    """
    explicit = os.environ.get("WWWDRIVE_HOME", "").strip() or os.environ.get("JEV_DRIVER_HOME", "").strip()
    if explicit:
        return (driver_home() / "scripts" / "drive.py").is_file()
    return all(_spec(name) for name in PACKAGE_MODULES)


def preflight() -> dict:
    """Every startup check as {"ok" | "missing"}, plus what is missing and how to fix it.

    `ready` is True only when all three pass. Cheap enough to call per tool
    invocation: filesystem, PATH and import specs only.
    """
    found = {
        "decision_key": has_decision_key(),
        "terminal_browser": terminal_browser_installed(),
        "package": package_available(),
    }
    missing = [name for name in CHECKS if not found[name]]
    return {
        **{name: _status(found[name]) for name in CHECKS},
        "ready": not missing,
        "missing": missing,
        "fixes": {name: FIXES[name] for name in missing},
    }
