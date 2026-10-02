"""MCP stdio server contract. No live browser: tool calls use the same
short-circuit paths as test_plugin_handler (missing goal), or monkeypatched
run_drive/run_read."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from plugin import handler

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def mcp(load_mcp):
    return load_mcp("jev_mcp")


@pytest.fixture(autouse=True)
def logs(monkeypatch, tmp_path):
    monkeypatch.setattr(handler, "LOG_DIR", tmp_path / "logs")


def req(method, params=None, msg_id=1):
    msg = {"jsonrpc": "2.0", "id": msg_id, "method": method}
    if params is not None:
        msg["params"] = params
    return msg


def test_initialize_negotiates_version(mcp):
    res = mcp.dispatch(req("initialize", {"protocolVersion": "2024-11-05"}))
    assert res["result"]["protocolVersion"] == "2024-11-05"
    assert res["result"]["serverInfo"]["name"] == "jev-driver"
    assert "tools" in res["result"]["capabilities"]


def test_initialize_unknown_version_falls_back(mcp):
    res = mcp.dispatch(req("initialize", {"protocolVersion": "1999-01-01"}))
    assert res["result"]["protocolVersion"] in mcp.SUPPORTED_PROTOCOLS


def test_tools_list_matches_hermes_schemas(mcp):
    from plugin import DESCRIPTION, PARAMETERS, READ_DESCRIPTION, READ_PARAMETERS

    res = mcp.dispatch(req("tools/list"))
    tools = {t["name"]: t for t in res["result"]["tools"]}
    assert set(tools) == {"jev_drive", "jev_read", "jev_status"}
    assert tools["jev_drive"]["inputSchema"] == PARAMETERS
    assert tools["jev_drive"]["description"] == DESCRIPTION
    assert tools["jev_read"]["inputSchema"] == READ_PARAMETERS
    assert tools["jev_read"]["description"] == READ_DESCRIPTION
    assert "goal" in tools["jev_drive"]["inputSchema"]["required"]


def test_notification_returns_none(mcp):
    assert mcp.dispatch({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_unknown_method(mcp):
    res = mcp.dispatch(req("nope/method"))
    assert res["error"]["code"] == mcp.METHOD_NOT_FOUND
    assert res["id"] == 1


def test_invalid_request(mcp):
    res = mcp.dispatch({"nope": True})
    assert res["error"]["code"] == mcp.INVALID_REQUEST


def test_call_unknown_tool(mcp):
    res = mcp.dispatch(req("tools/call", {"name": "jev_fly", "arguments": {}}))
    assert res["error"]["code"] == mcp.INVALID_PARAMS


def test_call_drive_missing_goal_short_circuits(mcp):
    res = mcp.dispatch(req("tools/call", {"name": "jev_drive", "arguments": {}}))
    body = json.loads(res["result"]["content"][0]["text"])
    assert body["error"] == "goal is required"
    assert res["result"]["isError"] is True


def test_call_strips_client_debug(mcp, monkeypatch):
    seen = {}

    def fake_drive(payload):
        seen.update(payload)
        return {"success": True, "status": "done"}

    monkeypatch.setattr(handler, "run_drive", fake_drive)
    monkeypatch.delenv("JEV_DEBUG", raising=False)
    mcp.dispatch(req("tools/call", {"name": "jev_drive", "arguments": {"goal": "g", "debug": True}}))
    assert seen["debug"] is True  # overlay on by default; model flag ignored


def test_debug_opt_out(mcp, monkeypatch):
    seen = {}

    def fake_drive(payload):
        seen.update(payload)
        return {"success": True, "status": "done"}

    monkeypatch.setattr(handler, "run_drive", fake_drive)
    monkeypatch.setenv("JEV_DEBUG", "0")
    mcp.dispatch(req("tools/call", {"name": "jev_drive", "arguments": {"goal": "g"}}))
    assert seen["debug"] is False


def test_call_read_returns_last_row(mcp, monkeypatch):
    monkeypatch.setattr(handler, "run_read", lambda payload: {"success": True, "url": "https://x.test/"})
    res = mcp.dispatch(req("tools/call", {"name": "jev_read", "arguments": {}}))
    body = json.loads(res["result"]["content"][0]["text"])
    assert body["url"] == "https://x.test/"
    assert res["result"]["isError"] is False


def test_call_blocked_is_not_mcp_error(mcp, monkeypatch):
    monkeypatch.setattr(
        handler, "run_drive", lambda payload: {"success": False, "status": "blocked", "reason": "max_steps"}
    )
    res = mcp.dispatch(req("tools/call", {"name": "jev_drive", "arguments": {"goal": "g"}}))
    assert res["result"]["isError"] is False


def test_stdio_end_to_end():
    """Full loop over stdio: initialize, list, call. No browser needed —
    the drive call fails fast on the missing goal before spawning."""
    proc = subprocess.Popen(
        [sys.executable, "scripts/mcp.py"],
        cwd=str(ROOT),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        lines = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "jev_drive", "arguments": {}}},
            {"jsonrpc": "2.0", "id": 4, "method": "ping"},
        ]
        assert proc.stdin is not None
        proc.stdin.write("\n".join(json.dumps(m) for m in lines) + "\n")
        proc.stdin.close()
        out = proc.communicate(timeout=60)[0]
    finally:
        if proc.poll() is None:
            proc.kill()
    rows = [json.loads(line) for line in out.splitlines() if line.strip().startswith("{")]
    by_id = {r.get("id"): r for r in rows if r.get("id") is not None}
    # notification (no id) gets no reply: 4 replies for 5 messages
    assert set(by_id) == {1, 2, 3, 4}
    assert by_id[1]["result"]["serverInfo"]["name"] == "jev-driver"
    assert {t["name"] for t in by_id[2]["result"]["tools"]} == {"jev_drive", "jev_read", "jev_status"}
    body = json.loads(by_id[3]["result"]["content"][0]["text"])
    assert body["error"] == "goal is required"
    assert by_id[4]["result"] == {}


# --- error bifurcation: client mistakes vs server faults ------------------
# (a) bad client args answer with an error object and leave the loop alone,
# (b) an unexpected handler exception answers internal-error, records the
# exception class, and the stdio loop keeps serving. No browser, no retries.


def _serve(mcp, lines):
    """Push messages (or raw text) through the real stdio loop."""
    import io

    stdin = io.StringIO("".join((line if isinstance(line, str) else json.dumps(line)) + "\n" for line in lines))
    out = io.StringIO()
    assert mcp.serve(stdin, out) == 0
    return [json.loads(row) for row in out.getvalue().splitlines() if row.strip()]


def test_unknown_tool_is_a_client_error_and_runs_no_handler(mcp, monkeypatch, tmp_path):
    def never(payload):
        raise AssertionError("an unknown tool must not reach a handler")

    monkeypatch.setattr(handler, "run_drive", never)
    res = mcp.dispatch(req("tools/call", {"name": "jev_fly", "arguments": {}}))
    assert res["error"]["code"] == mcp.INVALID_PARAMS
    assert not (tmp_path / "logs" / "drive.jsonl").exists()  # not a server fault


def test_bad_client_input_does_not_disturb_the_loop(mcp):
    rows = _serve(
        mcp,
        [
            "{not json",
            req("tools/call", {"name": "jev_fly", "arguments": {}}, msg_id=1),
            req("tools/call", {"name": "jev_drive", "arguments": "not an object"}, msg_id=2),
            req("tools/call", {"arguments": {}}, msg_id=3),
            req("nope/method", msg_id=4),
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            req("initialize", {}, msg_id=5),
        ],
    )
    assert rows[0]["error"]["code"] == mcp.PARSE_ERROR
    assert rows[0]["id"] is None
    assert [r["error"]["code"] for r in rows[1:5]] == [mcp.INVALID_PARAMS] * 3 + [mcp.METHOD_NOT_FOUND]
    assert rows[-1]["result"]["serverInfo"]["name"] == "jev-driver"
    assert len(rows) == 6  # the notification got no reply


def test_handler_crash_is_internal_error_and_never_logs_the_message(mcp, monkeypatch, tmp_path):
    def boom(payload):
        raise RuntimeError("cdp connect failed for wss://x.test/?api_key=sk-live-SUPERSECRET")

    monkeypatch.setattr(handler, "run_drive", boom)
    res = mcp.dispatch(req("tools/call", {"name": "jev_drive", "arguments": {"goal": "g"}}))
    assert res["error"]["code"] == mcp.INTERNAL_ERROR
    assert res["error"]["message"] == "internal error"
    assert "SUPERSECRET" not in json.dumps(res)
    logged = (tmp_path / "logs" / "drive.jsonl").read_text()
    assert "RuntimeError" in logged  # the class is what triage needs
    assert "SUPERSECRET" not in logged  # never the message


def test_a_value_error_from_the_handler_is_still_a_server_fault(mcp, monkeypatch):
    """Client mistakes are rejected before the handler runs, so anything it
    raises is ours: internal error, not invalid params."""
    monkeypatch.setattr(handler, "run_read", lambda payload: (_ for _ in ()).throw(ValueError("bad state")))
    res = mcp.dispatch(req("tools/call", {"name": "jev_read", "arguments": {}}))
    assert res["error"]["code"] == mcp.INTERNAL_ERROR
    assert res["error"]["message"] == "internal error"


def test_a_broken_logger_cannot_take_the_call_down(mcp, monkeypatch):
    def broken_logger(*args, **kwargs):
        raise OSError("log disk gone")

    def boom(payload):
        raise RuntimeError("boom")

    monkeypatch.setattr(handler, "run_drive", boom)
    monkeypatch.setattr(handler, "log_handler_event", broken_logger)
    res = mcp.dispatch(req("tools/call", {"name": "jev_drive", "arguments": {"goal": "g"}}))
    assert res["error"]["code"] == mcp.INTERNAL_ERROR


def test_stdio_loop_survives_a_handler_crash(mcp, monkeypatch):
    calls = []

    def flaky(payload):
        calls.append(payload)
        if len(calls) == 1:
            raise RuntimeError("driver subprocess exploded")
        return {"success": True, "status": "done"}

    monkeypatch.setattr(handler, "run_drive", flaky)
    rows = _serve(
        mcp,
        [
            req("tools/call", {"name": "jev_drive", "arguments": {"goal": "g"}}, msg_id=1),
            req("tools/list", msg_id=2),
            req("tools/call", {"name": "jev_drive", "arguments": {"goal": "g"}}, msg_id=3),
        ],
    )
    by_id = {r["id"]: r for r in rows}
    assert set(by_id) == {1, 2, 3}
    assert by_id[1]["error"]["code"] == mcp.INTERNAL_ERROR
    assert by_id[2]["result"]["tools"] == mcp._tool_list()
    assert by_id[3]["result"]["isError"] is False
    assert len(calls) == 2


def test_unserializable_response_becomes_an_internal_error(mcp, monkeypatch):
    real = mcp.dispatch
    state = {"first": True}

    def flaky(msg):
        if state["first"] and msg.get("method") == "tools/list":
            state["first"] = False
            return {"jsonrpc": "2.0", "id": msg["id"], "result": {"tools": {1, 2}}}
        return real(msg)

    monkeypatch.setattr(mcp, "dispatch", flaky)
    rows = _serve(mcp, [req("tools/list", msg_id=1), req("initialize", {}, msg_id=2)])
    assert rows[0]["error"]["code"] == mcp.INTERNAL_ERROR
    assert rows[1]["result"]["serverInfo"]["name"] == "jev-driver"


def test_subprocess_survives_garbage_input():
    """Same guarantee through the real process entrypoint."""
    proc = subprocess.Popen(
        [sys.executable, "scripts/mcp.py"],
        cwd=str(ROOT),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        lines = [
            "{not json",
            json.dumps(req("tools/call", {"name": "jev_fly", "arguments": {}}, msg_id=1)),
            json.dumps(req("tools/call", {"name": "jev_drive", "arguments": ["not", "an", "object"]}, msg_id=2)),
            json.dumps(req("initialize", {}, msg_id=3)),
        ]
        assert proc.stdin is not None
        proc.stdin.write("\n".join(lines) + "\n")
        proc.stdin.close()
        out = proc.communicate(timeout=60)[0]
    finally:
        if proc.poll() is None:
            proc.kill()
    rows = [json.loads(line) for line in out.splitlines() if line.strip().startswith("{")]
    assert rows[0]["error"]["code"] == -32700  # parse error, loop continues
    by_id = {r.get("id"): r for r in rows if r.get("id") is not None}
    assert by_id[1]["error"]["code"] == -32602
    assert by_id[2]["error"]["code"] == -32602
    assert by_id[3]["result"]["serverInfo"]["name"] == "jev-driver"
