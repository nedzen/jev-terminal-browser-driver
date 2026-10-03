"""Budget validation: reject rather than clamp. Stdlib only."""

from __future__ import annotations

import re

MAX_STEPS_CAP = 30
TIMEOUT_CAP = 900
TIME_BUDGET_CAP = 900
DEFAULT_MAX_STEPS = 12
DEFAULT_TIMEOUT = 300


def budget(value, lo: int, hi: int, name: str, default: int) -> int:
    """Validate a budget; reject rather than clamp. None → default."""
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
    """Compiled denylist patterns, or a pre-spawn rejection for bad input."""
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