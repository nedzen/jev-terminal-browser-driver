"""Hydration, scaled scroll, DriveAgent guard, degenerate flag, HUD. No live browser."""

from pathlib import Path
from unittest.mock import Mock

from jev_driver.browser import Browser, StalePage, _offer_enter, browser_operation
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
    assert "payload.marks" in src
    assert "3px solid #3dff7a" in src
    assert "<pre" not in src
    assert "position:absolute;top:0;left:0;right:0" not in src
    assert "<button" not in src
    assert "a[href]" not in src and "<a " not in src


def test_handler_passes_debug_flag():
    argv = handler.build_argv({"goal": "g", "debug": True})
    assert "--debug" in argv
    assert "--no-debug" not in argv
    argv = handler.build_argv({"goal": "g"})
    assert "--no-debug" in argv
    assert "--debug" not in argv
    assert "--background" not in argv
    argv = handler.build_argv({"goal": "g", "debug": False, "cdp_url": "http://127.0.0.1:9"})
    assert "--cdp" not in argv
    argv = handler.build_argv({"goal": "g", "background": True, "cdp_url": "http://127.0.0.1:9"})
    assert "--background" in argv
    assert "--cdp" in argv


def test_hud_payload_names_candidates():
    agent = DriveAgent.__new__(DriveAgent)
    agent.state = {
        "goal": "Click Widget",
        "status": "predicted",
        "page": {
            "url": "file:///click.html",
            "fingerprint": "same",
            "actions": [
                {"id": "e1", "kind": "click", "node": 4, "role": "link", "label": "Widget", "value": ""},
                {"id": "e2", "kind": "click", "node": 5, "role": "link", "label": "Unrelated", "value": ""},
            ],
        },
        "decision": {
            "choice": "e1",
            "operation": "CLICK",
            "target": "1",
            "confidence": 0.93,
            "fingerprint": "same",
            "operation_probabilities": {"CLICK": 0.93, "DONE": 0.07},
            "target_probabilities": {"1": 0.9, "2": 0.1},
            "request": {
                "questions": {
                    "click_target": {"criteria": {"1": "[1] Widget; link", "2": "[2] Unrelated; link"}}
                }
            },
        },
        "decisions": [],
        "history": [],
    }
    payload = agent._hud_payload()
    assert payload["target_label"] == "Widget"
    assert payload["targets"][0] == ["Widget", 0.9]
    chosen = [mark for mark in payload["marks"] if mark["chosen"]]
    assert chosen[0]["node"] == 4
    assert chosen[0]["label"] == "Widget"


def test_done_tick_names_done_and_keeps_its_own_usage():
    rec = tick_record(
        {
            "status": "done",
            "page": {"url": "file:///x#widget", "text": "Widget"},
            "history": [
                {
                    "action": "Widget",
                    "operation": "CLICK",
                    "page_changed": True,
                    "usage": {"input_tokens": 10, "output_tokens": 1, "cost": 0.1},
                }
            ],
            "decisions": [
                {
                    "choice": "DONE",
                    "operation": "DONE",
                    "confidence": 0.8,
                    "usage": {"input_tokens": 3, "output_tokens": 1, "cost": 0.01},
                    "operation_probabilities": {"DONE": 0.8, "CLICK": 0.2},
                    "target_probabilities": {},
                }
            ],
        },
        debug=True,
    )
    assert rec["last_action"] == "DONE"
    assert rec["usage"]["input_tokens"] == 3
    assert "not proof" in rec["why"]
    assert rec["insight"]["top_ops"][0] == {"name": "DONE", "p": 0.8}
    assert rec["insight"]["target"] is None


def test_compact_result_copies_degenerate():
    out = handler.compact_result(
        [
            {"event": "browser", "source": "x"},
            {"status": "ready", "url": "https://example.test/", "degenerate": True},
        ],
        0,
    )
    assert out["degenerate"] is True


SHELL = "Menu\nHome\nSearch\nAbout\nHelp"


def test_shell_page_is_not_treated_as_content():
    from jev_driver.readiness import done_acceptable, page_is_shell, unsupported_goal

    assert page_is_shell(SHELL)
    assert page_is_shell("")
    assert not page_is_shell("Home\nOpen the Widget page.\nWidget\nUnrelated")
    assert not page_is_shell("ready")
    assert not done_acceptable({"operation_probabilities": {"DONE": 0.34}}, {"text": "A real post. " * 20})
    assert done_acceptable({"operation_probabilities": {"DONE": 0.9}}, {"text": "A real post about Jev. " * 8})
    assert unsupported_goal("Take a screenshot of the results") == "unsupported"
    assert unsupported_goal("Click Latest") is None


