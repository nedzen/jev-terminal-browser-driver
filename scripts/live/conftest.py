"""Keep the live harness self-tests off the real ~/.cache/wwwdrive."""

from __future__ import annotations

import pytest

from scripts.live import isolation


@pytest.fixture(autouse=True)
def isolated_live_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(isolation, "LOG_DIR", tmp_path / "wwwdrive-cache")
