"""In-loop per-run time budget: a deadline checked before the Jev decision call
and again before browser input, so an over-budget decision never mutates the page.

Mocked clock, mocked browser, mocked model. No network, no paid call.
"""

import json
import subprocess

import pytest
from conftest import Clock, _Time

from jev_driver import agent as loop
from jev_driver import cli, drive_agent
from jev_driver.browser import StalePage, fingerprint
from jev_driver.discover import Discovery
from plugin import handler

URL = "https://example.test/widget"
GOAL = "Open the widget panel"

ACTION = {"id": "e1", "kind": "click", "label": "Open Widget", "role": "button", "value": "", "node": 7}


class FakeBrowser:
    """Only source of page reads, and it records every mutation it is asked for."""

    HYDRATE_SLEEP_S = 0

    def __init__(self, url):
        self.url = url
        self.debug = False
        self.acts = []
        self.reads = 0
        self.closed = False
        self.huds = []

    def _page(self):
        # A different url per read, so a performed action always counts as progress.
        page = {
            "url": self.url if self.reads == 0 else f"{self.url}#read{self.reads}",
            "title": "Widgets",
            "text": "The widget list is here with the panel control at the top of the page.",
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
        """The retry paths re-read the page before acting on it."""
        page = self._page()
        self.reads += 1
        return page

    def fresh(self, page, kind=None):
        return True

    def act(self, action, page, text=None):
        self.acts.append({"id": action.get("id"), "label": action.get("label")})

    def sleep(self, seconds):
        return None

    def paint_hud(self, payload):
        self.huds.append(payload)

    def close(self):
        self.closed = True


def _decision(action_id="e1"):
    return {
        "choice": action_id,
        "operation": "CLICK",
        "target": "1",
        "confidence": 0.9,
        "probabilities": {action_id: 0.9},
        "operation_probabilities": {"CLICK": 0.9},
        "target_probabilities": {"1": 0.9},
        "latency_ms": 12,
        "usage": {"input_tokens": 5, "output_tokens": 1, "cost": 0.0001},
    }


def _choose(clock, *, advance_s=0.0):
    """A model double that counts calls and can burn clock like a slow provider."""
    calls = []

    def choose(page, goal, history):
        calls.append({"goal": goal, "step": len(history)})
        if advance_s:
            clock.advance(advance_s)
        return _decision()

    choose.calls = calls
    return choose


@pytest.fixture
def clock(monkeypatch):
    fake = Clock()
    monkeypatch.setattr(drive_agent, "time", _Time(fake))
    return fake


@pytest.fixture
def browsers(monkeypatch):
    """Patch the Browser agent.py builds, and hand back the fakes it created."""
    made = []

    def factory(url):
        fake = FakeBrowser(url)
        made.append(fake)
        return fake

    monkeypatch.setattr(loop, "Browser", factory)
    return made


def _drive(monkeypatch, clock, browsers, *, time_budget_s=None, advance_s=0.0):
    monkeypatch.setattr(loop, "choose", _choose(clock, advance_s=advance_s))
    agent = drive_agent.DriveAgent(URL, GOAL, time_budget_s=time_budget_s)
    return agent, browsers[0]


def test_expired_before_the_decision_spends_no_model_call(clock, browsers, monkeypatch, tmp_path):
    agent, browser = _drive(monkeypatch, clock, browsers, time_budget_s=10)

    agent.command("tick")  # one decision well inside the budget
    clock.advance(20)  # the run now sits past its 10s deadline
    snap = agent.command("tick")  # must stop before a second model call

    assert len(loop.choose.calls) == 1
    assert len(browser.acts) == 1  # only the first, in-budget action ran
    assert snap["status"] == "blocked"
    assert snap["stop_reason"] == "time_budget"
    assert snap["decision"] is None
    assert len(snap["history"]) == 1  # the one in-budget action, and nothing after it
    events = [json.loads(line) for line in (tmp_path / "run-log" / "drive.jsonl").read_text().splitlines()]
    assert events[-1]["reason"] == "time_budget"
    assert "time budget" in events[-1]["why"]


def test_decision_that_outlived_the_deadline_is_discarded(clock, browsers, monkeypatch):
    # A model call slower than the whole budget: it returns, and it is dropped.
    agent, browser = _drive(monkeypatch, clock, browsers, time_budget_s=10, advance_s=30)
    before = agent.state["page"]["fingerprint"]

    snap = agent.command("tick")

    assert len(loop.choose.calls) == 1  # the call was spent...
    assert browser.acts == []  # ...and never executed
    assert snap["status"] == "blocked"
    assert snap["stop_reason"] == "time_budget"
    assert snap["decision"] is None  # discarded, not left pending for the next tick
    assert snap["history"] == []  # nothing ran, so nothing was recorded as run
    assert len(snap["decisions"]) == 1  # the paid call stays in the audit trail
    assert agent.state["page"]["fingerprint"] == before  # the page was never mutated


def test_the_clock_starts_at_the_first_decision_not_at_tab_creation(clock, browsers, monkeypatch):
    agent, browser = _drive(monkeypatch, clock, browsers, time_budget_s=10)
    assert agent._budget_deadline is None  # opening the tab spends none of it

    clock.advance(600)  # slow lease/navigation work before the first decision
    snap = agent.command("tick")

    assert agent._budget_deadline == clock.now + 10  # armed at the decision, in full
    assert agent._budget_deadline == 1610.0  # 1000 + 600 spent before it, + the full 10
    assert len(browser.acts) == 1
    assert snap["status"] == "ready"
    assert snap.get("stop_reason") is None


def test_without_a_budget_the_loop_is_unchanged(clock, browsers, monkeypatch):
    # Every model call is an hour; without a budget that must not matter.
    agent, browser = _drive(monkeypatch, clock, browsers, advance_s=3600)
    assert agent.time_budget_s is None

    snaps = [agent.command("tick") for _ in range(3)]

    assert len(loop.choose.calls) == 3
    assert len(browser.acts) == 3
    assert agent._budget_deadline is None  # the deadline was never armed
    assert snaps[-1]["status"] == "ready"
    assert snaps[-1].get("stop_reason") is None


def test_a_zero_budget_is_no_budget(clock, browsers, monkeypatch):
    """Only 1..900 is a budget; 0 and None both mean off (the CLI rejects 0)."""
    agent, browser = _drive(monkeypatch, clock, browsers, time_budget_s=0, advance_s=3600)
    agent.command("tick")
    assert agent._budget_deadline is None
    assert len(browser.acts) == 1
    assert agent.state["status"] == "ready"


class _LoopAgent:
    """Agent double that records constructor kwargs and stops after one tick."""

    def __init__(self, url, goal, screenshots=False, debug=False, **kwargs):
        self.kwargs = kwargs
        self.closed = False
        self.state = {
            "browser": None,
            "goal": goal,
            "page": {"url": URL, "title": "Widgets", "text": "the widget list", "fingerprint": "fp"},
            "decision": None,
            "history": [],
            "status": "ready",
            "decisions": [],
            "text_calls": [],
            "elapsed_ms": 3,
            "started_at": None,
        }

    def snapshot(self):
        return {key: value for key, value in self.state.items() if key != "browser"}

    def command(self, name, body=None):
        if name == "tick":
            self.state["status"] = "blocked"
        return self.snapshot()

    def close(self):
        self.closed = True


@pytest.fixture
def run_cli(monkeypatch, capsys):
    """cli.main with no browser and no model; returns (exit code, printed JSON rows)."""

    def run(argv, agent_cls=_LoopAgent):
        monkeypatch.setattr(
            cli,
            "discover",
            lambda **kw: Discovery("ws://127.0.0.1:1/x", "http://127.0.0.1:1", "explicit"),
        )
        monkeypatch.setattr(cli, "connect", lambda url: None)
        monkeypatch.setattr(cli, "set_lease", lambda **kw: None)
        monkeypatch.setattr(cli, "find_continuable_page", lambda: (None, None))
        monkeypatch.setattr(cli, "DriveAgent", agent_cls)
        code = cli.main(["--goal", GOAL, "--url", URL, *argv])
        rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
        return code, rows

    return run


def test_the_flag_is_optional():
    assert cli.parse_args(["--goal", GOAL]).time_budget_s is None
    assert cli.parse_args(["--goal", GOAL, "--time-budget-s", "45"]).time_budget_s == 45


def test_the_flag_reaches_the_agent_only_when_passed(run_cli):
    built = []

    class Recorder(_LoopAgent):
        def __init__(self, *args, **kwargs):
            built.append(kwargs)
            super().__init__(*args, **{k: v for k, v in kwargs.items() if k != "time_budget_s"})

    run_cli(["--time-budget-s", "45"], Recorder)
    assert built[-1]["time_budget_s"] == 45

    run_cli([], Recorder)
    assert "time_budget_s" not in built[-1]  # off means the agent is built exactly as before


@pytest.mark.parametrize("value", ["0", "901", "-5"])
def test_out_of_range_flag_is_rejected_before_any_browser(value, monkeypatch, capsys):
    def boom(**kwargs):
        raise AssertionError("must not open or attach to a browser")

    monkeypatch.setattr(cli, "discover", boom)
    code = cli.main(["--goal", GOAL, "--time-budget-s", value])
    assert code == 1
    assert json.loads(capsys.readouterr().err.strip()) == {
        "status": "blocked",
        "error": "--time-budget-s must be 1..900",
    }


def test_inner_budget_stop_reports_stopped_reason_time_budget(clock, browsers, monkeypatch, capsys):
    """End to end: the deadline stop travels the existing plumbing untouched."""
    monkeypatch.setattr(loop, "choose", _choose(clock, advance_s=30))
    monkeypatch.setattr(
        cli,
        "discover",
        lambda **kw: Discovery("ws://127.0.0.1:1/x", "http://127.0.0.1:1", "explicit"),
    )
    monkeypatch.setattr(cli, "connect", lambda url: None)
    monkeypatch.setattr(cli, "set_lease", lambda **kw: None)
    monkeypatch.setattr(cli, "find_continuable_page", lambda: (None, None))

    code = cli.main(["--goal", GOAL, "--url", URL, "--time-budget-s", "5"])
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    tick = rows[-1]

    assert code == 1
    assert tick["status"] == "blocked"
    assert tick["reason"] == "time_budget"
    assert tick["error"] == "timeout"  # what the existing taxonomy reads as time_budget
    assert "time budget" in tick["why"]
    assert browsers[0].acts == []

    result = handler.compact_result(rows, code)
    assert result["stopped_reason"] == "time_budget"
    assert result["status"] == "blocked"
    assert result["success"] is False
    assert result["reason"] == "time_budget"
    assert browsers[0].acts == []


class _FakeProc:
    def __init__(self, stdout="", returncode=0):
        self.stdout = stdout
        self.stderr = ""
        self.returncode = returncode

    def communicate(self, timeout=None):
        return self.stdout, ""


@pytest.fixture
def spawn(monkeypatch, tmp_path):
    """A fake driver home plus a popen that records argv instead of spawning."""
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "drive.py").write_text("# drive\n")
    monkeypatch.setenv("JEV_DRIVER_HOME", str(tmp_path))
    calls = []

    def popen(argv, **kwargs):
        calls.append(argv)
        return _FakeProc(stdout=json.dumps({"status": "done", "url": "x"}))

    return calls, popen


