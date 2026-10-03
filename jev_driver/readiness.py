"""When a page is ready to decide, and when a DONE choice is believable."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

DONE_MIN = 0.6

# Zero-action DONE claims the page was already the end state — require near-certainty.
# Prefer a false blocked over a false done.
ZERO_ACTION_DONE_MIN = 0.95

REASON_WHY = {
    "shell": "Stopped: the page was still only short labels, not a document. Here is the visible text.",
    "weak_done": "Model chose DONE with low confidence. The goal is not confirmed. Use the visible text.",
    "low_confidence": (
        "Stopped: two ticks in a row decided with no real preference. The second was discarded, "
        "so nothing was clicked or typed on it. Read the visible text and drive again deliberately."
    ),
    "covered_target": "Stopped: the target was covered and nothing was typed.",
    "field_changed": "Stopped: the field changed before the text could be typed.",
    "click_not_sent": "Stopped: the click was chosen but the page changed before it was sent.",
    "stale_page": "Stopped: the page kept changing before the action could run.",
    "already_followed": (
        "Stopped: this link was already followed and the new page is showing. "
        "The model wanted to click a same-named link again. Check page_text."
    ),
    "toggle_undo": (
        "Stopped: the next click would undo an earlier click on the same control"
        " (it now shows a different label or state). Check the page before driving again."
    ),
    "unsupported": "This driver does not take screenshots. Here is the visible text.",
    "extract": "drive only clicks the current view. Call read to collect structured data.",
    "model_blocked": (
        "Model chose BLOCKED. Here is the visible text; do not open another browser tool for the same look."
    ),
    "end_state_reached": (
        "Model chose BLOCKED, but this run had already navigated to the end state the goal named: "
        "the visible page carries it and an action in this run's own history got there. Reported done. "
        "Check the visible text; if the goal is not actually met, drive again with a narrower goal."
    ),
    "max_steps": "Stopped: tick budget exhausted before the goal was visibly done.",
    "time_budget": (
        "Stopped: the run's time budget ran out before the goal was visibly done. "
        "The decision that crossed the deadline was discarded, so nothing was clicked or typed after it."
    ),
    "scroll_only": (
        "Stopped: only scrolled, and the goal had no end state to reach. "
        "The page is below. Use read with scrolls to look at more of it."
    ),
}


def _is_sentence(line: str) -> bool:
    words = line.split()
    if len(words) < 4:
        return False
    return line.endswith((".", "!", "?")) or ". " in line or "! " in line or "? " in line


def page_is_shell(text: str | None) -> bool:
    """True when visible text is empty or only short labels (no sentence yet)."""
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    if not lines:
        return True
    if any(_is_sentence(line) for line in lines):
        return False
    if sum(1 for line in lines if len(line.split()) >= 6) >= 2:
        return False
    if len(lines) >= 40 or sum(len(line) for line in lines) >= 1200:
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


def done_acceptable(decision: dict | None, page: dict | None, *, executed_actions=None) -> bool:
    """Whether a DONE may end the run (confidence + shell + zero-action floor)."""
    probability = done_probability(decision)
    if probability < DONE_MIN:
        return False
    text = (page or {}).get("text") or ""
    if page_is_shell(text):
        return False
    if executed_actions == 0 and probability < ZERO_ACTION_DONE_MIN:
        return False
    return True


def degenerate(decision: dict | None) -> bool:
    """True when the operation spread has no real preference (low top, narrow gap)."""
    if not decision:
        return False
    probs = decision.get("operation_probabilities") or {}
    if not probs:
        return False
    ranked = sorted(probs.values(), reverse=True)
    top = ranked[0]
    gap = top - (ranked[1] if len(ranked) > 1 else 0)
    return top < 0.6 and gap < 0.1

# BLOCKED end-state rescue: if the run already reached the goal's end state,
# report done rather than looping on model_blocked. One-sided — false done is worse.


_STOPWORDS = frozenset(
    """
    a an the this that these those there here it its it's is are was were be been being am
    to of in on at for from by with without within into onto as and or but nor so then
    please kindly just only also very really quite rather some any each every all both
    i me my we our you your he she they them their his her
    do does did done doing have has had having make makes made get gets got
    page pages site website tab screen view open opens opening
    """.split()
)

_VERBS = frozenset(
    """
    click clicks clicked clicking tap taps tapped go goes going goto navigate navigates
    navigated navigation scroll scrolls scrolled scrolling type types typed typing fill
    fills filled filling select selects selected choosing choose chose press presses pressed
    enter enters search searches searched find finds found open opens opened wait waits
    take takes bring brings use uses used
    """.split()
)

# Scaffolding that names the driver's mechanics or a URL fragment rather than the
# goal's subject. "click the Learn more link" is about Learn more; "link" and
# ".com" are only how the goal was phrased.
_SCAFFOLDING = frozenset(
    """
    link links button buttons clickable nav navbar menu homepage index default
    stop stops stopping done finishing finish finished complete completed
    show shows shown showing see seen visible appears appear appeared
    com org net edu gov io co www http https html htm aspx php
    """.split()
)

# A token this short is noise: stray punctuation, and "a"/"x", even if a word
# list above misses one.
_MIN_TOKEN = 3

# Splits a goal into the steps it names. Only the connectives goals actually use
# to chain actions, plus sentence punctuation *surrounded by whitespace* — a dot
# inside "More information..." or inside "example.com" is part of a label or a
# host name, not the end of a sentence.
_STEP_SPLIT = re.compile(r"\s+\b(?:then|after that|and then|once that|finally)\b\s+|\s*[.;:]\s+")

# A clause that describes the *end state* rather than naming another action:
# "click Learn more; done when the IANA page shows" is one step plus a
# description of success, not two steps. Its words must still be evidenced, so
# they are held separately and checked, but they never inflate the step count.
_END_STATE_CLAUSE = re.compile(
    r"\s*\b(?:done\s+when|until|so\s+that|successfully|and\s+stop|and\s+then\s+stop)\b.*",
    re.IGNORECASE | re.DOTALL,
)


def goal_end_state_tokens(goal: str | None) -> set[str]:
    """Content words from a goal (drop stopwords, verbs, scaffolding)."""
    raw = "".join(ch if ch.isalnum() else " " for ch in (goal or "").lower())
    return {
        token
        for token in raw.split()
        if len(token) >= _MIN_TOKEN
        and token not in _STOPWORDS
        and token not in _VERBS
        and token not in _SCAFFOLDING
    }


def goal_steps(goal: str | None) -> tuple[list[set[str]], set[str]]:
    """Step token sets plus trailing end-state qualifier words (not a step)."""
    text = (goal or "").lower()
    qualifier = goal_end_state_tokens(_END_STATE_CLAUSE.search(text).group(0) if _END_STATE_CLAUSE.search(text) else "")
    body = _END_STATE_CLAUSE.sub("", text)
    steps = []
    for chunk in _STEP_SPLIT.split(body):
        tokens = goal_end_state_tokens(chunk)
        if tokens:
            steps.append(tokens)
    return steps, qualifier


def _url_path_evidence(url: str | None) -> str:
    """Path (+ query) only — host is never goal evidence."""
    if not url:
        return ""
    parts = urlsplit(str(url))
    path = parts.path or ""
    query = f"?{parts.query}" if parts.query else ""
    return f"{path}{query}".lower()


def _visible_text(page: dict | None) -> str:
    """Title, body text, and URL path/query — never the host."""
    page = page or {}
    return " ".join(
        (
            str(page.get("title") or "").lower(),
            str(page.get("text") or "").lower(),
            _url_path_evidence(page.get("url")),
        )
    )


def _history_text(history: list | None) -> str:
    """Labels/values this run acted on (destination pages often omit goal words)."""
    bits = []
    for entry in history or []:
        if not isinstance(entry, dict):
            continue
        bits.extend(str(entry.get(key) or "") for key in ("action", "label", "text", "operation", "kind"))
    return " ".join(bits).lower()


def _token_in_evidence(token: str, evidence: str) -> bool:
    """Whole-word match: ``coin`` must not hit ``coinmarketcap``."""
    if not token or not evidence:
        return False
    return re.search(rf"(?<![a-z0-9]){re.escape(token)}(?![a-z0-9])", evidence) is not None


def model_action_count(history: list | None) -> int:
    """Model actions only — auto-scrolls after rejected DONE/BLOCKED do not count."""
    return sum(1 for entry in (history or []) if isinstance(entry, dict) and not entry.get("auto"))


def progressed_action_count(history: list | None) -> int:
    """Non-scroll, non-wait, non-auto actions — the end-state step counter."""
    return sum(
        1
        for entry in (history or [])
        if isinstance(entry, dict)
        and not entry.get("auto")
        and str(entry.get("kind") or "") not in {"scroll", "wait"}
    )


def goal_evidenced(page: dict | None, *, goal: str | None, history: list | None) -> bool:
    """Every goal step (and qualifier) has at least one whole-word token in evidence."""
    steps, qualifier = goal_steps(goal)
    if not steps:
        return False
    evidence = _visible_text(page) + " " + _history_text(history)
    required = [*steps, qualifier] if qualifier else steps
    return all(any(_token_in_evidence(token, evidence) for token in step) for step in required)


def end_state_reached(page: dict | None, *, goal: str | None, history: list | None, moved_on: bool) -> bool:
    """True when moved_on, non-shell, every step evidenced, and progressed ≥ steps."""
    steps, _qualifier = goal_steps(goal)
    if not steps:
        return False
    if not moved_on:
        return False
    if page_is_shell((page or {}).get("text")):
        return False
    if not goal_evidenced(page, goal=goal, history=history):
        return False
    return progressed_action_count(history) >= len(steps)


@dataclass(frozen=True)
class Evidence:
    """Facts one act-tick's finish/stop gates need — no browser handles."""

    choice: str | None
    top_op: str | None
    done_p: float
    shell: bool
    moved_on: bool
    time_budget_spent: bool
    weak_done: int
    degenerate_streak: int
    looked: int
    look_budget: int
    has_browser: bool
    has_scroll_down: bool
    short_page: bool
    model_history: int
    end_state: bool
    goal_evidenced: bool = False


