"""The stdlib-only core both sides of the subprocess boundary share.

Hermes loads plugin/ without jev_driver; the driver in jev_driver/ imports this
package to build its tick rows. So the dependency runs one way only, and nothing
here may import outside the standard library and plugin/ itself — a test walks
the ASTs to keep it that way.

Three modules, three jobs:

- :mod:`plugin.core.env` — where the driver lives, whether this machine can drive.
- :mod:`plugin.core.budgets` — what a number may be, rejected rather than clamped.
- :mod:`plugin.core.result` — the tick row and the agent result, from one field table.
"""

from __future__ import annotations

from .budgets import (
    DEFAULT_MAX_STEPS,
    DEFAULT_TIMEOUT,
    MAX_STEPS_CAP,
    TIME_BUDGET_CAP,
    TIMEOUT_CAP,
    budget,
    deny_names,
)
from .env import (
    LOG_DIR,
    check_drive,
    driver_home,
    has_decision_key,
    log_handler_event,
    terminal_browser_installed,
)
from .result import (
    PAGE_TEXT_LIMIT,
    PASSTHROUGH,
    Field,
    build_tick_row,
    compact_result,
    parse_json_lines,
    stopped_reason,
    sum_usage,
)

__all__ = [
    "DEFAULT_MAX_STEPS",
    "DEFAULT_TIMEOUT",
    "LOG_DIR",
    "MAX_STEPS_CAP",
    "PAGE_TEXT_LIMIT",
    "PASSTHROUGH",
    "TIMEOUT_CAP",
    "TIME_BUDGET_CAP",
    "Field",
    "build_tick_row",
    "budget",
    "check_drive",
    "compact_result",
    "deny_names",
    "driver_home",
    "has_decision_key",
    "log_handler_event",
    "parse_json_lines",
    "stopped_reason",
    "sum_usage",
    "terminal_browser_installed",
]