def test_the_budget_is_forwarded_to_the_cli(spawn):
    calls, popen = spawn

    handler.run_drive({"goal": GOAL}, popen=popen)
    assert "--time-budget-s" not in calls[-1]  # absent means no inner deadline

    handler.run_drive({"goal": GOAL, "time_budget_s": 45}, popen=popen)
    argv = calls[-1]
    assert argv[argv.index("--time-budget-s") + 1] == "45"
    # The outer kill stays a separate knob.
    assert "--timeout-s" not in argv

    # Integral floats accepted, same as max_steps (cross-adapter JSON parity).
    handler.run_drive({"goal": GOAL, "time_budget_s": 45.0}, popen=popen)
    argv = calls[-1]
    assert argv[argv.index("--time-budget-s") + 1] == "45"


@pytest.mark.parametrize("bad", [0, 901, -3, 1.5, True, "45"])
def test_a_bad_budget_is_rejected_before_the_spawn(spawn, bad):
    calls, popen = spawn
    out = handler.run_drive({"goal": GOAL, "time_budget_s": bad}, popen=popen)
    assert calls == []
    assert out["error"] == "time_budget_s must be an integer 1..900; no action executed."
    assert out["stopped_reason"] == "error"
    assert out["success"] is False


def test_the_caps_are_the_same_in_python_and_across_layers():
    assert handler.TIME_BUDGET_CAP == cli.TIME_BUDGET_CAP == handler.TIMEOUT_CAP == 900


