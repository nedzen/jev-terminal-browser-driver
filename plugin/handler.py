"""Stdlib-only subprocess wrapper. Hermes loads this; it must not import jev_driver."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

DEFAULT_HOME = Path.home() / "Projects" / "jev-terminal-browser-driver"
MAX_STEPS_CAP = 30
TIMEOUT_CAP = 900
DEFAULT_MAX_STEPS = 12
DEFAULT_TIMEOUT = 300
TB = os.environ.get("TERMINAL_BROWSER", str(Path.home() / ".local" / "bin" / "terminal-browser"))
BUNDLED_AB = Path.home() / ".local" / "share" / "terminal-browser" / "app" / "agent-browser" / "bin" / "agent-browser"


def driver_home() -> Path:
    return Path(os.environ.get("JEV_DRIVER_HOME", DEFAULT_HOME)).expanduser()


def _read_key_from_env_file(path: Path) -> bool:
    if not path.is_file():
        return False
    for line in path.read_text().splitlines():
        if line.startswith("OPENROUTER_API_KEY=") or line.startswith("DECISION_GATE_API_KEY="):
            val = line.split("=", 1)[1].strip().strip('"').strip("'")
            if val:
                return True
    return False


def has_decision_key() -> bool:
    for var in ("DECISION_GATE_API_KEY", "OPENROUTER_API_KEY"):
        if os.environ.get(var, "").strip():
            return True
    homes = [Path.home() / ".hermes" / ".env"]
    hermes_home = os.environ.get("HERMES_HOME", "").strip()
    if hermes_home:
        homes.append(Path(hermes_home).expanduser() / ".env")
    return any(_read_key_from_env_file(path) for path in homes)


def resolve_agent_browser() -> str | None:
    found = shutil.which("agent-browser")
    if found:
        return found
    if BUNDLED_AB.is_file() and os.access(BUNDLED_AB, os.X_OK):
        return str(BUNDLED_AB)
    if shutil.which("npx"):
        return "npx agent-browser"
    return None


def terminal_browser_running() -> bool:
    try:
        out = subprocess.check_output([TB, "ls", "--all", "--json"], text=True, timeout=2, stderr=subprocess.DEVNULL)
        data = json.loads(out)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return False
    return any(b.get("cdpPort") for b in (data.get("browsers") or []))


def agent_browser_daemon_present() -> bool:
    root = Path.home() / ".agent-browser"
    if not root.is_dir():
        return False
    try:
        return any(root.iterdir())
    except OSError:
        return False


def check_jev_drive() -> bool:
    home = driver_home()
    if not (home / "scripts" / "drive.py").is_file():
        return False
    if not shutil.which("uv") and not (home / ".venv").exists():
        return False
    if not has_decision_key():
        return False
    return bool(terminal_browser_running() or resolve_agent_browser() or agent_browser_daemon_present())


def parse_json_lines(text: str) -> list[dict]:
    rows = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _sum_usage(rows: list[dict]) -> dict:
    total = {"input_tokens": 0, "output_tokens": 0, "cost": 0.0}
    for row in rows:
        usage = row.get("usage") or {}
        if not isinstance(usage, dict):
            continue
        total["input_tokens"] += int(usage.get("input_tokens") or 0)
        total["output_tokens"] += int(usage.get("output_tokens") or 0)
        total["cost"] += float(usage.get("cost") or 0)
    return total


def compact_result(rows: list[dict], exit_code: int, error: str | None = None) -> dict:
    meta = next((r for r in rows if r.get("event") == "browser"), {})
    ticks = [r for r in rows if r.get("status")]
    last = ticks[-1] if ticks else {}
    status = last.get("status") or ("error" if error else "blocked")
    if status not in {"done", "blocked", "error"}:
        status = "blocked"
    actions = [t.get("last_action") for t in ticks if t.get("last_action")]
    browser = {
        "source": meta.get("source"),
        "cdp_url": meta.get("cdp_url"),
        "auto_launched": bool(meta.get("auto_launched")),
    }
    success = exit_code == 0 and status == "done" and not error
    return {
        "success": success,
        "status": "error" if error and status != "blocked" else status,
        "final_url": last.get("url"),
        "actions": actions,
        "ticks": len(ticks),
        "usage": _sum_usage(ticks),
        "browser": browser,
        "error": error,
    }


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.terminate()
        except OSError:
            pass
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and proc.poll() is None:
        time.sleep(0.05)
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.kill()
            except OSError:
                pass


def build_argv(args: dict) -> list[str]:
    home = driver_home()
    max_steps = args.get("max_steps", DEFAULT_MAX_STEPS)
    try:
        max_steps = int(max_steps)
    except (TypeError, ValueError):
        max_steps = DEFAULT_MAX_STEPS
    max_steps = max(1, min(max_steps, MAX_STEPS_CAP))
    url = args.get("url") or str((home / "fixtures" / "click.html").resolve().as_uri())
    argv = [
        "uv",
        "run",
        "python",
        "scripts/drive.py",
        "--json",
        "--goal",
        str(args["goal"]),
        "--max-steps",
        str(max_steps),
        "--url",
        str(url),
    ]
    if args.get("target"):
        argv.extend(["--target", str(args["target"])])
    if args.get("cdp_url"):
        argv.extend(["--cdp", str(args["cdp_url"])])
    return argv


def clamp_timeout(value) -> int:
    try:
        timeout = int(value)
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT
    return max(1, min(timeout, TIMEOUT_CAP))


def run_drive(args: dict, *, popen=subprocess.Popen, kill_group=_kill_group) -> dict:
    home = driver_home()
    if not args.get("goal") or not str(args.get("goal")).strip():
        return compact_result([], 1, error="goal is required")
    if not (home / "scripts" / "drive.py").is_file():
        return compact_result([], 1, error=f"drive.py missing under {home}")
    timeout_s = clamp_timeout(args.get("timeout_s", DEFAULT_TIMEOUT))
    proc = popen(
        build_argv(args),
        cwd=str(home),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
        env=os.environ.copy(),
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        kill_group(proc)
        leftover = ""
        try:
            leftover = proc.communicate(timeout=1)[0] or ""
        except Exception:
            leftover = ""
        rows = parse_json_lines(leftover)
        result = compact_result(rows, 1, error="timeout")
        result["status"] = "blocked"
        result["success"] = False
        return result
    rows = parse_json_lines(stdout or "")
    error = None
    if proc.returncode not in (0, 1) and not rows:
        error = f"driver exited {proc.returncode}"
    elif proc.returncode != 0 and not rows:
        tail = (stderr or "").strip().splitlines()[-3:]
        error = "driver failed with no output" + (": " + " | ".join(tail) if tail else "")
    return compact_result(rows, proc.returncode or 0, error=error)


def handle_jev_drive(args: dict | None = None, **kwargs) -> str:
    payload = args if isinstance(args, dict) else kwargs
    return json.dumps(run_drive(payload))
