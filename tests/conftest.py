import pytest

from jev_driver import browser, runlog


@pytest.fixture(autouse=True)
def isolated_state(monkeypatch, tmp_path):
    """Tests must never touch the user's real run log or remembered tab."""
    monkeypatch.setattr(runlog, "JSONL_PATH", tmp_path / "run-log" / "drive.jsonl")
    monkeypatch.setattr(runlog, "TEXT_PATH", tmp_path / "run-log" / "drive.log")
    monkeypatch.setattr(browser, "LAST_PAGE_PATH", tmp_path / "run-log" / "last-page.json")
    monkeypatch.setattr(browser, "HUD_STATE_PATH", tmp_path / "run-log" / "hud.json")