def test_timeout_s_still_kills_the_subprocess(spawn):
    """The inner deadline is additive: the outer kill is unchanged."""
    calls, popen = spawn
    kills = []

    class TimingOut(_FakeProc):
        def communicate(self, timeout=None):
            raise subprocess.TimeoutExpired(cmd="drive", timeout=timeout)

    out = handler.run_drive(
        {"goal": GOAL, "timeout_s": 1, "time_budget_s": 300},
        popen=lambda argv, **kw: TimingOut(),
        kill_group=lambda proc: kills.append(proc),
    )
    assert out["error"] == "timeout"
    assert out["stopped_reason"] == "time_budget"
    assert len(kills) == 1


# --------------------------------------------------------------------------------------
# A spent clock may discard input, never a decision that performs none.


def _events(tmp_path):
    """Every record this test's run log holds."""
    path = tmp_path / "run-log" / "drive.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


def _stops(tmp_path):
    """Only the records that report the inner deadline, whatever else was logged."""
    return [row for row in _events(tmp_path) if row.get("reason") == "time_budget"]


def _terminal(choice, operation):
    """A decision that settles the run instead of touching the page."""
    return {
        "choice": choice,
        "operation": operation,
        "target": None,
        "confidence": 0.9,
        "probabilities": {choice: 0.9},
        "operation_probabilities": {operation: 0.9},
        "target_probabilities": {},
        "latency_ms": 12,
        "usage": {"input_tokens": 5, "output_tokens": 1, "cost": 0.0001},
    }


