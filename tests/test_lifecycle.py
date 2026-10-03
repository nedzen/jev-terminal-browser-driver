"""Root-terminal placement, env scrub, and auto_launched provenance on disk.

Score level only for the placement/scrub paths: frozen env dicts, no browser
opened. Provenance write tests exercise `browser.remember_page` against a temp
continuity file. The idle-instance reaper (PR #27 decide-only cluster) was never
wired to production and is gone; these tests cover what discover and continuity
still use.
"""

import pytest

from jev_driver import instances as lc
from jev_driver.discover import WatchUnavailable

# The live shape of a herdr pane hosted by cmux, captured from the environment these
# tests were written in. Both halves matter and neither starts with HERDR_.
NESTED_CMUX = {
    "HERDR_PANE_ID": "wR:p1",
    "HERDR_TAB_ID": "wR:t1",
    "HERDR_ENV": "1",
    "HERDR_SOCKET_PATH": "/Users/m/.config/herdr/herdr.sock",
    "SSH_AUTH_SOCK": "/Users/m/.config/herdr/herdr.sock.agent",
    "TERM_PROGRAM": "herdr",
    "CMUX_SURFACE_ID": "19C93F34",
    "CMUX_TAB_ID": "69150A35",
    "CMUX_PANEL_ID": "19C93F34",
    # The two the cmux route places a split from. Absent from the earlier capture of
    # this environment, which is why the route needs both rather than one.
    "CMUX_SOCKET_PATH": "/Users/m/.local/state/cmux/cmux.sock",
    "CMUX_WORKSPACE_ID": "69150A35-AD91-4117-893B-A7E4FE70B805",
    "CMUX_BUNDLED_CLI_PATH": "/Applications/cmux.app/Contents/Resources/bin/cmux",
    "PWD": "/Users/m/.herdr/worktrees/repo",
}


# --------------------------------------------------------------------------
# Root-terminal placement
# --------------------------------------------------------------------------


def test_provisioning_inside_a_herdr_pane_is_refused():
    assert lc.root_terminal_blocker(NESTED_CMUX) is not None


def test_the_refusal_names_the_outer_terminal_and_its_tab_command():
    """A refusal the operator cannot act on is a support ticket. The remedy belongs
    to whoever owns the outer terminal, so it has to be named."""
    reason = lc.root_terminal_blocker(NESTED_CMUX)
    assert "cmux" in reason
    assert "new-workspace" in reason


def test_the_refusal_says_splitting_the_surface_is_the_cause():
    """Otherwise the obvious next attempt is more env scrubbing, which is the thing
    already proven not to work."""
    assert "surface" in lc.root_terminal_blocker(NESTED_CMUX)


def test_provisioning_outside_a_herdr_pane_is_not_refused():
    assert lc.root_terminal_blocker({"CMUX_SURFACE_ID": "19C93F34"}) is None


def test_an_empty_environment_is_not_refused():
    assert lc.root_terminal_blocker({}) is None


# --------------------------------------------------------------------------
# The scrub: prefix is necessary, not sufficient
# --------------------------------------------------------------------------


def test_the_scrub_removes_the_herdr_prefix():
    env = lc.scrubbed_env({"HERDR_PANE_ID": "wR:p1", "KEEP": "yes"})
    assert "HERDR_PANE_ID" not in env
    assert env["KEEP"] == "yes"


def test_the_scrub_removes_the_herdr_ssh_agent_socket():
    """`SSH_AUTH_SOCK` does not start with HERDR_ and points at a herdr socket, so a
    prefix-only scrub hands the child a live pointer to the pane it is escaping."""
    assert "SSH_AUTH_SOCK" not in lc.scrubbed_env(NESTED_CMUX)


def test_the_scrub_removes_term_program_herdr():
    assert "TERM_PROGRAM" not in lc.scrubbed_env(NESTED_CMUX)


def test_the_scrub_keeps_a_real_ssh_agent_socket():
    """The socket is not the problem, the herdr socket is. Blanking every
    SSH_AUTH_SOCK would silently disable a legitimate forwarded agent."""
    env = lc.scrubbed_env({"SSH_AUTH_SOCK": "/tmp/ssh-abc/agent.sock", "HOME": "/Users/m"})
    assert env["SSH_AUTH_SOCK"] == "/tmp/ssh-abc/agent.sock"


def test_the_scrub_keeps_the_working_directory_even_under_a_herdr_worktree():
    """`PWD` mentions `.herdr` because the checkout lives there, not because the shell
    is one. Scrubbing it would break the child's cwd for no terminal-related gain."""
    assert lc.scrubbed_env(NESTED_CMUX)["PWD"].endswith("/.herdr/worktrees/repo")


