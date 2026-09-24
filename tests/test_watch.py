"""Watch ladder, takeover yield, session continuity. No live browser."""

import json
import time
from unittest.mock import Mock

import pytest

from jev_driver import browser as br
from jev_driver import discover as disc
from jev_driver.browser import (
    _is_ephemeral_url,
    _netloc_id,
    find_continuable_page,
    remember_page,
    set_lease,
)
from jev_driver.discover import Discovery
from jev_driver.takeover import TAKEOVER_REASON, WatchAgent


@pytest.fixture(autouse=True)
def reset_last(monkeypatch):
    disc.LAST = None
    set_lease(tab="new")
    monkeypatch.delenv("JEV_CDP_URL", raising=False)
    monkeypatch.delenv("BROWSER_CDP_URL", raising=False)
    yield
    disc.LAST = None
    set_lease(tab="new")


def test_watch_uses_running_tb_without_open(monkeypatch):
    opens = []
    monkeypatch.setattr(
        disc,
        "_terminal_browser_discovery",
        lambda: disc.Discovery("ws://127.0.0.1:1/devtools/browser/a", "http://127.0.0.1:1", "terminal-browser"),
    )
    monkeypatch.setattr(disc, "_provision_terminal_browser", lambda url: opens.append(url) or None)
    found = disc.discover(watch=True, launch_url="https://example.test/")
    assert found.source == "terminal-browser"
    assert found.visibility == "terminal-browser-pane"
    assert opens == []


def test_watch_opens_split_when_tb_binary_exists(monkeypatch):
    monkeypatch.setattr(disc, "_terminal_browser_discovery", lambda: None)
    monkeypatch.setattr(disc, "_daemon_db_discovery", lambda: None)
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

    monkeypatch.setattr(disc, "_provision_terminal_browser", fake_open)
    found = disc.discover(watch=True, launch_url="https://example.test/flights")
    assert spawned == ["https://example.test/flights"]
    assert found.auto_launched is True
    assert found.visibility == "terminal-browser-pane"


def test_watch_missing_binary_is_actionable(monkeypatch):
    monkeypatch.setattr(disc, "_terminal_browser_discovery", lambda: None)
    monkeypatch.setattr(disc, "_daemon_db_discovery", lambda: None)
    monkeypatch.setattr(disc, "resolve_terminal_browser", lambda: None)
    with pytest.raises(disc.WatchUnavailable, match="not installed"):
        disc.discover(watch=True)
    with pytest.raises(disc.WatchUnavailable, match="kitty-graphics"):
        disc.discover(watch=True)


def test_watch_open_surfaces_tb_stderr(monkeypatch):
    monkeypatch.setattr(disc, "_terminal_browser_discovery", lambda: None)
    monkeypatch.setattr(disc, "resolve_terminal_browser", lambda: "/bin/terminal-browser")

    class Completed:
        returncode = 1
        stdout = ""
        stderr = "unsupported terminal: iTerm2\n"

    monkeypatch.setattr(disc.subprocess, "run", lambda argv, **k: Completed())
    with pytest.raises(disc.WatchUnavailable, match="unsupported terminal"):
        disc._provision_terminal_browser("https://example.test/")


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


def test_netloc_id_normalizes_ipv6():
    assert _netloc_id("ws://127.0.0.1:9222/devtools/browser/x") == "127.0.0.1:9222"
    assert _netloc_id("ws://[::1]:9222/devtools/browser/x") == "[::1]:9222"
    assert _netloc_id("http://[::1]:50785") == "[::1]:50785"


def _pointer(path, **fields):
    rec = {
        "targetId": "T1",
        "url": "https://www.google.com/travel/flights",
        "source": "agent-browser-daemon",
        "browser_id": "127.0.0.1:9222",
        "ts": time.time(),
    }
    rec.update(fields)
    path.write_text(json.dumps(rec))
    return rec


