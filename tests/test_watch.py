"""Watch ladder, takeover yield, session continuity. No live browser."""

from unittest.mock import Mock

import pytest

from jev_driver import discover as disc
from jev_driver.browser import _is_ephemeral_url, find_continuable_page
from jev_driver.takeover import TAKEOVER_REASON, WatchAgent


@pytest.fixture(autouse=True)
def reset_last(monkeypatch):
    disc.LAST = None
    monkeypatch.delenv("JEV_CDP_URL", raising=False)
    monkeypatch.delenv("BROWSER_CDP_URL", raising=False)
    yield
    disc.LAST = None


def test_watch_uses_running_tb_without_open(monkeypatch):
    opens = []
    monkeypatch.setattr(
        disc,
        "_terminal_browser_discovery",
        lambda: disc.Discovery("ws://127.0.0.1:1/devtools/browser/a", "http://127.0.0.1:1", "terminal-browser"),
    )
    monkeypatch.setattr(disc, "_watch_open_split", lambda url: opens.append(url) or None)
    found = disc.discover(watch=True, launch_url="https://example.test/")
    assert found.source == "terminal-browser"
    assert found.visibility == "terminal-browser-pane"
    assert opens == []


def test_watch_opens_split_when_tb_binary_exists(monkeypatch):
    monkeypatch.setattr(disc, "_terminal_browser_discovery", lambda: None)
    monkeypatch.setattr(disc, "resolve_terminal_browser", lambda: "/bin/terminal-browser")
    spawned = []

    def fake_open(url):
        spawned.append(url)
        return disc.Discovery(
            "ws://127.0.0.1:2/devtools/browser/b",
            "http://127.0.0.1:2",
            "terminal-browser",
            auto_launched=True,
            visibility="terminal-browser-pane",
        )

    monkeypatch.setattr(disc, "_watch_open_split", fake_open)
    found = disc.discover(watch=True, launch_url="https://example.test/flights")
    assert spawned == ["https://example.test/flights"]
    assert found.auto_launched is True
    assert found.visibility == "terminal-browser-pane"


def test_watch_missing_binary_is_actionable(monkeypatch):
    monkeypatch.setattr(disc, "_terminal_browser_discovery", lambda: None)
    monkeypatch.setattr(disc, "resolve_terminal_browser", lambda: None)
    with pytest.raises(disc.WatchUnavailable, match="not installed"):
        disc.discover(watch=True)
    with pytest.raises(disc.WatchUnavailable, match="kitty-graphics"):
        disc.discover(watch=True)


def test_watch_open_surfaces_tb_stderr(monkeypatch):
    monkeypatch.setattr(disc, "_terminal_browser_discovery", lambda: None)
    monkeypatch.setattr(disc, "resolve_terminal_browser", lambda: "/bin/terminal-browser")

    class Proc:
        def __init__(self):
            self.stderr = Mock(read=lambda: "unsupported terminal: iTerm2\n")
            self.stdout = Mock(read=lambda: "")

        def poll(self):
            return 1

    monkeypatch.setattr(disc.subprocess, "Popen", lambda *a, **k: Proc())
    with pytest.raises(disc.WatchUnavailable, match="unsupported terminal"):
        disc._watch_open_split("https://example.test/")


def test_watch_agent_yields_on_stale_before_act():
    agent = WatchAgent.__new__(WatchAgent)
    browser = Mock()
    browser.fresh.return_value = False
    agent.pending_text = None
    agent.screenshots = False
    agent.record_dir = None
    agent.state = {
        "browser": browser,
        "page": {"url": "https://example.test/", "fingerprint": "a", "text": "", "actions": []},
        "decision": {"choice": "e1"},
        "goal": "x",
        "history": [],
        "status": "ready",
        "plan": ["x"],
        "plan_index": 0,
        "decisions": [],
        "text_calls": [],
        "elapsed_ms": 0,
        "started_at": None,
        "record": False,
    }
    snap = agent.command("tick")
    assert snap["status"] == "blocked"
    assert snap["takeover"] is True
    browser.act.assert_not_called()
    assert TAKEOVER_REASON


def test_ephemeral_fixture_urls():
    assert _is_ephemeral_url("file:///x/jev-terminal-browser-driver/fixtures/click.html")
    assert not _is_ephemeral_url("https://www.google.com/travel/flights")


def test_find_continuable_page_uses_remembered_target(monkeypatch, tmp_path):
    path = tmp_path / "last-page.json"
    path.write_text('{"targetId": "T1", "url": "https://www.google.com/travel/flights"}')
    monkeypatch.setattr("jev_driver.browser.LAST_PAGE_PATH", path)
    monkeypatch.setattr(
        "jev_driver.browser._json_pages",
        lambda: [
            {"id": "T1", "type": "page", "url": "https://www.google.com/travel/flights?tfs=1"},
            {"id": "FIX", "type": "page", "url": "file:///x/jev-terminal-browser-driver/fixtures/click.html"},
        ],
    )
    target, url = find_continuable_page()
    assert target == "T1"
    assert "flights" in url
