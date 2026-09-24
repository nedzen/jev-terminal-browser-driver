"""Plugin handler subprocess contract. No Hermes, no live browser."""

import json
import subprocess
import sys
import types
from unittest.mock import Mock

import pytest

import plugin
from jev_driver.cli import tick_record
from plugin import handler


class FakeProc:
    def __init__(self, stdout="", returncode=0, timeout=False):
        self.stdout = stdout
        self.returncode = returncode
        self.timeout = timeout
        self.pid = 4242
        self.killed = False

    def communicate(self, timeout=None):
        if self.timeout:
            self.timeout = False
            raise subprocess.TimeoutExpired(cmd="drive", timeout=timeout)
        return self.stdout, ""

    def poll(self):
        return self.returncode

    def terminate(self):
        self.killed = True

    def kill(self):
        self.killed = True


@pytest.fixture(autouse=True)
def home(monkeypatch, tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "drive.py").write_text("# drive\n")
    (tmp_path / "fixtures").mkdir()
    (tmp_path / "fixtures" / "click.html").write_text("<a>Widget</a>")
    monkeypatch.setenv("JEV_DRIVER_HOME", str(tmp_path))
    monkeypatch.setattr(handler, "LOG_DIR", tmp_path / "logs")
    return tmp_path


def test_happy_path_aggregates_ticks(home):
    stdout = "\n".join(
        [
            json.dumps(
                {
                    "event": "browser",
                    "source": "terminal-browser",
                    "cdp_url": "ws://127.0.0.1:9/devtools/browser/x",
                    "auto_launched": False,
                }
            ),
            json.dumps(
                {
                    "status": "ready",
                    "url": "file:///click.html",
                    "last_action": "Widget",
                    "usage": {"input_tokens": 10, "output_tokens": 2, "cost": 0.001},
                }
            ),
            json.dumps(
                {
                    "status": "done",
                    "url": "file:///click.html#widget",
                    "last_action": "DONE",
                    "usage": {"input_tokens": 5, "output_tokens": 1, "cost": 0.0005},
                }
            ),
        ]
    )
    proc = FakeProc(stdout=stdout, returncode=0)
    result = handler.run_drive({"goal": "Click Widget"}, popen=lambda *a, **k: proc)
    assert result["success"] is True
    assert result["status"] == "done"
    assert result["final_url"].endswith("#widget")
    assert result["actions"] == ["Widget", "DONE"]
    assert result["ticks"] == 2
    assert result["usage"]["input_tokens"] == 15
    assert result["browser"]["source"] == "terminal-browser"