def _labelled(action_id, operation, label, target="1"):
    """A decision whose target criteria name the label, so the retry paths can find it."""
    criteria = {target: f"[0.9] {label}"}
    return {
        "choice": action_id,
        "operation": operation,
        "target": target,
        "confidence": 0.9,
        "probabilities": {action_id: 0.9},
        "operation_probabilities": {operation: 0.9},
        "target_probabilities": {target: 0.9},
        "latency_ms": 12,
        "usage": {"input_tokens": 5, "output_tokens": 1, "cost": 0.0001},
        "request": {"questions": {operation.lower() + "_target": {"criteria": criteria}}},
    }


def _answering(clock, decision, *, advance_s=0.0):
    """A model double that hands back one fixed decision and can burn clock doing it."""
    calls = []

    def choose(page, goal, history):
        calls.append({"goal": goal, "step": len(history)})
        if advance_s:
            clock.advance(advance_s)
        return dict(decision)

    choose.calls = calls
    return choose


def _install(monkeypatch, browser):
    """Point the loop's Browser at exactly this double, and hand it back."""
    monkeypatch.setattr(loop, "Browser", lambda url: browser)
    return browser


class _ProseBrowser(FakeBrowser):
    """Enough visible prose that _look_further reads the page as loaded, not still drawing."""

    def _page(self):
        page = super()._page()
        page["text"] = "The widget list is here with the panel control at the top of the page. " * 3
        page["fingerprint"] = fingerprint(page)
        return page


