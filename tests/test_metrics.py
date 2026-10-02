"""Per-run metrics: counters accumulate, snapshots are stable, and nothing but
counts and timings can ever reach disk.

Offline throughout: a fake browser and a fake model, and a clock the test
drives by hand, so every timing in a snapshot is an exact number.
"""

import json
import re
from unittest.mock import Mock

import pytest

from jev_driver import agent as loop
from jev_driver import drive_agent
from jev_driver import metrics as metrics_mod
from jev_driver.browser import Browser, StalePage, fingerprint
from jev_driver.metrics import Metrics, instrument_browser, metrics_path

URL = "https://example.test/widget"
GOAL = "Open the widget panel"

CLICK = {"id": "e1", "kind": "click", "label": "Open Widget", "role": "button", "value": "", "node": 7}
FILL = {"id": "e2", "kind": "fill", "label": "Search query", "role": "textbox", "value": "", "node": 8}
SCROLL = {"id": "scroll_down", "kind": "scroll", "label": "Scroll down", "node": None}

# Seconds the fake browser charges the fake clock, so every phase total is exact.
COSTS = {"observe": 0.005, "act": 0.003, "fresh": 0.001, "sleep": 0.002}

# Every string a snapshot is allowed to contain: closed vocabularies plus a timestamp.
VOCAB = {
    "click", "enter", "fill", "other", "scroll", "select", "wait",  # action kinds
    "observe", "act", "fresh",  # phase names
    "blocked", "done", "error", "predicted", "ready", "unknown",  # statuses
    "budget", "cancelled", "model", "runtime", "stale", "timeout", "value",  # error kinds
}
STAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def _strings(value, out):
    """Every string *value* in a snapshot. Keys are pinned by the shape test instead."""
    if isinstance(value, str):
        out.append(value)
    elif isinstance(value, dict):
        for item in value.values():
            _strings(item, out)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _strings(item, out)


def _timer(name):
    return {"count": 0, "total_ms": 0.0, "max_ms": 0.0, "avg_ms": 0.0} if name == "count" else {
        "calls": 0,
        "total_ms": 0.0,
        "max_ms": 0.0,
        "avg_ms": 0.0,
    }


class Clock:
    """Monotonic clock the test drives by hand, so no wall time is spent."""

    def __init__(self, start=1000.0):
        self.now = float(start)

    def perf_counter(self):
        return self.now

    def advance(self, seconds):
        self.now += float(seconds)


class _Time:
    """Stands in for the time module drive_agent and metrics read their clock from."""

    def __init__(self, clock):
        self.perf_counter = clock.perf_counter


class FakeBrowser:
    """Only source of page reads, and it records every mutation it is asked for."""

    HYDRATE_SLEEP_S = 0

    def __init__(self, url, clock, *, actions=(CLICK,), stable=False, stale_acts=0, keep_after_reads=None):
        self.url = url
        self.clock = clock
        self.debug = False
        self.actions = [dict(item) for item in actions]
        self.stable = stable  # same title and text every read: the text helper's cache can hit
        self.stale_acts = stale_acts  # refuse this many inputs with StalePage
        self.keep_after_reads = keep_after_reads  # past this read the controls are gone
        self.acts = []
        self.reads = 0
        self.closed = False

    def _page(self):
        # A different url per read, so a performed action always counts as progress.
        # ``stable`` freezes title and text, which is what the text helper's cache compares.
        self.reads += 1
        actions = self.actions if self.keep_after_reads is None or self.reads <= self.keep_after_reads else []
        page = {
            "url": self.url if self.stable else f"{self.url}#read{self.reads}",
            "title": "Widgets",
            "text": "The widget list is here with the panel control at the top of the page.",
            "scroll": {"x": 0, "y": 0, "height": 900},
            "actions": [dict(item) for item in actions],
        }
        page["fingerprint"] = fingerprint(page)
        return page

    def observe(self, screenshot=True):
        self.clock.advance(COSTS["observe"])
        return self._page()

    def _observe_once(self, screenshot=True):
        return self.observe(screenshot=screenshot)

    def fresh(self, page, action=None):
        self.clock.advance(COSTS["fresh"])
        return True

    def act(self, action, page, text=None):
        self.acts.append({"id": action.get("id"), "kind": action.get("kind")})
        self.clock.advance(COSTS["act"])
        if self.stale_acts > 0:
            self.stale_acts -= 1
            raise StalePage("Page changed since this decision. Observe again.")

    def sleep(self, seconds):
        self.clock.advance(COSTS["sleep"])
        return None

    def paint_hud(self, payload):
        return None

    def close(self):
        self.closed = True


