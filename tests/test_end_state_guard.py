"""P2: the end-state rescue on the BLOCKED path.

A model that answers BLOCKED is usually right. Sometimes it is not, in a way it
cannot see: the run already navigated to the end state the goal named, and the
model is looking at a page whose text no longer repeats the goal's words. That
reads as `model_blocked` forever and the caller re-asks a goal that is already
met. These tests pin the guard that converts that one case, and — far more
importantly — the much larger set of BLOCKED cases it must leave alone.

Score level only: a frozen page, a frozen goal, a fake history. No browser, no
model call, no live drive. The end-state evidence is a text match over data the
run already has, so nothing here needs a page that renders.

Every guard is one-sided. A false `done` is worse than an honest `blocked`: the
caller stops asking and the result never says the goal was missed. So most of
this file is about the guard *not* firing.
"""

import pytest

from jev_driver.drive_agent import DriveAgent
from jev_driver.metrics import _STOP_REASONS, _stop_reason
from jev_driver.readiness import (
    REASON_WHY,
    end_state_reached,
    goal_end_state_tokens,
    goal_steps,
    page_is_shell,
)

# The frozen IANA end state, from the run that motivated this guard (the 21:30
# release-gate run). Same excerpt test_stop_framing.py froze: truncated there,
# but the tail is a real sentence, so it clears the shell gate.
IANA_TEXT = (
    "Domains\nProtocols\nNumbers\nAbout\nInstructions and Guides\nExample Domains\n"
    "A number of domains such as\nexample.com\nand\nexample.org\nare maintained\n"
    "for documentation purposes. These domains may be used as…"
)
IANA_URL = "https://www.iana.org/help/example-domains"
IANA_PAGE = {"url": IANA_URL, "title": "", "text": IANA_TEXT}

# The exact goal that run was given, and the exact control it clicked.
GATE_GOAL = "Go to example.com and click the 'More information...' / 'Learn more' link, then stop."
GATE_CLICK = "More information..."


def _clicks(n, label="Learn more"):
    """n executed clicks in this run's own history, the shape agent.py writes."""
    return [
        {"step": i + 1, "kind": "click", "operation": "CLICK", "action": label, "page_changed": True} for i in range(n)
    ]


def _reached(goal=GATE_GOAL, *, page=IANA_PAGE, history=None, moved_on=True):
    default = _clicks(1, GATE_CLICK) if history is None else history
    return end_state_reached(page, goal=goal, history=default, moved_on=moved_on)


# --------------------------------------------------------------------------
# The guard fires: a correct end state reached through BLOCKED
# --------------------------------------------------------------------------


def test_a_blocked_on_an_end_state_this_run_reached_is_the_end_state():
    """The motivating case: arrived, text carries the goal, one click got there."""
    assert _reached() is True


def test_the_gate_goal_is_one_step_not_three():
    """Regression on the splitter: the "..." in "More information..." and the dot
    in "example.com" are part of a label and a host name. Counting either as a
    step boundary inflated one action into three and made the guard unsatisfiable
    for every real navigation goal."""
    steps, qualifier = goal_steps(GATE_GOAL)
    assert len(steps) == 1


def test_the_end_state_clause_is_evidence_but_not_an_extra_step():
    """"done when the IANA page shows" names what success looks like, not another
    action. It must be required as evidence without being charged as a step, or
    every "do X; done when Y" goal looks unfinished."""
    steps, qualifier = goal_steps("Click the Learn more link; done when the IANA page shows")
    assert len(steps) == 1
    assert "iana" in qualifier


def test_the_named_end_state_goal_is_also_rescued():
    """The P1 wording is the one the guard is meant to serve: the goal names its
    end state, so the page can be checked against it."""
    assert _reached("Click the Learn more link; done when the IANA example domains page shows") is True


# --------------------------------------------------------------------------
# The guard stays blocked: the model was right
# --------------------------------------------------------------------------


def test_a_multi_step_goal_with_one_step_done_stays_blocked():
    """Reviewer #14's hedge. Two steps named, one performed: firing here would
    report done on the first action of a three-action goal."""
    assert _reached("Open the article, then share it", history=_clicks(1, "Open the article")) is False