def test_hydrate_waits_out_search_chrome(monkeypatch):
    pages = [
        {"actions": [{}] * 10, "text": SHELL},
        {"actions": [{}] * 10, "text": SHELL},
        {"actions": [{}] * 12, "text": "Search timeline\n" + ("Jev drives the browser for the agent. " * 6)},
    ]
    calls = {"n": 0}

    def once(self, screenshot=True):
        page = pages[min(calls["n"], len(pages) - 1)]
        calls["n"] += 1
        return page

    monkeypatch.setattr(Browser, "_observe_once", once)
    monkeypatch.setattr(Browser, "HYDRATE_SLEEP_S", 0)
    b = Browser.__new__(Browser)
    b._needs_hydrate = True
    out = b.observe(screenshot=False)
    assert calls["n"] == 4
    assert "Jev drives" in out["text"]


def test_covered_fill_focuses_and_types(monkeypatch):
    sent = []
    answers = [None, True]

    def fake_cdp(method, session_id=None, **params):
        sent.append((method, params))
        if method == "Runtime.evaluate":
            return {"result": {"value": answers.pop(0)}}
        return {}

    monkeypatch.setattr("jev_driver.browser.cdp", fake_cdp)
    action = {"kind": "fill", "node": 4, "id": "e1", "label": "Search query"}
    result = browser_operation({"operation": "act", "session": "s", "action": action, "text": "jev"})
    assert result["executed"] == "e1"
    assert result["via"] == "focus"
    typed = [params["text"] for method, params in sent if method == "Input.insertText"]
    assert typed == ["jev"]


def test_weak_done_on_shell_waits_for_posts(monkeypatch):
    monkeypatch.setattr("jev_driver.drive_agent.write_event", lambda event: None)
    agent = DriveAgent.__new__(DriveAgent)
    agent.screenshots = False
    agent.debug = False
    browser = Mock()
    browser.sleep = Mock()
    browser._observe_once = Mock(
        return_value={"text": "Jev is the decision layer for browser agents. " * 4, "actions": [], "url": "https://x.com/search"}
    )
    agent.state = {
        "decision": {"choice": "DONE", "operation": "DONE", "operation_probabilities": {"DONE": 0.34}},
        "page": {"text": SHELL, "actions": [], "url": "https://x.com/search"},
        "browser": browser,
        "status": "predicted",
        "history": [],
        "decisions": [],
        "goal": "see posts",
    }
    snap = agent._reject_weak_done()
    assert snap["status"] == "ready"
    assert agent.state["decision"] is None
    assert "decision layer" in snap["page"]["text"]
    browser._observe_once.assert_called()


def test_second_weak_done_is_not_success(monkeypatch):
    monkeypatch.setattr("jev_driver.drive_agent.write_event", lambda event: None)
    agent = DriveAgent.__new__(DriveAgent)
    agent.screenshots = False
    agent._weak_done = 1
    text = "A long visible post about browsing with an agent and Jev. " * 4
    agent.state = {
        "decision": {
            "choice": "DONE",
            "operation": "DONE",
            "operation_probabilities": {"DONE": 0.42, "SCROLL_DOWN": 0.3},
        },
        "page": {"text": text, "actions": [{"id": "scroll_down", "kind": "scroll"}], "url": "https://x.com/search"},
        "browser": Mock(),
        "status": "predicted",
        "history": [],
        "decisions": [],
        "goal": "find posts",
    }
    snap = agent._reject_weak_done()
    assert snap["status"] == "blocked"
    assert snap["stop_reason"] == "weak_done"
    rec = tick_record(snap)
    assert rec["reason"] == "weak_done"
    assert "not confirmed" in rec["why"]
    assert "visible post" in rec["page_text"]
    out = handler.compact_result([rec], 1)
    assert out["success"] is False
    assert out["reason"] == "weak_done"
    assert "visible post" in out["page_text"]


def test_two_unexecuted_types_stop(monkeypatch):
    monkeypatch.setattr("jev_driver.drive_agent.write_event", lambda event: None)
    agent = DriveAgent.__new__(DriveAgent)
    agent.state = {
        "decisions": [{"operation": "TYPE_TEXT", "target": "1", "choice": "e1"}],
        "decision": {"operation": "TYPE_TEXT"},
        "status": "predicted",
        "page": {"text": "x", "actions": [], "url": "u"},
        "history": [],
    }
    assert agent._note_unexecuted(StalePage("Page changed since this decision. Observe again.")) is False
    assert agent._note_unexecuted(StalePage("Page changed since this decision. Observe again.")) is True
    assert agent.state["stop_reason"] == "stale_page"
    assert agent.state["status"] == "blocked"


