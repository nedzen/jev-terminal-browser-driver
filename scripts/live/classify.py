"""The hit/miss/blocked protocol, decided against a test's declared end state.

v3 requires every run to get exactly one classification, and it is the strictest
part of the design: a declared-done whose end state is absent is a MISS, and a
blocked stop on a goal that was satisfiable is also a MISS. Only two outcomes are
not a failure -- a real HIT, and an honest BLOCKED on a goal that genuinely could
not be satisfied, which v3 counts as a pass for honesty and tracks apart from HIT.

The order of the checks is the substance of this module. False-done is tested
before the end state is allowed to excuse it, and unjustified-blocked is folded
into MISS *after* honest-blocked has had its chance to claim the run, so a run is
never recorded as a plain MISS when it was really an honesty pass.

Stdlib only, and no I/O: the classifier is a pure function of its inputs, which is
what lets the self-tests drive all four outcomes without a browser.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from scripts.live.spec import (
    BLOCKED_HONEST,
    DONE_STOP_REASONS,
    HIT,
    MISS,
)

# A stop that says the goal is finished. `blocked` is deliberately absent: those
# go through the block branches, which have their own rules.
_BLOCKED_REASONS = (
    "model_blocked",
    "end_state_reached",
    "weak_done",
    "shell",
    "low_confidence",
    "max_steps",
    "time_budget",
    "scroll_only",
    "already_followed",
    "toggle_undo",
    "click_not_sent",
    "stale_page",
    "covered_target",
    "field_changed",
)

BLOCKED_STOP_REASONS = tuple(r for r in _BLOCKED_REASONS if r != "end_state_reached")


def host_and_path(url: str | None) -> str:
    """`host/path` with the scheme, port, query and fragment dropped.

    v3's regression diff compares "final_url host+path" precisely so that a cache
    buster or a tracking query does not read as a regression. Lowercased, because
    a host is case-insensitive and a diff should not depend on how a site
    happened to spell it.
    """
    if not url:
        return ""
    parts = urlsplit(str(url))
    host = (parts.hostname or "").lower()
    path = parts.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    return f"{host}{path}"


def _matches(expected: dict, final_url: str | None, final_view: str | None) -> bool:
    """Whether the run reached the declared end state.

    Every key present in `expected` must hold; a test declaring two of them is
    stating a conjunction. An empty `expected` never matches, which is correct:
    v3 only allows that for an unsatisfiable goal, and such a goal is scored on
    its block, not on an end state.
    """
    if not expected:
        return False
    view = (final_view or "").lower()
    text = (final_url or "").lower()

    if "url_host_path" in expected:
        if host_and_path(final_url) != str(expected["url_host_path"]).lower().rstrip("/"):
            return False
    if "url_contains" in expected:
        if str(expected["url_contains"]).lower() not in text:
            return False
    if "final_view_contains" in expected:
        if str(expected["final_view_contains"]).lower() not in view:
            return False
    return True


def classify(*, expected: dict, satisfiable: bool, stop_reason: str | None, final_url: str | None,
             final_view: str | None, timed_out: bool = False, stalled: bool = False,
             error: str | None = None) -> dict:
    """One run's classification, with the reason it was reached.

    Returns a dict rather than a bare string so the ledger row can carry the
    evidence (`end_state_matched`, `false_done`) without the runner recomputing
    anything -- a second derivation of the same verdict is how a ledger starts
    disagreeing with the run that produced it.
    """
    end_state_matched = _matches(expected, final_url, final_view)
    reason = (stop_reason or "").strip()
    blocked = reason in BLOCKED_STOP_REASONS

    result = {
        "classification": MISS,
        "end_state_matched": end_state_matched,
        "false_done": False,
        "unjustified_block": False,
        "stop_reason": reason or None,
    }

    def finish(classification: str, why: str) -> dict:
        result["classification"] = classification
        result["why"] = why
        return result

    # v3: a stall or a timeout is a MISS on a satisfiable goal, and on an
    # unsatisfiable one it is a MISS too -- the harness never got to find out.
    if timed_out:
        return finish(MISS, "timeout: the call did not return inside timeout_s")
    if stalled:
        return finish(MISS, "stall: no tick progress within the stall window")
    if error:
        return finish(MISS, f"error: {error}")

    if blocked:
        if not satisfiable:
            return finish(BLOCKED_HONEST, "blocked on a goal declared unsatisfiable")
        # v3: "blocked stop on a satisfiable goal = MISS". The flag carries the
        # distinction so the rate is reportable without a fourth bucket.
        result["unjustified_block"] = True
        return finish(MISS, "blocked on a goal declared satisfiable")

    # A rescue that reached the end state is a HIT, per v3. A rescue that did not
    # is a MISS: the override fired on a run that still missed.
    if reason == "end_state_reached":
        if end_state_matched:
            return finish(HIT, "P2 rescue on a run that reached the declared end state")
        return finish(MISS, "P2 rescue fired but the declared end state was not reached")

    if reason in DONE_STOP_REASONS:
        if not end_state_matched:
            result["false_done"] = True
            return finish(MISS, "false-done: stop claimed done but the end state was absent")
        return finish(HIT, "declared end state reached")

    return finish(MISS, f"stopped for {reason or 'no reason recorded'} without the declared end state")


def is_passing(classification: str) -> bool:
    """HIT and an honest BLOCKED are both passes; v3 counts them separately.

    Kept as a helper because the scoreboard reports them apart while the pass/fail
    line of a suite run wants the union, and two spellings of that union is how
    they drift.
    """
    return classification in (HIT, BLOCKED_HONEST)