@dataclass(frozen=True)
class Verdict:
    """What DriveAgent should do next. Side effects stay in the agent."""

    kind: str  # allow | stop | reject_done | look_scroll | look_wait | rescue_done | noop
    status: str | None = None
    stop_reason: str | None = None


# Mid-band acted DONEs need goal-token evidence.
ACTED_DONE_EVIDENCE_MAX = 0.8


def verdict(ev: Evidence) -> Verdict:
    """Finish/stop decision (DriveAgent owns browser I/O)."""
    if ev.degenerate_streak >= 2:
        return Verdict("stop", "blocked", "low_confidence")

    if ev.choice == "DONE":
        executed = None if (ev.model_history == 0 and ev.time_budget_spent) else ev.model_history
        fake = {
            "choice": "DONE",
            "operation_probabilities": {"DONE": ev.done_p},
            "confidence": ev.done_p,
        }
        page = {"text": "" if ev.shell else "A real paragraph of visible text that is not only short labels."}
        floor_ok = done_acceptable(fake, page, executed_actions=executed)
        mid_band = (
            isinstance(executed, int)
            and executed > 0
            and DONE_MIN <= ev.done_p < ACTED_DONE_EVIDENCE_MAX
        )
        if floor_ok and (not mid_band or ev.goal_evidenced):
            return Verdict("allow")
        if ev.top_op == "DONE" and ev.end_state:
            return Verdict("allow")
        if ev.shell:
            return Verdict("reject_done")
        if ev.weak_done + 1 >= 2:
            return Verdict("stop", "blocked", "weak_done")
        return Verdict("reject_done")

    if ev.choice == "BLOCKED":
        if ev.looked >= ev.look_budget or not ev.has_browser:
            if ev.end_state:
                return Verdict("rescue_done", "done", "end_state_reached")
            return Verdict("noop")
        if ev.shell or ev.short_page:
            return Verdict("look_wait")
        if not ev.has_scroll_down:
            if ev.end_state:
                return Verdict("rescue_done", "done", "end_state_reached")
            return Verdict("noop")
        return Verdict("look_scroll")

    return Verdict("noop")


def unsupported_goal(goal: str | None) -> str | None:
    text = (goal or "").lower()
    if "screenshot" in text or "screen shot" in text or "take a picture" in text:
        return "unsupported"
    if any(word in text for word in ("extract", "scrape", "as json", "return json", "collect all")):
        return "extract"
    return None
