"""Tab lease and shared-browser safety. No live Chromium."""

import pytest

from jev_driver import browser
from jev_driver.browser import Browser, set_lease


@pytest.fixture(autouse=True)
def reset_lease():
    set_lease(tab="new")
    yield
    set_lease(tab="new")


def test_close_owned_tui_tab_detaches_and_does_not_destroy(monkeypatch):
    calls = []

    def fake_cdp(method, session_id=None, **params):
        calls.append(method)
        return {}

    monkeypatch.setattr(browser, "cdp", fake_cdp)
    b = Browser.__new__(Browser)
    b.target = "OWNED"
    b.session = "sess"
    b.owned = True
    b.close()
    assert "Target.closeTarget" not in calls
    assert "Target.detachFromTarget" in calls
    assert b.target is None


def test_close_unowned_target_detaches_only(monkeypatch):
    calls = []

    def fake_cdp(method, session_id=None, **params):
        calls.append(method)
        return {}

    monkeypatch.setattr(browser, "cdp", fake_cdp)
    b = Browser.__new__(Browser)
    b.target = "USER"
    b.session = "sess"
    b.owned = False
    b.close()
    assert "Target.closeTarget" not in calls
    assert "Target.detachFromTarget" in calls


def test_target_ok_skips_workers_and_chrome_urls():
    assert not browser._target_ok({"type": "worker", "url": "https://example.test/"}, allow_denylist=False)
    assert not browser._target_ok({"type": "page", "url": "chrome://newtab"}, allow_denylist=False)
    assert not browser._target_ok(
        {"type": "page", "url": "https://hindsight.vectorize.io/x"}, allow_denylist=False
    )
    assert browser._target_ok(
        {"type": "page", "url": "https://hindsight.vectorize.io/x"}, allow_denylist=True
    )
    assert browser._target_ok({"type": "page", "url": "https://en.wikipedia.org/wiki/X"}, allow_denylist=False)


def test_init_does_not_set_device_metrics(monkeypatch):
    methods = []

    def fake_cdp(method, session_id=None, **params):
        methods.append(method)
        if method == "Target.getTargets":
            return {"targetInfos": [{"targetId": "T1", "type": "page", "url": "https://example.test/"}]}
        if method == "Target.attachToTarget":
            return {"sessionId": "S1"}
        if method == "Runtime.evaluate":
            return {"result": {"value": "complete"}}
        return {}

    monkeypatch.setattr(browser, "cdp", fake_cdp)
    monkeypatch.setattr(browser, "connect", lambda: None)
    monkeypatch.setattr(browser, "list_browsers", lambda: {"browsers": [{"key": "1-1", "cdpPort": 1}]})
    monkeypatch.setattr(browser, "_open_owned_tab", lambda url: ("T1", True))
    set_lease(tab="new")
    Browser("https://example.test/")
    assert "Emulation.setDeviceMetricsOverride" not in methods
    assert "Emulation.setFocusEmulationEnabled" in methods
    assert "Target.activateTarget" not in methods


def test_set_lease_target_requires_id(monkeypatch):
    set_lease(tab="target", target_id=None)
    monkeypatch.setattr(browser, "connect", lambda: None)
    with pytest.raises(ValueError, match="target_id"):
        Browser("https://example.test/")
