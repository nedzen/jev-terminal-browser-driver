import importlib.util
from pathlib import Path

import pytest

from jev_driver import browser, runlog
from plugin.core import env as core_env

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def load_mcp():
    """Execute scripts/mcp.py under a module name the caller picks.

    The name is the caller's because `spec.loader.exec_module` is what binds
    the module, and a shared name would hand the next test the one that ran
    first instead of a fresh server.
    """

    def _load(name):
        spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / "mcp.py")
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    return _load


@pytest.fixture(autouse=True)
def isolated_state(monkeypatch, tmp_path):
    """Tests must never touch the user's real run log or remembered tab.

    Patches both runlog paths and plugin handler LOG_DIR: log_handler_event
    writes through core_env.LOG_DIR, and patching only the handler re-export
    left the real ~/.cache/wwwdrive open.
    """
    log_dir = tmp_path / "run-log"
    monkeypatch.setattr(runlog, "LOG_DIR", log_dir)
    monkeypatch.setattr(runlog, "JSONL_PATH", log_dir / "drive.jsonl")
    monkeypatch.setattr(runlog, "TEXT_PATH", log_dir / "drive.log")
    monkeypatch.setattr(browser, "LAST_PAGE_PATH", log_dir / "last-page.json")
    monkeypatch.setattr(browser, "HUD_STATE_PATH", log_dir / "hud.json")
    monkeypatch.setattr(core_env, "LOG_DIR", log_dir)


class Clock:
    """Monotonic clock the test drives by hand, so no wall time is spent."""

    def __init__(self, start=1000.0):
        self.now = float(start)

    def perf_counter(self):
        return self.now

    def advance(self, seconds):
        self.now += float(seconds)
        return self.now


class _Time:
    """Stands in for the time module production code reads its clock from."""

    def __init__(self, clock):
        self.perf_counter = clock.perf_counter
