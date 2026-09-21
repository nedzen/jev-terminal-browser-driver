"""Plugin handler subprocess contract. No Hermes, no live browser."""

import json
import subprocess
from unittest.mock import Mock

import pytest

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


def test_max_steps_and_timeout_clamped(home):
    captured = {}

    def popen(argv, **kwargs):
        captured["argv"] = argv
        return FakeProc(stdout=json.dumps({"status": "done", "url": "x"}), returncode=0)

    handler.run_drive({"goal": "g", "max_steps": 99, "timeout_s": 9999}, popen=popen)
    assert "--max-steps" in captured["argv"]
    assert captured["argv"][captured["argv"].index("--max-steps") + 1] == "30"
    assert handler.clamp_timeout(9999) == 900
