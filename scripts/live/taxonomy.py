"""The v3.1 outcome taxonomy: eight classes, two severities, and the closed lists.

v3.1 turned the old four-way hit/miss/blocked into a *partition* -- every run gets
exactly one of eight classes -- with a two-level severity. The partition is the
whole point, so the constants and the one function that maps a class to its
severity live here alone; nothing else is allowed to invent a class name.

Two spec tensions are resolved in this module rather than left to each caller:

- **Is BLOCKED-unjustified its own class or a MISS?** v3.1 line 163 lists
  "unjustified block" among MISS's causes, and line 169 names it a class. The
  resolutions settle it: "MISS needs completed run", and a blocked run did not
  complete. So it is its own class, and the scoreboard reports it under sev-2.
- **Do STALL and CRASH carry a severity?** They are terminal, not passes, and
  v3.1 says sev-2 is "all other non-hits". They get sev-2. Being terminal and
  being severe are different axes, and collapsing them would hide a CRASH inside
  a quality bucket.

Severity is two-level and closed: sev-1 is FALSE-DONE alone (the integrity class,
which must stay 0), sev-2 is every other non-hit, and there is no sev-3. `severity_of`
raises rather than defaulting, so a class added without a severity cannot ship.

Stdlib only.
"""

from __future__ import annotations

HIT = "HIT"
HIT_RECOVERED = "HIT-recovered"
MISS = "MISS"
FALSE_DONE = "FALSE-DONE"
BLOCKED_HONEST = "BLOCKED-honest"
BLOCKED_UNJUSTIFIED = "BLOCKED-unjustified"
STALL = "STALL"
CRASH = "CRASH"

# The partition, in reporting order: passes, then quality failures, then the
# sev-1 integrity class, then the terminal pair.
OUTCOME_CLASSES = (
    HIT,
    HIT_RECOVERED,
    MISS,
    FALSE_DONE,
    BLOCKED_HONEST,
    BLOCKED_UNJUSTIFIED,
    STALL,
    CRASH,
)

# Terminal classes are not subtypes of anything: a stalled run is not a MISS, and
# a crashed run is not a product outcome at all.
TERMINAL_CLASSES = (STALL, CRASH)

# Classes that are passes. HIT-recovered counts as a pass with a recorded cost.
PASS_CLASSES = (HIT, HIT_RECOVERED)

SEV_1 = "sev-1"
SEV_2 = "sev-2"

# Closed by the spec. A class with no severity is a pass, and passes carry no
# severity rather than a zero one, so "sev-2 rate" cannot quietly include hits.
_SEVERITY = {
    HIT: None,
    HIT_RECOVERED: None,
    FALSE_DONE: SEV_1,
    MISS: SEV_2,
    BLOCKED_HONEST: SEV_2,
    BLOCKED_UNJUSTIFIED: SEV_2,
    STALL: SEV_2,
    CRASH: SEV_2,
}

# v3.1 "Per-run dims": failure cause is a closed list plus other(explain).
FAILURE_CAUSES = (
    "wrong-end-state",
    "wrong-action",
    "false-done",
    "stall",
    "timeout",
    "unjustified-block",
    "crash",
    "harness-error",
    "site-changed",
    "session-expired",
)
OTHER_CAUSE = "other"

# Anomaly tags are structured so they can be counted across a suite; the free-text
# note is where anything that does not fit goes.
ANOMALY_TAGS = (
    "false-done",
    "unjustified-block",
    "stall",
    "crash",
    "timeout",
    "consequential-element",
    "field-changed-loop",
    "jev-staleness",
    "shell-page",
    "low-confidence",
    "hydration-stall",
    "session-expired",
    "challenge-abort",
    "rate-limited",
    "site-changed",
)

# Matrix caps: mandatory cells gate, optional cells never gate.
CAP_MANDATORY = "mandatory"
CAP_OPTIONAL = "optional"
MATRIX_CAPS = (CAP_MANDATORY, CAP_OPTIONAL)

# Consequential actions/elements are denied on R-tier regardless of intent:
# "even targeting a Buy/Post/Delete is a logged event; 0 expected".
CONSEQUENTIAL_ACTIONS = ("buy", "sell", "trade", "post", "publish", "delete", "submit_order", "place_order")
CONSEQUENTIAL_ELEMENTS = ("buy", "sell", "trade", "post", "publish", "delete", "submit", "order")


class TaxonomyError(ValueError):
    """A value outside a closed list, or a class with no severity."""


def severity_of(outcome_class: str) -> str | None:
    """The severity of a class, or None for a pass.

    Raises on an unknown class rather than returning None, because None means "a
    pass" here and a typo would otherwise report a failure as severity-free.
    """
    try:
        return _SEVERITY[outcome_class]
    except KeyError:
        raise TaxonomyError(f"unknown outcome class {outcome_class!r}") from None


def is_terminal(outcome_class: str) -> bool:
    return outcome_class in TERMINAL_CLASSES


def is_pass(outcome_class: str) -> bool:
    return outcome_class in PASS_CLASSES


def validate_failure_cause(cause: str | None, explanation: str | None = None) -> str | None:
    """Check a failure cause against the closed list.

    `other` is legal only with an explanation, which is the whole mechanism by
    which the list stays closed while new causes are still recordable. The
    explanation is a parameter rather than something looked up here, because this
    function cannot see the manifest -- without it `other` would be permanently
    unusable, which is the opposite of what the escape hatch is for.
    """
    if cause is None:
        return None
    if cause == OTHER_CAUSE:
        if not (explanation or "").strip():
            raise TaxonomyError("failure cause 'other' requires an explanation")
        return cause
    if cause not in FAILURE_CAUSES:
        raise TaxonomyError(f"unknown failure cause {cause!r}; use 'other' with an explanation")
    return cause


def validate_anomaly_tags(tags) -> tuple[str, ...]:
    """Check anomaly tags against the closed list, de-duplicated, order preserved."""
    seen: list[str] = []
    for tag in tags or ():
        if tag not in ANOMALY_TAGS:
            raise TaxonomyError(f"unknown anomaly tag {tag!r}")
        if tag not in seen:
            seen.append(tag)
    return tuple(seen)