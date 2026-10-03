"""Two-strikes stop on degenerate decisions: the second consecutive tick with no real
preference stops the run instead of acting on a coin flip.

Mocked browser, mocked model, run log redirected by conftest. No network, no paid call.
"""

import json

import pytest
from conftest import DEFAULT_FAKE_ACTION, FakeBrowser

from jev_driver import cli, drive_agent
from jev_driver import drive_agent as loop
from jev_driver.readiness import REASON_WHY, done_acceptable

URL = "https://example.test/widget"
GOAL = "Open the widget panel"

ACTION = dict(DEFAULT_FAKE_ACTION)


class _StuckPageBrowser(FakeBrowser):
    """Same URL on every read, growing text — a run stuck here has not moved on."""

    def __init__(self, url):
        super().__init__(url, fixed_url=True, growing_text=True)


def _click(probabilities):
    """A CLICK decision with the given operation spread; the spread decides degeneracy."""
    top = max(probabilities.values())
    return {
        "choice": "e1",
        "operation": "CLICK",
        "target": "1",
        "confidence": top,
        "probabilities": {"e1": top},
        "operation_probabilities": dict(probabilities),
        "target_probabilities": {"1": top},
        "latency_ms": 12,
        "usage": {"input_tokens": 5, "output_tokens": 1, "cost": 0.0001},
    }


# Degenerate: top under 0.6 with a gap under 0.1. Decisive: a high top and a wide gap.
DEGENERATE = _click({"CLICK": 0.49, "BLOCKED": 0.44})
DECISIVE = _click({"CLICK": 0.9, "BLOCKED": 0.1})


@pytest.fixture
def browser(monkeypatch):
    """The one browser this test's agent will ever see."""
    fake = _StuckPageBrowser(URL)
    monkeypatch.setattr(loop, "Browser", lambda url: fake)
    return fake


@pytest.fixture
def run(monkeypatch):
    """Drive one agent against a scripted sequence of decisions, one per model call."""

    def _run(*decisions, debug=False):
        pending = list(decisions)
        calls = []

        def choose(page, goal, history):
            assert pending, "the model was called more often than the test scripted"
            calls.append({"step": len(history)})
            return dict(pending.pop(0))

        monkeypatch.setattr(loop, "choose", choose)
        agent = drive_agent.DriveAgent(URL, GOAL, debug=debug)
        return agent, calls

    return _run


def _events(tmp_path, reason=None):
    path = tmp_path / "run-log" / "drive.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
    return [row for row in rows if reason is None or row.get("reason") == reason]


# --------------------------------------------------------------------------
# A DONE that executed nothing (the seven sev-1 rows in the live ledger)
# --------------------------------------------------------------------------

# Every score below is the operation head as the backend actually returned it for
# that run, read back from drive.jsonl. DONE_MIN alone let all three false-dones
# through -- M15 sat exactly on the threshold -- which is why DONE_MIN is a
# confidence gate and cannot be the only one.


def _done(probabilities):
    """A DONE decision carrying the given operation spread."""
    decision = _click(probabilities)
    decision.update({"choice": "DONE", "operation": "DONE", "target": None,
                     "target_probabilities": {}})
    return decision


S5B_ZERO_ACTION_DONE = _done({"DONE": 0.69, "SCROLL_DOWN": 0.3, "CLICK": 0.01})
S7B_ZERO_ACTION_DONE = _done({"DONE": 0.81, "CLICK": 0.19})
M15_ZERO_ACTION_DONE = _done({"DONE": 0.6, "CLICK": 0.39, "BLOCKED": 0.01})
S1D_ALREADY_SATISFIED = _done({"DONE": 1.0, "WAIT": 0.0, "BLOCKED": 0.0, "CLICK": 0.0})
S6A_ALREADY_SATISFIED = _done({"DONE": 0.99, "CLICK": 0.01})

PAGE = {"text": "A real paragraph of visible text that is not only short labels."}


@pytest.mark.parametrize(
    "decision, row",
    [
        (S5B_ZERO_ACTION_DONE, "S5b"),
        (S7B_ZERO_ACTION_DONE, "S7b"),
        (M15_ZERO_ACTION_DONE, "M15"),
    ],
)
def test_a_zero_action_done_from_a_sev1_row_is_not_acceptable(decision, row):
    """Each of the three zero-action sev-1 rows, at the probability it really scored."""
    assert not done_acceptable(decision, PAGE, executed_actions=0), row


@pytest.mark.parametrize("decision, row", [(S1D_ALREADY_SATISFIED, "S1d"), (S6A_ALREADY_SATISFIED, "S6a")])
def test_an_already_satisfied_goal_may_still_declare_done_with_no_actions(decision, row):
    """The exemption the sev-1 fix must not break: S1d and S6a are HITs at zero actions."""
    assert done_acceptable(decision, PAGE, executed_actions=0), row


def test_a_done_that_executed_actions_is_judged_on_done_min_alone():
    """M16 scored 0.99 after one click and S7b-shaped runs acted too: the gate is
    unchanged for a run that did something, so this fix cannot cause a new MISS."""
    assert done_acceptable(_done({"DONE": 0.62, "CLICK": 0.38}), PAGE, executed_actions=1)


def test_an_unknown_action_count_falls_back_to_done_min():
    """`None` means the caller does not know, and must not silently tighten a gate
    it was not asking about."""
    assert done_acceptable(_done({"DONE": 0.9}), PAGE)