def test_stale_click_retries_the_same_like(monkeypatch):
    from jev_driver.drive_agent import _click_named

    monkeypatch.setattr("jev_driver.drive_agent.write_event", lambda event: None)
    agent = DriveAgent.__new__(DriveAgent)
    agent.screenshots = False
    clicked = {}

    def act(action, page, text=None):
        clicked["label"] = action["label"]

    browser = Mock()
    browser.act = act
    page = {
        "url": "https://x.com/home",
        "title": "Home",
        "text": "govi",
        "actions": [{"id": "e1", "kind": "click", "node": 3, "label": "13 comments", "role": "button"}],
        "fingerprint": "f",
    }
    browser._observe_once = Mock(return_value=page)
    browser.observe = Mock(return_value=page)
    agent.state = {
        "goal": "Click Like",
        "page": {"url": "https://x.com/home", "title": "Home", "text": "govi", "actions": []},
        "browser": browser,
        "history": [],
        "decisions": [
            {
                "operation": "CLICK",
                "target": "1",
                "request": {"questions": {"click_target": {"criteria": {"1": "[1] 12 comments; button"}}}},
            }
        ],
        "decision": None,
        "status": "predicted",
    }
    assert _click_named(page["actions"], "12 comments")["label"] == "13 comments"
    snap = agent._retry_click(agent.state["decisions"][-1])
    assert snap["status"] == "ready"
    assert clicked["label"] == "13 comments"
    assert agent.state["history"][-1]["kind"] == "click"


def test_stale_fill_retries_on_the_same_label(monkeypatch):
    monkeypatch.setattr("jev_driver.drive_agent.write_event", lambda event: None)
    monkeypatch.setattr(
        "jev_driver.drive_agent.field_text",
        lambda context: ("jev browser agent", {"model": "m", "latency_ms": 1}),
    )
    agent = DriveAgent.__new__(DriveAgent)
    agent.screenshots = False
    typed = {}

    def act(action, page, text=None):
        typed["label"] = action["label"]
        typed["text"] = text

    browser = Mock()
    browser.act = act
    page = {
        "url": "https://x.com/explore",
        "title": "Explore",
        "text": "Explore",
        "actions": [{"id": "e9", "kind": "fill", "node": 9, "label": "Search query", "role": "searchbox", "value": ""}],
        "fingerprint": "f",
    }
    browser._observe_once = Mock(return_value=page)
    browser.observe = Mock(return_value=page)
    agent.state = {
        "goal": "Type jev browser agent",
        "page": {"url": "https://x.com/explore", "title": "Explore", "text": "Explore", "actions": []},
        "browser": browser,
        "history": [],
        "text_calls": [],
        "decisions": [
            {
                "operation": "TYPE_TEXT",
                "target": "1",
                "request": {"questions": {"type_text_target": {"criteria": {"1": "[1] Search query; searchbox"}}}},
            }
        ],
        "decision": None,
        "status": "predicted",
    }
    snap = agent._retry_fill(agent.state["decisions"][-1])
    assert snap["status"] == "ready"
    assert typed == {"label": "Search query", "text": "jev browser agent"}
    assert agent.state["history"][-1]["kind"] == "fill"


def test_fill_freshness_ignores_feed_text(monkeypatch):
    stored_key = ["origin", "https://x.com/explore", 0, 0, 1200, 800, [[4, ""]]]
    now_key = ["origin", "https://x.com/explore", 0, 40, 1200, 800, [[4, ""]]]
    stored_guard = [4, "textbox", "Search query", "", None, None, False, "old feed text"]
    now_guard = [4, "textbox", "Search query", "", None, None, False, "new feed text"]

    def evaluate(self, expression):
        return [now_key, now_guard]

    monkeypatch.setattr(Browser, "evaluate", evaluate)
    browser = Browser.__new__(Browser)
    page = {"page_key": stored_key, "guards": {"4": stored_guard}, "marker": "feed-changed"}
    assert browser.fresh(page, {"kind": "fill", "node": 4}) is True
    now_guard[3] = "typed"
    assert browser.fresh(page, {"kind": "fill", "node": 4}) is False


def test_enter_is_offered_only_after_a_field_has_text():
    from jev_driver.model import action_space

    empty = {"actions": [{"id": "e1", "kind": "fill", "node": 1, "label": "Query", "value": ""}]}
    _offer_enter(empty)
    assert [item["id"] for item in empty["actions"]] == ["e1"]
    filled = {"actions": [{"id": "e1", "kind": "fill", "node": 1, "label": "Query", "value": "jev"}]}
    _offer_enter(filled)
    _offer_enter(filled)
    assert [item["id"] for item in filled["actions"]] == ["e1", "press_enter"]
    _elements, _targets, controls = action_space(filled["actions"])
    assert controls["PRESS_ENTER"]["label"] == "Press Enter"


def test_enter_key_is_dispatched(monkeypatch):
    sent = []

    def fake_cdp(method, session_id=None, **params):
        sent.append((method, params))
        return {}

    monkeypatch.setattr("jev_driver.browser.cdp", fake_cdp)
    action = {"kind": "enter", "id": "press_enter", "label": "Press Enter"}
    result = browser_operation({"operation": "act", "session": "s", "action": action})
    assert result["via"] == "enter"
    keys = [params for method, params in sent if method == "Input.dispatchKeyEvent"]
    assert [item["type"] for item in keys] == ["keyDown", "keyUp"]
    assert keys[0]["key"] == "Enter"