def test_the_scrub_keeps_the_cmx_variables_it_is_escaping_toward():
    """The point of the scrub is to make terminal-browser pick cmux, which it can only
    do if cmux's own variables survive."""
    env = lc.scrubbed_env(NESTED_CMUX)
    assert env["CMUX_SURFACE_ID"] == "19C93F34"
    assert env["CMUX_TAB_ID"] == "69150A35"


def test_the_scrub_on_the_live_environment_leaves_no_terminal_signal():
    """The end-to-end version, against the real environment.

    Scoped to the variables terminal-browser actually detects on. Deliberately not
    "no value mentions herdr": `PWD`, `OLDPWD` and `_` all point into a worktree
    under ~/.herdr, and a worktree path is not a terminal signal -- scrubbing those
    would break the child's working directory for nothing.
    """
    import os

    env = lc.scrubbed_env()
    assert [k for k in env if k.startswith("HERDR_")] == []
    assert env.get("TERM_PROGRAM") != "herdr"
    assert "herdr" not in str(env.get("SSH_AUTH_SOCK", "")).lower()
    # And the outer terminal it is escaping toward is still detectable.
    assert env.get("CMUX_SURFACE_ID") == os.environ.get("CMUX_SURFACE_ID")


# --------------------------------------------------------------------------
# discover() honours the guard, and only on the provisioning path
# --------------------------------------------------------------------------


def test_discover_refuses_to_provision_from_inside_a_herdr_pane(monkeypatch):
    """The refusal, on the one shape where the adapter fallback is actually reachable.

    Driven from `HERDR_NON_ADDRESSABLE`, not `NESTED_CMUX`. `NESTED_CMUX` carries both
    CMUX_SOCKET_PATH and CMUX_WORKSPACE_ID, so the cmux route intercepts and this test
    never reached the guard at all -- it passed on `match="herdr"`, which
    `WATCH_TERMINAL_NOTE` also satisfies because it lists herdr among the supported
    terminals and is appended to every provisioning failure. Both routes are mocked to
    `pytest.fail` below, so the only thing that can satisfy this test is the refusal.
    """
    from jev_driver import discover as disc
    from jev_driver import instances as _lc

    monkeypatch.setattr(disc, "_terminal_browser_discovery", lambda: None)
    monkeypatch.setattr(disc, "_daemon_db_discovery", lambda: None)
    monkeypatch.setattr(
        disc, "_provision_cmux_split",
        lambda url, env=None: pytest.fail(
            "the cmux route must not be taken: there is no CMUX_WORKSPACE_ID to place a split in"
        ),
    )
    monkeypatch.setattr(
        disc, "_provision_terminal_browser",
        lambda url: pytest.fail("the adapter ladder must not run from inside a herdr pane"),
    )
    assert disc._in_cmux_context(HERDR_NON_ADDRESSABLE) is False

    with pytest.raises(WatchUnavailable) as caught:
        disc.discover(env=HERDR_NON_ADDRESSABLE)

    # The guard's own wording, exactly -- not a loose substring that a neighbouring
    # failure could also satisfy.
    assert str(caught.value) == _lc.root_terminal_blocker(HERDR_NON_ADDRESSABLE)


def test_the_refusal_cannot_be_mistaken_for_a_terminal_note_failure():
    """Why the match above is an equality rather than `match="herdr"`.

    `WATCH_TERMINAL_NOTE` names herdr as a supported terminal and is appended to every
    provisioning failure, so a substring match cannot distinguish "the guard refused"
    from "the cmux route failed" or "no terminal-browser binary". If that note ever
    stopped naming herdr the loose test would have failed for the wrong reason; worse,
    while it names herdr the loose test passes for the wrong reason. The refusal says
    something the note never does.
    """
    from jev_driver.discover import WATCH_INSTALL, WATCH_TERMINAL_NOTE

    reason = lc.root_terminal_blocker(HERDR_NON_ADDRESSABLE)
    assert "kitty-graphics" not in reason
    assert reason not in WATCH_TERMINAL_NOTE
    assert reason not in WATCH_INSTALL


def test_discover_still_attaches_to_an_existing_pane_from_inside_a_herdr_pane(monkeypatch):
    """Attaching does not nest anything, so the guard must not fire on it. A guard
    that refused here would strand a driver that is already correctly pointed at a
    visible browser."""
    from jev_driver import discover as disc

    monkeypatch.setattr(
        disc, "_terminal_browser_discovery",
        lambda: disc.Discovery("ws://127.0.0.1:1/a", "http://127.0.0.1:1", "terminal-browser"),
    )
    monkeypatch.setattr(
        disc, "_provision_terminal_browser",
        lambda url: pytest.fail("an existing pane must be reused, not re-provisioned"),
    )
    found = disc.discover(env=NESTED_CMUX)
    assert found.source == "terminal-browser"


# --------------------------------------------------------------------------
# The provenance field, on disk
# --------------------------------------------------------------------------


