"""cmux-socket provisioning: the pane is made by cmux, not by terminal-browser
guessing a terminal.

Three layers:
  - unit tests, no terminal touched;
  - entry-path tests, proving each of MCP stdio / scripts/drive.py / the plugin
    path reaches the cmux provisioner rather than the adapter guess;
  - live pane-list checks per entry path, asserting from cmux's own listing (and
    herdr's) that the pane landed outside the herdr pane tree.

No browser is driven. The live checks only provision, and are skipped unless
their own preconditions hold.
"""

import contextlib
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from unittest.mock import Mock

import pytest

from jev_driver import discover as disc

ROOT = Path(__file__).resolve().parents[1]

# The ambient cmux session, captured at import — before any fixture clears it — so a
# live check can ask for it back by name.
REAL_SOCKET_PATH = (os.environ.get("CMUX_SOCKET_PATH") or "").strip()
REAL_WORKSPACE = (os.environ.get("CMUX_WORKSPACE_ID") or "").strip()


@pytest.fixture(autouse=True)
def isolated_state(monkeypatch, tmp_path):
    """Every test starts as a non-cmux session, with no ledger, and leaves state as found.

    The CMUX_* variables are cleared so a test about the adapter route cannot be
    satisfied by whichever terminal the suite happens to run inside. The live tests
    below ask for `real_cmux_session` to put them back — see its docstring for why
    that split matters.
    """
    monkeypatch.delenv("CMUX_SOCKET_PATH", raising=False)
    monkeypatch.delenv("CMUX_WORKSPACE_ID", raising=False)
    monkeypatch.setattr(disc, "LEDGER_PATH", tmp_path / "cmux-instance.json")
    monkeypatch.setattr(disc, "LAST", None)
    yield
    disc.LAST = None


def _socket(monkeypatch, tmp_path, *, workspace="ws-1"):
    """A cmux context backed by a socket path that exists."""
    path = tmp_path / "cmux.sock"
    path.write_text("")
    monkeypatch.setenv("CMUX_SOCKET_PATH", str(path))
    if workspace:
        monkeypatch.setenv("CMUX_WORKSPACE_ID", workspace)
    return str(path)


def _recorded(monkeypatch, *, stdout="", returncode=0):
    """A fake subprocess.run that records argv/env instead of touching a terminal.

    Only the provisioning spawn is recorded; `terminal-browser ls` runs around the
    edges (instance lookup, ledger) and is not what these tests are about.
    """
    seen = {}

    def run(argv, **kwargs):
        argv = list(argv)
        seen["argvs"] = seen.get("argvs", []) + [argv]
        if not (len(argv) >= 3 and os.path.basename(str(argv[0])) == "cmux" and argv[1] == "new-split"):
            return subprocess.CompletedProcess(argv, returncode, stdout, "")
        seen["argv"] = argv
        seen["env"] = kwargs.get("env")
        return subprocess.CompletedProcess(argv, returncode, stdout, "")

    monkeypatch.setattr(disc.subprocess, "run", run)
    return seen


def _adapter_spawn(seen):
    """The terminal-browser provisioning spawn, from the recorded sequence."""
    for argv in seen.get("argvs", []):
        if any(str(item) == "open" for item in argv):
            return argv
    raise AssertionError(f"no terminal-browser open spawn; saw {seen.get('argvs')}")


def _fake_terminal_browser(monkeypatch, tmp_path):
    binary = tmp_path / "terminal-browser"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    monkeypatch.setattr(disc, "resolve_terminal_browser", lambda: str(binary))
    monkeypatch.setattr(disc, "_resolve_cmux", lambda: "/bin/cmux")
    return str(binary)


def _fake_ready(monkeypatch, port=40001):
    monkeypatch.setattr(
        disc,
        "_terminal_browser_discovery",
        lambda: disc.Discovery(f"ws://127.0.0.1:{port}/x", f"http://127.0.0.1:{port}", "terminal-browser"),
    )


# --- the cmux route is taken when, and only when, a socket is there ---


def test_a_cmux_socket_makes_the_pane_instead_of_guessing_a_terminal(monkeypatch, tmp_path):
    """The bug: the adapter chain resolves to herdr and nests the browser. cmux is asked first."""
    _socket(monkeypatch, tmp_path)
    binary = _fake_terminal_browser(monkeypatch, tmp_path)
    seen = _recorded(monkeypatch)
    _fake_ready(monkeypatch)

    found = disc._provision_terminal_browser("https://example.test/")

    # cmux made the split; terminal-browser only ran as the new pane's command.
    assert seen["argv"][:3] == ["/bin/cmux", "new-split", "right"]
    assert "--split" not in seen["argv"], "the new pane already exists; terminal-browser must not split again"
    assert seen["argv"][-2:] == ["--command", f"{binary} open https://example.test/"]
    assert found.auto_launched is True


