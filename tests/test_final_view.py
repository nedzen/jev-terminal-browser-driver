"""Independent re-read after DONE: evidence attached to the result, never a status change. No live browser."""

import json

import pytest

from jev_driver import cli
from jev_driver.discover import Discovery

PAGE = {
    "url": "https://example.test/next",
    "title": "Next page",
    "fingerprint": "fp-decision",
    "text": "The order was confirmed and a receipt was emailed.",
    "scroll": {"y": 0, "height": 900},
    "actions": [],
}


def _snap(**over):
    snap = {
        "status": "done",
        "stop_reason": None,
        "page": dict(PAGE),
        "history": [],
        "decisions": [{"choice": "DONE", "operation": "DONE", "fingerprint": "fp-decision"}],
    }
    snap.update(over)
    return snap


class FakeBrowser:
    """Only source of page reads in these tests, so the probe is countable."""

    def __init__(self, *pages):
        self.pages = list(pages)
        self.reads = 0
        self.calls = []

    def observe(self, screenshot=True):
        self.calls.append({"screenshot": screenshot})
        page = self.pages[min(self.reads, len(self.pages) - 1)]
        self.reads += 1
        if isinstance(page, Exception):
            raise page
        return dict(page)


class ListPageBrowser(FakeBrowser):
    """A browser that answers the probe with something that is not a page."""

    def observe(self, screenshot=True):
        self.calls.append({"screenshot": screenshot})
        self.reads += 1
        return ["not", "a", "page"]


def test_unchanged_page_is_not_flagged():
    browser = FakeBrowser(PAGE)
    view = cli._final_view(_snap(), browser)
    assert view["page_changed_since_decision"] is False
    assert view["url"] == "https://example.test/next"
    assert view["title"] == "Next page"
    assert browser.reads == 1
    assert browser.calls == [{"screenshot": False}]  # the probe never pays for a screenshot


def test_page_that_moved_after_the_decision_is_flagged():
    moved = {**PAGE, "url": "https://example.test/receipt", "fingerprint": "fp-after"}
    view = cli._final_view(_snap(), FakeBrowser(moved))
    assert view["page_changed_since_decision"] is True
    assert view["url"] == "https://example.test/receipt"


def test_changed_page_without_a_decision_fingerprint_falls_back_to_the_page():
    moved = {**PAGE, "fingerprint": "fp-after"}
    snap = _snap(decisions=[{"choice": "DONE", "operation": "DONE"}])
    assert cli._final_view(snap, FakeBrowser(moved))["page_changed_since_decision"] is True


def test_missing_fingerprints_report_unknown_not_unchanged():
    snap = _snap(page={**PAGE, "fingerprint": None}, decisions=[{"choice": "DONE"}])
    assert cli._final_view(snap, FakeBrowser(PAGE))["page_changed_since_decision"] is None


def test_failed_re_read_reports_the_error_instead_of_raising():
    view = cli._final_view(_snap(), FakeBrowser(RuntimeError("Target closed")))
    assert view == {"error": "Target closed"}
    assert "page_changed_since_decision" not in view


def test_missing_browser_reports_an_error():
    assert cli._final_view(_snap(), None) == {"error": "no browser to re-read"}


class FakeAgent:
    """One tick, then the CLI loop exits. Same state shape as DriveAgent."""

    def __init__(self, url, goal, screenshots=False, debug=False):
        self.url = url
        self.goal = goal
        self.browser = FakeAgent.pages
        self.state = {
            "browser": self.browser,
            "goal": goal,
            "page": dict(PAGE),
            "decision": {"choice": "DONE"},
            "history": [],
            "status": "ready",
            "decisions": [],
            "text_calls": [],
            "elapsed_ms": 12,
            "started_at": None,
        }
        self.closed = False

    def snapshot(self):
        return {key: value for key, value in self.state.items() if key != "browser"}

    def command(self, name, body=None):
        if name == "tick":
            self.state["decisions"].append(
                {"choice": "DONE", "operation": "DONE", "fingerprint": self.state["page"]["fingerprint"]}
            )
            self.state["status"] = "done"
        return self.snapshot()

    def close(self):
        self.closed = True


class FakeBlockedAgent(FakeAgent):
    """Same loop, but the tick blocks instead of finishing: no probe expected."""

    def command(self, name, body=None):
        if name == "tick":
            self.state["status"] = "blocked"
        return self.snapshot()


@pytest.fixture
def drive(monkeypatch, capsys):
    """Run cli.main with no browser and no model, one mocked tick."""

    def run(pages, agent_cls=FakeAgent):
        if isinstance(pages, FakeBrowser):
            browser = pages
        else:
            browser = FakeBrowser(*(pages if isinstance(pages, tuple) else (pages,)))
        FakeAgent.pages = browser
        agent_cls.pages = browser
        monkeypatch.setattr(
            cli,
            "discover",
            lambda **kw: Discovery("ws://127.0.0.1:1/x", "http://127.0.0.1:1", "explicit"),
        )
        monkeypatch.setattr(cli, "connect", lambda url: None)
        monkeypatch.setattr(cli, "set_lease", lambda **kw: None)
        monkeypatch.setattr(cli, "find_continuable_page", lambda: (None, None))
        monkeypatch.setattr(cli, "DriveAgent", agent_cls)
        code = cli.main(["--goal", "Confirm the order", "--url", "https://example.test/next"])
        rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
        return code, rows[-1], browser

    return run


def test_done_result_carries_the_independent_re_read(drive):
    code, rec, browser = drive(PAGE)
    assert code == 0
    assert rec["status"] == "done"
    assert rec["final_view"]["page_changed_since_decision"] is False
    assert browser.reads == 1  # exactly one probe, nothing else read the page


def test_done_stays_done_when_the_page_changed_before_the_probe(drive):
    moved = {**PAGE, "url": "https://example.test/receipt", "fingerprint": "fp-after"}
    code, rec, _browser = drive(moved)
    assert code == 0
    assert rec["status"] == "done"
    assert rec["final_view"] == {
        "page_changed_since_decision": True,
        "url": "https://example.test/receipt",
        "title": "Next page",
    }


def test_failed_probe_still_returns_the_done_result(drive):
    code, rec, _browser = drive(RuntimeError("Target closed"))
    assert code == 0
    assert rec["status"] == "done"
    assert rec["final_view"] == {"error": "Target closed"}
    assert rec["page_text"].startswith("The order was confirmed")


def test_probe_that_answers_with_a_non_page_reports_an_error(drive):
    # The fake agent's browser is the one handed in, so the DONE tick's probe answers with a list.
    code, rec, browser = drive(ListPageBrowser())
    assert code == 0
    assert rec["status"] == "done"
    assert rec["final_view"] == {"error": "observe returned list, not a page"}
    assert rec["page_text"].startswith("The order was confirmed")
    assert browser.calls == [{"screenshot": False}]


def test_final_view_reaches_the_run_log(drive, tmp_path):
    drive(PAGE)
    events = [json.loads(line) for line in (tmp_path / "run-log" / "drive.jsonl").read_text().splitlines()]
    done = [event for event in events if event.get("status") == "done"]
    assert done and done[-1]["final_view"]["page_changed_since_decision"] is False


def test_blocked_result_is_untouched(drive):
    code, rec, browser = drive(PAGE, FakeBlockedAgent)
    assert code == 1
    assert rec["status"] == "blocked"
    assert "final_view" not in rec
    assert browser.reads == 0
