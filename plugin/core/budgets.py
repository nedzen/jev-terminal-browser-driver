"""Budget validation: one place that decides what a number may be.

Every adapter reaches the driver through these rules, so a cap that lives in two
places is a cap that will disagree. Validation rejects rather than clamps: the
caller is told what it asked for and refused, instead of getting a run on a
budget nobody chose.

Stdlib only. See plugin/core/result.py for why that is a hard rule.
"""

from __future__ import annotations

import re

MAX_STEPS_CAP = 30
TIMEOUT_CAP = 900
TIME_BUDGET_CAP = 900
DEFAULT_MAX_STEPS = 12
DEFAULT_TIMEOUT = 300


def budget(value, lo: int, hi: int, name: str, default: int) -> int:
    """Strict budget validation: reject (don't silently clamp) so the caller
    knows the budget it got. Missing/None falls back to the default.
    Integral floats (12.0) are accepted for JSON cross-adapter parity."""
    if value is None:
        return default
    if isinstance(value, bool):
        raise ValueError(f"{name} must be an integer {lo}..{hi}; no action executed.")
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if not isinstance(value, int) or not (lo <= value <= hi):
        raise ValueError(f"{name} must be an integer {lo}..{hi}; no action executed.")
    return value


def deny_names(value) -> list[str]:
    """The denylist as a list of patterns, or a pre-spawn rejection.

    A pattern that does not compile, an empty one (it would deny every name and
    leave the run nothing to drive), or a list that is not made of strings is a
    malformed argument, not something to clamp quietly: the caller is told
    instead of getting a browser opened against a denylist nobody asked for.
    """
    if value is None:
        return []
    bad = "deny_names must be a list of regular expressions; no action executed."
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise ValueError(bad)
    patterns = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(bad)
        try:
            re.compile(item)
        except re.error:
            raise ValueError(
                f"deny_names pattern {item[:60]!r} is not a valid regular expression; no action executed."
            ) from None
        patterns.append(item)
    return patterns