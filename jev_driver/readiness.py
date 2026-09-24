"""When a page is ready to decide, and when a DONE choice is believable."""

from __future__ import annotations

DONE_MIN = 0.6

REASON_WHY = {
    "shell": "Stopped: the page was still only short labels, not a document. Here is the visible text.",
    "weak_done": "Model chose DONE with low confidence. The goal is not confirmed. Use the visible text.",
    "covered_target": "Stopped: the target was covered and nothing was typed.",
    "field_changed": "Stopped: the field changed before the text could be typed.",
    "click_not_sent": "Stopped: the click was chosen but the page changed before it was sent.",
    "stale_page": "Stopped: the page kept changing before the action could run.",
    "unsupported": "This driver does not take screenshots. Here is the visible text.",
    "model_blocked": (
        "Model chose BLOCKED. Here is the visible text; do not open another browser tool for the same look."
    ),
    "max_steps": "Stopped: tick budget exhausted before the goal was visibly done.",
}


def _is_sentence(line: str) -> bool:
    words = line.split()
    if len(words) < 4:
        return False
    return line.endswith((".", "!", "?")) or ". " in line or "! " in line or "? " in line


def page_is_shell(text: str | None) -> bool:
    """True when the visible text is empty or only short labels, with no sentence yet.

    This is not a site list. Any page that has not drawn a sentence is treated
    as not ready, whether that is a nav bar, a spinner, or an empty result chrome.
    """
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    if not lines:
        return True
    if any(_is_sentence(line) for line in lines):
        return False
    return len(lines) >= 3


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
    if page_is_shell(text):
        return False
    return True


def unsupported_goal(goal: str | None) -> str | None:
    text = (goal or "").lower()
    if "screenshot" in text or "screen shot" in text or "take a picture" in text:
        return "unsupported"
    return None