def _decision(action_id="e1", operation="CLICK", latency_ms=12, label=None, head="click_target", probability=0.9):
    return {
        "choice": action_id,
        "operation": operation,
        "target": "1",
        "confidence": probability,
        "probabilities": {action_id: probability},
        "operation_probabilities": {operation: probability},
        "target_probabilities": {"1": probability},
        "latency_ms": latency_ms,
        "usage": {"input_tokens": 5, "output_tokens": 1, "cost": 0.0001},
        # What a real decision carries: with no label the retry path cannot find
        # its target, which is how a stale decision ends.
        "request": {"questions": {head: {"criteria": {"1": f"[a control] {label}"}}} if label else {}},
    }


@pytest.fixture
def clock(monkeypatch):
    fake = Clock()
    monkeypatch.setattr(drive_agent, "time", _Time(fake))
    monkeypatch.setattr(metrics_mod, "time", _Time(fake))
    return fake


def _decide(*decisions):
    """A model double that answers with the given decisions, in order."""
    queue = list(decisions)
    calls = []

    def choose(page, goal, history):
        calls.append({"goal": goal, "step": len(history)})
        return queue.pop(0) if len(queue) > 1 else queue[0]

    choose.calls = calls
    return choose


def _drive(monkeypatch, clock, *, actions=(CLICK,), decide=None, field=None, driver_field=None, **kwargs):
    made = []

    def factory(url):
        fake = FakeBrowser(url, clock, actions=actions, **kwargs)
        made.append(fake)
        return fake

    monkeypatch.setattr(loop, "Browser", factory)
    monkeypatch.setattr(loop, "choose", decide or _decide(_decision()))
    if field is not None:
        monkeypatch.setattr(loop, "field_text", field)
    if driver_field is not None:  # the retry path calls the helper through its own import
        monkeypatch.setattr(drive_agent, "field_text", driver_field)
    agent = drive_agent.DriveAgent(URL, GOAL)
    return agent, made[0]


# --- snapshot shape ------------------------------------------------------


def test_an_empty_run_still_has_every_key():
    snap = Metrics().snapshot()

    assert set(snap) == {
        "schema",
        "status",
        "error",
        "finished",
        "finished_at",
        "jev",
        "actions",
        "phases",
        "stale",
        "text_helper",
        "startup_ms",
        "cleanup_ms",
    }
    assert snap["schema"] == 1
    assert snap["status"] is None and snap["error"] is None and snap["finished"] is False
    assert snap["finished_at"] is None and snap["cleanup_ms"] is None
    assert snap["actions"] == {"attempted": 0, "succeeded": 0, "failed": 0, "by_kind": {}}
    assert snap["stale"] == 0 and snap["startup_ms"] == 0.0
    assert snap["jev"] == _timer("calls")
    assert snap["text_helper"] == _timer("calls")
    # Every phase is present from the start, so two runs can be diffed field by field.
    assert set(snap["phases"]) == {"observe", "act", "fresh", "wait"}
    assert all(snap["phases"][phase] == _timer("count") for phase in snap["phases"])


def test_counters_accumulate_and_average_exactly():
    m = Metrics()
    for ms in (10, 30, 20):
        m.record_jev(ms)

    assert m.snapshot()["jev"] == {"calls": 3, "total_ms": 60.0, "max_ms": 30.0, "avg_ms": 20.0}