def _identity(monkeypatch, source="agent-browser-daemon", ws="ws://127.0.0.1:9222/devtools/browser/x"):
    monkeypatch.setattr(
        br._discover,
        "LAST",
        Discovery(ws, "http://127.0.0.1:9222", source),
    )


def test_find_continuable_page_uses_remembered_target(monkeypatch, tmp_path):
    path = tmp_path / "last-page.json"
    _pointer(path)
    monkeypatch.setattr(br, "LAST_PAGE_PATH", path)
    _identity(monkeypatch)
    monkeypatch.setattr(
        br,
        "_json_pages",
        lambda: [
            {"id": "T1", "type": "page", "url": "https://www.google.com/travel/flights?tfs=1"},
            {"id": "FIX", "type": "page", "url": "file:///x/jev-terminal-browser-driver/fixtures/click.html"},
        ],
    )
    target, url = find_continuable_page()
    assert target == "T1"
    assert "flights" in url
    assert br.LAST_CONTINUITY == "re-attach"


def test_dead_id_does_not_fall_back_to_other_live_tabs(monkeypatch, tmp_path):
    path = tmp_path / "last-page.json"
    _pointer(path, targetId="T-DEAD")
    monkeypatch.setattr(br, "LAST_PAGE_PATH", path)
    _identity(monkeypatch)
    monkeypatch.setattr(
        br,
        "_json_pages",
        lambda: [
            {"id": "T-AA", "type": "page", "url": "https://artificialanalysis.ai/"},
            {"id": "T-OTHER", "type": "page", "url": "https://detail.design/about"},
        ],
    )
    assert find_continuable_page() == (None, None)
    assert br.LAST_CONTINUITY == "dropped:stale-id"


def test_ttl_expired_is_not_continuable(monkeypatch, tmp_path):
    path = tmp_path / "last-page.json"
    _pointer(path, ts=time.time() - 1801)
    monkeypatch.setattr(br, "LAST_PAGE_PATH", path)
    _identity(monkeypatch)
    monkeypatch.setattr(
        br,
        "_json_pages",
        lambda: [{"id": "T1", "type": "page", "url": "https://www.google.com/travel/flights"}],
    )
    assert find_continuable_page() == (None, None)
    assert br.LAST_CONTINUITY == "dropped:ttl-expired"


def test_source_mismatch_is_not_continuable(monkeypatch, tmp_path):
    path = tmp_path / "last-page.json"
    _pointer(path, source="terminal-browser", browser_id="127.0.0.1:50785")
    monkeypatch.setattr(br, "LAST_PAGE_PATH", path)
    _identity(monkeypatch, source="agent-browser-daemon", ws="ws://127.0.0.1:9222/devtools/browser/x")
    monkeypatch.setattr(
        br,
        "_json_pages",
        lambda: [{"id": "OTHER", "type": "page", "url": "https://www.google.com/travel/flights"}],
    )
    assert find_continuable_page() == (None, None)
    assert br.LAST_CONTINUITY == "dropped:browser-mismatch"


def test_label_mismatch_reuses_the_tab_on_this_browser(monkeypatch, tmp_path):
    path = tmp_path / "last-page.json"
    _pointer(path, targetId="T1", source="terminal-browser", browser_id="127.0.0.1:1")
    monkeypatch.setattr(br, "LAST_PAGE_PATH", path)
    _identity(monkeypatch, source="terminal-browser", ws="ws://127.0.0.1:9222/devtools/browser/x")
    monkeypatch.setattr(
        br,
        "_json_pages",
        lambda: [{"id": "T1", "type": "page", "url": "https://x.com/explore"}],
    )
    target, url = find_continuable_page()
    assert target == "T1"
    assert url == "https://x.com/explore"
    assert br.LAST_CONTINUITY == "re-attach"


