"""Per-run aggregate counters: counts and timings, in one small file.

The run log records what happened, event by event, so comparing two runs
means grepping JSONL (upstream PR browser-use/jev-ultrafast#141). This module
keeps the numbers that comparison actually needs -- how many model calls, how
many browser inputs, how long each phase took -- so a run can be summarised
without reading its log.

Secret-free by construction, not by scrubbing: no record method accepts page
text, a URL, a label, or a key. The only strings that can reach a snapshot
come from the closed vocabularies below (``_KINDS``, ``_STATUSES``,
``_ERROR_KINDS``, ``_STOP_REASONS``), plus the *type name* of an exception,
never its message, plus the run's own identity (see ``bind_run``). There is no
door for a hostile page to smuggle anything through.

Run identity, and why the file needs it
---------------------------------------
``metrics.json`` is one file, overwritten at close, so a run that was killed
before closing leaves the *previous* run's numbers sitting there looking
current. Three identity fields travel with every snapshot — ``run_id``,
``started_at`` and ``goal_hash`` — and the same ``run_id`` is written into the
run's own ``event: run`` line, so a stale file is provably stale: its ``run_id``
is not the one the log says the last run had. ``goal_hash`` is a digest, never
the goal: a goal can hold anything a user typed, and the identity only has to
say "the same goal".

``Metrics`` is backend-independent: no browser, no CDP, no network, and a
single instance owned by the agent. ``instrument_browser`` is the one
backend-aware piece -- it times the browser's own methods in place, so the
phases the base loop spends inside its own code are counted too. It only
delegates and re-raises, and every recorder call is wrapped so a metrics
failure can never reach the caller.

``Metrics.write`` never raises: a metrics failure must not break a run.
"""

from __future__ import annotations

import functools
import hashlib
import json
import os
import time
import uuid
from pathlib import Path

from . import runlog

SCHEMA = 1  # snapshot layout; bump if a key is renamed or removed
METRICS_NAME = "metrics.json"
GOAL_HASH_CHARS = 16

# Closed vocabularies. Anything unrecognised collapses to the last entry, so
# the snapshot has a fixed shape whatever the page or the model hands us.
_KINDS = ("click", "enter", "fill", "other", "scroll", "select", "wait")
_PHASES = ("observe", "act", "fresh", "wait")  # report order: what a reader compares
_STATUSES = ("blocked", "done", "error", "predicted", "ready", "unknown")
_ERROR_KINDS = ("budget", "cancelled", "model", "other", "runtime", "stale", "timeout", "value")
# Why a run stopped, as the driver spells it: the same tokens `readiness.REASON_WHY`
# explains, minus the one that is only ever a `why` ("scroll_only"). Recorded as a
# token, never a sentence: "budget ran out" is prose, and a snapshot that carried
# only prose would make a deadline stop indistinguishable from any other blocked run.
_STOP_REASONS = (
    "already_followed",
    "click_not_sent",
    "covered_target",
    "extract",
    "field_changed",
    "max_steps",
    "model_blocked",
    "other",
    "shell",
    "stale_page",
    "time_budget",
    "toggle_undo",
    "unsupported",
    "weak_done",
)
# The one stop reason that is itself a deadline rather than a failure, so it is
# reported as a budget kind instead of as no error at all.
_BUDGET_STOP = "time_budget"
# Exception *type names* only, matched against these hints in order.
_ERROR_HINTS = (
    ("timeout", "timeout"),
    ("budget", "budget"),
    ("stale", "stale"),
    ("cancel", "cancelled"),
    ("interrupt", "cancelled"),
    ("model", "model"),
    ("provider", "model"),
    ("value", "value"),
    ("runtime", "runtime"),
)
# Browser method -> phase. ``observe`` reaches ``_observe_once`` and hydration,
# ``act`` dispatches the input, ``fresh`` is the staleness probe, ``sleep`` waits.
_METHOD_PHASES = {
    "observe": ("observe", "_observe_once"),
    "act": ("act",),
    "fresh": ("fresh",),
    "wait": ("sleep",),
}
_ATTACHED = "_jev_metrics"


def _number(value) -> float:
    """A finite, non-negative float. NaN, infinity, negatives and junk all become 0.0."""
    try:
        out = float(value)
    except (TypeError, ValueError):
        return 0.0
    if out != out or out < 0.0 or out > 1e12:  # NaN, a negative, or an infinity
        return 0.0
    return out


def _kind(value) -> str:
    text = value.strip().lower() if isinstance(value, str) else ""
    return text if text in _KINDS else "other"


def _status(value) -> str | None:
    text = value.strip().lower() if isinstance(value, str) else ""
    return text if text in _STATUSES else ("unknown" if text else None)


def _stop_reason(value) -> str | None:
    """The stop vocabulary, or None when there was no stop to name."""
    text = value.strip().lower() if isinstance(value, str) else ""
    return text if text in _STOP_REASONS else ("other" if text else None)


