"""Two-strikes stop on degenerate decisions: the second consecutive tick with no real
preference stops the run instead of acting on a coin flip.

Mocked browser, mocked model, run log redirected by conftest. No network, no paid call.
"""

import json

import pytest

from jev_driver import agent as loop
from jev_driver import cli, drive_agent
from jev_driver.browser import fingerprint
from jev_driver.readiness import REASON_WHY

URL = "https://example.test/widget"
GOAL = "Open the widget panel"

ACTION = {"id": "e1", "kind": "click", "label": "Open Widget", "role": "button", "value": "", "node": 7}


class FakeBrowser:
    """Same URL on every read, growing text, and a record of every mutation asked for."""

    HYDRATE_SLEEP_S = 0

    def __init__(self, url):
        self.url = url
        self.debug = False
        self.acts = []
        self.reads = 0
        self.closed = False
        self.huds = []

    def _page(self):
        # The URL never changes, so a run stuck here has not moved on; the text grows, so
        # every observation is a fresh fingerprint and a performed action reads as progress.
        page = {
            "url": self.url,
            "title": "Widgets",
            "text": "The widget list is here with the panel control at the top of the page. " + "." * self.reads,
            "scroll": {"x": 0, "y": 0, "height": 900},
            "actions": [dict(ACTION)],
        }
        page["fingerprint"] = fingerprint(page)
        return page

    def observe(self, screenshot=True):
        page = self._page()
        self.reads += 1
        return page

    def _observe_once(self, screenshot=False):
        page = self._page()
        self.reads += 1
        return page

    def fresh(self, page, kind=None):
        return True

    def act(self, action, page, text=None, **kw):
        self.acts.append({"id": action.get("id"), "label": action.get("label")})

    def sleep(self, seconds):
        return None

    def paint_hud(self, payload):
        self.huds.append(payload)

    def close(self):
        self.closed = True


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
    fake = FakeBrowser(URL)
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