def test_a_snapshot_is_a_copy_not_the_live_state():
    m = Metrics()
    m.record_jev(5)
    snap = m.snapshot()
    snap["jev"]["calls"] = 99
    snap["phases"]["act"]["total_ms"] = 99

    assert m.snapshot()["jev"]["calls"] == 1
    assert m.snapshot()["phases"]["act"]["total_ms"] == 0.0


def test_finish_is_idempotent_and_the_first_outcome_wins():
    m = Metrics()
    m.record_jev(5)
    first = m.finish("done")
    second = m.finish("blocked", ValueError("later"))

    assert first == second
    assert second["status"] == "done" and second["error"] is None and second["finished"] is True
    assert STAMP.match(second["finished_at"])
    assert m.finish()["status"] == "done"  # a third call cannot rewrite it either


def test_an_error_is_classified_by_type_and_never_reads_the_message():
    secret = "Model provider returned HTTP 401; key sk-live-SECRET"

    assert Metrics().finish("blocked", ValueError(secret))["error"] == "value"
    assert Metrics().finish("blocked", TimeoutError(secret))["error"] == "timeout"
    assert Metrics().finish("blocked", RuntimeError(secret))["error"] == "runtime"
    assert Metrics().finish("blocked", StalePage(secret))["error"] == "stale"
    assert Metrics().finish("blocked", KeyboardInterrupt(secret))["error"] == "cancelled"
    assert Metrics().finish("blocked", Exception(secret))["error"] == "other"
    assert Metrics().finish("done", None)["error"] is None


def test_unrecognised_words_collapse_to_the_last_vocabulary_entry():
    m = Metrics()
    m.record_action("sk-live-SECRET")  # a hostile kind is not a kind
    m.record_action("sk-live-SECRET", ok=True)
    m.record_action("CLICK", ok=True)  # kinds are case-insensitive
    m.record_phase("skim", 4)  # not a phase
    snap = m.finish("weird-status")

    assert snap["actions"]["by_kind"] == {
        "click": {"attempted": 0, "succeeded": 1, "failed": 0},
        "other": {"attempted": 1, "succeeded": 1, "failed": 0},
    }
    assert set(snap["phases"]) == {"observe", "act", "fresh", "wait"}
    assert snap["status"] == "unknown"


def test_non_finite_and_hostile_timings_cannot_break_the_json():
    m = Metrics()
    for value in (float("nan"), float("inf"), float("-inf"), None, "abc", -5, 10**20):
        m.record_jev(value)
        m.record_phase("observe", value)
        m.record_startup(value)

    dumped = json.dumps(m.snapshot(), allow_nan=False)
    assert json.loads(dumped)["jev"] == {"calls": 7, "total_ms": 0.0, "max_ms": 0.0, "avg_ms": 0.0}
    assert "NaN" not in dumped and "Infinity" not in dumped


def test_write_lands_next_to_the_run_log_and_names_the_run_file(tmp_path):
    assert metrics_path() == tmp_path / "run-log" / "metrics.json"
    assert metrics_path(tmp_path / "other" / "drive.jsonl") == tmp_path / "other" / "metrics.json"

    m = Metrics()
    assert m.record_startup(12) is None  # recorders return nothing; the assert is the point
    m.finish("done")
    assert m.write() is True
    assert json.loads((tmp_path / "run-log" / "metrics.json").read_text())["status"] == "done"