def test_the_cmux_path_keeps_herdr_vars_out_of_the_child(monkeypatch, tmp_path):
    """Scrubbing is what stops a nested terminal-browser re-entering the herdr adapter."""
    _socket(monkeypatch, tmp_path)
    _fake_terminal_browser(monkeypatch, tmp_path)
    seen = _recorded(monkeypatch)
    monkeypatch.setenv("HERDR_PANE_ID", "wJ:p2T")
    monkeypatch.setenv("HERDR_ENV", "1")
    _fake_ready(monkeypatch)

    disc._provision_terminal_browser("https://example.test/")

    assert not any(key.startswith("HERDR_") for key in seen["env"])
    assert seen["env"]["CMUX_SOCKET_PATH"], "kept: cmux needs to know where its own socket is"


def test_the_split_is_addressed_to_the_calling_workspace(monkeypatch, tmp_path):
    _socket(monkeypatch, tmp_path, workspace="ws-42")
    _fake_terminal_browser(monkeypatch, tmp_path)
    seen = _recorded(monkeypatch)
    _fake_ready(monkeypatch)

    disc._provision_terminal_browser("https://example.test/")

    argv = seen["argv"]
    assert argv[argv.index("--workspace") + 1] == "ws-42"


def test_a_url_cannot_inject_a_second_command_into_the_new_pane(monkeypatch, tmp_path):
    """cmux runs --command through a shell, so the URL must be quoted, not pasted."""
    _socket(monkeypatch, tmp_path)
    binary = _fake_terminal_browser(monkeypatch, tmp_path)
    seen = _recorded(monkeypatch)
    _fake_ready(monkeypatch)

    disc._provision_terminal_browser("https://example.test/; rm -rf /")

    command = seen["argv"][seen["argv"].index("--command") + 1]
    assert command == f"{binary} open 'https://example.test/; rm -rf /'"


def test_provision_routes_on_the_cmux_context_alone(monkeypatch, tmp_path):
    """The branch itself: socket present -> cmux, absent -> terminal-browser.

    The other tests each pin one route's argv; this one pins the decision between
    them, so a delegation that is deleted, inverted, or made unconditional fails
    here even though the route tests could still pass on their own.
    """
    _fake_terminal_browser(monkeypatch, tmp_path)
    routes = []
    monkeypatch.setattr(
        disc, "_provision_via_cmux", lambda url, context, **kw: routes.append(("cmux", context)) or _ready()
    )
    monkeypatch.setattr(disc, "_terminal_browser_discovery", lambda: routes.append(("adapter", None)) or _ready())

    # No CMUX_SOCKET_PATH at all: the adapter route, which parses an instance record.
    monkeypatch.setattr(disc, "_instance_record_port", lambda text: 40005)
    disc._provision_terminal_browser("https://example.test/")

    # Socket present: the cmux route, with the context handed through.
    context = _socket(monkeypatch, tmp_path, workspace="ws-7")
    disc._provision_terminal_browser("https://example.test/")

    assert [name for name, _payload in routes] == ["adapter", "cmux"]
    assert routes[1][1]["workspace"] == "ws-7"
    assert routes[1][1]["socket_path"] == context
    assert routes[0][1] is None  # the adapter route takes no context


def _ready():
    return disc.Discovery("ws://127.0.0.1:40006/x", "http://127.0.0.1:40006", "terminal-browser")


# --- reuse: tabs in the instance a previous drive provisioned, never a second pane ---


def _instance_listing(key="33709-1", port=55538, pid=33709):
    """`terminal-browser ls --all --json` shape, as the instance check reads it."""
    return {
        "browsers": [
            {
                "key": key,
                "pid": pid,
                "cdpPort": port,
                "socket": f"/tmp/instances/{key}.sock",
                "tty": "/dev/ttys013",
                "inCurrentTab": False,
                "tabs": [{"id": 1, "url": "https://example.com/", "active": True}],
            }
        ]
    }


def _ledger_write(monkeypatch, tmp_path, *, key="33709-1", port=55538, pid=33709, workspace="ws-1"):
    _socket(monkeypatch, tmp_path, workspace=workspace)
    disc._write_ledger({"key": key, "port": port, "pid": pid, "socket": ""})
    return disc._read_ledger()


def test_a_provisioned_instance_is_ledged_with_its_identity(monkeypatch, tmp_path):
    """Key, port and pid together: the port alone cannot prove it is the same process."""
    _socket(monkeypatch, tmp_path, workspace="ws-3")
    monkeypatch.setattr(disc, "list_browsers", lambda: _instance_listing(port=55538, pid=33709))
    monkeypatch.setattr(disc.subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "", ""))
    found = disc.Discovery("ws://127.0.0.1:55538/x", "http://127.0.0.1:55538", "terminal-browser")

    disc._remember_instance(found)

    record = disc._read_ledger()
    assert record["key"] == "33709-1"
    assert record["port"] == 55538
    assert record["pid"] == 33709
    assert record["workspace"] == "ws-3"


