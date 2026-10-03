"""When a page is ready to decide, and when a DONE choice is believable."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

DONE_MIN = 0.6

# A DONE that executed nothing is a claim that the page was *already* the end
# state, so it has to be near-certain rather than merely more likely than not.
#
# Calibrated from the seven sev-1 rows in the live ledger: the legitimate
# already-satisfied DONEs scored 1.00 (S1d, Tether price) and 0.99 (S6a,
# notifications list), while the zero-action false-dones scored 0.81 (S7b),
# 0.69 (S5b) and 0.60 (M15) -- every one of them at or above DONE_MIN, which is
# why DONE_MIN alone let them through. 0.95 separates the two groups on the frozen
# evidence without touching any run that acted.
#
# The cost is a real false-negative risk: a genuine already-satisfied DONE landing
# between 0.90 and 0.94 would now be rejected and the run would keep driving. That
# is the cheaper error -- it ends blocked (sev-2) rather than falsely done (sev-1).
ZERO_ACTION_DONE_MIN = 0.95

REASON_WHY = {
    "shell": "Stopped: the page was still only short labels, not a document. Here is the visible text.",
    "weak_done": "Model chose DONE with low confidence. The goal is not confirmed. Use the visible text.",
    # Kept under runlog's 200-char string cap, so the reason reaches the log and the agent
    # verbatim instead of arriving clipped.
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
    """True when the visible text is empty or only short labels, with no sentence yet.

    This is not a site list. Any page that has not drawn a sentence is treated
    as not ready, whether that is a nav bar, a spinner, or an empty result chrome.
    """
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
    """Whether a DONE may end the run (confidence + shell + zero-action floor).

    Mid-band acted-DONE goal evidence is applied in ``verdict()`` / callers that
    have a goal — this helper stays the pure probability/shell gate for unit tests.
    """
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
    """True when the operation spread carries no real preference: a low top and a narrow gap."""
    if not decision:
        return False
    probs = decision.get("operation_probabilities") or {}
    if not probs:
        return False
    ranked = sorted(probs.values(), reverse=True)
    top = ranked[0]
    gap = top - (ranked[1] if len(ranked) > 1 else 0)
    return top < 0.6 and gap < 0.1


# ---------------------------------------------------------------------------
# End-state rescue on the BLOCKED path.
#
# A model that answers BLOCKED is usually right: the target is below the fold,
# the page is a shell, the form will not submit. But it is sometimes wrong in a
# way the model cannot see — the run already arrived where the goal pointed, and
# the model is looking at a page whose visible text no longer repeats the goal's
# words (the 21:30 release-gate run: DONE 0.19, blocked on an end state it had
# reached). That reads as `model_blocked` and the caller re-asks forever.
#
# The guard below is the code-side half of the P1 prompt fix (questions.py):
# DONE already accepts "arriving at the page the goal named, by an action in
# your own history". BLOCKED does not, so a correct end state reached by
# BLOCKED has nowhere to go. This gives it one.
#
# It is deliberately one-sided. Every condition must hold; there is no partial
# credit and no retry. A false `done` is worse than an honest `blocked`: the
# caller stops asking, the goal was not met, and nothing in the result says so.
# ---------------------------------------------------------------------------

# Words in a goal that are never the thing being looked for: grammar, politeness,
# and the driver's own vocabulary. "Click the Search button" names Search; every
# other token is scaffolding.
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

# Verbs the driver itself performs. A goal is about the thing acted on, not the
# acting, so "click", "scroll" and "go" carry no end-state signal.
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
    """The words in a goal that name the thing the goal is about.

    Split on everything that is not a letter or digit, lowercased. Stopwords,
    driver verbs and scaffolding are dropped because they are true of nearly every
    goal and so discriminate nothing; what survives is what the run would have to
    produce for this particular goal to count as met.
    """
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
    """The goal's steps, plus the words of any end-state clause that trails them.

    Split on the connectives goals use to chain steps, then set aside any clause
    that says what "done" should look like. That clause is evidence the run must
    produce (the destination page has to carry those words) but not an action the
    run still has to perform, so it is returned separately instead of being
    counted as a step. Counting it as one is what would make a single
    action-completed goal look like an unfinished two-step goal.
    """
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
    """Path (+ query) only — the host is never goal evidence.

    Live S2a: goal token ``coin`` matched ``coinmarketcap.com`` in the URL host
    and a 0.47 DONE on the wrong page cleared the nav bypass.
    """
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
    """Everything this run itself acted on: the labels and values it touched.

    Half the evidence lives here rather than on the page. "Click the Learn more
    link" is satisfied by the run having clicked Learn more; the destination page
    never repeats the words "learn" or "more", so a page-only match would reject
    every run that actually succeeded.
    """
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
    """Actions the *model* performed — driver auto-scrolls after a rejected DONE/BLOCKED
    do not count (live S7c: auto-scroll laundered a zero-action DONE onto DONE_MIN).
    """
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
    """True when this run performed every step of the goal and reached its end state.

    Every clause must hold; there is no partial credit. A false ``done`` is worse
    than an honest ``blocked``, because the caller stops asking and the result
    never says the goal was missed.

    1. ``moved_on`` — the run navigated away from where it started.
    2. the page is not a shell.
    3. **every step** is whole-word evidenced in title/text/URL-path or history
       (host excluded — see ``_url_path_evidence``).
    4. progressed (non-scroll) actions ≥ step count.

    Returns False whenever any clause fails.
    """
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


# ---------------------------------------------------------------------------
# Single stop/finish verdict. DriveAgent owns browser I/O; this owns the decision.
# ---------------------------------------------------------------------------


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


# Acted DONEs in [DONE_MIN, ACTED_DONE_EVIDENCE_MAX) need goal-token evidence.
# Live S2a window-2: DONE 0.68 after 5 clicks on the wrong page cleared DONE_MIN alone.
ACTED_DONE_EVIDENCE_MAX = 0.8


def verdict(ev: Evidence) -> Verdict:
    """Finish/stop decision. DriveAgent owns browser I/O.

    BLOCKED order matches main: look-budget/no-browser → shell/short *wait*
    (even with no scroll_down) → scroll or rescue. snapshot.js only offers
    scroll_down when the page is taller than the viewport, so short loading
    pages must wait first (Kalshi/Polymarket/X).
    """
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
        # Match main `_look_further`: budget or missing browser first, then wait on
        # shell/short while budget remains (scroll_down may be absent), then scroll.
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