def test_missing_drive_does_not_spawn(monkeypatch, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("JEV_DRIVER_HOME", str(empty))
    popen = Mock(side_effect=AssertionError("must not spawn"))
    result = handler.run_drive({"goal": "x"}, popen=popen)
    assert result["success"] is False
    assert "drive.py missing" in result["error"]
    popen.assert_not_called()


def test_timeout_kills_process_group(home):
    kills = []
    proc = FakeProc(timeout=True)

    def kill(p):
        kills.append(p.pid)
        p.killed = True
        p.returncode = -9

    result = handler.run_drive({"goal": "Click", "timeout_s": 1}, popen=lambda *a, **k: proc, kill_group=kill)
    assert result["status"] == "blocked"
    assert result["error"] == "timeout"
    assert result["success"] is False
    assert kills == [4242]


def test_schema_is_openai_function_shape():
    schema = plugin.SCHEMA
    assert schema["name"] == "jev_drive"
    assert schema["description"]
    assert schema["parameters"]["type"] == "object"
    assert "goal" in schema["parameters"]["required"]
    assert "goal" in schema["parameters"]["properties"]


def test_schema_accepts_watch_and_passes_argv(home):
    assert plugin.SCHEMA["parameters"]["properties"]["watch"]["type"] == "boolean"
    captured = {}

    def popen(argv, **kwargs):
        captured["argv"] = argv
        return FakeProc(stdout=json.dumps({"status": "done", "url": "x"}), returncode=0)

    handler.run_drive({"goal": "g", "watch": True}, popen=popen)
    assert "--watch" in captured["argv"]
    assert "--url" not in captured["argv"]


def test_page_text_truncated_on_final_tick_only():
    snap = {
        "status": "ready",
        "page": {"url": "https://example.test/", "text": "x" * 3000},
        "history": [],
        "decisions": [],
    }
    ready = tick_record(snap)
    assert "page_text" not in ready
    snap["status"] = "done"
    done = tick_record(snap)
    assert done["page_text"] == "x" * 2000


def test_compact_result_passes_page_text():
    rows = [
        {"status": "done", "url": "file:///x", "page_text": "hello widget"},
    ]
    out = handler.compact_result(rows, 0)
    assert out["page_text"] == "hello widget"


def test_compact_result_passes_continuity_meta():
    rows = [
        {
            "event": "browser",
            "source": "agent-browser-daemon",
            "continuity": "dropped:stale-id",
        },
        {"status": "done", "url": "https://example.test/"},
    ]
    out = handler.compact_result(rows, 0)
    assert out["browser"]["continuity"] == "dropped:stale-id"


def test_tui_run_does_not_open_desktop_preview(monkeypatch, home):
    calls = []
    dui = types.ModuleType("tools.desktop_ui")
    tools = types.ModuleType("tools")

    def emit_or_error(*args, **kwargs):
        calls.append(args)
        raise AssertionError("desktop preview is out of TUI-only scope")

    dui.emit_or_error = emit_or_error
    tools.desktop_ui = dui
    monkeypatch.setitem(sys.modules, "tools", tools)
    monkeypatch.setitem(sys.modules, "tools.desktop_ui", dui)
    proc = FakeProc(stdout=json.dumps({"status": "done", "url": "x", "why": "done"}), returncode=0)
    result = handler.run_drive({"goal": "g", "url": "https://example.test/widget"}, popen=lambda *a, **k: proc)
    assert calls == []
    assert result["success"] is True


def test_compact_result_keeps_debug_insights():
    out = handler.compact_result(
        [
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
        ],
        0,
    )
    assert out["why"] == "Model chose DONE."
    assert [row["operation"] for row in out["insights"]] == ["CLICK", "DONE"]
    assert out["browser"]["visibility"] == "terminal-browser-pane"


def test_max_steps_and_timeout_clamped(home):
    captured = {}

    def popen(argv, **kwargs):
        captured["argv"] = argv
        return FakeProc(stdout=json.dumps({"status": "done", "url": "x"}), returncode=0)

    handler.run_drive({"goal": "g", "max_steps": 99, "timeout_s": 9999}, popen=popen)
    assert "--max-steps" in captured["argv"]
    assert captured["argv"][captured["argv"].index("--max-steps") + 1] == "30"
    assert handler.clamp_timeout(9999) == 900


def test_debug_setting_ignores_the_model_flag():
    assert "debug" not in plugin.SCHEMA["parameters"]["properties"]
    assert plugin.apply_debug_setting({"goal": "g", "debug": False}, True)["debug"] is True
    assert plugin.apply_debug_setting({"goal": "g", "debug": True}, False)["debug"] is False


def test_read_expression_scrolls_then_runs_the_body():
    from jev_driver.page_read import read_expression

    outline = read_expression(None, 2)
    assert "scrollBy" in outline
    assert "outline" in outline
    scripted = read_expression("return document.title", 99)
    assert "return document.title" in scripted
    assert "i < 15" in scripted
    assert "\\n" not in scripted
    assert "return \n" not in outline


def test_blocked_row_error_reaches_the_agent(home):
    out = handler.compact_result([{"status": "blocked", "error": "No page to reuse. Pass url."}], 1)
    assert out["error"] == "No page to reuse. Pass url."


def test_timeout_is_logged_by_the_handler(home):
    handler.run_drive({"goal": "g"}, popen=lambda argv, **kw: FakeProc(timeout=True), kill_group=lambda proc: None)
    rows = [json.loads(line) for line in (home / "logs" / "drive.jsonl").read_text().splitlines()]
    assert rows[-1]["event"] == "handler"
    assert rows[-1]["tool"] == "jev_drive"
    assert "timeout" in rows[-1]["error"]


def test_driver_home_walks_up_to_drive_py(monkeypatch):
    monkeypatch.delenv("JEV_DRIVER_HOME", raising=False)
    assert (handler.driver_home() / "scripts" / "drive.py").is_file()


def test_read_script_forms_all_return_a_value():
    from jev_driver.page_read import read_page

    class Page:
        def __init__(self):
            self.calls = []

        def call(self, method, **params):
            expression = params["expression"]
            self.calls.append(expression)
            if "await (return" in expression or "await (const" in expression:
                return {"exceptionDetails": {"exception": {"description": "SyntaxError: Unexpected token"}}}
            return {"result": {"value": {"json": "1"}}}

        def evaluate(self, expression):
            return "https://example.test/"

    for script in ("document.title", "() => 1", "return 1", "const a = 1; return a"):
        page = Page()
        assert read_page(page, script, 0)["data"] == 1
    assert len(page.calls) == 2
