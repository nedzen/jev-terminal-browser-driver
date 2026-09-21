"""Discovery ladder order. No live browser."""

from unittest.mock import Mock

import pytest

from jev_driver import discover as disc


@pytest.fixture(autouse=True)
def reset_last(monkeypatch):
    disc.LAST = None
    monkeypatch.delenv("JEV_CDP_URL", raising=False)
    monkeypatch.delenv("BROWSER_CDP_URL", raising=False)
    yield
    disc.LAST = None


def test_explicit_skips_terminal_browser(monkeypatch):
    tb = Mock(side_effect=AssertionError("tb should not run"))
    monkeypatch.setattr(disc, "_terminal_browser_discovery", tb)
    monkeypatch.setattr(disc, "normalize_cdp_url", lambda raw: "ws://127.0.0.1:9/devtools/browser/x")
    found = disc.discover(explicit="http://127.0.0.1:9")
    assert found.source == "explicit"
    assert found.ws_url.endswith("/devtools/browser/x")
    tb.assert_not_called()


def test_terminal_browser_skips_agent_browser(monkeypatch):
    monkeypatch.setattr(
        disc,
        "_terminal_browser_discovery",
        lambda: disc.Discovery("ws://127.0.0.1:50785/devtools/browser/a", "http://127.0.0.1:50785", "terminal-browser"),
    )
    ab = Mock(side_effect=AssertionError("agent-browser should not run"))
    monkeypatch.setattr(disc, "resolve_agent_browser", ab)
    found = disc.discover()
    assert found.source == "terminal-browser"
    ab.assert_not_called()


def test_empty_tb_uses_agent_browser_daemon(monkeypatch):
    monkeypatch.setattr(disc, "_terminal_browser_discovery", lambda: None)
    monkeypatch.setattr(disc, "resolve_agent_browser", lambda: "/bin/agent-browser")
    monkeypatch.setattr(disc, "agent_browser_cdp_url", lambda binary, session=disc.SESSION: "ws://127.0.0.1:9222/devtools/browser/d")
    monkeypatch.setattr(disc, "_loopback_discovery", lambda: (_ for _ in ()).throw(AssertionError("loopback")))
    found = disc.discover()
    assert found.source == "agent-browser-daemon"
    assert found.session == disc.SESSION


def test_auto_provision_uses_jev_driver_session(monkeypatch):
    launches = []
    monkeypatch.setattr(disc, "_terminal_browser_discovery", lambda: None)
    monkeypatch.setattr(disc, "resolve_agent_browser", lambda: "/bin/agent-browser")
    calls = {"n": 0}

    def cdp_url(binary, session=disc.SESSION):
        calls["n"] += 1
        if calls["n"] == 1:
            return None
        return "ws://127.0.0.1:9333/devtools/browser/h"

    monkeypatch.setattr(disc, "agent_browser_cdp_url", cdp_url)
    monkeypatch.setattr(disc, "_loopback_discovery", lambda: None)
    monkeypatch.setattr(disc.time, "sleep", lambda _s: None)

    def launch(binary, url):
        launches.append((binary, url))

    monkeypatch.setattr(disc, "_launch_headless", launch)
    found = disc.discover(launch_url="https://example.test/")
    assert found.source == "headless-launched"
    assert found.auto_launched is True
    assert launches == [("/bin/agent-browser", "https://example.test/")]


def test_auto_provision_polls_cdp_url_after_launch(monkeypatch):
    monkeypatch.setattr(disc, "_terminal_browser_discovery", lambda: None)
    monkeypatch.setattr(disc, "resolve_agent_browser", lambda: "/bin/agent-browser")
    monkeypatch.setattr(disc, "_loopback_discovery", lambda: None)
    monkeypatch.setattr(disc, "_launch_headless", lambda binary, url: None)
    sleeps = []
    monkeypatch.setattr(disc.time, "sleep", lambda s: sleeps.append(s))
    calls = {"n": 0}

    def cdp_url(binary, session=disc.SESSION):
        calls["n"] += 1
        if calls["n"] < 4:
            return None
        return "ws://127.0.0.1:9333/devtools/browser/h"

    monkeypatch.setattr(disc, "agent_browser_cdp_url", cdp_url)
    found = disc.discover(launch_url="about:blank")
    assert found.source == "headless-launched"
    assert calls["n"] == 4
    assert sleeps == [disc.POST_LAUNCH_DELAY_S, disc.POST_LAUNCH_DELAY_S]


def test_resolve_agent_browser_prefers_path_then_bundled(monkeypatch, tmp_path):
    bundled = tmp_path / "agent-browser"
    bundled.write_text("#!/bin/sh\n")
    bundled.chmod(0o755)
    def which(name):
        return "/usr/bin/agent-browser" if name == "agent-browser" else None

    monkeypatch.setattr(disc.shutil, "which", which)
    assert disc.resolve_agent_browser() == "/usr/bin/agent-browser"
    monkeypatch.setattr(disc.shutil, "which", lambda name: None)
    monkeypatch.setattr(disc, "BUNDLED_AGENT_BROWSER", bundled)
    assert disc.resolve_agent_browser() == str(bundled)
