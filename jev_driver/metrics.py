"""Per-run counters and timings. Secret-free by construction (closed vocabularies).

``metrics.json`` is overwritten at close; ``run_id``/``started_at``/``goal_hash``
make a stale file detectable. ``write`` never raises.
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
    "end_state_reached",
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
    """Truncated sha256 of the goal, or None — never stores the goal text."""
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
    """Per-run counters/timings with identity (``run_id``, ``started_at``, ``goal_hash``)."""

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
        """Attach goal digest (first call wins). Never raises."""
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
        """Record stop reason as a closed-vocabulary token."""
        stop = _stop_reason(reason)
        if stop is not None:
            self._stop = stop

    # -- results -------------------------------------------------------
    def finish(self, status=None, error=None) -> dict:
        """Freeze outcome (first call wins); exception kind outranks stop reason."""
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
        """Atomic write beside the run log. Never raises; True if it landed."""
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
            _book(metrics.record_phase, phase, elapsed)

    return timed


def instrument_browser(browser, metrics) -> None:
    """Wrap browser methods to record phase times. Idempotent; no-op if missing."""
    if browser is None or metrics is None:
        return
    try:
        # vars() — getattr on a Mock invents attributes.
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