def test_provisioning_through_cmux_writes_the_ledger(monkeypatch, tmp_path):
    """The wiring, not just the writer: a real cmux provision must leave a record.

    Calling `_remember_instance` directly would still pass if the provisioner
    stopped calling it, so this drives `_provision_via_cmux` and checks the record
    appeared. Without it, deleting the one line that connects the two breaks nothing.
    """
    _socket(monkeypatch, tmp_path, workspace="ws-5")
    _fake_terminal_browser(monkeypatch, tmp_path)
    _recorded(monkeypatch)
    monkeypatch.setattr(disc, "browser_websocket_url", lambda port: f"ws://127.0.0.1:{port}/devtools/browser/x")
    monkeypatch.setattr(disc, "list_browsers", lambda: _instance_listing(port=40001, pid=40001))
    monkeypatch.setattr(
        disc,
        "_terminal_browser_discovery",
        lambda: disc.Discovery("ws://127.0.0.1:40001/x", "http://127.0.0.1:40001", "terminal-browser"),
    )

    assert not disc.LEDGER_PATH.exists()
    disc._provision_via_cmux("https://example.test/", {"workspace": "ws-5", "socket_path": str(tmp_path)})

    record = disc._read_ledger()
    assert record is not None, "provisioning opened a pane but recorded nothing"
    assert (record["port"], record["pid"], record["key"]) == (40001, 40001, "33709-1")


def test_a_ledgered_instance_is_reused_instead_of_a_second_pane(monkeypatch, tmp_path):
    """The whole point: one pane, and the next drive attaches to it.

    Named without the word "live" deliberately. A `-k "not live"` filter deselects
    by substring, so a unit test called `test_a_live_...` is skipped by the very
    command used to run the live checks and to mutation-check the unit ones — and a
    mutation check that silently stops selecting the covering test reports a false
    pass. That happened to this test.
    """
    _ledger_write(monkeypatch, tmp_path)
    monkeypatch.setattr(disc, "list_browsers", _instance_listing)
    monkeypatch.setattr(disc, "browser_websocket_url", lambda port: f"ws://127.0.0.1:{port}/devtools/browser/x")
    provision = Mock(side_effect=AssertionError("must not provision a second pane"))
    monkeypatch.setattr(disc, "_provision_terminal_browser", provision)
    monkeypatch.setattr(disc, "_terminal_browser_discovery", lambda: None)
    monkeypatch.setattr(disc, "_daemon_db_discovery", lambda: None)

    found = disc.discover()

    assert found.http_origin == "http://127.0.0.1:55538"
    assert found.source == "terminal-browser"
    assert found.auto_launched is False, "a reused instance launched nothing"
    provision.assert_not_called()


def test_an_instance_whose_pid_changed_is_not_reused(monkeypatch, tmp_path):
    """A different process on the same port is a different browser. Never drive it."""
    _ledger_write(monkeypatch, tmp_path, pid=33709)
    # No cmux route in this test (after the ledger helper, which installs a
    # fake socket context): the adapter fallback is the subject, and any cmux
    # env would rightly route to the socket instead.
    monkeypatch.delenv("CMUX_SOCKET_PATH", raising=False)
    monkeypatch.delenv("CMUX_WORKSPACE_ID", raising=False)
    monkeypatch.setattr(disc, "list_browsers", lambda: _instance_listing(port=55538, pid=99999))
    monkeypatch.setattr(disc, "browser_websocket_url", lambda port: f"ws://127.0.0.1:{port}/devtools/browser/x")
    monkeypatch.setattr(
        disc,
        "_provision_terminal_browser",
        lambda url, **kw: disc.Discovery("ws://127.0.0.1:60001/x", "http://127.0.0.1:60001", "terminal-browser"),
    )
    monkeypatch.setattr(disc, "_terminal_browser_discovery", lambda: None)
    monkeypatch.setattr(disc, "_daemon_db_discovery", lambda: None)
    monkeypatch.setattr(disc, "_resolve_cmux", lambda: "/bin/cmux")
    monkeypatch.setattr(disc, "resolve_terminal_browser", lambda: "/bin/terminal-browser")

    # Explicit empty env: neither a cmux route nor the herdr refusal may fire
    # here — the adapter fallback is the subject, and the real shell carries
    # both CMUX_* and HERDR_* exports.
    found = disc.discover(env={})

    assert found.http_origin == "http://127.0.0.1:60001", "attached to a stranger's port"


def test_an_instance_that_is_gone_provisions_again(monkeypatch, tmp_path):
    """A dead ledger must send the run back through provisioning, not silently reuse nothing."""
    _ledger_write(monkeypatch, tmp_path)
    # The instance the ledger names is no longer listed: the pane was closed.
    monkeypatch.setattr(disc, "list_browsers", lambda: {"browsers": []})
    monkeypatch.setattr(disc, "_resolve_cmux", lambda: "/bin/cmux")
    monkeypatch.setattr(disc, "resolve_terminal_browser", lambda: "/bin/terminal-browser")
    spawned = []
    monkeypatch.setattr(
        disc.subprocess,
        "run",
        lambda argv, **kw: spawned.append(list(argv)) or subprocess.CompletedProcess(argv, 0, "", ""),
    )

    assert disc._provisioned_instance_discovery() is None

    # The readiness poll answers with the fresh instance, so this stops one poll in.
    monkeypatch.setattr(
        disc,
        "_terminal_browser_discovery",
        lambda: disc.Discovery("ws://127.0.0.1:40008/x", "http://127.0.0.1:40008", "terminal-browser"),
    )
    found = disc._provision_terminal_browser("https://example.test/")

    assert any(argv[:2] == ["/bin/cmux", "new-split"] for argv in spawned), spawned
    assert found.auto_launched is True