class _StaleOnActBrowser(FakeBrowser):
    """The first act finds the page moved under it, and burns the rest of the clock finding out.

    `attempts` counts every act call, `acts` only the ones that landed. A stale raise never
    appends to `acts`, so `acts == []` proves nothing was clicked or typed.
    """

    ACTIONS = [ACTION]

    def __init__(self, url, clock, spend_s):
        super().__init__(url)
        self.clock = clock
        self.spend_s = spend_s
        self.attempts = []
        self.stale_left = 1

    def _page(self):
        page = super()._page()
        page["actions"] = [dict(item) for item in self.ACTIONS]
        page["fingerprint"] = fingerprint(page)
        return page

    def act(self, action, page, text=None):
        self.attempts.append({"id": action.get("id"), "kind": action.get("kind")})
        if self.stale_left:
            self.stale_left -= 1
            self.clock.advance(self.spend_s)
            raise StalePage("Page changed since the decision. Choose again.")
        super().act(action, page, text=text)


FILL_ACTION = {"id": "f1", "kind": "fill", "label": "Your name", "role": "textbox", "value": "", "node": 9}


class _StaleFillBrowser(_StaleOnActBrowser):
    ACTIONS = [FILL_ACTION]


def test_a_done_that_crossed_the_deadline_still_finishes(clock, browsers, monkeypatch):
    """DONE performs no input, so a spent clock may not turn a free finish into a failure."""
    done = _answering(clock, _terminal("DONE", "DONE"), advance_s=30)
    monkeypatch.setattr(loop, "choose", done)
    agent = drive_agent.DriveAgent(URL, GOAL, time_budget_s=10)
    browser = browsers[0]

    snap = agent.command("tick")

    assert len(done.calls) == 1  # the model call was spent...
    assert browser.acts == []  # ...and DONE clicked and typed nothing, deadline or not
    assert snap["status"] == "done"  # honoured, not discarded into a blocked stop
    assert snap.get("stop_reason") is None
    assert snap["plan_index"] == 1  # the run really finished rather than stalling


def test_a_blocked_that_crossed_the_deadline_is_the_models_stop_not_the_budgets(clock, monkeypatch, tmp_path):
    blocked = _answering(clock, _terminal("BLOCKED", "BLOCKED"), advance_s=30)
    monkeypatch.setattr(loop, "choose", blocked)
    browser = _install(monkeypatch, _ProseBrowser(URL))
    agent = drive_agent.DriveAgent(URL, GOAL, time_budget_s=10)

    snap = agent.command("tick")

    assert len(blocked.calls) == 1
    assert browser.acts == []
    assert snap["status"] == "blocked"
    # The state carries the reason; the snapshot does not, because the base loop snapshots
    # before this subclass annotates the stop.
    assert agent.state["stop_reason"] == "model_blocked"  # the model's own stop, not the clock's
    assert _stops(tmp_path) == []  # and the deadline was never reported as the reason


def test_a_decision_whose_fields_disagree_is_read_as_input(clock, browsers, monkeypatch):
    """`choice` is what the base loop acts on, so it alone decides which reading is safe."""
    conflicting = _labelled("e1", "CLICK", "Open Widget")
    conflicting["operation"] = "DONE"  # claims to settle the run, but names an element
    monkeypatch.setattr(loop, "choose", _answering(clock, conflicting, advance_s=30))
    agent = drive_agent.DriveAgent(URL, GOAL, time_budget_s=10)
    browser = browsers[0]

    snap = agent.command("tick")

    assert browser.acts == []  # discarded, because discarding cannot click
    assert snap["status"] == "blocked"
    assert snap["stop_reason"] == "time_budget"


