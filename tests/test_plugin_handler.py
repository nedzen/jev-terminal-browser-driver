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


def test_preview_emit_called_with_url(monkeypatch, home):
    calls = []
    dui = types.ModuleType("tools.desktop_ui")
    tools = types.ModuleType("tools")

    def emit_or_error(event, payload, fail_prefix, desktop_only, result):
        calls.append((event, payload, result))
        return json.dumps(result)

    dui.emit_or_error = emit_or_error
    tools.desktop_ui = dui
    monkeypatch.setitem(sys.modules, "tools", tools)
    monkeypatch.setitem(sys.modules, "tools.desktop_ui", dui)
    proc = FakeProc(stdout=json.dumps({"status": "done", "url": "x"}), returncode=0)
    handler.run_drive({"goal": "g", "url": "https://example.test/widget"}, popen=lambda *a, **k: proc)
    assert calls[0][0] == "preview.open"
    assert calls[0][1] == {"url": "https://example.test/widget", "label": "Jev"}


def test_preview_import_failure_is_silent(monkeypatch, home):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "tools" or name.startswith("tools."):
            raise ImportError("missing")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    handler.maybe_open_preview("https://example.test/")
    proc = FakeProc(stdout=json.dumps({"status": "done", "url": "x"}), returncode=0)
    result = handler.run_drive({"goal": "g"}, popen=lambda *a, **k: proc)
    assert result["success"] is True


def test_max_steps_and_timeout_clamped(home):
    captured = {}

    def popen(argv, **kwargs):
        captured["argv"] = argv
        return FakeProc(stdout=json.dumps({"status": "done", "url": "x"}), returncode=0)

    handler.run_drive({"goal": "g", "max_steps": 99, "timeout_s": 9999}, popen=popen)
    assert "--max-steps" in captured["argv"]
    assert captured["argv"][captured["argv"].index("--max-steps") + 1] == "30"
    assert handler.clamp_timeout(9999) == 900
