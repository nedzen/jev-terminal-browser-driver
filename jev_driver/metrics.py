"""Per-run aggregate counters: counts and timings, in one small file.

The run log records what happened, event by event, so comparing two runs
means grepping JSONL (upstream PR browser-use/jev-ultrafast#141). This module
keeps the numbers that comparison actually needs -- how many model calls, how
many browser inputs, how long each phase took -- so a run can be summarised
without reading its log.

Secret-free by construction, not by scrubbing: no record method accepts page
text, a URL, a label, or a key. The only strings that can reach a snapshot
come from the closed vocabularies below (``_KINDS``, ``_STATUSES``,
``_ERROR_KINDS``) plus the *type name* of an exception, never its message.
There is no door for a hostile page to smuggle anything through.

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
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from . import runlog

SCHEMA = 1  # snapshot layout; bump if a key is renamed or removed
METRICS_NAME = "metrics.json"

# Closed vocabularies. Anything unrecognised collapses to the last entry, so
# the snapshot has a fixed shape whatever the page or the model hands us.
_KINDS = ("click", "enter", "fill", "other", "scroll", "select", "wait")
_PHASES = ("observe", "act", "fresh", "wait")  # report order: what a reader compares
_STATUSES = ("blocked", "done", "error", "predicted", "ready", "unknown")
_ERROR_KINDS = ("budget", "cancelled", "model", "other", "runtime", "stale", "timeout", "value")
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


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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
    """Counters and timings for one run. Owned by the agent, never global."""

    def __init__(self) -> None:
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
        self._finished_at: str | None = None

    # -- recorders -----------------------------------------------------
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

    # -- results -------------------------------------------------------
    def finish(self, status=None, error=None) -> dict:
        """Freeze the run's outcome and return the snapshot.

        The first call wins: a later call with a different status or a
        different error changes nothing, so ``close()`` twice is harmless.
        """
        if not self._finished:
            self._finished = True
            self._status = _status(status)
            self._error = _error_kind(error)
            self._finished_at = _stamp()
        return self.snapshot()

    def snapshot(self) -> dict:
        """A JSON-safe copy. Every phase is present, so consumers can diff runs."""
        return {
            "schema": SCHEMA,
            "status": self._status,
            "error": self._error,
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
        a failed write leaves the previous run's snapshot readable.
        """
        try:
            target = Path(path) if path is not None else metrics_path()
            target.parent.mkdir(parents=True, exist_ok=True)
            scratch = target.with_name(target.name + ".tmp")
            scratch.write_text(
                json.dumps(self.snapshot(), ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n",
                encoding="utf-8",
            )
            scratch.replace(target)
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
