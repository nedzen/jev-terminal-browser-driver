"""CDP helper contract: unwrap result, strip Target sessionId, raise RuntimeError."""

from unittest.mock import Mock

import pytest

from jev_driver import cdp as cdp_mod
from jev_driver.browser import browser_operation


def test_target_methods_do_not_carry_session_id(monkeypatch):
    sent = []

    def fake_recv_until(ws, message_id):
        return {"id": message_id, "result": {"targetInfos": []}}

    ws = Mock()
    ws.send = lambda payload: sent.append(payload)
    monkeypatch.setattr(cdp_mod, "_ws", ws)
    monkeypatch.setattr(cdp_mod, "_recv_until", fake_recv_until)
    monkeypatch.setattr(cdp_mod, "_next_id", 0)
    cdp_mod.cdp("Target.getTargets", session_id="should-drop")
    body = __import__("json").loads(sent[0])
    assert "sessionId" not in body
    assert body["method"] == "Target.getTargets"


def test_runtime_methods_keep_session_id(monkeypatch):
    sent = []

    def fake_recv_until(ws, message_id):
        return {"id": message_id, "result": {"result": {"value": "ok"}}}

    ws = Mock()
    ws.send = lambda payload: sent.append(payload)
    monkeypatch.setattr(cdp_mod, "_ws", ws)
    monkeypatch.setattr(cdp_mod, "_recv_until", fake_recv_until)
    monkeypatch.setattr(cdp_mod, "_next_id", 0)
    result = cdp_mod.cdp("Runtime.evaluate", session_id="sess-1", expression="1")
    body = __import__("json").loads(sent[0])
    assert body["sessionId"] == "sess-1"
    assert result == {"result": {"value": "ok"}}


def test_cdp_error_raises_runtime_error(monkeypatch):
    def fake_recv_until(ws, message_id):
        return {"id": message_id, "error": {"message": "Inspect before retrying"}}

    monkeypatch.setattr(cdp_mod, "_ws", Mock(send=Mock()))
    monkeypatch.setattr(cdp_mod, "_recv_until", fake_recv_until)
    monkeypatch.setattr(cdp_mod, "_next_id", 0)
    with pytest.raises(RuntimeError, match="Inspect before retrying"):
        cdp_mod.cdp("Runtime.evaluate", session_id="s", expression="1")


def test_select_interrupt_is_runtime_error_not_stale(monkeypatch):
    import jev_driver.browser as browser

    monkeypatch.setattr(
        browser,
        "cdp",
        Mock(return_value={"exceptionDetails": {"text": "Execution context destroyed"}}),
    )
    with pytest.raises(RuntimeError, match="Dropdown execution"):
        browser_operation(
            {
                "operation": "act",
                "session": "test",
                "action": {"id": "e1", "kind": "select", "node": 1, "value": "Design"},
            }
        )
