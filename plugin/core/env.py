"""Env walks: where the driver lives, and whether this machine can drive.

The answers come from the environment, the filesystem and PATH — never a browser,
never the network, never key material. They are read the same way by the Hermes
gate (``check_fn``), the MCP status tool and the driver's own preflight, so
they live here once.

Stdlib only. See plugin/core/result.py for why that is a hard rule.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

TB = os.environ.get("TERMINAL_BROWSER", str(Path.home() / ".local" / "bin" / "terminal-browser"))
LOG_DIR = Path.home() / ".cache" / "wwwdrive"


def log_handler_event(tool: str, error: str, stderr: str = "") -> None:
    """Record failures the driver process could not log itself: timeouts and crashes."""
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    tail = " | ".join((stderr or "").strip().splitlines()[-8:])[:1500]
    record = {"ts": stamp, "event": "handler", "tool": tool, "error": error, "stderr": tail or None}
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with (LOG_DIR / "drive.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        line = f"{stamp} handler tool={tool} error={error}"
        if tail:
            line += f"\n  stderr: {tail[:400]}"
        with (LOG_DIR / "drive.log").open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        return


def driver_home() -> Path:
    """Directory that contains scripts/drive.py.

    Walks up from this file so a catalog install works without a checkout at
    ~/Projects. WWWDRIVE_HOME overrides it; JEV_DRIVER_HOME is still read as a
    fallback so pre-1.0 configs keep working.
    """
    env = os.environ.get("WWWDRIVE_HOME", "").strip() or os.environ.get("JEV_DRIVER_HOME", "").strip()
    if env:
        return Path(env).expanduser()
    here = Path(__file__).resolve().parent
    for candidate in (here, *here.parents):
        if (candidate / "scripts" / "drive.py").is_file():
            return candidate
    return here


def _read_key_from_env_file(path: Path) -> bool:
    if not path.is_file():
        return False
    for line in path.read_text().splitlines():
        if line.startswith(("OPENROUTER_API_KEY=", "DECISION_GATE_API_KEY=", "TYPESAFE_API_KEY=")):
            val = line.split("=", 1)[1].strip().strip('"').strip("'")
            if val:
                return True
    return False


def has_decision_key() -> bool:
    for var in ("DECISION_GATE_API_KEY", "TYPESAFE_API_KEY", "OPENROUTER_API_KEY"):
        if os.environ.get(var, "").strip():
            return True
    homes = [Path.home() / ".hermes" / ".env"]
    hermes_home = os.environ.get("HERMES_HOME", "").strip()
    if hermes_home:
        homes.append(Path(hermes_home).expanduser() / ".env")
    return any(_read_key_from_env_file(path) for path in homes)


def terminal_browser_installed() -> bool:
    return bool(shutil.which("terminal-browser")) or Path(TB).is_file()


def check_drive() -> bool:
    home = driver_home()
    if not (home / "scripts" / "drive.py").is_file():
        return False
    if not shutil.which("uv") and not (home / ".venv").exists():
        return False
    if not has_decision_key():
        return False
    # TUI-only: the driver provisions a visible terminal-browser pane itself,
    # so presence of the binary is enough — no running browser required.
    return terminal_browser_installed()