def _remember(monkeypatch, tmp_path, *, spawned):
    """Run `remember_page` for a browser this run either opened or merely attached to."""
    import json

    from jev_driver import browser as br
    from jev_driver import discover as disc

    path = tmp_path / "last-page.json"
    monkeypatch.setattr(br, "LAST_PAGE_PATH", path)
    monkeypatch.setattr(
        disc, "LAST",
        disc.Discovery("ws://127.0.0.1:5000/a", "http://127.0.0.1:5000", "terminal-browser", auto_launched=spawned),
    )
    monkeypatch.setitem(br.LEASE, "tab", "new")
    br.remember_page("T1", "https://example.test/")
    return json.loads(path.read_text())


def test_a_browser_this_run_opened_is_recorded_as_driver_spawned(tmp_path, monkeypatch):
    """Without this, after the process exits a browser the driver opened looks exactly
    like one the human opened."""
    assert _remember(monkeypatch, tmp_path, spawned=True)["auto_launched"] is True


def test_a_browser_this_run_attached_to_is_recorded_as_owner_opened(tmp_path, monkeypatch):
    assert _remember(monkeypatch, tmp_path, spawned=False)["auto_launched"] is False


def test_the_provenance_flag_is_not_made_a_required_key():
    """`LAST_PAGE_KEYS` is a presence test, so requiring the flag would make every
    record written before it existed read as no record at all -- silently voiding
    continuity for every existing install on upgrade. Extra keys are tolerated."""
    from jev_driver.browser import LAST_PAGE_KEYS, PROVENANCE_KEY

    assert PROVENANCE_KEY not in LAST_PAGE_KEYS


def test_a_record_written_before_the_flag_still_loads(tmp_path, monkeypatch):
    """The migration, as a test: an existing install's continuity record must survive
    the upgrade rather than read as absent."""
    import json

    from jev_driver import browser as br

    path = tmp_path / "last-page.json"
    path.write_text(json.dumps({
        "targetId": "T1", "url": "https://example.test/", "source": "terminal-browser",
        "browser_id": "127.0.0.1:5000", "ts": 1.0,
    }))
    monkeypatch.setattr(br, "LAST_PAGE_PATH", path)
    assert br._load_last_page()["targetId"] == "T1"


def test_a_pre_flag_record_has_no_provenance_key(tmp_path, monkeypatch):
    """An absent flag must not be invented as False on load — unknown is not owner-opened."""
    import json

    from jev_driver import browser as br
    from jev_driver.browser import PROVENANCE_KEY

    path = tmp_path / "last-page.json"
    path.write_text(json.dumps({
        "targetId": "T1", "url": "https://example.test/", "source": "terminal-browser",
        "browser_id": "127.0.0.1:5000", "ts": 1.0,
    }))
    monkeypatch.setattr(br, "LAST_PAGE_PATH", path)
    assert PROVENANCE_KEY not in br._load_last_page()


def test_the_open_run_ledger_can_be_consulted_standing_alone(tmp_path, monkeypatch):
    """open_run_ids must resolve the default log_dir itself when callers pass nothing.

    It used to raise TypeError on None -- invisible in-tree because the one existing
    caller resolves first and passes the path down, and fatal to a caller that has no
    such helper.
    """
    import json

    from scripts.live import isolation as iso

    monkeypatch.setattr(iso, "LOG_DIR", tmp_path)
    (tmp_path / "drive.jsonl").write_text("".join(
        json.dumps(e) + "\n" for e in (
            {"event": "run", "stage": "start", "metrics": {"run_id": "r1"}},
            {"event": "run", "stage": "start", "metrics": {"run_id": "r2"}},
            {"event": "run", "stage": "finish", "metrics": {"run_id": "r2"}},
        )
    ))
    assert sorted(iso.open_run_ids()) == ["r1"]


# --------------------------------------------------------------------------
# Route order: the cmux socket route is not subject to the herdr refusal
# --------------------------------------------------------------------------

# A herdr pane hosted by cmux. This is the shape the cmux route exists for: the pane
# is nested, but cmux can still be addressed directly, so there is nothing to refuse.
HERDR_IN_CMUX = dict(NESTED_CMUX)

# A herdr pane whose cmux socket is still in the environment but whose workspace id is
# not: the one shape in which the adapter fallback is both reachable and unsafe. cmux
# cannot be addressed (so the cmux route is correctly skipped) and the current surface
# belongs to herdr (so `--split` would nest). This is the fixture the refusal test must
# use -- `HERDR_IN_CMUX` is intercepted by the cmux route before the guard is consulted.
HERDR_NON_ADDRESSABLE = {k: v for k, v in NESTED_CMUX.items() if k != "CMUX_WORKSPACE_ID"}


