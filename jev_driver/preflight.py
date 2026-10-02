"""Startup gate: can this machine drive a browser at all?

Answers before the first tick, so a missing key or a missing terminal-browser
surfaces as a named status instead of a failure halfway through a paid run.

Stdlib only and side-effect free: no browser launch, no CDP probe, no model
call, nothing written to disk. The result is statuses and a fix hint, never key
material — a key is reported as present or missing, and its value is never read
into the result.

Mirrors the checks behind the Hermes gate (has_decision_key,
terminal_browser_installed, check_drive) so `status` and the gate agree. Those
three now live in plugin/core/env.py, which is stdlib-only and importable from
here; the checks are kept spelled out locally rather than imported, because the
MCP status tool must answer with no browser and no paid call, and a status that
could not answer would be the worst possible failure for a gate.

The duplication is deliberate and pinned by tests/test_preflight.py: the two
answers must agree, and they are read from different places (a PATH probe for
the binary vs. a file check) so a shared helper would hide which one answered.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

KEY_VARS = ("DECISION_GATE_API_KEY", "TYPESAFE_API_KEY", "OPENROUTER_API_KEY")
_KEY_PREFIXES = tuple(f"{var}=" for var in KEY_VARS)

# Report order. Every field is a status the caller can act on.
CHECKS = ("decision_key", "terminal_browser", "driver_home", "python_env")

FIXES = {
    "decision_key": "set DECISION_GATE_API_KEY or TYPESAFE_API_KEY in env, or OPENROUTER_API_KEY",
    "terminal_browser": "install terminal-browser, or point TERMINAL_BROWSER at the binary",
    "driver_home": "keep scripts/drive.py next to this checkout, or set WWWDRIVE_HOME",
    "python_env": "install uv, or create the repo .venv",
}


def _status(found: bool) -> str:
    return "ok" if found else "missing"


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
    """Directory holding scripts/drive.py.

    Walks up from this file so an installed copy still finds the checkout.
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


def python_env_ready() -> bool:
    return bool(shutil.which("uv")) or (driver_home() / ".venv").exists()


def preflight() -> dict:
    """Every startup check as {"ok" | "missing"}, plus what is missing and how to fix it.

    `ready` is True only when all four pass. Cheap enough to call per tool
    invocation: filesystem and PATH only.
    """
    found = {
        "decision_key": has_decision_key(),
        "terminal_browser": terminal_browser_installed(),
        "driver_home": (driver_home() / "scripts" / "drive.py").is_file(),
        "python_env": python_env_ready(),
    }
    missing = [name for name in CHECKS if not found[name]]
    return {
        **{name: _status(found[name]) for name in CHECKS},
        "ready": not missing,
        "missing": missing,
        "fixes": {name: FIXES[name] for name in missing},
    }