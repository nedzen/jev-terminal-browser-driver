import importlib.util
from pathlib import Path

import pytest

from jev_driver import browser, runlog

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
    """Tests must never touch the user's real run log or remembered tab."""
    monkeypatch.setattr(runlog, "JSONL_PATH", tmp_path / "run-log" / "drive.jsonl")
    monkeypatch.setattr(runlog, "TEXT_PATH", tmp_path / "run-log" / "drive.log")
    monkeypatch.setattr(browser, "LAST_PAGE_PATH", tmp_path / "run-log" / "last-page.json")
    monkeypatch.setattr(browser, "HUD_STATE_PATH", tmp_path / "run-log" / "hud.json")


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