def test_a_cmux_context_is_recognised_by_socket_and_workspace():
    from jev_driver.discover import _in_cmux_context

    assert _in_cmux_context(HERDR_IN_CMUX) is True


def test_a_cmux_socket_without_a_workspace_is_not_a_cmux_context():
    """A stale socket left in the environment must not claim a context whose split
    would then be created nowhere. Without this the route fails at the socket instead
    of falling back to the adapter ladder."""
    from jev_driver.discover import _in_cmux_context

    assert _in_cmux_context({"CMUX_SOCKET_PATH": "/tmp/cmux.sock"}) is False


def test_provisioning_through_the_cmux_socket_works_from_inside_a_herdr_pane(monkeypatch):
    """The half of the pair that must NOT refuse. cmux is addressed directly, so the
    split is created at cmux's own level rather than inside the agent's pane -- there
    is no adapter to guess wrong and nothing to nest inside."""
    from jev_driver import discover as disc

    monkeypatch.setattr(disc, "_terminal_browser_discovery", lambda: None)
    monkeypatch.setattr(disc, "_daemon_db_discovery", lambda: None)
    monkeypatch.setattr(
        disc, "_provision_cmux_split",
        lambda url, env=None: disc.Discovery(
            "ws://127.0.0.1:53397/a", "http://127.0.0.1:53397", "terminal-browser", auto_launched=True
        ),
    )
    monkeypatch.setattr(
        disc, "_provision_terminal_browser",
        lambda url: pytest.fail("a cmux context must not fall through to the adapter ladder"),
    )

    found = disc.discover(env=HERDR_IN_CMUX)

    assert found.auto_launched is True
    assert found.visibility == "terminal-browser-pane"


def test_the_adapter_guess_still_refuses_from_inside_a_herdr_pane(monkeypatch):
    """The other half of the pair, and the reason the ordering matters: without a cmux
    context the adapter ladder has no way to escape the calling pane, so it refuses."""
    from jev_driver import discover as disc

    monkeypatch.setattr(disc, "_terminal_browser_discovery", lambda: None)
    monkeypatch.setattr(disc, "_daemon_db_discovery", lambda: None)
    monkeypatch.setattr(
        disc, "_provision_cmux_split",
        lambda url, env=None: pytest.fail("there is no cmux context to route through"),
    )
    monkeypatch.setattr(
        disc, "_provision_terminal_browser",
        lambda url: pytest.fail("the adapter ladder must not run from inside a herdr pane"),
    )

    with pytest.raises(WatchUnavailable, match="herdr"):
        disc.discover(env={"HERDR_PANE_ID": "wR:p1", "TERM_PROGRAM": "herdr"})


def test_the_cmux_route_is_never_reached_without_a_workspace(monkeypatch):
    """Ordering is not enough on its own: the refusal has to survive for a herdr pane
    that is *not* in cmux, which is the case the guard was written for."""
    from jev_driver import discover as disc

    monkeypatch.setattr(disc, "_terminal_browser_discovery", lambda: None)
    monkeypatch.setattr(disc, "_daemon_db_discovery", lambda: None)
    monkeypatch.setattr(
        disc, "_provision_cmux_split",
        lambda url, env=None: pytest.fail("no cmux workspace, so no cmux route"),
    )
    with pytest.raises(WatchUnavailable, match="herdr"):
        disc.discover(env={"HERDR_PANE_ID": "wR:p1"})


# --------------------------------------------------------------------------
# The scrub must not cost the cmux route what it needs
# --------------------------------------------------------------------------


def test_the_scrub_preserves_every_cmux_variable():
    """The cmux route places the split from `CMUX_WORKSPACE_ID` over `CMUX_SOCKET_PATH`.
    A scrub that dropped the CMUX_* family would leave that route unable to say where
    to create anything, so it is pinned on the whole family and not on a sample."""
    import os

    env = lc.scrubbed_env()
    assert {k for k in env if k.startswith("CMUX_")} == {k for k in os.environ if k.startswith("CMUX_")}


def test_the_scrub_preserves_the_workspace_id_the_cmux_route_needs():
    assert "CMUX_WORKSPACE_ID" in lc.scrubbed_env(NESTED_CMUX)
    assert "CMUX_SOCKET_PATH" in lc.scrubbed_env(NESTED_CMUX)


def test_the_cmux_route_passes_the_scrubbed_env_so_the_child_sees_both_families():
    """The child needs cmux's variables to place the split *and* needs the herdr trace
    gone so terminal-browser does not re-derive a herdr placement of its own. One env
    has to satisfy both, which is what makes this worth a test rather than a comment."""
    env = lc.scrubbed_env(NESTED_CMUX)
    assert "CMUX_WORKSPACE_ID" in env and "CMUX_SOCKET_PATH" in env
    assert [k for k in env if k.startswith("HERDR_")] == []
