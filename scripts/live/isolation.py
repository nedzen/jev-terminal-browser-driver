"""Run isolation: an idle pane, and a quarantine on the last observed page.

v3 requires sequential drives on one designated pane under a mutex the caller
holds, and a run-isolation checklist per run. Two of those steps are mechanical
and belong here:

- *pane idle check*: nothing may be mid-drive when a run starts. Two drivers on one
  pane is the failure this whole design is serial to avoid, and the symptom -- two
  runs interleaving into one tab -- is very hard to read after the fact.
- *last-page quarantine*: `last-page.json` is how the driver remembers which tab
  it was last on. Left in place, the next run silently re-attaches to the previous
  run's tab, which looks exactly like a continuity success and is not one.

Both are checks the caller can only get right if something checks them, so this
module refuses rather than warns.

Stdlib only, no network. It reads the run-log directory, which is where the pane's
own bookkeeping already lives.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

# The driver's log directory, matching plugin/core/env.py's LOG_DIR.
LOG_DIR = Path.home() / ".cache" / "wwwdrive"
LAST_PAGE = "last-page.json"

# A drive in flight leaves a `run` start with no matching `run` finish. The quiet
# window is deliberately short: this is a liveness check, not a timeout.
IDLE_QUIET_S = 2.0


class IsolationError(RuntimeError):
    """The pane is not in a state where a run may start."""


def _events(log_dir: Path):
    path = log_dir / "drive.jsonl"
    if not path.is_file():
        return []
    events = []
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except ValueError:
                continue
    except OSError:
        return []
    return events


def _dir(log_dir) -> Path:
    """Resolve a log directory at call time.

    Every entry point here takes `log_dir=None` and reads LOG_DIR *now*, rather
    than defaulting to `log_dir: Path = LOG_DIR`. A default argument is bound once
    at import, so it ignores a caller (or a test) that redirects the module
    constant -- and `quarantine_last_page` renames a file, which makes that a
    mutation of the real browser bookkeeping rather than a read.
    """
    return Path(log_dir) if log_dir is not None else Path(LOG_DIR)


def open_run_ids(log_dir=None) -> list[str]:
    """Runs that started and never finished: a drive still in flight, or one that died.

    Keyed on the run event's own id so a crashed process still shows up -- the
    absence of a finish line is the signal, not the presence of a live process,
    because a wedged drive has no process left to ask.
    """
    started: dict[str, dict] = {}
    for event in _events(log_dir):
        if event.get("event") != "run" or event.get("stage") not in ("start", "finish"):
            continue
        metrics = event.get("metrics") or {}
        run_id = metrics.get("run_id") or event.get("run_id")
        if not run_id:
            continue
        if event.get("stage") == "start":
            started[run_id] = event
        else:
            started.pop(run_id, None)
    return list(started)


def assert_pane_idle(log_dir=None, *, quiet_s: float = IDLE_QUIET_S) -> None:
    """Refuse to start while any run is open, or while the log is still moving.

    The movement check catches the case `open_run_ids` cannot: a drive that has not
    written its start line yet, which is the window where two runners both believe
    they went first.
    """
    log_dir = _dir(log_dir)
    open_runs = open_run_ids(log_dir)
    if open_runs:
        raise IsolationError(f"pane is not idle: {len(open_runs)} run(s) never finished: {open_runs}")

    path = log_dir / "drive.jsonl"
    if path.is_file():
        try:
            age = time.time() - path.stat().st_mtime
        except OSError:
            age = None
        if age is not None and age < quiet_s:
            raise IsolationError(
                f"pane log moved {age:.2f}s ago (<{quiet_s}s): another driver is active"
            )


def read_last_page(log_dir=None) -> dict | None:
    path = _dir(log_dir) / LAST_PAGE
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def quarantine_last_page(log_dir=None) -> dict | None:
    """Clear the remembered tab and return what it was.

    Renamed rather than deleted: if a run then re-attaches to nothing and fails,
    the previous run's target is still on disk to diagnose the attach with. The
    rename is the isolation -- a driver that reads LAST_PAGE finds nothing -- and
    the retained file is the forensics.
    """
    log_dir = _dir(log_dir)
    path = log_dir / LAST_PAGE
    if not path.is_file():
        return None
    previous = read_last_page(log_dir)
    try:
        path.replace(log_dir / f"{LAST_PAGE}.quarantined")
    except OSError:
        # An unquarantined page is worth reporting, not worth crashing a run over:
        # the caller still gets the previous target in the return value.
        return previous
    return previous


def isolation_report(log_dir=None) -> dict:
    """What the checklist saw, for the run record.

    Records the previous target rather than just asserting on it, so a run that
    re-attaches unexpectedly can be told apart from one that did not.
    """
    previous = read_last_page(log_dir)
    return {
        "pane_idle": True,
        "quarantined_target": (previous or {}).get("targetId"),
        "quarantined_url": (previous or {}).get("url"),
    }