def test_a_zero_action_done_is_still_refused_on_a_shell_page():
    """Certainty does not buy a label-only page."""
    assert not done_acceptable(S1D_ALREADY_SATISFIED, {"text": "Menu\nHome\nAbout"},
                               executed_actions=0)


def test_a_zero_action_done_does_not_end_the_run(browser, run):
    """End to end through DriveAgent: the tick is rejected and the run keeps going
    instead of reporting a success it did not earn."""
    agent, _calls = run(S7B_ZERO_ACTION_DONE)

    snap = agent.command("tick")

    assert browser.acts == []
    assert snap["status"] != "done"
    assert snap.get("stop_reason") is None  # one weak DONE is not a stop; it re-observes
    assert snap["decision"] is None  # discarded, not left pending


def test_the_drivers_own_scroll_does_not_turn_a_zero_action_done_into_an_acted_one(browser, run, monkeypatch):
    """Rejected zero-action DONE scrolls once by itself. That scroll is the driver's,
    not the run's: the same 0.81 DONE on the next tick must still face the 0.95 bar
    instead of DONE_MIN, or the gate is a one-tick delay (live S7c)."""
    scroll = {"id": "scroll_down", "kind": "scroll", "label": "Scroll down"}
    page = FakeBrowser._page

    def with_scroll(self):
        out = page(self)
        out["actions"] = [*out["actions"], dict(scroll)]
        return out

    monkeypatch.setattr(FakeBrowser, "_page", with_scroll)
    agent, _calls = run(S7B_ZERO_ACTION_DONE, S7B_ZERO_ACTION_DONE)

    agent.command("tick")
    snap = agent.command("tick")

    assert [row["id"] for row in browser.acts] == ["scroll_down"]  # only the driver's scroll
    assert snap["status"] != "done"


def test_an_already_satisfied_goal_still_finishes_on_its_first_tick(browser, run):
    """S1d's shape, end to end: no action, DONE at 1.0, run done."""
    agent, _calls = run(S1D_ALREADY_SATISFIED)

    snap = agent.command("tick")

    assert browser.acts == []
    assert snap["status"] == "done"
    assert snap.get("stop_reason") is None


def test_the_second_consecutive_degenerate_tick_stops_the_run(browser, run):
    agent, calls = run(DEGENERATE, DEGENERATE)

    agent.command("tick")
    snap = agent.command("tick")

    assert len(calls) == 2  # the second decision was paid for, then discarded
    assert [row["label"] for row in browser.acts] == ["Open Widget"]  # the first one only
    assert snap["status"] == "blocked"
    assert snap["stop_reason"] == "low_confidence"
    assert snap["decision"] is None  # discarded, not left pending for the next tick


def test_a_single_degenerate_tick_acts_normally(browser, run):
    """One uncertain tick is ordinary: the next observation may still settle the goal."""
    agent, _calls = run(DEGENERATE)

    snap = agent.command("tick")

    assert [row["label"] for row in browser.acts] == ["Open Widget"]
    assert snap["status"] == "ready"
    assert snap.get("stop_reason") is None


def test_a_degenerate_tick_does_not_stop_a_run_that_reaches_its_step_budget(browser, run):
    """Only consecutive uncertainty stops. Three separated degenerate ticks all act."""
    agent, calls = run(DEGENERATE, DECISIVE, DEGENERATE, DECISIVE, DEGENERATE)

    for _ in range(3):
        snap = agent.command("tick")
        assert snap["status"] == "ready"

    assert len(calls) == 3
    assert len(browser.acts) == 3


def test_the_streak_counts_consecutive_ticks_not_decisions_in_total(browser, run):
    """A decisive tick in between resets it, so two uncertain ticks that never meet are not two strikes."""
    agent, _calls = run(DEGENERATE, DECISIVE, DEGENERATE)

    agent.command("tick")
    agent.command("tick")
    snap = agent.command("tick")

    assert snap["status"] == "ready"
    assert snap.get("stop_reason") is None
    assert len(browser.acts) == 3


def test_the_stop_is_logged_once_with_its_reason_and_why(browser, run, tmp_path):
    agent, _calls = run(DEGENERATE, DEGENERATE)

    agent.command("tick")
    agent.command("tick")

    stops = _events(tmp_path, reason="low_confidence")
    assert len(stops) == 1
    assert stops[0]["event"] == "blocked"
    assert stops[0]["goal"] == GOAL
    assert stops[0]["why"] == REASON_WHY["low_confidence"]
    assert "discarded" in stops[0]["why"]  # the log says the page was left alone


def test_the_stop_reaches_the_tick_record_the_driver_prints(browser, run):
    """No new field: the existing reason/why plumbing carries it to the agent."""
    agent, _calls = run(DEGENERATE, DEGENERATE)

    agent.command("tick")
    rec = cli.tick_record(agent.command("tick"))

    assert rec["status"] == "blocked"
    assert rec["reason"] == "low_confidence"
    assert rec["why"] == REASON_WHY["low_confidence"]


def test_the_overlay_explains_the_stop(browser, run):
    agent, _calls = run(DEGENERATE, DEGENERATE, debug=True)

    agent.command("tick")
    agent.command("tick")

    hud = browser.huds[-1]
    assert hud["status"] == "blocked"
    assert hud["reason"] == "low_confidence"
    assert hud["why"] == REASON_WHY["low_confidence"]