def _goal_digest(goal) -> str | None:
    """A short digest of the goal, or None when there is no goal to digest.

    The goal itself never reaches a snapshot — it is free text a caller typed
    and can hold anything — so identity is a sha256 over it, truncated. Equal
    goals give equal digests, which is all a reader compares.
    """
    if goal is None:
        return None
    try:
        return hashlib.sha256(str(goal).encode("utf-8", "replace")).hexdigest()[:GOAL_HASH_CHARS]
    except Exception:
        return None


def _error_kind(error) -> str | None:
    """Classify an exception by its type name. The message is never read."""
    if error is None:
        return None
    name = type(error).__name__.lower()
    for hint, kind in _ERROR_HINTS:
        if hint in name:
            return kind
    return "other"


def _book(fn, *args, **kwargs) -> None:
    """Call a recorder, ignoring its failures. Telemetry must not break a run."""
    try:
        fn(*args, **kwargs)
    except Exception:
        return


class _Timer:
    """One running total, max and count -- the only shape a timing takes."""

    __slots__ = ("count", "total", "max")

    def __init__(self) -> None:
        self.count = 0
        self.total = 0.0
        self.max = 0.0

    def add(self, ms) -> None:
        value = _number(ms)
        self.count += 1
        self.total += value
        self.max = max(self.max, value)

    def snapshot(self, name: str) -> dict:
        return {
            name: self.count,
            "total_ms": round(self.total, 1),
            "max_ms": round(self.max, 1),
            "avg_ms": round(self.total / self.count, 1) if self.count else 0.0,
        }