def test_a_write_failure_is_reported_not_raised(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    taken = tmp_path / "a-directory"
    taken.mkdir()

    assert Metrics().write(blocker / "sub" / "metrics.json") is False  # no parent to create
    assert Metrics().write(taken) is False  # a directory where a file belongs


def test_a_torn_write_never_replaces_the_last_good_snapshot(monkeypatch):
    first = Metrics()
    first.record_jev(5)
    first.finish("done")
    assert first.write() is True
    before = metrics_path().read_text()
    real_write = metrics_mod.Path.write_text

    def torn(self, *args, **kwargs):
        real_write(self, '{"schema": 1, "sta', encoding="utf-8")
        raise OSError("the disk went away mid-write")

    monkeypatch.setattr(metrics_mod.Path, "write_text", torn)
    second = Metrics()
    second.finish("blocked")

    assert second.write() is False
    assert metrics_path().read_text() == before  # the rename never happened, so nothing is half-written


# --- the browser adapter -------------------------------------------------


def test_instrument_is_idempotent_and_skips_methods_the_browser_lacks(clock):
    browser = FakeBrowser(URL, clock)
    m = Metrics()
    instrument_browser(browser, m)
    instrument_browser(browser, m)
    del browser.sleep  # a browser without a wait phase must still be instrumented

    browser.act(dict(CLICK), {"fingerprint": "fp"})

    assert browser.acts == [{"id": "e1", "kind": "click"}]  # still the original behaviour
    assert m.snapshot()["actions"] == {
        "attempted": 1,
        "succeeded": 1,
        "failed": 0,
        "by_kind": {"click": {"attempted": 1, "succeeded": 1, "failed": 0}},
    }
    assert m.snapshot()["phases"]["act"]["count"] == 1


def test_instrument_books_a_refused_input_and_re_raises_it(clock):
    browser = FakeBrowser(URL, clock, stale_acts=1)
    m = Metrics()
    instrument_browser(browser, m)

    with pytest.raises(StalePage):
        browser.act(dict(CLICK), {"fingerprint": "fp"})

    assert m.snapshot()["actions"]["failed"] == 1
    assert m.snapshot()["actions"]["by_kind"]["click"]["failed"] == 1


def test_a_nested_observe_is_one_observe_not_two(clock):
    class Settling:
        HYDRATE_SLEEP_S = 0

        def __init__(self, clock):
            self.clock = clock
            self.sleeps = 0

        def _observe_once(self, screenshot=True):
            self.clock.advance(COSTS["observe"])
            return {"fingerprint": "fp"}

        def observe(self, screenshot=True):
            self.sleep(0.1)
            return self._observe_once(screenshot=screenshot)

        def sleep(self, seconds):
            self.clock.advance(COSTS["sleep"])
            self.sleeps += 1

    browser = Settling(clock)
    m = Metrics()
    instrument_browser(browser, m)

    page = browser.observe()

    assert page == {"fingerprint": "fp"}
    phases = m.snapshot()["phases"]
    # Phases overlap by design: observe's total covers the wait it does internally,
    # so the phases describe work, not slices of one timeline.
    assert phases["observe"] == {"count": 1, "total_ms": 7.0, "max_ms": 7.0, "avg_ms": 7.0}
    assert phases["wait"] == {"count": 1, "total_ms": 2.0, "max_ms": 2.0, "avg_ms": 2.0}


def test_a_fresh_probe_inside_an_input_is_its_own_phase(clock):
    class Probing(FakeBrowser):
        """A browser that checks freshness itself, the way the real one does."""

        def act(self, action, page, text=None):
            if not self.fresh(page, action):
                raise StalePage("Page changed since this decision. Observe again.")
            self.clock.advance(COSTS["act"])

    browser = Probing(URL, clock)
    m = Metrics()
    instrument_browser(browser, m)

    browser.act(dict(CLICK), {"fingerprint": "fp"})

    phases = m.snapshot()["phases"]
    assert phases["act"] == {"count": 1, "total_ms": 4.0, "max_ms": 4.0, "avg_ms": 4.0}  # probe included
    assert phases["fresh"] == {"count": 1, "total_ms": 1.0, "max_ms": 1.0, "avg_ms": 1.0}


# --- a fake run ----------------------------------------------------------


def test_a_test_double_does_not_read_as_already_instrumented():
    # A Mock invents any attribute asked of it, so an "is it attached?" check
    # that uses getattr would silently leave this browser uninstrumented.
    browser = Mock(HYDRATE_SLEEP_S=0)
    m = Metrics()
    instrument_browser(browser, m)
    instrument_browser(browser, m)

    browser.act({"kind": "click"}, {}, text=None)

    assert m.snapshot()["actions"]["attempted"] == 1  # one call, one booking, however often we attach
    assert m.snapshot()["phases"]["act"]["count"] == 1


def test_the_real_browser_can_be_instrumented_without_connecting():
    browser = Browser.__new__(Browser)  # a real Browser that never opened a tab
    m = Metrics()

    instrument_browser(browser, m)

    assert {"observe", "_observe_once", "act", "fresh", "sleep"} <= set(browser.__dict__)
    assert browser.__class__ is Browser and not isinstance(browser, Metrics)
    with pytest.raises(Exception):  # no session: instrumenting must not make it look runnable
        browser.fresh({"marker": "x"})


def test_a_fake_run_counts_calls_inputs_and_phase_times(clock, monkeypatch):
    agent, browser = _drive(monkeypatch, clock)

    agent.command("tick")
    snap = agent.metrics.snapshot()

    assert loop.choose.calls and len(loop.choose.calls) == 1
    assert snap["jev"] == {"calls": 1, "total_ms": 12.0, "max_ms": 12.0, "avg_ms": 12.0}
    assert snap["actions"] == {
        "attempted": 1,
        "succeeded": 1,
        "failed": 0,
        "by_kind": {"click": {"attempted": 1, "succeeded": 1, "failed": 0}},
    }
    # The observe that opens the tab happens before the browser is instrumented,
    # so it is priced into startup_ms rather than into the observe phase.
    assert snap["phases"]["observe"] == {"count": 1, "total_ms": 5.0, "max_ms": 5.0, "avg_ms": 5.0}
    assert snap["phases"]["act"] == {"count": 1, "total_ms": 3.0, "max_ms": 3.0, "avg_ms": 3.0}
    assert snap["phases"]["fresh"] == {"count": 1, "total_ms": 1.0, "max_ms": 1.0, "avg_ms": 1.0}
    assert snap["phases"]["wait"]["count"] == 0
    assert snap["startup_ms"] == 5.0  # opening the tab costs one read
    assert snap["stale"] == 0 and snap["text_helper"]["calls"] == 0
    assert [act["kind"] for act in browser.acts] == ["click"]


def test_inputs_are_broken_down_by_kind(clock, monkeypatch):
    decide = _decide(_decision("e1"), _decision("scroll_down", "SCROLL_DOWN", latency_ms=7))
    agent, _browser = _drive(monkeypatch, clock, actions=(CLICK, SCROLL), decide=decide)

    agent.command("tick")
    agent.command("tick")
    actions = agent.metrics.snapshot()["actions"]

    assert actions["attempted"] == 2 and actions["succeeded"] == 2 and actions["failed"] == 0
    assert actions["by_kind"] == {
        "click": {"attempted": 1, "succeeded": 1, "failed": 0},
        "scroll": {"attempted": 1, "succeeded": 1, "failed": 0},
    }
    assert agent.metrics.snapshot()["jev"] == {"calls": 2, "total_ms": 19.0, "max_ms": 12.0, "avg_ms": 9.5}


def test_a_stale_input_is_a_failed_action_and_a_stale_page(clock, monkeypatch):
    # One click is refused; the driver re-reads and clicks the same label again.
    decide = _decide(_decision("e1", label="Open Widget"), _decision("e1", label="Open Widget"))
    agent, browser = _drive(monkeypatch, clock, decide=decide, stale_acts=1)

    agent.command("tick")
    snap = agent.metrics.snapshot()

    assert [act["kind"] for act in browser.acts] == ["click", "click"]
    assert snap["stale"] == 1
    assert snap["actions"]["attempted"] == 2
    assert snap["actions"]["succeeded"] == 1
    assert snap["actions"]["failed"] == 1
    assert snap["actions"]["by_kind"]["click"] == {"attempted": 2, "succeeded": 1, "failed": 1}
    # The refusal was retried, so the run carries on.
    assert agent.state["status"] == "ready" and len(agent.state["history"]) == 1


def test_a_refused_decision_the_loop_swallowed_is_still_reported(clock, monkeypatch):
    # The click is refused and the control is gone by the time the driver re-reads,
    # so the decision cannot be retried: the tick loop swallows it and asks again.
    decide = _decide(_decision("e1", label="Open Widget"), _decision("DONE", "DONE"))
    agent, browser = _drive(monkeypatch, clock, decide=decide, stale_acts=1, keep_after_reads=1)

    agent.command("tick")
    agent.command("tick")
    assert agent.state["status"] == "done"
    agent.close()

    raw = metrics_path().read_text()
    snap = json.loads(raw)
    assert [act["kind"] for act in browser.acts] == ["click"]
    assert snap["status"] == "done" and snap["finished"] is True
    assert snap["error"] == "stale"  # the run's outcome, and what killed a decision on the way
    assert snap["actions"]["failed"] == 1 and snap["stale"] == 1 and snap["jev"]["calls"] == 2
    assert "Observe again" not in raw  # the exception's kind, not its words


def test_a_cached_text_helper_is_not_a_call(clock, monkeypatch):
    # Same field, same page: the second fill reuses the text the first one generated.
    calls = []

    def field_text(context):
        calls.append(context)
        return "Zurich", {"model": "text-model", "latency_ms": 10, "usage": {}}

    decide = _decide(_decision("e2", "TYPE_TEXT"), _decision("e2", "TYPE_TEXT"))
    agent, browser = _drive(
        monkeypatch, clock, actions=(FILL,), decide=decide, field=field_text, stale_acts=1, stable=True
    )

    agent.command("tick")  # the first fill is refused, after the text was already paid for
    agent.command("tick")  # the same decision again: this time the cached text is typed

    snap = agent.metrics.snapshot()
    assert [act["kind"] for act in browser.acts] == ["fill", "fill"]  # two inputs...
    assert len(calls) == 1  # ...one helper call...
    assert snap["text_helper"] == {"calls": 1, "total_ms": 10.0, "max_ms": 10.0, "avg_ms": 10.0}
    assert snap["actions"]["by_kind"]["fill"] == {"attempted": 2, "succeeded": 1, "failed": 1}


@pytest.mark.parametrize(
    ("stale_acts", "expected_inputs", "expected_stale"),
    [(1, ["fill", "fill"], 1), (2, ["fill", "fill", "fill"], 2)],  # the retry is refused too
)
def test_a_helper_call_during_a_stale_recovery_is_counted(
    clock, monkeypatch, stale_acts, expected_inputs, expected_stale
):
    # The typed field goes stale, so the driver writes the same value on a fresh read.
    calls = []

    def field_text(context):
        calls.append(context)
        return "Zurich", {"model": "text-model", "latency_ms": 40, "usage": {}}

    decide = _decide(_decision("e2", "TYPE_TEXT", label="Search query", head="type_text_target"))
    agent, browser = _drive(
        monkeypatch,
        clock,
        actions=(FILL,),
        decide=decide,
        field=field_text,
        driver_field=field_text,
        stale_acts=stale_acts,
    )

    agent.command("tick")
    snap = agent.metrics.snapshot()

    assert [act["kind"] for act in browser.acts] == expected_inputs  # refused, then written
    assert len(calls) == 2  # the helper ran twice: once before the refusal, once to retry
    assert snap["stale"] == expected_stale
    assert snap["text_helper"] == {"calls": 2, "total_ms": 80.0, "max_ms": 40.0, "avg_ms": 40.0}
    assert snap["actions"]["by_kind"]["fill"] == {
        "attempted": len(expected_inputs),
        "succeeded": 1,
        "failed": len(expected_inputs) - 1,
    }


def test_a_refused_retry_click_is_a_stale_too(clock, monkeypatch):
    decide = _decide(_decision("e1", label="Open Widget"), _decision("e1", label="Open Widget"))
    agent, browser = _drive(monkeypatch, clock, decide=decide, stale_acts=2)

    agent.command("tick")
    snap = agent.metrics.snapshot()

    assert [act["kind"] for act in browser.acts] == ["click", "click", "click"]
    assert snap["stale"] == 2  # the first refusal and the refused retry both count
    assert snap["actions"]["by_kind"]["click"] == {"attempted": 3, "succeeded": 1, "failed": 2}


def test_every_helper_call_costs_ms(clock, monkeypatch):
    latencies = [10, 250]
    decide = _decide(_decision("e2", "TYPE_TEXT"), _decision("e2", "TYPE_TEXT"))
    agent, _browser = _drive(
        monkeypatch,
        clock,
        actions=(FILL,),
        decide=decide,
        field=lambda context: ("Zurich", {"model": "text-model", "latency_ms": latencies.pop(0)}),
    )

    agent.command("tick")
    agent.command("tick")
    snap = agent.metrics.snapshot()

    assert snap["text_helper"] == {"calls": 2, "total_ms": 260.0, "max_ms": 250.0, "avg_ms": 130.0}


def test_close_writes_one_snapshot_with_the_final_status(clock, monkeypatch):
    decide = _decide(_decision("e1"), _decision("DONE", "DONE"))
    agent, browser = _drive(monkeypatch, clock, decide=decide)
    agent.command("tick")
    agent.command("tick")
    assert agent.state["status"] == "done"

    writes = []
    real_write = Metrics.write

    def counting_write(self, path=None):
        writes.append(path)
        return real_write(self, path)

    monkeypatch.setattr(Metrics, "write", counting_write)
    agent.close()
    agent.close()  # a second close must not rewrite the file

    assert browser.closed is True
    assert len(writes) == 1
    snap = json.loads(metrics_path().read_text())
    assert snap["status"] == "done" and snap["finished"] is True and snap["error"] is None
    assert snap["cleanup_ms"] == 0.0 and snap["cleanup_ms"] is not None
    assert snap["actions"]["succeeded"] == 1


def test_a_run_that_never_starts_still_reports_itself(clock, monkeypatch):
    agent, _browser = _drive(monkeypatch, clock, decide=_decide(_decision("DONE", "DONE")))

    agent.close()

    snap = json.loads(metrics_path().read_text())
    assert snap["status"] == "ready" and snap["jev"]["calls"] == 0
    assert snap["cleanup_ms"] is not None and snap["startup_ms"] == 5.0


def test_a_metrics_write_failure_never_breaks_the_run(clock, monkeypatch):
    decide = _decide(_decision("e1"), _decision("DONE", "DONE"))
    agent, browser = _drive(monkeypatch, clock, decide=decide)
    agent.command("tick")
    blocker = metrics_path().parent / "blocker"
    blocker.parent.mkdir(parents=True, exist_ok=True)
    blocker.write_text("not a directory")
    monkeypatch.setattr(metrics_mod, "metrics_path", lambda jsonl_path=None: blocker / "metrics.json")

    agent.close()  # must not raise: the run is already over, telemetry is not worth losing it

    assert browser.closed is True
    assert not (blocker / "metrics.json").exists()


def test_metrics_recorders_cannot_break_a_browser_call(clock, monkeypatch):
    browser = FakeBrowser(URL, clock)
    metrics = Metrics()
    instrument_browser(browser, metrics)
    for name in ("record_action", "record_phase", "record_stale"):
        monkeypatch.setattr(
            metrics, name, lambda *a, **k: (_ for _ in ()).throw(AssertionError("recorder must not run out"))
        )

    browser.act(dict(CLICK), {"fingerprint": "fp"})  # still performs the input

    assert browser.acts == [{"id": "e1", "kind": "click"}]
    assert metrics.snapshot()["actions"]["attempted"] == 0


def test_a_bare_instance_gets_its_metrics_on_demand():
    # Tests build agents with __new__; a hook must not need a constructor.
    agent = drive_agent.DriveAgent.__new__(drive_agent.DriveAgent)

    assert isinstance(agent.metrics, Metrics)
    assert agent.metrics is agent.metrics


def _bare_agent(browser, decision, page, **attrs):
    """A DriveAgent built without a constructor, the way the other suites build one."""
    agent = drive_agent.DriveAgent.__new__(drive_agent.DriveAgent)
    agent.screenshots = False
    agent.state = {
        "decision": decision,
        "page": page,
        "browser": browser,
        "status": "predicted",
        "history": [],
        "decisions": [],
        "goal": GOAL,
    }
    for key, value in attrs.items():
        setattr(agent, key, value)
    return agent


def _stale_browser(page):
    return Mock(
        act=Mock(side_effect=StalePage("Page changed since this decision. Observe again.")),
        observe=Mock(return_value={**page, "fingerprint": "fp2"}),
        sleep=Mock(),
    )


def _visible_page():
    return {
        "url": URL,
        "text": "A long visible page about browsing with an agent and Jev. " * 4,
        "actions": [dict(SCROLL)],
        "fingerprint": "fp",
    }


def test_a_stale_scroll_on_a_bare_instance_is_counted():
    agent = _bare_agent(
        _stale_browser(_visible_page()),
        {"choice": "BLOCKED", "operation": "BLOCKED"},
        _visible_page(),
    )

    agent._look_further()

    assert agent.metrics.snapshot()["stale"] == 1


def test_a_stale_scroll_after_a_rejected_done_is_counted():
    # A hedged DONE is refused, and the page scrolls on to look for the target.
    page = _visible_page()
    decision = {
        "choice": "DONE",
        "operation": "DONE",
        "operation_probabilities": {"DONE": 0.34, "SCROLL_DOWN": 0.3},
        "target": None,
    }
    agent = _bare_agent(_stale_browser(page), decision, page, _weak_done=0, _start_url=URL, _clicked=[])

    agent._reject_weak_done()

    assert agent.metrics.snapshot()["stale"] == 1
    assert agent.state["status"] == "ready"  # a refused scroll is not a reason to stop


def test_a_hostile_page_cannot_reach_the_snapshot(clock, monkeypatch):
    hostile_text = "admin@hacker.test password: hunter2 Authorization: Bearer sk-live-SECRET " + "padding " * 40
    hostile_url = "https://user:pw-secret-token@evil.test/login?api_key=sk-live-ABCDEF"
    hostile_label = "Pay 4111111111111111 with sk_live_ZZZZ"
    browser = FakeBrowser(URL, clock)
    original = browser._page

    def hostile():
        page = original()
        page["url"] = hostile_url
        page["text"] = hostile_text
        page["title"] = hostile_label
        page["actions"] = [{**CLICK, "label": hostile_label}]
        return page

    browser._page = hostile
    monkeypatch.setattr(loop, "Browser", lambda url: browser)
    monkeypatch.setattr(loop, "choose", _decide(_decision("e1")))
    agent = drive_agent.DriveAgent(URL, GOAL)
    agent.command("tick")
    agent.close()

    raw = metrics_path().read_text()
    for secret in ("hunter2", "sk-live", "SECRET", "pw-secret", "4111111", "evil.test", "admin@hacker", "padding"):
        assert secret not in raw, secret

    found: list[str] = []
    _strings(json.loads(raw), found)
    assert found, "the snapshot should not be empty"
    assert all(word in VOCAB or STAMP.match(word) for word in found), sorted(set(found) - VOCAB)
