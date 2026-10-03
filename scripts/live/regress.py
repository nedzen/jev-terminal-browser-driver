"""Regression diff against the last green slice, with v3's tolerances.

v3 fixes the compared fields: stop_reason, ticks, bytes/call, final_url host+path
and spec hash. Each is compared for a different reason, so each has its own rule
rather than one "changed" flag:

- stop_reason and final_url host+path: any change is reported, because a run that
  ends somewhere else is a finding whatever the size of the movement.
- spec_hash: reported, never a regression by itself. A prompt revision changes it
  on every field at once and reading that as 5 simultaneous regressions is how a
  real one gets lost in the noise.
- ticks: a count, so v3 treats counts as ranges. Drift is reported, not failed.
- bytes/call: the only gated field. ±10% with the 2-consecutive-breach rule --
  a single excursion is investigate-only, because churn moves it and calling that
  a regression trains people to ignore the gate.

Stdlib only.
"""

from __future__ import annotations

from scripts.live.classify import host_and_path

BYTES_TOLERANCE = 0.10
CONSECUTIVE_BREACHES_FOR_REGRESSION = 2

# Fields compared by exact match. A change is a finding, not a verdict.
# `final_url` is compared on host+path only; see `host_and_path`.
EXACT_FIELDS = ("stop_reason",)


def _pct_change(before: float, after: float) -> float | None:
    """Relative movement, or None when there is no baseline to be relative to."""
    if before in (None, 0):
        return None
    try:
        return round((float(after) - float(before)) / float(before), 4)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def diff_run(current: dict, baseline: dict | None) -> dict:
    """One run against its baseline. `baseline=None` means a first run.

    Returns the full comparison even when nothing breached, because a green diff
    that cannot be inspected is how a silently-wrong baseline survives.
    """
    if not baseline:
        return {
            "has_baseline": False,
            "regressions": [],
            "info": {},
            "bytes_breach": False,
            "note": "first run for this test: nothing to compare",
        }

    findings = []
    info = {}

    for field in EXACT_FIELDS:
        was, now = baseline.get(field), current.get(field)
        if was != now:
            findings.append({"field": field, "baseline": was, "current": now, "kind": "changed"})

    base_url = host_and_path(baseline.get("final_url"))
    now_url = host_and_path(current.get("final_url"))
    if base_url != now_url:
        findings.append(
            {"field": "final_url", "baseline": base_url, "current": now_url, "kind": "changed"}
        )

    for field in ("ticks", "spec_hash"):
        was, now = baseline.get(field), current.get(field)
        if was != now:
            info[field] = {"baseline": was, "current": now}

    base_bytes = baseline.get("bytes_per_call")
    now_bytes = current.get("bytes_per_call")
    change = _pct_change(base_bytes, now_bytes)
    breach = change is not None and abs(change) > BYTES_TOLERANCE
    bytes_info = {"baseline": base_bytes, "current": now_bytes, "change": change, "breach": breach}
    if breach:
        findings.append(
            {"field": "bytes_per_call", "baseline": base_bytes, "current": now_bytes,
             "kind": "outside_tolerance", "change": change}
        )

    return {
        "has_baseline": True,
        "regressions": findings,
        "info": {**info, "bytes_per_call": bytes_info},
        "bytes_breach": breach,
    }


def is_regression(diff: dict, *, consecutive_breaches: int = 1) -> bool:
    """Whether a diff counts as a regression, applying the 2-breach rule.

    A bytes excursion needs two consecutive breaches before it is a regression;
    everything else in the diff is already a finding and needs no repetition.
    """
    if not diff.get("has_baseline"):
        return False
    non_bytes = [f for f in diff.get("regressions", []) if f["field"] != "bytes_per_call"]
    if non_bytes:
        return True
    if diff.get("bytes_breach"):
        return consecutive_breaches >= CONSECUTIVE_BREACHES_FOR_REGRESSION
    return False