class Metrics:
    """Counters and timings for one run. Owned by the agent, never global.

    Every instance is born with an identity — a ``run_id``, the moment it was
    constructed and, once the caller supplies one, a digest of the goal — so two
    snapshots on the same disk can always be told apart.
    """

    def __init__(self) -> None:
        self._run_id = uuid.uuid4().hex
        self._started_at = runlog._stamp()
        self._goal_hash: str | None = None
        self._jev = _Timer()
        self._text = _Timer()
        self._phases = {phase: _Timer() for phase in _PHASES}
        self._kinds: dict[str, list[int]] = {}
        self._attempted = 0
        self._succeeded = 0
        self._failed = 0
        self._stale = 0
        self._startup_ms = 0.0
        self._cleanup_ms: float | None = None
        self._finished = False
        self._status: str | None = None
        self._error: str | None = None
        self._stop: str | None = None
        self._finished_at: str | None = None

    # -- recorders -----------------------------------------------------
    def bind_run(self, goal=None) -> None:
        """Name the run this snapshot belongs to. First call wins, like ``finish``.

        The goal is digested, never stored: identity only has to say "the same
        goal", and a goal is free text that can carry anything. A caller that
        never binds one still gets a ``run_id`` and a ``started_at``, which is
        what makes a stale ``metrics.json`` detectable.
        """
        if self._goal_hash is None:
            self._goal_hash = _goal_digest(goal)

    def record_jev(self, latency_ms=None) -> None:
        """One decision call, with the provider's own latency."""
        self._jev.add(latency_ms)

    def record_text_helper(self, latency_ms=None) -> None:
        """One text-helper call. A cached value is not a call and is not recorded."""
        self._text.add(latency_ms)

    def record_phase(self, phase, ms) -> None:
        """One outermost observation or input. An unknown phase is ignored."""
        timer = self._phases.get(phase) if isinstance(phase, str) else None
        if timer is not None:
            timer.add(ms)

    def record_action(self, kind=None, *, ok: bool | None = None) -> None:
        """Book a browser input: the attempt with ``ok=None``, then its outcome."""
        row = self._kinds.setdefault(_kind(kind), [0, 0, 0])
        if ok is None:
            self._attempted += 1
            row[0] += 1
            return
        if ok:
            self._succeeded += 1
            row[1] += 1
        else:
            self._failed += 1
            row[2] += 1

    def record_stale(self, count: int = 1) -> None:
        """A page changed under a decision and the input was refused."""
        self._stale += int(_number(count))

    def record_startup(self, ms) -> None:
        """Opening the tab and reading the first page, before any decision."""
        self._startup_ms = _number(ms)

    def record_cleanup(self, ms) -> None:
        """Closing the browser. Recorded even when closing raises."""
        self._cleanup_ms = _number(ms)

    def record_stop(self, reason=None) -> None:
        """Why the run stopped, as one vocabulary token.

        Recorded through the closed vocabulary, never as the driver's sentence:
        a ``time_budget`` stop has to be readable as ``time_budget`` without
        anyone parsing prose, and an unrecognised reason collapses to
        ``other`` rather than widening the snapshot's shape.
        """
        stop = _stop_reason(reason)
        if stop is not None:
            self._stop = stop

    # -- results -------------------------------------------------------
    def finish(self, status=None, error=None) -> dict:
        """Freeze the run's outcome and return the snapshot.

        The first call wins: a later call with a different status or a
        different error changes nothing, so ``close()`` twice is harmless.
        An exception's kind always outranks the stop reason, so a stale page
        that ended the run is still reported as ``stale``; a deadline stop with
        no exception behind it is recorded as ``budget`` rather than as the
        ``null`` that means "no error kind was ever determined".
        """
        if not self._finished:
            self._finished = True
            self._status = _status(status)
            self._error = _error_kind(error) or ("budget" if self._stop == _BUDGET_STOP else None)
            self._finished_at = runlog._stamp()
        return self.snapshot()

    def snapshot(self) -> dict:
        """A JSON-safe copy. Every phase is present, so consumers can diff runs."""
        return {
            "schema": SCHEMA,
            "run_id": self._run_id,
            "goal_hash": self._goal_hash,
            "started_at": self._started_at,
            "status": self._status,
            "error": self._error,
            "stop_reason": self._stop,
            "finished": self._finished,
            "finished_at": self._finished_at,
            "jev": self._jev.snapshot("calls"),
            "actions": {
                "attempted": self._attempted,
                "succeeded": self._succeeded,
                "failed": self._failed,
                "by_kind": {
                    kind: {"attempted": row[0], "succeeded": row[1], "failed": row[2]}
                    for kind, row in sorted(self._kinds.items())
                },
            },
            "phases": {phase: self._phases[phase].snapshot("count") for phase in _PHASES},
            "stale": self._stale,
            "text_helper": self._text.snapshot("calls"),
            "startup_ms": round(self._startup_ms, 1),
            "cleanup_ms": None if self._cleanup_ms is None else round(self._cleanup_ms, 1),
        }

    def write(self, path=None) -> bool:
        """Write the snapshot next to the run log. Never raises; True if it landed.

        Written to a sibling and renamed, so a reader never sees half a file and
        a failed write leaves the previous run's snapshot readable. The scratch
        name carries the pid and a uuid: a fixed ``.tmp`` is only atomic while
        there is one writer, and two runs (an MCP server driving one while a
        second process drives another) would otherwise rename each other's
        half-written body into place.
        """
        try:
            target = Path(path) if path is not None else metrics_path()
            target.parent.mkdir(parents=True, exist_ok=True)
            scratch = target.with_name(f"{target.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
            try:
                scratch.write_text(
                    json.dumps(self.snapshot(), ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
                    + "\n",
                    encoding="utf-8",
                )
                scratch.replace(target)
            finally:
                # A failed or renamed scratch leaves nothing behind to be mistaken
                # for a snapshot; a scratch we cannot remove is not worth raising over.
                try:
                    scratch.unlink()
                except OSError:
                    pass
            return True
        except Exception:
            return False


def metrics_path(jsonl_path=None) -> Path:
    """The run log's own directory, so the two files stay together."""
    base = Path(jsonl_path) if jsonl_path is not None else runlog.JSONL_PATH
    return base.parent / METRICS_NAME


def _action_kind(args, kwargs):
    action = kwargs.get("action")
    if action is None and args:
        action = args[0]
    return action.get("kind") if isinstance(action, dict) else None


def _timed(busy, metrics, phase, original, *, with_action):
    """Wrap one browser method: time the outermost call, book the outcome."""

    @functools.wraps(original)
    def timed(*args, **kwargs):
        if phase in busy:
            # Nested inside the same phase (observe -> _observe_once): count it once.
            return original(*args, **kwargs)
        busy.add(phase)
        kind = _action_kind(args, kwargs) if with_action else None
        if with_action:
            _book(metrics.record_action, kind)
        ok = False
        started = time.perf_counter()
        try:
            result = original(*args, **kwargs)
            ok = True
            return result
        finally:
            elapsed = (time.perf_counter() - started) * 1000
            busy.discard(phase)
            if with_action:
                _book(metrics.record_action, kind, ok=ok)
            # Phases overlap: act's total includes the fresh probe it triggers.
            _book(metrics.record_phase, phase, elapsed)

    return timed


def instrument_browser(browser, metrics) -> None:
    """Time a browser's own methods in place. Idempotent, and a no-op without a browser.

    Phase times come from the methods themselves rather than from the driver's
    call sites, so the reads and inputs the base loop performs internally are
    counted too. Nothing about the returned values changes: each wrapper calls
    the original, returns its result, and re-raises whatever it raised.
    """
    if browser is None or metrics is None:
        return
    try:
        # vars(), not getattr(): a Mock invents any attribute asked of it, which
        # would read as "already instrumented" and leave the browser untouched.
        if vars(browser).get(_ATTACHED) is not None:
            return
    except TypeError:
        return  # an object that cannot hold attributes stays uninstrumented
    busy: set[str] = set()
    for phase, names in _METHOD_PHASES.items():
        for name in names:
            original = getattr(browser, name, None)
            if not callable(original):
                continue
            try:
                wrapped = _timed(busy, metrics, phase, original, with_action=phase == "act")
                setattr(browser, name, wrapped)
            except Exception:
                continue  # a read-only or exotic object: skip this method, keep the run
    _book(setattr, browser, _ATTACHED, metrics)