def test_a_ledger_from_another_cmux_session_is_ignored(monkeypatch, tmp_path):
    """A pane belongs to the cmux that made it; a foreign record must not satisfy this drive."""
    _ledger_write(monkeypatch, tmp_path, workspace="ws-old")
    monkeypatch.setattr(disc, "list_browsers", _instance_listing)
    monkeypatch.setattr(disc, "browser_websocket_url", lambda port: f"ws://127.0.0.1:{port}/devtools/browser/x")
    monkeypatch.setattr(disc, "_provision_via_cmux", Mock(side_effect=AssertionError("must provision")))

    # A second cmux session on this box: same instance, different socket.
    other = tmp_path / "other-cmux.sock"
    other.write_text("")
    monkeypatch.setenv("CMUX_SOCKET_PATH", str(other))

    assert disc._read_ledger() is None
    assert disc._provisioned_instance_discovery() is None


def test_an_unreadable_ledger_provisions_rather_than_failing(monkeypatch, tmp_path):
    """A corrupt record must cost a pane, never a drive."""
    disc.LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    disc.LEDGER_PATH.write_text("{not json")
    _socket(monkeypatch, tmp_path)

    assert disc._read_ledger() is None
    assert disc._provisioned_instance_discovery() is None


def test_a_stale_socket_still_routes_to_the_adapter(monkeypatch, tmp_path):
    """The branch reads a *live* socket, not merely an exported variable."""
    monkeypatch.setenv("CMUX_SOCKET_PATH", str(tmp_path / "not-here.sock"))
    routes = []
    monkeypatch.setattr(disc, "_provision_via_cmux", lambda *a, **k: routes.append("cmux"))
    monkeypatch.setattr(disc, "_instance_record_port", lambda text: 40007)
    monkeypatch.setattr(disc, "browser_websocket_url", lambda port: f"ws://127.0.0.1:{port}/devtools/browser/a")
    monkeypatch.setattr(disc.subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "", ""))

    disc._provision_terminal_browser("https://example.test/")

    assert routes == [], "a socket path that does not exist is not a cmux context"


def test_without_a_socket_the_adapter_path_still_runs(monkeypatch, tmp_path):
    """Non-cmux terminals keep the terminal-browser split; nothing regresses."""
    binary = _fake_terminal_browser(monkeypatch, tmp_path)
    seen = _recorded(monkeypatch, stdout=json.dumps({"cdpPort": 40002}))
    monkeypatch.setattr(disc, "browser_websocket_url", lambda port: f"ws://127.0.0.1:{port}/devtools/browser/a")

    found = disc._provision_terminal_browser("https://example.test/")

    assert _adapter_spawn(seen) == [binary, "open", "https://example.test/", "--split", "right", "--no-merge"]
    assert found.auto_launched is True


def test_a_stale_socket_path_falls_through_to_the_adapter_path(monkeypatch, tmp_path):
    """An exported socket that no longer exists means cmux is gone, not that cmux is unusable."""
    monkeypatch.setenv("CMUX_SOCKET_PATH", str(tmp_path / "gone.sock"))
    assert disc.cmux_context() is None

    binary = _fake_terminal_browser(monkeypatch, tmp_path)
    seen = _recorded(monkeypatch, stdout=json.dumps({"cdpPort": 40003}))
    monkeypatch.setattr(disc, "browser_websocket_url", lambda port: f"ws://127.0.0.1:{port}/devtools/browser/a")

    disc._provision_terminal_browser("https://example.test/")

    assert _adapter_spawn(seen)[:2] == [binary, "open"]


def test_cmux_context_never_exposes_the_socket_capability(monkeypatch, tmp_path):
    """A credential must not travel through a value that could reach a log or an error."""
    path = _socket(monkeypatch, tmp_path)
    monkeypatch.setenv("CMUX_SOCKET_CAPABILITY", "v1.supersecret")
    monkeypatch.setenv("CMUX_SOCKET_PASSWORD", "hunter2")

    context = disc.cmux_context()
    dumped = json.dumps(context)

    assert context["socket_path"] == path
    assert "supersecret" not in dumped
    assert "hunter2" not in dumped
    assert "CAPABILITY" not in dumped
    assert "PASSWORD" not in dumped


def test_a_cmux_failure_is_reported_not_swallowed(monkeypatch, tmp_path):
    _socket(monkeypatch, tmp_path)
    _fake_terminal_browser(monkeypatch, tmp_path)

    def run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, "", "cmux: no such workspace")

    monkeypatch.setattr(disc.subprocess, "run", run)

    with pytest.raises(disc.WatchUnavailable, match="no such workspace"):
        disc._provision_terminal_browser("https://example.test/")


