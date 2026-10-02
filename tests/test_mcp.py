"""MCP stdio server contract. No live browser: tool calls use the same
short-circuit paths as test_plugin_handler (missing goal), or monkeypatched
run_drive/run_read."""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from plugin import handler

ROOT = Path(__file__).resolve().parents[1]


def load_mcp():
    spec = importlib.util.spec_from_file_location("jev_mcp", ROOT / "scripts" / "mcp.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def mcp():
    return load_mcp()


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
    assert set(tools) == {"jev_drive", "jev_read"}
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
    mcp.dispatch(req("tools/call", {"name": "jev_drive", "arguments": {"goal": "g", "debug": True}}))
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
    assert {t["name"] for t in by_id[2]["result"]["tools"]} == {"jev_drive", "jev_read"}
    body = json.loads(by_id[3]["result"]["content"][0]["text"])
    assert body["error"] == "goal is required"
    assert by_id[4]["result"] == {}
