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
    # Routed through `_dir` like every other entry point, so `open_run_ids()` works
    # when called with no argument. It read `log_dir / "drive.jsonl"` directly, which
    # raises TypeError on None -- masked everywhere it was used, because the one
    # in-tree caller (`assert_pane_idle`) resolves first and passes the path down.
    # The reaper consults this ledger directly, so it has to stand on its own.
    path = _dir(log_dir) / "drive.jsonl"
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


class LogDirMismatch(IsolationError):
    """The harness's log directory is not the one the driver keeps its state in.

    Subclasses IsolationError so it lands in the existing "refused before the
    drive" path and is recorded as a harness fault rather than a product outcome.
    """


def assert_log_dir_matches(log_dir, driver_state_dir) -> None:
    """Refuse to run unless `log_dir` is where the driver keeps its lease state.

    This is the check whose absence voided S4a and S4b. The harness takes
    `log_dir` as a parameter while the driver *hardcodes* its state directory
    (`lease.LAST_PAGE_PATH`), so pointing `log_dir` anywhere else does not fail
    loudly -- `quarantine_last_page` finds no file to rename, `assert_pane_idle`
    reads no events, and both report success while isolating nothing. The driver
    meanwhile keeps re-attaching to whatever tab the previous run left, and a
    no-url test inherits that page instead of its own.

    Comparing resolved paths (not string forms) keeps a trailing slash or a `..`
    segment from reading as a mismatch.
    """
    try:
        wanted = Path(driver_state_dir).expanduser().resolve()
        got = Path(log_dir).expanduser().resolve()
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise LogDirMismatch(f"could not resolve the isolation log_dir: {exc}") from exc
    if got != wanted:
        raise LogDirMismatch(
            f"log_dir {got} is not the driver's state directory {wanted}; "
            "quarantine and the pane-idle check would silently isolate nothing"
        )


def _driver_last_page(driver_state_dir) -> Path:
    return Path(driver_state_dir) / LAST_PAGE


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


def quarantine_last_page(log_dir=None, *, driver_state_dir=None) -> dict | None:
    """Clear the remembered tab and return what it was.

    Renamed rather than deleted: if a run then re-attaches to nothing and fails,
    the previous run's target is still on disk to diagnose the attach with. The
    rename is the isolation -- a driver that reads LAST_PAGE finds nothing -- and
    the retained file is the forensics.

    Loud when the file is absent *while the driver's real one exists*. That
    combination is the signature of a misdirected `log_dir`: the old behaviour
    returned None, which reads as "nothing to quarantine" and let the run proceed
    into a tab it had not isolated. Absence everywhere is still a legitimate
    first run.
    """
    log_dir = _dir(log_dir)
    path = log_dir / LAST_PAGE
    if not path.is_file():
        if driver_state_dir is not None and _driver_last_page(driver_state_dir).is_file():
            raise LogDirMismatch(
                f"no {LAST_PAGE} in {log_dir}, but the driver has one at "
                f"{_driver_last_page(driver_state_dir)}: log_dir is misdirected, so "
                "quarantine isolated nothing"
            )
        return None
    previous = read_last_page(log_dir)
    try:
        path.replace(log_dir / f"{LAST_PAGE}.quarantined")
    except OSError:
        # An unquarantined page is worth reporting, not worth crashing a run over:
        # the caller still gets the previous target in the return value.
        return previous
    return previous


def isolation_report(log_dir=None, *, driver_state_dir=None) -> dict:
    """What the checklist actually observed, for the run record.

    `pane_idle` is computed, not asserted. It used to be a literal True, so a
    record could claim the pane was idle on the strength of a check that had read
    nothing at all -- which is what happened for every row in the window that
    voided S4a/S4b. Two conditions must hold: the directory has to look like a
    driver log directory (a `drive.jsonl` is present, so there were events to
    read), and no run may be left open.
    """
    log_dir = _dir(log_dir)
    previous = read_last_page(log_dir)
    looks_like_a_log_dir = (log_dir / "drive.jsonl").is_file()
    matches = None
    if driver_state_dir is not None:
        try:
            matches = (log_dir.expanduser().resolve()
                       == Path(driver_state_dir).expanduser().resolve())
        except (OSError, RuntimeError, TypeError, ValueError):
            matches = False
    return {
        "pane_idle": bool(looks_like_a_log_dir) and not open_run_ids(log_dir),
        "log_dir_is_driver_state_dir": matches,
        "log_dir_has_driver_log": looks_like_a_log_dir,
        "quarantined_target": (previous.get("targetId") if previous else None),
        "quarantined_url": (previous.get("url") if previous else None),
    }