def test_a_later_step_named_only_by_an_on_screen_button_is_not_done():
    """The count check earns its own test, because the token check cannot catch
    this one: the page carries every goal word because the page has a Bookmark
    button and a Share button on it. The run clicked one thing; the goal needs
    two. Only "did the run perform a step per step the goal names" refuses it."""
    page = {**IANA_PAGE, "text": IANA_TEXT + "\nShare\nBookmark"}
    history = _clicks(1, "Open the article")
    assert end_state_reached(page, goal="Open the article, then bookmark it", history=history, moved_on=True) is False


def test_a_multi_step_goal_fully_performed_is_rescued():
    """The other side of the hedge, so the rule is a real completion test and
    not a blanket refusal on multi-step goals."""
    history = _clicks(1, "Open the article") + _clicks(1, "Share")
    assert _reached("Open the article, then share it", history=history) is True


def test_a_goal_whose_words_are_nowhere_on_the_page_stays_blocked():
    """The model is right about a page it cannot complete: nothing here shows
    the Husqvarna article, so the run has not arrived."""
    history = _clicks(1, "Open") + _clicks(1, "Bookmark")
    assert _reached("Open the Husqvarna article, then bookmark it", history=history) is False


def test_a_run_that_never_navigated_stays_blocked():
    """It is still on the start page, so it has not arrived anywhere. This is the
    guard's answer to a model that blocks on a page it is already looking at."""
    assert _reached(moved_on=False) is False


def test_a_shell_page_stays_blocked_even_with_a_navigated_run():
    """Chrome-only text carries no evidence, and a shell is exactly the condition
    BLOCKED is usually right about."""
    shell = {"url": "https://iana.org/x", "title": "", "text": "Menu\nHome\nSearch\nAbout"}
    assert page_is_shell(shell["text"]) is True
    assert _reached(page=shell) is False


def test_a_run_that_only_scrolled_stays_blocked():
    """Scrolling is looking, not acting. BLOCKED after three scrolls is the model
    being right, and crediting the run for its own looking would fire here."""
    assert _reached(history=[{"kind": "scroll", "action": "Scroll down"}]) is False


def test_scrolling_past_the_content_is_not_performing_the_goal():
    """The acted-on filter earns its own test, because the token check cannot catch
    this one. A goal naming a thing that is visible down the page satisfies every
    token after a scroll; only "the run acted, it did not merely look" refuses to
    call that done."""
    page = {**IANA_PAGE, "text": IANA_TEXT + "\nLearn more"}
    history = [{"kind": "scroll", "action": "Scroll down"}]
    assert end_state_reached(page, goal="Click the Learn more link", history=history, moved_on=True) is False


def test_a_goal_with_no_content_words_stays_blocked():
    """"click it" names nothing, so no page can be shown to satisfy it."""
    assert goal_end_state_tokens("click it") == set()
    assert _reached("click it") is False


def test_an_empty_target_list_blocked_stays_blocked_with_no_actions():
    """The empty-targets case from the task: BLOCKED with nothing to click and
    nothing done. There is no evidence of an end state, so it must stay blocked."""
    assert _reached(history=[]) is False


# --------------------------------------------------------------------------
# Tokenisation
# --------------------------------------------------------------------------


def test_driver_vocabulary_and_scaffolding_are_not_end_state_tokens():
    """"click the Learn more link" is about Learn more. link/click/page are how
    any goal is phrased and discriminate nothing."""
    tokens = goal_end_state_tokens("Click the Learn more link")
    assert tokens == {"learn", "more"}


def test_a_host_name_is_not_split_into_content_tokens():
    """"example.com" must survive as one word: splitting it leaves a bare "com"
    that no page text will ever contain, failing every navigation goal."""
    assert "com" not in goal_end_state_tokens("Go to example.com and read the page")


# --------------------------------------------------------------------------
# The stop machinery it converts through
# --------------------------------------------------------------------------


def _blocked_agent(page=IANA_PAGE, history=None):
    agent = DriveAgent.__new__(DriveAgent)
    agent.state = {
        "goal": GATE_GOAL,
        "status": "predicted",
        "page": {**page, "actions": []},
        "decision": {"choice": "BLOCKED", "operation": "BLOCKED"},
        "history": _clicks(1, GATE_CLICK) if history is None else list(history),
        "elapsed_ms": 100,
        "started_at": 0.0,
        "decisions": [{"choice": "BLOCKED", "operation": "BLOCKED"}],
    }
    agent._start_url = "https://example.com/"
    agent._weak_done = 0
    agent._degenerate_streak = 0
    agent._looked = DriveAgent.LOOK_SCROLLS
    agent._clicked = []
    agent.screenshots = False
    return agent