def test_a_pane_that_never_becomes_ready_is_a_failure_not_a_hidden_browser(monkeypatch, tmp_path):
    _socket(monkeypatch, tmp_path)
    _fake_terminal_browser(monkeypatch, tmp_path)
    _recorded(monkeypatch)
    monkeypatch.setattr(disc, "_terminal_browser_discovery", lambda: None)
    monkeypatch.setattr(disc, "_daemon_db_discovery", lambda: None)
    monkeypatch.setattr(disc.time, "sleep", lambda seconds: None)

    with pytest.raises(disc.WatchUnavailable, match="did not become ready"):
        disc._provision_terminal_browser("https://example.test/")


def test_no_cmux_cli_on_path_is_reported_clearly(monkeypatch, tmp_path):
    _socket(monkeypatch, tmp_path)
    _fake_terminal_browser(monkeypatch, tmp_path)
    monkeypatch.setattr(disc, "_resolve_cmux", lambda: None)

    with pytest.raises(disc.WatchUnavailable, match="cmux CLI is not on PATH"):
        disc._provision_terminal_browser("https://example.test/")


# --- every entry path inherits the fix, because the fix is in discover() ---


class _ReachedSpawn(BaseException):
    """Raised at the moment provisioning would spawn, to end the entry path there.

    A BaseException so the driver code cannot catch it by accident: the point is to
    stop the entry path at its provisioning boundary, before it builds a Browser or
    opens a CDP socket. Everything above that boundary — argument parsing, budget
    validation, the argv the handler built — has already run for real by then.
    """


def _provisioning_argv(monkeypatch):
    """Record the argv each real entry path hands to provisioning, then stop it.

    Only the terminal-binary lookup is faked. The entry path itself runs for real
    from its own entry function, so what is asserted is genuinely the argv that
    path produced, not one this test assembled.

    The two pre-existing-pane readers are stubbed to "none" because provisioning
    only runs when there is no pane yet — and a real machine running this suite
    usually has one. Without the stub the entry path would correctly attach to it
    and never reach the code under test.
    """
    monkeypatch.setattr(disc, "_terminal_browser_discovery", lambda: None)
    monkeypatch.setattr(disc, "_daemon_db_discovery", lambda: None)
    seen = {}

    def run(argv, **kwargs):
        if _is_cmux_split(list(argv)):
            seen.setdefault("argvs", []).append(list(argv))
            raise _ReachedSpawn
        # Everything before provisioning is the entry path doing its own work
        # (terminal-browser ls, for one). Let it answer, so the test sees the real
        # sequence rather than a truncated one.
        return subprocess.CompletedProcess(argv, 1, "", "no instances")

    monkeypatch.setattr(disc.subprocess, "run", run)
    return seen


def _is_cmux_split(argv):
    """A `cmux new-split` spawn, however the cmux binary was resolved."""
    return len(argv) >= 3 and os.path.basename(str(argv[0])) == "cmux" and argv[1] == "new-split"


def _provisioned_argv(seen):
    """The argv that reached the cmux spawn."""
    assert seen.get("argvs"), "the entry path never reached the cmux provisioner"
    return seen["argvs"][0]


def test_the_cli_entry_path_provisions_through_cmux(monkeypatch, tmp_path):
    """scripts/drive.py -> cli.main() -> discover()."""
    from jev_driver import cli

    _socket(monkeypatch, tmp_path)
    binary = _fake_terminal_browser(monkeypatch, tmp_path)
    seen = _provisioning_argv(monkeypatch)

    with pytest.raises(_ReachedSpawn):
        cli.main(["--goal", "Open https://example.com and stop", "--url", "https://example.com", "--no-debug"])

    spawn = _provisioned_argv(seen)
    assert spawn[:3] == ["/bin/cmux", "new-split", "right"]
    assert spawn[-2:] == ["--command", f"{binary} open https://example.com"]


def test_the_mcp_entry_path_provisions_through_cmux(monkeypatch, tmp_path):
    """scripts/mcp.py stdio serve() -> handler.run_drive() -> scripts/drive.py -> discover()."""
    from plugin import handler

    _socket(monkeypatch, tmp_path)
    binary = _fake_terminal_browser(monkeypatch, tmp_path)
    seen = _provisioning_argv(monkeypatch)

    class InlinePopen:
        """Runs the handler's own argv through cli.main instead of spawning it.

        The handler's subprocess boundary is what the MCP server and the Hermes
        plugin both go through, so standing in for the spawn keeps that boundary
        real — argv, cwd, and all — while the driver inside it still executes.
        """

        def __init__(self, argv, **kwargs):
            from jev_driver import cli

            assert str(argv[1]) == "run" and str(argv[3]).endswith("scripts/drive.py"), argv
            self.kwargs = kwargs
            goal = str(argv[argv.index("--goal") + 1])
            url = str(argv[argv.index("--url") + 1])
            # Not wrapped: the sentinel has to leave this constructor so the test
            # can see that the handler's subprocess boundary really was crossed.
            cli.main(["--goal", goal, "--url", url, "--no-debug"])

        def communicate(self, timeout=None):
            return "", ""

        @property
        def returncode(self):
            return 0

    # `popen` is run_drive's own injection seam (its default is bound at def time,
    # so patching the module attribute would not be seen here).
    with pytest.raises(_ReachedSpawn):
        handler.run_drive(
            {"goal": "Open https://example.com and stop", "url": "https://example.com"},
            popen=InlinePopen,
        )

    spawn = _provisioned_argv(seen)
    assert spawn[:3] == ["/bin/cmux", "new-split", "right"]
    assert spawn[-2:] == ["--command", f"{binary} open https://example.com"]


