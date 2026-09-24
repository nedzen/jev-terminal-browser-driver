"""When a page is ready to decide, and when a DONE choice is believable."""

from __future__ import annotations

import re

# Lines X paints before the timeline exists. Matching is case-insensitive.
_CHROME_LINES = {
    "to view keyboard shortcuts, press question mark",
    "view keyboard shortcuts",
    "top",
    "latest",
    "people",
    "media",
    "lists",
    "search timeline",
    "see new posts",
    "show more",
}

_FOLLOW_LINE = re.compile(r"(?im)^follow$")
DONE_MIN = 0.6

REASON_WHY = {
    "shell": "Stopped: the page was still only the search chrome. Here is the visible text.",
    "weak_done": "Model chose DONE with low confidence. The goal is not confirmed. Use the visible text.",
    "covered_target": "Stopped: the target was covered and nothing was typed.",
    "field_changed": "Stopped: the field changed before the text could be typed.",
    "stale_page": "Stopped: the page kept changing before the action could run.",
    "unsupported": "This driver does not take screenshots. Here is the visible text.",
    "model_blocked": (
        "Model chose BLOCKED. Here is the visible text; do not open another browser tool for the same look."
    ),
    "max_steps": "Stopped: tick budget exhausted before the goal was visibly done.",
}


def page_is_shell(text: str | None) -> bool:
    """True when the visible text is only the chrome around a timeline, not the timeline."""
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    if not lines:
        return True
    body = [line for line in lines if line.lower() not in _CHROME_LINES]
    if not body:
        return True
    body_text = " ".join(body)
    return len(body_text) < 40 and len(lines) >= 3 and len(body) <= 2


def page_is_follow_directory(text: str | None) -> bool:
    """A list of accounts with Follow buttons, not posts. Posts carry a middle-dot timestamp."""
    raw = text or ""
    return len(_FOLLOW_LINE.findall(raw)) >= 3 and "·" not in raw


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


def done_acceptable(decision: dict | None, page: dict | None) -> bool:
    if done_probability(decision) < DONE_MIN:
        return False
    text = (page or {}).get("text") or ""
    if page_is_shell(text) or page_is_follow_directory(text):
        return False
    return True


def unsupported_goal(goal: str | None) -> str | None:
    text = (goal or "").lower()
    if "screenshot" in text or "screen shot" in text or "take a picture" in text:
        return "unsupported"
    return None