def test_the_rescue_converts_to_done_with_its_own_stop_reason(monkeypatch):
    """Not a silent success: status done, and `end_state_reached` recorded, so the
    result says the model blocked and the driver overrode it."""
    monkeypatch.setattr("jev_driver.drive_agent.write_event", lambda event: None)
    agent = _blocked_agent()
    snap = agent._blocked_rescue(agent.state["page"])
    assert snap["status"] == "done"
    assert agent.state["stop_reason"] == "end_state_reached"


def test_the_rescue_survives_the_metrics_stop_vocabulary(monkeypatch):
    """An unrecognised reason collapses to "other" in metrics.json, which would
    erase the distinction between this rescue and a plain BLOCKED. The vocabulary
    is closed, so the new reason has to be added to it."""
    monkeypatch.setattr("jev_driver.drive_agent.write_event", lambda event: None)
    assert "end_state_reached" in _STOP_REASONS
    assert _stop_reason("end_state_reached") == "end_state_reached"


def test_the_rescue_explains_itself_in_the_agents_words(monkeypatch):
    """A done whose why reads like a model's own DONE would be a lie. The agent
    must be able to say the model blocked and why the driver disagreed."""
    monkeypatch.setattr("jev_driver.drive_agent.write_event", lambda event: None)
    agent = _blocked_agent()
    agent._blocked_rescue(agent.state["page"])
    why = REASON_WHY[agent.state["stop_reason"]]
    assert "BLOCKED" in why
    assert "end state" in why


def test_the_rescue_consumes_the_decision_it_overrode():
    """agent.py writes history and metrics from the live decision. Leaving a
    BLOCKED in state after reporting done would let the next reader see a blocked
    choice under a done status."""
    agent = _blocked_agent()
    agent._blocked_rescue(agent.state["page"])
    assert agent.state["decision"] is None


@pytest.mark.parametrize("choice", ["DONE", "CLICK", None])
def test_the_rescue_ignores_a_decision_that_is_not_blocked(choice):
    """It hangs off the BLOCKED path only. A DONE or a CLICK that reaches this
    function is a caller bug and must not be rewritten into a done."""
    agent = _blocked_agent()
    agent.state["decision"] = {"choice": choice, "operation": choice}
    assert agent._blocked_rescue(agent.state["page"]) is None
    assert agent.state["status"] == "predicted"


def test_a_blocked_the_guard_declines_is_left_exactly_as_it_was():
    """The no-op must not touch status, reason, or the decision: this path runs on
    every BLOCKED, so it has to be invisible when it declines."""
    agent = _blocked_agent(history=[])
    before = dict(agent.state)
    assert agent._blocked_rescue(agent.state["page"]) is None
    assert agent.state["status"] == before["status"]
    assert agent.state.get("stop_reason") == before.get("stop_reason")
    assert agent.state["decision"] == before["decision"]


# --------------------------------------------------------------------------
# It hangs off the exhausted-BLOCKED path, after scrolling has been tried
# --------------------------------------------------------------------------


def test_look_further_consults_the_guard_once_its_scroll_budget_is_spent(monkeypatch):
    """Ordering is the conservatism: scroll first on every BLOCKED, so the guard
    cannot pre-empt a rescue a scroll would have found anyway."""
    monkeypatch.setattr("jev_driver.drive_agent.write_event", lambda event: None)
    agent = _blocked_agent()
    agent.state["browser"] = None  # no browser => _look_further would otherwise give up
    snap = agent._look_further()
    assert snap["status"] == "done"
    assert agent.state["stop_reason"] == "end_state_reached"


class _ScrollingBrowser:
    """Records that a scroll was dispatched, so the guard cannot be claiming the
    run gave up when it had not."""

    def __init__(self, page):
        self.scrolled = 0
        self._page = page

    def act(self, action, page, **kw):
        self.scrolled += 1

    def observe(self, screenshot=False):
        return {**IANA_PAGE, "actions": [], "scroll": {"y": 0}, "url": IANA_URL, "fingerprint": f"fp{self.scrolled}"}