def test_stem_match_same_browser(monkeypatch, tmp_path):
    path = tmp_path / "last-page.json"
    _pointer(path, targetId="T1", url="https://www.google.com/travel/flights")
    monkeypatch.setattr(br, "LAST_PAGE_PATH", path)
    _identity(monkeypatch)
    monkeypatch.setattr(
        br,
        "_json_pages",
        lambda: [
            {"id": "T2", "type": "page", "url": "https://www.google.com/travel/flights?tfs=abc"},
        ],
    )
    target, url = find_continuable_page()
    assert target == "T2"
    assert "flights" in url


def test_legacy_schema_is_not_continuable(monkeypatch, tmp_path):
    path = tmp_path / "last-page.json"
    path.write_text('{"targetId": "T1", "url": "https://www.google.com/travel/flights"}')
    monkeypatch.setattr(br, "LAST_PAGE_PATH", path)
    _identity(monkeypatch)
    monkeypatch.setattr(
        br,
        "_json_pages",
        lambda: [{"id": "T1", "type": "page", "url": "https://www.google.com/travel/flights"}],
    )
    assert find_continuable_page() == (None, None)
    assert br.LAST_CONTINUITY == "dropped:legacy-schema"


def test_remembered_fixture_tab_is_reused(monkeypatch, tmp_path):
    path = tmp_path / "last-page.json"
    _pointer(
        path,
        targetId="FIX",
        url="file:///x/jev-terminal-browser-driver/fixtures/click.html",
    )
    monkeypatch.setattr(br, "LAST_PAGE_PATH", path)
    _identity(monkeypatch)
    monkeypatch.setattr(
        br,
        "_json_pages",
        lambda: [
            {"id": "FIX", "type": "page", "url": "file:///x/jev-terminal-browser-driver/fixtures/click.html?jev=1"},
            {"id": "USER", "type": "page", "url": "https://x.com/home"},
        ],
    )
    target, url = find_continuable_page()
    assert target == "FIX"
    assert "fixtures/click.html" in url


def test_remember_stores_fixture_tab(monkeypatch, tmp_path):
    path = tmp_path / "last-page.json"
    monkeypatch.setattr(br, "LAST_PAGE_PATH", path)
    _identity(monkeypatch)
    set_lease(tab="new")
    remember_page("FIX", "file:///x/jev-terminal-browser-driver/fixtures/click.html")
    stored = json.loads(path.read_text())
    assert stored["targetId"] == "FIX"


def test_remember_new_tab_changes_target_id(monkeypatch, tmp_path):
    path = tmp_path / "last-page.json"
    monkeypatch.setattr(br, "LAST_PAGE_PATH", path)
    _identity(monkeypatch)
    set_lease(tab="new")
    remember_page("T-NEW", "https://artificialanalysis.ai/")
    stored = json.loads(path.read_text())
    assert stored["targetId"] == "T-NEW"
    assert stored["source"] == "agent-browser-daemon"
    assert stored["browser_id"] == "127.0.0.1:9222"


def test_remember_continuity_same_id_refreshes_url(monkeypatch, tmp_path):
    path = tmp_path / "last-page.json"
    _pointer(path, targetId="T1", url="https://www.google.com/travel/flights")
    monkeypatch.setattr(br, "LAST_PAGE_PATH", path)
    _identity(monkeypatch)
    set_lease(tab="target", target_id="T1")
    remember_page("T1", "https://www.google.com/travel/flights?tfs=later")
    stored = json.loads(path.read_text())
    assert stored["targetId"] == "T1"
    assert "tfs=later" in stored["url"]


def test_remember_continuity_different_id_does_not_overwrite(monkeypatch, tmp_path):
    path = tmp_path / "last-page.json"
    _pointer(path, targetId="T1", url="https://www.google.com/travel/flights")
    monkeypatch.setattr(br, "LAST_PAGE_PATH", path)
    _identity(monkeypatch)
    set_lease(tab="target", target_id="T2")
    remember_page("T2", "https://artificialanalysis.ai/")
    stored = json.loads(path.read_text())
    assert stored["targetId"] == "T1"
    assert "flights" in stored["url"]
