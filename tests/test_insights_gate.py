"""H2: the ranked operations/targets trace is opt-in, and debug-on is unchanged.

One behavior per test, each named for it. The gate has three seams and each
gets its own test, so removing any one of them fails exactly one test:

- ``compact_result`` drops the trace when the caller did not ask for it
- ``compact_result`` with the gate open returns the trace byte-identical
- ``run_drive`` carries the caller's choice through to the result
- the MCP adapter treats an unset debug env as "off", and =1 as "on"

No browser, no model, no spawn: FakeProc stands in for the subprocess.
"""

import json

import pytest

from plugin import handler

TICK_ROWS = [
    {"event": "browser", "source": "terminal-browser"},
    {
        "status": "ready",
        "url": "file:///click.html",
        "last_action": "Widget",
        "why": "CLICK Widget (page changed)",
        "insight": {"operation": "CLICK", "target": "Widget", "why": "CLICK Widget (page changed)"},
    },
    {
        "status": "done",
        "url": "file:///click.html#widget",
        "last_action": "DONE",
        "why": "Model chose DONE.",
        "insight": {"operation": "DONE", "why": "Model chose DONE."},
    },
]


class FakeProc:
    def __init__(self, stdout="", returncode=0):
        self.stdout = stdout
        self.returncode = returncode
        self.pid = 4242

    def communicate(self, timeout=None):
        return self.stdout, ""

    def poll(self):
        return self.returncode


@pytest.fixture()
def home(monkeypatch, tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "drive.py").write_text("# drive\n")
    monkeypatch.setenv("WWWDRIVE_HOME", str(tmp_path))
    monkeypatch.setattr(handler, "LOG_DIR", tmp_path / "logs")
    return tmp_path


def test_result_carries_no_insight_trace_when_debug_is_off():
    out = handler.compact_result(TICK_ROWS, 0, insights=False)
    assert "insights" not in out


def test_debug_on_returns_the_insight_trace_byte_identical():
    out = handler.compact_result(TICK_ROWS, 0, insights=True)
    assert out["insights"] == [row["insight"] for row in TICK_ROWS if "insight" in row]


def test_gating_the_trace_changes_nothing_else_in_the_result():
    gated = handler.compact_result(TICK_ROWS, 0, insights=False)
    open_ = handler.compact_result(TICK_ROWS, 0, insights=True)
    assert {k: v for k, v in open_.items() if k != "insights"} == gated


def test_run_drive_drops_the_trace_the_caller_did_not_ask_for(home):
    proc = FakeProc(stdout="\n".join(json.dumps(row) for row in TICK_ROWS))
    out = handler.run_drive({"goal": "g", "insights": False}, popen=lambda *a, **k: proc)
    assert out["status"] == "done"
    assert "insights" not in out


def test_run_drive_keeps_the_trace_when_the_caller_asked_for_it(home):
    proc = FakeProc(stdout="\n".join(json.dumps(row) for row in TICK_ROWS))
    out = handler.run_drive({"goal": "g", "insights": True}, popen=lambda *a, **k: proc)
    assert [row["operation"] for row in out["insights"]] == ["CLICK", "DONE"]


def test_unset_debug_env_var_keeps_the_trace_out_of_the_result(load_mcp, monkeypatch):
    monkeypatch.delenv("WWWDRIVE_DEBUG", raising=False)
    monkeypatch.delenv("JEV_DEBUG", raising=False)
    mcp = load_mcp("mcp_insights_unset")
    assert mcp.insights_default() is False
    # The overlay default is untouched: a human still watches the pane.
    assert mcp.debug_default() is True


@pytest.mark.parametrize("raw", ["1", "true", "YES", "on", " 1 "])
def test_explicit_debug_env_var_keeps_the_trace(load_mcp, monkeypatch, raw):
    monkeypatch.setenv("WWWDRIVE_DEBUG", raw)
    monkeypatch.delenv("JEV_DEBUG", raising=False)
    assert load_mcp(f"mcp_insights_on_{raw.strip()}_{abs(hash(raw))}").insights_default() is True


@pytest.mark.parametrize("raw", ["0", "false", "no", ""])
def test_debug_env_var_off_keeps_the_trace_out(load_mcp, monkeypatch, raw):
    monkeypatch.setenv("WWWDRIVE_DEBUG", raw)
    monkeypatch.delenv("JEV_DEBUG", raising=False)
    assert load_mcp(f"mcp_insights_off_{abs(hash(raw))}").insights_default() is False


def test_pre_1_0_env_var_still_turns_the_trace_on(load_mcp, monkeypatch):
    monkeypatch.delenv("WWWDRIVE_DEBUG", raising=False)
    monkeypatch.setenv("JEV_DEBUG", "1")
    assert load_mcp("mcp_insights_jev_debug").insights_default() is True


def test_mcp_drive_call_asks_for_the_trace_only_when_debug_is_explicit(load_mcp, monkeypatch):
    monkeypatch.setenv("WWWDRIVE_DEBUG", "1")
    mcp = load_mcp("mcp_insights_payload_on")
    seen = {}
    monkeypatch.setattr(mcp.h, "run_drive", lambda payload: seen.update(payload) or {"status": "done"})
    mcp._call_tool("drive", {"goal": "g"})
    assert seen["insights"] is True
    assert seen["debug"] is True

    monkeypatch.setenv("WWWDRIVE_DEBUG", "")
    mcp = load_mcp("mcp_insights_payload_off")
    monkeypatch.setattr(mcp.h, "run_drive", lambda payload: seen.update(payload) or {"status": "done"})
    mcp._call_tool("drive", {"goal": "g"})
    assert seen["insights"] is False
    assert seen["debug"] is True


def test_model_supplied_flag_cannot_buy_the_trace(load_mcp, monkeypatch):
    """The trace rides the operator's switch, like the overlay before it."""
    monkeypatch.delenv("WWWDRIVE_DEBUG", raising=False)
    monkeypatch.delenv("JEV_DEBUG", raising=False)
    mcp = load_mcp("mcp_insights_model_flag")
    seen = {}
    monkeypatch.setattr(mcp.h, "run_drive", lambda payload: seen.update(payload) or {"status": "done"})
    mcp._call_tool("drive", {"goal": "g", "insights": True})
    assert seen["insights"] is False
