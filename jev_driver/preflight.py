"""Startup gate: statuses + fix hints, no side effects, never key material.

Spelled out here (not shared with the Hermes env probes) so PATH vs file-check
answers stay distinct; tests/test_preflight.py pins agreement.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

KEY_VARS = ("DECISION_GATE_API_KEY", "TYPESAFE_API_KEY", "OPENROUTER_API_KEY")
_KEY_PREFIXES = tuple(f"{var}=" for var in KEY_VARS)

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
    """terminal-browser path (PATH, TERMINAL_BROWSER, ~/.local/bin), or None."""
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
    """Directory holding scripts/drive.py (WWWDRIVE_HOME / JEV_DRIVER_HOME override)."""
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
    """Startup checks as ok/missing, plus missing names and fix hints."""
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