def test_one_tick_reports_the_budget_stop_exactly_once(clock, browsers, monkeypatch, tmp_path):
    """predict refuses the model call, then the tick's act finds the same clock spent."""
    agent, _browser = _drive(monkeypatch, clock, browsers, time_budget_s=10)
    agent.command("tick")  # one decision well inside the budget
    clock.advance(20)

    snap = agent.command("tick")  # the tick that runs out

    assert len(_stops(tmp_path)) == 1  # one event, not one per branch that noticed
    assert snap["status"] == "blocked"
    assert snap["stop_reason"] == "time_budget"
    assert len(loop.choose.calls) == 1  # and still no second model call


def test_a_retry_click_does_not_land_after_the_deadline(clock, monkeypatch):
    """The stale-retry loop checks the clock before its click, not only at act entry."""
    browser = _install(monkeypatch, _StaleOnActBrowser(URL, clock, spend_s=30))
    click = _answering(clock, _labelled("e1", "CLICK", "Open Widget"))
    monkeypatch.setattr(loop, "choose", click)
    agent = drive_agent.DriveAgent(URL, GOAL, time_budget_s=10)

    snap = agent.command("tick")

    assert len(click.calls) == 1
    assert len(browser.attempts) == 1  # only the stale one; the retry never reached act
    assert browser.acts == []  # so the label was never clicked
    assert snap["status"] == "blocked"
    assert snap["stop_reason"] == "time_budget"
    assert snap["decision"] is None  # the retry was discarded, not left pending
    assert snap["history"] == []  # nothing landed, so nothing is recorded as run


def test_a_retry_fill_does_not_type_after_the_deadline(clock, monkeypatch):
    """Same guard on the fill path: no keystrokes after the deadline either."""
    helper = {"model": "test", "latency_ms": 1}
    monkeypatch.setattr(loop, "field_text", lambda context: ("hello", helper))
    monkeypatch.setattr(drive_agent, "field_text", lambda context: ("hello", helper))
    browser = _install(monkeypatch, _StaleFillBrowser(URL, clock, spend_s=30))
    fill = _answering(clock, _labelled("f1", "TYPE_TEXT", "Your name"))
    monkeypatch.setattr(loop, "choose", fill)
    agent = drive_agent.DriveAgent(URL, GOAL, time_budget_s=10)

    snap = agent.command("tick")

    assert len(fill.calls) == 1
    assert len(browser.attempts) == 1
    assert browser.acts == []  # the field was never typed into
    assert snap["status"] == "blocked"
    assert snap["stop_reason"] == "time_budget"
    assert snap["history"] == []


def test_a_retry_inside_the_budget_still_clicks(clock, monkeypatch):
    """The guard costs nothing while there is budget left: the retry lands as before."""
    browser = _install(monkeypatch, _StaleOnActBrowser(URL, clock, spend_s=2))
    click = _answering(clock, _labelled("e1", "CLICK", "Open Widget"))
    monkeypatch.setattr(loop, "choose", click)
    agent = drive_agent.DriveAgent(URL, GOAL, time_budget_s=10)

    snap = agent.command("tick")

    assert len(browser.attempts) == 2  # the stale one and the retry that answered it
    assert [row["label"] for row in browser.acts] == ["Open Widget"]
    assert snap["status"] == "ready"
    assert snap.get("stop_reason") is None
    assert [row["kind"] for row in snap["history"]] == ["click"]


def test_the_stop_is_logged_once_across_ticks_not_reset_by_a_later_one(clock, browsers, monkeypatch, tmp_path):
    """A driver that keeps ticking a stopped run still reports the deadline once."""
    agent, _browser = _drive(monkeypatch, clock, browsers, time_budget_s=10)
    agent.command("tick")
    clock.advance(20)

    agent.command("tick")
    agent.command("tick")  # a driver that ignores the stop and asks again
    agent.command("tick")

    assert len(_stops(tmp_path)) == 1