def test_look_further_scrolls_before_the_guard_is_consulted():
    """With scrolls left and a scroll_down on offer, the run scrolls. The guard is
    the last chance, not the first — otherwise it would convert a BLOCKED that one
    more look would have resolved."""
    agent = _blocked_agent()
    agent._looked = 0
    browser = _ScrollingBrowser(IANA_PAGE)
    agent.state["browser"] = browser
    agent.state["page"] = {**IANA_PAGE, "actions": [{"id": "scroll_down", "kind": "scroll"}], "scroll": {"y": 0}}
    agent._look_further()
    assert browser.scrolled == 1
    assert agent.state["status"] == "ready"
    assert agent.state.get("stop_reason") != "end_state_reached"


# --------------------------------------------------------------------------
# V4: the BLOCKED is legible in the log without reconstructing it
# --------------------------------------------------------------------------


def _capture_blocked_log(monkeypatch, agent):
    """Run `_note_model_blocked` and return the `model_blocked` record it wrote."""
    written = []
    monkeypatch.setattr("jev_driver.drive_agent.write_event", written.append)
    agent._note_model_blocked()
    return [event for event in written if event.get("event") == "model_blocked"]


def test_a_blocked_records_what_done_was_worth_on_the_same_page(monkeypatch):
    """`done_p` is the number that was compared against DONE_MIN, so a BLOCKED can
    be read as a near-miss rather than guessed at."""
    agent = _blocked_agent()
    agent.state["decisions"] = [
        {"choice": "BLOCKED", "operation": "BLOCKED", "operation_probabilities": {"DONE": 0.26, "BLOCKED": 0.62}}
    ]
    record = _capture_blocked_log(monkeypatch, agent)[0]
    assert record["done_p"] == 0.26


def test_a_blocked_records_how_many_targets_were_on_offer(monkeypatch):
    """An empty target head is the difference between "nothing to click" and "the
    model would not click", and the log has to distinguish them."""
    agent = _blocked_agent()
    agent.state["decisions"] = [{"choice": "BLOCKED", "operation": "BLOCKED", "target_probabilities": {}}]
    assert _capture_blocked_log(monkeypatch, agent)[0]["ranked_targets_count"] == 0

    agent = _blocked_agent()
    agent.state["decisions"] = [
        {"choice": "BLOCKED", "operation": "BLOCKED", "target_probabilities": {"1": 0.9, "2": 0.1}}
    ]
    assert _capture_blocked_log(monkeypatch, agent)[0]["ranked_targets_count"] == 2


def test_a_blocked_records_where_the_run_ended_up_and_whether_it_navigated(monkeypatch):
    """`moved_on` is the precondition of the `_reject_weak_done` bypass, so it is
    what says whether DONE could have been accepted at all on this page."""
    agent = _blocked_agent()
    record = _capture_blocked_log(monkeypatch, agent)[0]
    assert record["final_url"] == IANA_URL
    assert record["moved_on"] is True


def test_a_blocked_on_the_start_page_records_that_the_run_never_moved(monkeypatch):
    """The other half of the bypass precondition, so `moved_on: false` is a real
    recorded answer and not an absent one."""
    agent = _blocked_agent(page={**IANA_PAGE, "url": "https://example.com/"})
    assert _capture_blocked_log(monkeypatch, agent)[0]["moved_on"] is False


def test_a_blocked_is_logged_even_when_the_rescue_later_converts_the_run(monkeypatch):
    """A BLOCKED the rescue overrode is precisely the case worth auditing, so the
    record has to be written before the run's stop_reason is decided."""
    agent = _blocked_agent()
    agent.state["stop_reason"] = "end_state_reached"
    agent.state["status"] = "done"
    assert len(_capture_blocked_log(monkeypatch, agent)) == 1


def test_a_decision_that_was_not_blocked_logs_no_blocked_record(monkeypatch):
    """The record is about BLOCKED specifically; a DONE tick must stay quiet."""
    agent = _blocked_agent()
    agent.state["decisions"] = [{"choice": "DONE", "operation": "DONE"}]
    assert _capture_blocked_log(monkeypatch, agent) == []