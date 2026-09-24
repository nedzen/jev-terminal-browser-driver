"""TUI-only discovery: explicit → running terminal-browser → visible provision."""

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
    found = disc.discover(explicit="http://127.0.0.1:9", background=True)
    assert found.source == "explicit"
    assert found.visibility == "background"
    assert found.ws_url.endswith("/devtools/browser/x")
    tb.assert_not_called()


def test_explicit_cdp_is_ignored_without_background(monkeypatch):
    monkeypatch.setenv("JEV_CDP_URL", "http://127.0.0.1:9")
    monkeypatch.setattr(
        disc,
        "_terminal_browser_discovery",
        lambda: disc.Discovery("ws://127.0.0.1:50785/devtools/browser/a", "http://127.0.0.1:50785", "terminal-browser"),
    )
    found = disc.discover(explicit="http://127.0.0.1:9")
    assert found.source == "terminal-browser"
    assert found.visibility == "terminal-browser-pane"


def test_background_without_cdp_does_not_launch(monkeypatch):
    monkeypatch.setattr(disc, "_provision_terminal_browser", Mock(side_effect=AssertionError("no launch")))
    with pytest.raises(disc.WatchUnavailable, match="does not launch a hidden browser"):
        disc.discover(background=True)


def test_running_terminal_browser_wins(monkeypatch):
    monkeypatch.setattr(
        disc,
        "_terminal_browser_discovery",
        lambda: disc.Discovery("ws://127.0.0.1:50785/devtools/browser/a", "http://127.0.0.1:50785", "terminal-browser"),
    )
    prov = Mock(side_effect=AssertionError("provision should not run"))
    monkeypatch.setattr(disc, "_provision_terminal_browser", prov)
    found = disc.discover()
    assert found.source == "terminal-browser"
    assert found.visibility == "terminal-browser-pane"
    prov.assert_not_called()


def test_no_browser_provisions_visible_pane(monkeypatch):
    monkeypatch.setattr(disc, "_terminal_browser_discovery", lambda: None)
    monkeypatch.setattr(disc, "_daemon_db_discovery", lambda: None)
    calls = []

    def provision(url):
        calls.append(url)
        return disc.Discovery(
            "ws://127.0.0.1:57463/devtools/browser/b",
            "http://127.0.0.1:57463",
            "terminal-browser",
            auto_launched=True,
            visibility="terminal-browser-pane",
        )

    monkeypatch.setattr(disc, "_provision_terminal_browser", provision)
    found = disc.discover(launch_url="https://example.test/")
    assert calls == ["https://example.test/"]
    assert found.source == "terminal-browser"
    assert found.auto_launched is True
    assert found.visibility == "terminal-browser-pane"


def test_no_browser_no_provision_raises(monkeypatch):
    monkeypatch.setattr(disc, "_terminal_browser_discovery", lambda: None)
    monkeypatch.setattr(disc, "_daemon_db_discovery", lambda: None)
    with pytest.raises(disc.WatchUnavailable, match="auto-provision is disabled"):
        disc.discover(auto_provision=False)


def test_no_browser_no_binary_raises(monkeypatch):
    monkeypatch.setattr(disc, "_terminal_browser_discovery", lambda: None)
    monkeypatch.setattr(disc, "_daemon_db_discovery", lambda: None)
    monkeypatch.setattr(disc, "resolve_terminal_browser", lambda: None)
    with pytest.raises(disc.WatchUnavailable, match="terminal-browser is not installed"):
        disc.discover()


def test_daemon_db_discovery_finds_live_instance(tmp_path, monkeypatch):
    import sqlite3

    db = tmp_path / ".local/share/terminal-browser-test/terminal-browser.db"
    db.parent.mkdir(parents=True)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE instances (key text PRIMARY KEY, cdp_port integer, started_at integer)"
        )
        conn.execute("INSERT INTO instances VALUES ('a-1', 57463, 2)")
        conn.execute("INSERT INTO instances VALUES ('b-1', NULL, 1)")
    monkeypatch.setattr(disc.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(disc, "browser_websocket_url", lambda port: f"ws://127.0.0.1:{port}/devtools/browser/x")
    found = disc._daemon_db_discovery()
    assert found is not None
    assert found.http_origin == "http://127.0.0.1:57463"


def test_daemon_db_discovery_none_when_no_db(tmp_path, monkeypatch):
    monkeypatch.setattr(disc.Path, "home", lambda: tmp_path)
    assert disc._daemon_db_discovery() is None


def test_provision_env_scrubs_herdr(monkeypatch):
    monkeypatch.setenv("HERDR_PANE_ID", "wG:p1")
    monkeypatch.setenv("HERDR_ENV", "1")
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("TERM_PROGRAM", "ghostty")
    env = disc._provision_env()
    assert not any(k.startswith("HERDR_") for k in env)
    assert env["PATH"] == "/usr/bin"
    assert env["TERM_PROGRAM"] == "ghostty"


def test_provision_parses_instance_record_cdp_port(monkeypatch):
    record = '{"key": "67930-1", "pid": 67930, "cdpPort": 57463, "url": "https://x.com"}'
    assert disc._instance_record_port(record) == 57463
    assert disc._instance_record_port("not json") is None
    assert disc._instance_record_port('{"cdpPort": "57463"}') is None


def test_provision_command_is_visible_split(monkeypatch, tmp_path):
    monkeypatch.setenv("HERDR_PANE_ID", "wG:p1")
    seen = {}

    class Completed:
        returncode = 0
        stdout = '{"cdpPort": 57463}'
        stderr = ""

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen["env"] = kwargs.get("env")
        return Completed()

    monkeypatch.setattr(disc.subprocess, "run", fake_run)
    monkeypatch.setattr(disc, "browser_websocket_url", lambda port: f"ws://127.0.0.1:{port}/devtools/browser/x")
    found = disc._provision_terminal_browser("https://example.test/")
    argv = seen["argv"]
    assert argv[1:4] == ["open", "https://example.test/", "--split"]
    assert "--no-merge" in argv
    assert "HERDR_PANE_ID" not in seen["env"]
    assert found.visibility == "terminal-browser-pane"
    assert found.auto_launched is True


def test_provision_nonzero_exit_raises(monkeypatch):
    monkeypatch.setattr(disc, "resolve_terminal_browser", lambda: "/bin/terminal-browser")

    class Completed:
        returncode = 1
        stdout = ""
        stderr = "unsupported terminal"

    monkeypatch.setattr(disc.subprocess, "run", lambda argv, **kwargs: Completed())
    with pytest.raises(disc.WatchUnavailable, match="could not open a visible pane"):
        disc._provision_terminal_browser("about:blank")