def test_the_read_entry_path_provisions_through_cmux(monkeypatch, tmp_path):
    """scripts/read.py -> discover(). The third caller, same module."""
    import scripts.read as read_entry  # noqa: PLC0415

    _socket(monkeypatch, tmp_path)
    _fake_terminal_browser(monkeypatch, tmp_path)
    seen = _provisioning_argv(monkeypatch)

    with pytest.raises(_ReachedSpawn):
        read_entry.main(["--url", "https://example.com", "--no-debug"])

    assert _provisioned_argv(seen)[:3] == ["/bin/cmux", "new-split", "right"]


def test_every_caller_goes_through_the_one_discover_module():
    """The structural reason the three entry paths cannot diverge.

    `cdp.connect` and `cdp.http_origin` import `discover` lazily from inside the
    function body, so they are checked by reading the module source: that is the
    only place the binding exists.
    """
    import scripts.read as read_entry  # noqa: PLC0415
    from jev_driver import cdp, cli

    assert cli.discover is disc.discover
    assert read_entry.discover is disc.discover
    cdp_source = Path(cdp.__file__).read_text()
    assert "from .discover import LAST, discover" in cdp_source
    assert "from .discover import LAST" in cdp_source


# --- live: the pane really lands outside herdr, per entry path ---


def _cmux_pane_ids():
    """Pane ids from cmux's own listing. None when cmux cannot be asked."""
    if not shutil.which("cmux"):
        return None
    try:
        out = subprocess.run(["cmux", "list-panes"], capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return {token.strip("*") for token in out.split() if token.strip("*").startswith("pane:")}


def _cmux_panes():
    """Pane ref -> surface refs, from cmux's JSON listing. {} when cmux cannot be asked.

    The surface ref is what `cmux close-surface` takes, so the mapping is what
    lets a test close the pane it opened instead of guessing.
    """
    if not shutil.which("cmux"):
        return {}
    try:
        out = subprocess.run(["cmux", "list-panes", "--json"], capture_output=True, text=True, timeout=20).stdout
        panes = json.loads(out)["panes"]
    except (OSError, subprocess.SubprocessError, ValueError, KeyError):
        return {}
    return {pane["ref"]: list(pane.get("surface_refs") or []) for pane in panes}


def _cmux_close(surface_ref):
    """Close one surface, so the pane the test opened does not outlive it.

    `--workspace` is not optional here. cmux rejects an explicit `--surface`
    without a workspace or window context ("close-surface requires --workspace or
    --window with explicit --surface"), so omitting it leaves the pane open — which
    is exactly the leak this hygiene rule exists to prevent, and it fails silently
    unless the return code is checked.

    Returns None on success, or a short record of what cmux said.
    """
    argv = ["cmux", "close-surface", "--surface", surface_ref]
    workspace = (os.environ.get("CMUX_WORKSPACE_ID") or "").strip()
    if workspace:
        argv += ["--workspace", workspace]
    result = subprocess.run(argv, capture_output=True, text=True, timeout=20, check=False)
    if result.returncode == 0:
        return None
    return f"{surface_ref}:rc={result.returncode}:{(result.stderr or result.stdout or '').strip()[:120]}"


def _browser_pids():
    """pids of every terminal-browser instance. {} when the CLI cannot be asked."""
    try:
        data = json.loads(
            subprocess.run(
                ["terminal-browser", "ls", "--all", "--json"], capture_output=True, text=True, timeout=20
            ).stdout
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return set()
    return {int(item["pid"]) for item in data.get("browsers") or [] if item.get("pid")}


def _wait_for(predicate, *, timeout=20.0, interval=0.25):
    """Poll until predicate() is truthy. Returns its value, or None on timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return None


def _herdr_pane_ids():
    """Pane ids from herdr's own listing. None when herdr cannot be asked."""
    if not shutil.which("herdr"):
        return None
    try:
        out = subprocess.run(["herdr", "pane", "list"], capture_output=True, text=True, timeout=20).stdout
        panes = json.loads(out)["result"]["panes"]
    except (OSError, subprocess.SubprocessError, ValueError, KeyError):
        return None
    return {pane["pane_id"] for pane in panes}


def _live_session_ready():
    """A live check needs a real socket, both CLIs, and both listings answering."""
    if not REAL_SOCKET_PATH or not Path(REAL_SOCKET_PATH).exists():
        return False
    return _cmux_pane_ids() is not None and _herdr_pane_ids() is not None


live_only = pytest.mark.skipif(not _live_session_ready(), reason="needs a live cmux-in-herdr session")


@pytest.fixture
def real_cmux_session(monkeypatch):
    """Put the real cmux socket and workspace back for a live check.

    `isolated_state` clears CMUX_* for every test so the adapter-route tests cannot
    be satisfied by the ambient terminal. The live checks are the opposite case and
    must see the genuine socket, or they do not test the cmux route at all.

    That failure was real and it was silent: with the variables cleared, these tests
    took the adapter path — which still opens a pane through ghostty — so "the pane
    landed outside herdr" passed while asserting something about the fallback. Every
    live test therefore also asserts the cmux route was the one taken.
    """
    assert _live_session_ready(), "live check without a live cmux session"
    monkeypatch.setenv("CMUX_SOCKET_PATH", REAL_SOCKET_PATH)
    if REAL_WORKSPACE:
        monkeypatch.setenv("CMUX_WORKSPACE_ID", REAL_WORKSPACE)
    yield


def _assert_pane_landed_outside_herdr(provision, *, ledger=None):
    """Assert where the new pane went, then close everything this test opened.

    Nothing the provisioner says about itself is trusted: cmux reports its own
    panes and herdr reports its own. A pane created inside the herdr pane tree
    grows herdr's list and leaves cmux's untouched — exactly the nesting this fixes
    — so both assertions fail together if it recurs.

    Hygiene, and it is asserted rather than assumed: the pane and every browser
    this test caused are closed, and the pane list is checked to be back at the
    baseline before the test returns. A test that leaks a pane fails here instead
    of quietly leaving one behind.
    """
    before_panes = _cmux_panes()
    before_cmux = set(before_panes)
    before_herdr = _herdr_pane_ids() or set()
    before_browsers = _browser_pids()
    closed = []

    try:
        with contextlib.suppress(_ReachedSpawn):
            provision()

        # `cmux new-split` returns before the pane is registered, so the pane list
        # has to be polled. Asserting straight after the call reads an empty diff
        # and then leaks the pane that arrives a moment later — which is exactly
        # what an earlier version of this test did.
        _wait_for(lambda: set(_cmux_panes()) - before_cmux, timeout=25)

        after_cmux = set(_cmux_panes())
        after_herdr = _herdr_pane_ids() or set()
        new_cmux = after_cmux - before_cmux
        new_herdr = after_herdr - before_herdr

        assert new_cmux, f"cmux reported no new pane; it has {sorted(after_cmux)}"
        assert not new_herdr, (
            f"the pane was created inside the herdr pane tree (new herdr panes: {sorted(new_herdr)}); "
            "the browser is nested where nobody can see it"
        )
    finally:
        closed = _close_what_it_opened(before_panes, before_browsers)

    assert set(_cmux_panes()) == before_cmux, (
        f"this test leaked a cmux pane: baseline {sorted(before_cmux)}, "
        f"now {sorted(set(_cmux_panes()))}; closes={closed}"
    )
    assert not (_browser_pids() - before_browsers), "this test leaked a terminal-browser instance"
    if ledger is not None:
        ledger.unlink(missing_ok=True)


def _close_what_it_opened(before_panes, before_browsers):
    """Return cmux and terminal-browser to the state they were in before the test.

    Order is load-bearing, and it is the other way round from the obvious one. The
    browser is killed first and the pane closed second: a live terminal-browser
    holds its pane open, so closing the surface while it runs leaves the pane
    behind and the ref shifts underneath the next close. Killing first lets the pane
    close cleanly.

    One surface per pass, re-listing between closes, for the same reason: cmux
    re-assigns surface refs as panes go away, so a batch of refs captured up front
    addresses the wrong panes and silently leaks the ones that were meant.

    Best-effort by design: the assertions in the caller are what turn a failed
    cleanup into a test failure.
    """
    said = []
    for pid in _browser_pids() - before_browsers:
        with contextlib.suppress(OSError, ProcessLookupError):
            os.kill(pid, 15)
    _wait_for(lambda: not (_browser_pids() - before_browsers), timeout=10)
    for _attempt in range(10):
        extra = set(_cmux_panes()) - set(before_panes)
        if not extra:
            break
        surfaces = [s for ref in extra for s in _cmux_panes().get(ref, [])]
        if not surfaces:
            break
        before_close = set(_cmux_panes())
        complaint = _cmux_close(surfaces[0])
        if complaint:
            said.append(complaint)
            break  # cmux refused: retrying the same call ten times only buries the reason
        said.append(surfaces[0])
        _wait_for(lambda: set(_cmux_panes()) != before_close, timeout=5)
    return said


def _assert_cmux_route_was_taken(monkeypatch):
    """Fail unless this run provisioned through cmux rather than the adapter path.

    Without this the live checks can pass while testing the wrong thing: the adapter
    path also opens a pane (through ghostty), so "the pane is outside herdr" says
    nothing about which route ran. This is the assertion that would have caught the
    false pass where the cmux environment had been cleared and the fallback was
    silently exercised instead.
    """
    seen = []
    real_run = disc.subprocess.run

    def run(argv, **kwargs):
        argv = [str(item) for item in argv]
        if len(argv) >= 3 and os.path.basename(argv[0]) == "cmux" and argv[1] == "new-split":
            seen.append(argv)
        return real_run(argv, **kwargs)

    monkeypatch.setattr(disc.subprocess, "run", run)
    return seen


def _stop_after_pane_exists(monkeypatch):
    """Let the real cmux split happen, then stop before anything drives a page.

    The pane placement is the thing under test, and it is settled the moment cmux
    has made the pane. Letting the run continue would navigate to example.com and
    spend a model call to learn nothing further about where the pane went, so the
    readiness poll is where the entry path is stopped.

    The two readers are called twice per run — once by `discover` to ask whether a
    pane already exists, once by the provisioner to poll for the new one. Only the
    second pair is stopped: answering the first with "no pane" is what makes the
    run provision at all, and stubbing it to raise would stop the run before cmux
    was ever asked.
    """
    calls = {"n": 0}

    def reader():
        calls["n"] += 1
        if calls["n"] <= 2:
            return None  # no existing pane: go and provision
        raise _ReachedSpawn  # the pane cmux made is not ours to drive

    monkeypatch.setattr(disc, "_terminal_browser_discovery", reader)
    monkeypatch.setattr(disc, "_daemon_db_discovery", reader)


@live_only
def test_live_cli_entry_path_puts_the_pane_outside_herdr(monkeypatch, tmp_path, real_cmux_session):
    """scripts/drive.py: cli.main()'s own discover() call, with a real cmux split."""
    from jev_driver import cli

    ledger = tmp_path / "cmux-instance.json"
    monkeypatch.setattr(disc, "LEDGER_PATH", ledger)
    route = _assert_cmux_route_was_taken(monkeypatch)
    _stop_after_pane_exists(monkeypatch)

    def provision():
        with contextlib.suppress(SystemExit):
            cli.main(["--goal", "noop", "--url", "https://example.com", "--no-debug"])

    _assert_pane_landed_outside_herdr(provision, ledger=ledger)
    assert route, "the live check exercised the adapter path, not the cmux route"


@live_only
def test_live_mcp_entry_path_puts_the_pane_outside_herdr(monkeypatch, tmp_path, real_cmux_session):
    """scripts/mcp.py's route: the handler's own subprocess boundary, really spawned.

    `handler.run_drive` builds argv and spawns `uv run python scripts/drive.py`
    as a genuine child process. A `sitecustomize.py` on the child's PYTHONPATH
    makes that child stop right after cmux has made its pane: a real split,
    observed from a separate process — which is the only honest way to cover the
    MCP stdio case — with no page driven and no paid call.
    """
    from plugin import handler

    stub_dir = tmp_path / "child-stub"
    stub_dir.mkdir()
    marker = tmp_path / "child-route.txt"
    # Same two-call shape as _stop_after_pane_exists: answer "no pane" to
    # discover()'s existence check so the run provisions, then exit the child once
    # the provisioner starts polling for the pane cmux just made.
    #
    # The marker is how this test avoids the false pass it was written for: the
    # provisioning happens in the child, so the parent's spy on subprocess.run never
    # sees it. The child records which route it took, and the test asserts on that.
    (stub_dir / "sitecustomize.py").write_text(
        "import os\n"
        "from jev_driver import discover as d\n"
        f"_MARKER = {str(marker)!r}\n"
        "_calls = []\n"
        "def _reader():\n"
        "    _calls.append(1)\n"
        "    if len(_calls) <= 2:\n"
        "        return None\n"
        "    raise SystemExit(97)\n"
        "d._terminal_browser_discovery = _reader\n"
        "d._daemon_db_discovery = _reader\n"
        "_cmux, _adapter = d._provision_via_cmux, d._provision_terminal_browser\n"
        "def _via_cmux(*a, **k):\n"
        "    open(_MARKER, 'w').write('cmux')\n"
        "    return _cmux(*a, **k)\n"
        "def _via_adapter(*a, **k):\n"
        "    open(_MARKER, 'w').write('adapter')\n"
        "    return _adapter(*a, **k)\n"
        "d._provision_via_cmux = _via_cmux\n"
        "d._provision_terminal_browser = _via_adapter\n"
    )
    monkeypatch.setenv("PYTHONPATH", f"{stub_dir}{os.pathsep}{ROOT}")

    def provision():
        with contextlib.suppress(BaseException):
            handler.run_drive({"goal": "noop", "url": "https://example.com", "max_steps": 1})

    _assert_pane_landed_outside_herdr(provision)
    assert marker.read_text() == "cmux", (
        f"the child took the {marker.read_text()!r} route; the cmux path was not exercised"
    )


@live_only
def test_live_read_entry_path_puts_the_pane_outside_herdr(monkeypatch, real_cmux_session):
    """scripts/read.py: read.main()'s own discover() call."""
    import scripts.read as read_entry  # noqa: PLC0415

    route = _assert_cmux_route_was_taken(monkeypatch)
    _stop_after_pane_exists(monkeypatch)

    def provision():
        read_entry.main(["--url", "https://example.com", "--no-debug"])

    _assert_pane_landed_outside_herdr(provision)
    assert route, "the live check exercised the adapter path, not the cmux route"
