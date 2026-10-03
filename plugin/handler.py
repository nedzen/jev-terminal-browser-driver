"""Hermes adapter: subprocess in, JSON out. Shared rules live in plugin/core."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time

from .core.budgets import (
    DEFAULT_MAX_STEPS,
    DEFAULT_TIMEOUT,
    MAX_STEPS_CAP,
    TIME_BUDGET_CAP,
    TIMEOUT_CAP,
    budget,
    deny_names,
)
from .core.env import (
    LOG_DIR,
    TB,
    check_drive,
    driver_home,
    has_decision_key,
    log_handler_event,
    terminal_browser_installed,
)
from .core.result import PAGE_TEXT_LIMIT, compact_result, parse_json_lines

__all__ = [
    "DEFAULT_MAX_STEPS",
    "DEFAULT_TIMEOUT",
    "LOG_DIR",
    "MAX_STEPS_CAP",
    "PAGE_TEXT_LIMIT",
    "TB",
    "TIMEOUT_CAP",
    "TIME_BUDGET_CAP",
    "build_argv",
    "build_read_argv",
    "check_drive",
    "compact_result",
    "driver_home",
    "handle_drive",
    "handle_read",
    "has_decision_key",
    "log_handler_event",
    "parse_json_lines",
    "run_drive",
    "run_read",
    "terminal_browser_installed",
]


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
    max_steps = args.get("max_steps", DEFAULT_MAX_STEPS)
    try:
        max_steps = int(max_steps)
    except (TypeError, ValueError):
        max_steps = DEFAULT_MAX_STEPS
    max_steps = max(1, min(max_steps, MAX_STEPS_CAP))
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
    ]
    if args.get("url"):
        argv.extend(["--url", str(args["url"])])
    if args.get("target"):
        argv.extend(["--target", str(args["target"])])
    for pattern in args.get("deny_names") or []:
        argv.extend(["--deny-name", pattern])
    # Optional inner deadline. timeout_s stays the outer subprocess kill; this one
    # is checked inside the driver's tick loop. Absent means no inner deadline.
    if args.get("time_budget_s") is not None:
        argv.extend(["--time-budget-s", str(args["time_budget_s"])])
    background = args.get("background") in {True, "true", "True", 1, "1"}
    if background:
        argv.append("--background")
        # A CDP URL is only honored for an explicit hidden attach. Otherwise it
        # can point the run at a browser the user cannot see.
        if args.get("cdp_url"):
            argv.extend(["--cdp", str(args["cdp_url"])])
    if args.get("watch") in {True, "true", "True", 1, "1"}:
        argv.append("--watch")
    if args.get("debug") in {True, "true", "True", 1, "1"}:
        argv.append("--debug")
    else:
        argv.append("--no-debug")
    return argv


def run_drive(args: dict, *, popen=subprocess.Popen, kill_group=_kill_group) -> dict:
    home = driver_home()
    if not args.get("goal") or not str(args.get("goal")).strip():
        return compact_result([], 1, error="goal is required")
    if not (home / "scripts" / "drive.py").is_file():
        return compact_result([], 1, error=f"drive.py missing under {home}")
    try:
        validated = {
            **args,
            "max_steps": budget(args.get("max_steps"), 1, MAX_STEPS_CAP, "max_steps", DEFAULT_MAX_STEPS),
            # Strict, like every other budget: rejected before the spawn, and
            # integral floats (45.0) accepted for cross-adapter parity.
            "time_budget_s": budget(args.get("time_budget_s"), 1, TIME_BUDGET_CAP, "time_budget_s", None),
            # Rejected before the spawn for the same reason a budget is: a pattern
            # that cannot compile is the caller's to fix, and a run opened against
            # a denylist nobody meant to set is a browser nobody asked for.
            "deny_names": deny_names(args.get("deny_names")),
        }
        timeout_s = budget(args.get("timeout_s"), 1, TIMEOUT_CAP, "timeout_s", DEFAULT_TIMEOUT)
    except ValueError as exc:
        return compact_result([], 1, error=str(exc))
    # The insight trace is opt-in per adapter; a caller that says nothing keeps
    # it, so a direct run_drive caller is unchanged.
    want_insights = args.get("insights", True)
    proc = popen(
        build_argv(validated),
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
        log_handler_event("drive", f"timeout after {timeout_s}s")
        result = compact_result(rows, 1, error="timeout", insights=want_insights)
        result["status"] = "blocked"
        result["success"] = False
        return result
    rows = parse_json_lines(stdout or "")
    error = None
    if proc.returncode not in (0, 1) and not rows:
        error = f"driver exited {proc.returncode}"
    elif proc.returncode != 0 and not rows:
        tail = (stderr or "").strip().splitlines()[-15:]
        error = "driver failed with no output" + (": " + " | ".join(tail) if tail else "")
    if error:
        log_handler_event("drive", error, stderr or "")
    return compact_result(rows, proc.returncode or 0, error=error, insights=want_insights)


def handle_drive(args: dict | None = None, **kwargs) -> str:
    payload = args if isinstance(args, dict) else kwargs
    return json.dumps(run_drive(payload))


def build_read_argv(args: dict) -> list[str]:
    try:
        scrolls = int(args.get("scrolls", 0) or 0)
    except (TypeError, ValueError):
        scrolls = 0
    scrolls = max(0, min(scrolls, 15))
    argv = ["uv", "run", "python", "scripts/read.py", "--json"]
    if args.get("url"):
        argv.extend(["--url", str(args["url"])])
    if args.get("script"):
        argv.extend(["--script", str(args["script"])])
    argv.extend(["--scrolls", str(scrolls)])
    if args.get("target"):
        argv.extend(["--target", str(args["target"])])
    if args.get("background") in {True, "true", "True", 1, "1"}:
        argv.append("--background")
        # Same rule as drive: a CDP URL is only honored for an explicit
        # hidden attach, never for the visible pane.
        if args.get("cdp_url"):
            argv.extend(["--cdp", str(args["cdp_url"])])
    argv.append("--debug" if args.get("debug") in {True, "true", "True", 1, "1"} else "--no-debug")
    return argv


def run_read(args: dict, *, popen=subprocess.Popen, kill_group=_kill_group) -> dict:
    home = driver_home()
    if not (home / "scripts" / "read.py").is_file():
        return {"success": False, "error": f"read.py missing under {home}"}
    try:
        validated = {
            **args,
            "scrolls": budget(args.get("scrolls"), 0, 15, "scrolls", 0),
        }
        timeout_s = budget(args.get("timeout_s"), 1, TIMEOUT_CAP, "timeout_s", DEFAULT_TIMEOUT)
    except ValueError as exc:
        return {"success": False, "error": str(exc)}
    proc = popen(
        build_read_argv(validated),
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
        log_handler_event("read", f"timeout after {timeout_s}s")
        return {"success": False, "status": "blocked", "error": "timeout"}
    rows = parse_json_lines(stdout or "")
    if rows:
        return rows[-1]
    tail = (stderr or "").strip().splitlines()[-8:]
    error = "read failed" + (": " + " | ".join(tail) if tail else "")
    log_handler_event("read", error, stderr or "")
    return {"success": False, "error": error}


def handle_read(args: dict | None = None, **kwargs) -> str:
    payload = args if isinstance(args, dict) else kwargs
    return json.dumps(run_read(payload))