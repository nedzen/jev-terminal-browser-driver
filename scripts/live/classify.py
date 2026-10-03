"""The v3.1 partition: exactly one outcome class per run, with severity attached.

The judgement order is the substance here, and it is fixed:

1. CRASH and STALL are terminal and are decided first, because a run that never
   produced an end state cannot be scored against one. v3.1 resolution D: MISS
   requires a completed run, so these are not MISS subtypes.
2. FALSE-DONE is decided next, before a stop reason is allowed to excuse it.
3. A blocked stop is honest or unjustified depending on satisfiability.
4. Only then does a completed run become HIT, HIT-recovered or MISS.

`human_judged` is the other thing this module is careful about. Where a manifest
declares no machine predicate, the harness physically cannot tell HIT from
FALSE-DONE: both present a `done` stop with an end state only a person can
confirm. Guessing there would put a possible sev-1 into the pass column, so the
run is left *unclassified* with `needs_human_verdict` set instead.

Stdlib only, and no I/O: the classifier is a pure function, which is what lets the
self-tests drive all eight classes without a browser.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from scripts.live.taxonomy import (
    BLOCKED_HONEST,
    BLOCKED_UNJUSTIFIED,
    CRASH,
    FALSE_DONE,
    HIT,
    HIT_RECOVERED,
    MISS,
    PASS_CLASSES,
    STALL,
    severity_of,
    validate_failure_cause,
)

# The rescue reason v3.1 bridges HIT-recovered from, "until a taxonomy entry
# exists" -- so the bridge is read off the reason field, not inferred.
RESCUE_REASON = "end_state_reached"

# v3.1: confidence-vs-outcome inversion needs at least this many ticks, or the
# answer is "insufficient data" rather than a number computed from three points.
INVERSION_MIN_TICKS = 4
INVERSION_INSUFFICIENT = "insufficient_data"

BLOCKED_STOP_REASONS = (
    "model_blocked",
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


def host_and_path(url: str | None) -> str:
    """`host/path`, dropping scheme, port, query and fragment, lowercased.

    v3's regression diff compares host+path so a cache buster does not read as a
    regression; lowercasing because a host is case-insensitive.
    """
    if not url:
        return ""
    parts = urlsplit(str(url))
    host = (parts.hostname or "").lower()
    path = parts.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    return f"{host}{path}"


def done_probability(decision: dict | None) -> float:
    decision = decision or {}
    probs = decision.get("operation_probabilities") or {}
    if "DONE" in probs:
        try:
            return float(probs["DONE"])
        except (TypeError, ValueError):
            return 0.0
    if decision.get("choice") != "DONE":
        return 0.0
    try:
        return float(decision.get("confidence") or 0)
    except (TypeError, ValueError):
        return 0.0


def end_state_matched(expected: dict, final_url: str | None, final_view: str | None) -> bool:
    """Whether the run reached the declared end state.

    Every predicate present must hold -- a test declaring two states a
    conjunction. `text_present` searches the page text; `final_view_contains` is
    its v3 spelling and is still honoured.
    """
    if not expected:
        return False
    if isinstance(final_view, dict):
        # Live drive results carry final_view as an object (url/title/flags);
        # match predicates against its text content, never its structure.
        final_view = " ".join(str(v) for v in final_view.values())
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
    if "text_present" in expected:
        if str(expected["text_present"]).lower() not in view:
            return False
    return True


def confidence_inversion(*, ticks: int, confidences, outcomes) -> dict:
    """Confidence-vs-outcome inversion, or an explicit refusal to compute it.

    v3.1 requires the refusal: below four ticks there is no trend to speak of, and
    a number computed from three samples is worse than no number because it looks
    like one.
    """
    if ticks < INVERSION_MIN_TICKS:
        return {"verdict": INVERSION_INSUFFICIENT, "ticks": ticks, "inverted_ticks": None}
    pairs = list(zip(confidences or (), outcomes or ()))
    if len(pairs) != ticks:
        # Mismatched streams mean the caller wired the log wrong; refuse rather
        # than compare the wrong series.
        return {"verdict": INVERSION_INSUFFICIENT, "ticks": ticks, "inverted_ticks": None}
    inverted = sum(
        1 for confidence, outcome in pairs if (confidence or 0) > 0 and not outcome
    )
    return {"verdict": "measured", "ticks": ticks, "inverted_ticks": inverted}


def classify(*, expected: dict, satisfiable: bool, stop_reason: str | None, final_url: str | None,
             final_view: str | None, timed_out: bool = False, stalled: bool = False,
             crashed: bool = False, human_judged: bool = False, waste_ticks: int | None = None,
             error: str | None = None, failure_cause: str | None = None,
             anomaly_tags=()) -> dict:
    """One run's outcome class, severity, cause and evidence.

    Returns a dict so the ledger row can carry the evidence without the runner
    re-deriving any of it: a second derivation of the same verdict is how a
    ledger starts disagreeing with the run that produced it.
    """
    reason = (stop_reason or "").strip()
    matched = end_state_matched(expected, final_url, final_view)
    result = {
        "outcome_class": None,
        "severity": None,
        "end_state_matched": matched,
        "false_done": False,
        "needs_human_verdict": False,
        "failure_cause": None,
        "anomaly_tags": list(anomaly_tags or ()),
        "stop_reason": reason or None,
        "waste_ticks": waste_ticks,
    }

    def finish(outcome_class: str, cause: str | None, why: str) -> dict:
        result["outcome_class"] = outcome_class
        result["severity"] = severity_of(outcome_class)
        result["failure_cause"] = validate_failure_cause(cause)
        result["why"] = why
        return result

    # 1. Terminal classes first: neither produced an end state to score.
    if crashed:
        return finish(CRASH, "crash", f"crash: {error or 'driver or browser exception'}")
    if timed_out:
        return finish(STALL, "timeout", f"timeout: no result inside timeout_s ({error or 'no response'})")
    if stalled:
        return finish(STALL, "stall", "stall: no tick progress within the stall window")
    if error:
        return finish(CRASH, "harness-error", f"harness error: {error}")

    # 2. A blocked stop is honest or unjustified; neither is a completed run.
    if reason in BLOCKED_STOP_REASONS:
        if not satisfiable:
            return finish(BLOCKED_HONEST, None, "blocked on a goal declared unsatisfiable")
        return finish(BLOCKED_UNJUSTIFIED, "unjustified-block",
                      "blocked on a goal declared satisfiable")

    # 3. The rescue bridge: a rescue that reached the end state is HIT-recovered,
    #    and the waste it cost is the recovery cost v3.1 asks to be recorded.
    if reason == RESCUE_REASON:
        if matched:
            result["recovery_cost_ticks"] = waste_ticks
            return finish(HIT_RECOVERED, None,
                          f"recovered via {RESCUE_REASON}; recovery cost {waste_ticks} waste-tick(s)")
        return finish(MISS, "wrong-end-state",
                      f"{RESCUE_REASON} fired but the declared end state was not reached")

    # 4. A done stop. With no machine predicate and no human ruling, the class is
    #    genuinely undecidable here, so it is left open rather than guessed.
    if reason in ("model_done", "done", "DONE"):
        if human_judged or not expected:
            result["needs_human_verdict"] = True
            result["severity"] = None
            result["why"] = "done stop on a goal with no machine predicate: needs a human verdict"
            return result
        if not matched:
            result["false_done"] = True
            return finish(FALSE_DONE, "false-done",
                          "false-done: done declared while the end state was absent")
        return finish(HIT, None, "declared end state reached")

    return finish(MISS, "wrong-end-state",
                  f"stopped for {reason or 'no reason recorded'} without the declared end state")


def is_pass(outcome_class: str | None) -> bool:
    return outcome_class in PASS_CLASSES