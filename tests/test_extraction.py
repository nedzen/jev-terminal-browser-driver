"""Hydration, scaled scroll, DriveAgent guard, degenerate flag, HUD. No live browser."""

from pathlib import Path
from unittest.mock import Mock

from jev_driver.browser import Browser, browser_operation
from jev_driver.cli import degenerate, tick_record
from jev_driver.drive_agent import DriveAgent
from plugin import handler


def test_hydrate_stops_when_text_stops_growing(monkeypatch):
    pages = [
        {"actions": [{}] * 3, "text": "a" * 10},
        {"actions": [{}] * 9, "text": "a" * 40},
        {"actions": [{}] * 9, "text": "a" * 40},
    ]
    calls = {"n": 0}

    def once(self, screenshot=True):
        page = pages[min(calls["n"], len(pages) - 1)]
        calls["n"] += 1
        return page

    monkeypatch.setattr(Browser, "_observe_once", once)
    monkeypatch.setattr(Browser, "HYDRATE_MAX_ROUNDS", 3)
    monkeypatch.setattr(Browser, "HYDRATE_MIN_ACTIONS", 8)
    monkeypatch.setattr(Browser, "HYDRATE_SLEEP_S", 0)
    b = Browser.__new__(Browser)
    b._needs_hydrate = True
    out = b.observe(screenshot=False)
    assert calls["n"] == 3
    assert out["text"] == "a" * 40
    assert b._needs_hydrate is False


def test_hydrate_single_observe_when_already_rich(monkeypatch):
    calls = {"n": 0}

    def once(self, screenshot=True):
        calls["n"] += 1
        return {"actions": [{}] * 20, "text": "ready"}

    monkeypatch.setattr(Browser, "_observe_once", once)
    monkeypatch.setattr(Browser, "HYDRATE_MAX_ROUNDS", 3)
    monkeypatch.setattr(Browser, "HYDRATE_MIN_ACTIONS", 8)
    monkeypatch.setattr(Browser, "HYDRATE_SLEEP_S", 0)
    b = Browser.__new__(Browser)
    b._needs_hydrate = True
    b.observe(screenshot=False)
    assert calls["n"] == 1


def test_scroll_delta_scales_to_inner_height(monkeypatch):
    sent = []

    def fake_cdp(method, session_id=None, **params):
        sent.append((method, params))
        if method == "Runtime.evaluate":
            return {"result": {"value": {"h": 1000, "w": 1200}}}
        return {}

    monkeypatch.setattr("jev_driver.browser.cdp", fake_cdp)
    action = {"kind": "scroll", "delta": 560, "id": "scroll_down"}
    browser_operation({"operation": "act", "session": "s", "action": action})
    wheel = [p for m, p in sent if m == "Input.dispatchMouseEvent"][0]
    assert wheel["deltaY"] == 800
    assert wheel["x"] == 600
    assert wheel["y"] == 500


def test_drive_agent_unblocks_advancing_scrolls():
    agent = DriveAgent.__new__(DriveAgent)
    agent.debug = False
    agent.screenshots = False
    agent.state = {
        "status": "blocked",
        "history": [{"kind": "scroll"}, {"kind": "scroll"}, {"kind": "scroll"}],
        "page": {"scroll": {"y": 200}, "url": "https://example.test/", "text": "", "actions": []},
        "browser": Mock(),
        "goal": "x",
        "decision": None,
        "decisions": [],
        "started_at": None,
        "record": False,
        "text_calls": [],
        "elapsed_ms": 0,
    }
    snap = agent._maybe_unblock_scroll({"status": "blocked"}, before_y=0)
    assert agent.state["status"] == "ready"
    assert snap["status"] == "ready"


def test_drive_agent_keeps_repeat_guard_for_clicks():
    agent = DriveAgent.__new__(DriveAgent)
    agent.debug = False
    agent.state = {
        "status": "blocked",
        "history": [{"kind": "click"}, {"kind": "click"}, {"kind": "click"}],
        "page": {"scroll": {"y": 0}, "url": "https://example.test/", "text": "", "actions": []},
        "browser": Mock(),
        "goal": "x",
        "decision": None,
        "decisions": [],
        "started_at": None,
        "record": False,
        "text_calls": [],
        "elapsed_ms": 0,
    }
    agent._maybe_unblock_scroll({"status": "blocked"}, before_y=0)
    assert agent.state["status"] == "blocked"


def test_degenerate_helper():
    assert degenerate({"operation_probabilities": {"SCROLL_DOWN": 0.49, "BLOCKED": 0.44}})
    assert not degenerate({"operation_probabilities": {"CLICK": 0.9, "BLOCKED": 0.1}})
    rec = tick_record(
        {
            "status": "ready",
            "page": {"url": "https://example.test/"},
            "history": [],
            "decisions": [{"operation_probabilities": {"SCROLL_DOWN": 0.49, "BLOCKED": 0.44}}],
        }
    )
    assert rec.get("degenerate") is True


def test_hud_js_is_inert_and_noninteractive():
    src = Path("jev_driver/hud.js").read_text()
    assert "aria-hidden" in src
    assert "inert" in src
    assert "<button" not in src
    assert "a[href]" not in src and "<a " not in src


def test_handler_passes_debug_flag():
    argv = handler.build_argv({"goal": "g", "debug": True})
    assert "--debug" in argv
    argv = handler.build_argv({"goal": "g"})
    assert "--debug" not in argv


def test_compact_result_copies_degenerate():
    out = handler.compact_result(
        [
            {"event": "browser", "source": "x"},
            {"status": "ready", "url": "https://example.test/", "degenerate": True},
        ],
        0,
    )
    assert out["degenerate"] is True
