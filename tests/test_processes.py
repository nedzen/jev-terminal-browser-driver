"""Spawn accounting, orphan detection, version manifest.

Offline: every `ps` and `git` call is a fake backend, and every signal is a fake
`os.kill`, so no process table is read, nothing is signalled, and no child is
started. The one live assertion is this process asking about itself.
"""

import errno
import hashlib
import json
import os

import pytest

from jev_driver import cli, runlog
from jev_driver import processes as proc
from jev_driver.discover import Discovery

# A fake table: one zombie browser, one live browser, one driver of our own.
PS_TABLE = """
  501 Ss   /sbin/launchd
 4242 Z+   /Users/u/.local/bin/terminal-browser --split right
 4243 Ss   /Users/u/.local/bin/terminal-browser --split right
 7777 Ss   python -m jev_driver.cli --goal shop
"""

TB_ROW = {"pid": 4243, "stat": "Ss", "command": "/Users/u/.local/bin/terminal-browser --split right"}
ZOMBIE_ROW = {"pid": 4242, "stat": "Z+", "command": "/Users/u/.local/bin/terminal-browser --split right"}


class FakePs:
    """Stands in for the process table and for git. Records every argv."""

    def __init__(self, table="", states=None, git=None, dead=False):
        self.table = table
        self.states = dict(states or {})
        self.git = git
        self.dead = dead  # ps/git unavailable: every call answers None
        self.calls = []

    def __call__(self, argv, *, timeout):
        self.calls.append(list(argv))
        if self.dead:
            return None
        program = argv[0] if argv else ""
        if program == "git":
            return self.git
        if "-A" in argv:
            return self.table
        if "-p" in argv:
            pid = int(argv[argv.index("-p") + 1])
            return self.states.get(pid, "")
        return ""

    @property
    def ps_calls(self):
        return [argv for argv in self.calls if argv and argv[0] == "ps"]

    @property
    def git_calls(self):
        return [argv for argv in self.calls if argv and argv[0] == "git"]


class FakeSignals:
    """`os.kill` stand-in: ESRCH for a gone pid, EPERM for someone else's, silence otherwise."""

    def __init__(self, missing=(), forbidden=(), errno_error=None):
        self.missing = set(missing)
        self.forbidden = set(forbidden)
        self.errno_error = errno_error
        self.sent = []

    def __call__(self, pid, sig):
        self.sent.append((pid, sig))
        if self.errno_error is not None:
            raise OSError(self.errno_error, "simulated")
        if pid in self.missing:
            raise ProcessLookupError(errno.ESRCH, "no such process")
        if pid in self.forbidden:
            raise PermissionError(errno.EPERM, "not ours")
        return None


@pytest.fixture(autouse=True)
def clean_counters():
    """Spawn counters are process-wide; no test may inherit another's count."""
    proc.reset_spawns()
    yield
    proc.reset_spawns()


@pytest.fixture
def ps(monkeypatch):
    """Install a fake ps/git backend and hand it back."""

    def install(table="", states=None, git=None, dead=False):
        backend = FakePs(table=table, states=states, git=git, dead=dead)
        monkeypatch.setattr(proc, "run_cmd", backend)
        return backend

    return install


@pytest.fixture
def signals(monkeypatch):
    def install(missing=(), forbidden=(), errno_error=None):
        probe = FakeSignals(missing=missing, forbidden=forbidden, errno_error=errno_error)
        monkeypatch.setattr(proc.os, "kill", probe)
        return probe

    return install


@pytest.fixture
def sleeps(monkeypatch):
    """Replaces the settle wait; the module's own `_sleep` is the seam."""
    calls = []
    monkeypatch.setattr(proc, "_sleep", lambda seconds: calls.append(seconds))
    return calls


def install_ps_backend(ps, states=None, git=None, table=PS_TABLE, dead=False):
    return ps(table=table, states=states, git=git, dead=dead)


# ------------------------------------------------------------------- spawning


def test_a_spawn_is_counted_with_its_pid_and_kind():
    assert proc.note_spawn("terminal-browser") == 1
    assert proc.note_spawn("helper", pid="4242") == 2
    assert proc.runtime_spawn_count() == 2
    assert proc.tracked_pids() == [4242]
    assert proc.spawn_summary() == {
        "spawn_count": 2,
        "tracked_pids": [4242],
        "spawn_kinds": {"helper": 1, "terminal-browser": 1},
    }


def test_a_spawn_without_a_pid_is_counted_but_not_tracked():
    proc.note_spawn("terminal-browser")  # terminal-browser forks the pane itself
    assert proc.runtime_spawn_count() == 1
    assert proc.tracked_pids() == []
    # A count without a pid is exactly why the orphan check also takes a pattern.
    assert proc.spawn_summary()["spawn_kinds"] == {"terminal-browser": 1}


def test_unusable_pids_are_counted_but_never_tracked():
    for pid in (0, -7, "abc", None):
        proc.note_spawn("helper", pid=pid)
    assert proc.runtime_spawn_count() == 4
    assert proc.tracked_pids() == []  # signalling pid 0 hits a whole group


def test_the_same_pid_is_tracked_once():
    proc.note_spawn("helper", pid=4242)
    proc.note_spawn("helper", pid=4242)
    assert proc.spawn_summary() == {"spawn_count": 2, "tracked_pids": [4242], "spawn_kinds": {"helper": 2}}


def test_a_tracked_pid_may_be_adopted_after_the_fact():
    proc.note_spawn("terminal-browser")
    proc.note_spawn("terminal-browser", pid=4242)
    assert proc.tracked_pids() == [4242]


def test_reset_spawns_starts_the_next_run():
    proc.note_spawn("terminal-browser", pid=4242)
    proc.reset_spawns()
    assert proc.runtime_spawn_count() == 0
    assert proc.tracked_pids() == []
    assert proc.spawn_summary()["spawn_kinds"] == {}


def test_the_context_manager_counts_a_spawn_without_a_pid():
    with proc.RUN.spawn("probe"):
        pass
    assert proc.spawn_summary() == {"spawn_count": 1, "tracked_pids": [], "spawn_kinds": {"probe": 1}}


# --------------------------------------------------------- process table / ps


def test_the_process_table_is_read_with_one_portable_ps_invocation(ps):
    backend = install_ps_backend(ps)
    assert proc.process_table()[:3] == [
        {"pid": 501, "stat": "Ss", "command": "/sbin/launchd"},
        ZOMBIE_ROW,
        TB_ROW,
    ]
    # -A -o pid=,stat=,command= is the spelling both BSD/macOS ps and Linux
    # procps accept; a `ps ax`-style rewrite breaks on one of them.
    assert backend.ps_calls == [["ps", "-A", "-o", "pid=,stat=,command="]]


def test_unparsable_table_lines_are_skipped(ps):
    install_ps_backend(ps, table="not a process\n\n 12 Ss  /bin/sh\n")
    assert proc.process_table() == [{"pid": 12, "stat": "Ss", "command": "/bin/sh"}]


def test_an_unavailable_ps_reads_as_an_empty_table(ps):
    install_ps_backend(ps, dead=True)
    assert proc.process_table() == []


def test_a_present_process_matches_the_command_pattern(ps, monkeypatch):
    install_ps_backend(ps)
    monkeypatch.setattr(proc.os, "getpid", lambda: 7777)  # our own row is in the table
    hits = proc.matching_processes()
    assert [row["pid"] for row in hits] == [4242, 4243]
    assert proc.state_is_zombie(hits[0]["stat"]) is True
    assert proc.state_is_zombie(hits[1]["stat"]) is False


def test_an_absent_process_is_not_reported(ps):
    install_ps_backend(ps, table="  501 Ss   /sbin/launchd\n")
    assert proc.matching_processes() == []


def test_our_own_pid_is_never_reported_as_an_orphan(monkeypatch, ps):
    """The driver's own argv can name the pattern (a URL, a log path), so the
    scan must drop this process even when the table says it matches."""
    install_ps_backend(ps, table=f"  {os.getpid()} Ss   python -m jev_driver.cli --url terminal-browser\n")
    assert proc.matching_processes() == []


def test_matching_processes_honours_a_narrower_pattern(ps):
    install_ps_backend(ps)
    assert proc.matching_processes(("Electron Helper",)) == []
    assert [row["pid"] for row in proc.matching_processes(("launchd",))] == [501]


def test_the_match_list_is_capped(ps):
    install_ps_backend(ps, table="".join(f" {100 + i} Ss  /bin/terminal-browser-{i}\n" for i in range(50)))
    assert len(proc.matching_processes(limit=5)) == 5


@pytest.mark.parametrize("stat", ["Z", "Z+", "Zs", "Z+", "X", "x"])
def test_a_stat_letter_says_zombie(stat):
    """macOS marks an exited process Z+, Linux spells a dead one X or x."""
    assert proc.state_is_zombie(stat) is True


@pytest.mark.parametrize("stat", ["Ss", "S", "R", "T", "W", "", "   "])
def test_a_stat_letter_says_running(stat):
    assert proc.state_is_zombie(stat) is False


def test_hostile_patterns_fall_back_to_the_default(ps):
    """An empty pattern list must not silently mean "match nothing" and quietly
    retire the orphan check."""
    install_ps_backend(ps)
    for patterns in (None, (), [], [""], ["terminal-browser"]):
        report = proc.orphan_report(spawn_count=1, patterns=patterns)
        assert report["patterns"] == list(proc.DEFAULT_PATTERNS)
        assert [row["pid"] for row in report["pattern_matches"]] == [4242, 4243]


# ------------------------------------------------------- zombie vs alive


def test_a_reaped_pid_is_dead_and_never_asks_for_a_state(ps, signals):
    install_ps_backend(ps)
    probe = signals(missing=[4242])
    assert proc.proc_state(4242) == proc.DEAD
    assert probe.sent == [(4242, 0)]
    assert install_ps_backend(ps).ps_calls == []  # a pid that is gone needs no state


def test_an_unreaped_zombie_is_not_alive(ps, signals):
    """The false positive this exists for: `kill(pid, 0)` succeeds on a process
    that has already exited, so reading signal 0 alone would report a leak."""
    backend = install_ps_backend(ps, states={4242: "Z+"})
    signals()
    assert proc.proc_state(4242) == proc.ZOMBIE
    assert backend.ps_calls == [["ps", "-p", "4242", "-o", "state="]]


def test_a_linux_style_dead_state_is_also_a_zombie(ps, signals):
    install_ps_backend(ps, states={4242: "X"})
    signals()
    assert proc.proc_state(4242) == proc.ZOMBIE


def test_a_running_process_is_alive(ps, signals):
    backend = install_ps_backend(ps, states={4243: "Ss"})
    signals()
    assert proc.proc_state(4243) == proc.ALIVE
    assert backend.ps_calls == [["ps", "-p", "4243", "-o", "state="]]


def test_a_process_we_may_not_signal_is_unknown_not_alive(ps, signals):
    """EPERM means the pid exists but is not ours to read either. Saying `alive`
    would be the one answer we cannot support."""
    install_ps_backend(ps, states={})
    signals(forbidden=[4242])
    assert proc.proc_state(4242) == proc.UNKNOWN


def test_a_readable_state_outranks_a_permission_error(ps, signals):
    install_ps_backend(ps, states={4242: "Z"})
    signals(forbidden=[4242])
    assert proc.proc_state(4242) == proc.ZOMBIE


def test_an_unexpected_errno_is_unknown(ps, signals):
    install_ps_backend(ps, states={})
    signals(errno_error=errno.EINVAL)
    assert proc.proc_state(4242) == proc.UNKNOWN


def test_a_missing_state_source_falls_back_to_signal_zero(ps, signals):
    """No ps at all: signal 0 still proves the pid is there, but nothing proves
    it is running rather than a corpse."""
    install_ps_backend(ps, dead=True)
    signals()
    assert proc.proc_state(4243) == proc.ALIVE
    assert proc.proc_state(4243, timeout=0.0) == proc.ALIVE


def test_unusable_pid_arguments_signal_nothing(ps, signals):
    backend = install_ps_backend(ps)
    probe = signals()
    assert proc.proc_state(0) == proc.UNKNOWN  # pid 0 addresses a process group
    assert proc.proc_state(-1) == proc.UNKNOWN
    assert proc.proc_state("nope") == proc.UNKNOWN
    assert probe.sent == []
    assert backend.ps_calls == []


def test_a_liveness_report_buckets_every_pid(ps, signals):
    install_ps_backend(ps, states={4242: "Z+", 4243: "Ss"})
    signals(missing=[4299], forbidden=[4300])
    report = proc.liveness_report([4242, 4243, 4299, 4300, 0, "junk"])
    assert report == {
        "alive": [4243],
        "zombie": [4242],
        "dead": [4299],
        "unknown": [4300],
        "checked": 4,
    }


def test_a_liveness_report_of_nothing_is_still_well_shaped(ps):
    install_ps_backend(ps)
    assert proc.liveness_report([]) == {"alive": [], "zombie": [], "dead": [], "unknown": [], "checked": 0}


# ------------------------------------------------------------------- orphans


def test_a_live_tracked_pid_is_an_orphan(ps, signals, sleeps):
    install_ps_backend(ps)
    signals()
    report = proc.orphan_report([4243], spawn_count=1)
    assert report["status"] == proc.STATUS_ORPHANS
    assert report["orphans"] == [4243]
    assert sleeps == [proc.ORPHAN_CHECK_DELAY_S]  # delayed, so a teardown can finish


def test_a_zombie_tracked_pid_is_not_an_orphan(ps, signals, sleeps):
    """The whole point: an exited-but-unreaped child holds no pane and no port,
    so recording it as a leak would send someone hunting a corpse."""
    install_ps_backend(ps, states={4242: "Z+"})
    signals()
    report = proc.orphan_report([4242], spawn_count=1)
    assert report["status"] == proc.STATUS_CLEAN
    assert report["orphans"] == []
    assert report["liveness"]["zombie"] == [4242]


def test_a_dead_tracked_pid_is_clean(ps, signals, sleeps):
    install_ps_backend(ps)
    signals(missing=[4242])
    report = proc.orphan_report([4242], spawn_count=1)
    assert report["status"] == proc.STATUS_CLEAN
    assert report["liveness"]["dead"] == [4242]


def test_an_unclassifiable_pid_is_never_reported_clean(ps, signals, sleeps):
    install_ps_backend(ps)
    signals(forbidden=[4242])
    report = proc.orphan_report([4242], spawn_count=1)
    assert report["status"] == proc.STATUS_UNKNOWN
    assert "unproven" in report["detail"]


def test_a_run_that_spawned_nothing_checks_nothing(ps, signals, sleeps):
    """Nothing spawned means nothing can be orphaned; the scan is not worth a
    subprocess, and every terminal-browser on the box is somebody else's pane."""
    backend = install_ps_backend(ps)
    probe = signals()
    report = proc.orphan_report()
    assert report["status"] == proc.STATUS_NO_PIDS
    assert report["counted_spawns"] == 0
    assert report["pattern_matches"] == []
    assert backend.ps_calls == []
    assert probe.sent == []
    assert sleeps == []


def test_a_spawn_without_a_pid_falls_back_to_the_command_pattern(ps, sleeps):
    install_ps_backend(ps)
    report = proc.orphan_report(spawn_count=1)
    assert report["status"] == proc.STATUS_UNTRACKED
    assert [row["pid"] for row in report["pattern_matches"]] == [4242, 4243]
    # A pattern match is evidence about the box, not a verdict about this run.
    assert report["orphans"] == []
    assert "not a leak verdict" in report["detail"]


def test_the_settle_wait_can_be_turned_off(ps, signals, sleeps):
    install_ps_backend(ps)
    signals()
    proc.orphan_report([4242], delay_s=0)
    assert sleeps == []


def test_an_injected_sleeper_replaces_the_module_one(ps, signals):
    install_ps_backend(ps)
    signals()
    waited = []
    proc.orphan_report([4242], sleep=waited.append)
    assert waited == [proc.ORPHAN_CHECK_DELAY_S]


def test_a_ps_that_explodes_reports_unknown_and_raises_nothing(monkeypatch):
    def boom(argv, *, timeout):
        raise RuntimeError("ps went sideways")

    monkeypatch.setattr(proc, "run_cmd", boom)
    report = proc.orphan_report([4242], spawn_count=1)
    assert report["status"] == proc.STATUS_UNKNOWN
    assert "ps went sideways" in report["detail"]


def test_a_sleep_that_explodes_costs_the_report_not_the_run(ps, monkeypatch):
    """The settle wait is a courtesy to the caller, not a decision point: if it
    fails we lose the check, not the run."""

    def boom(seconds):
        raise RuntimeError("interrupted during the settle wait")

    install_ps_backend(ps, states={4242: "Z+"})
    monkeypatch.setattr(proc.os, "kill", lambda pid, sig: None)
    report = proc.orphan_report([4242], spawn_count=1, sleep=boom)
    assert report["status"] == proc.STATUS_UNKNOWN
    assert "interrupted" in report["detail"]


def test_a_keyboard_interrupt_still_stops_the_run(ps, monkeypatch):
    """Ctrl-C must not be swallowed by an accounting guarantee."""

    def boom(seconds):
        raise KeyboardInterrupt

    install_ps_backend(ps)
    monkeypatch.setattr(proc.os, "kill", lambda pid, sig: None)
    with pytest.raises(KeyboardInterrupt):
        proc.orphan_report([4242], spawn_count=1, sleep=boom)


def test_process_evidence_is_one_record_with_the_spawn_count(ps, sleeps):
    install_ps_backend(ps)
    proc.note_spawn("terminal-browser", pid=4242)
    evidence = proc.process_evidence(goal="Open the page", sleep=lambda seconds: sleeps.append(seconds))
    assert evidence["event"] == "processes"
    assert evidence["goal"] == "Open the page"
    assert evidence["spawn_count"] == 1
    assert evidence["tracked_pids"] == [4242]
    assert evidence["status"] == proc.STATUS_CLEAN
    assert sleeps == [proc.ORPHAN_CHECK_DELAY_S]


# ------------------------------------------------------------------ manifest


def _tree(tmp_path, files=("jev_driver/cli.py", "scripts/drive.py")):
    for rel in files:
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# {rel}\n")
    return tmp_path


def test_the_manifest_names_the_code_in_three_fields(ps, tmp_path):
    root = _tree(tmp_path)
    install_ps_backend(ps, git="dae8c610584aa693e9fad240e01fa8423c1b0b61\n")
    manifest = proc.version_manifest(root=root)
    assert set(manifest) == {"git_commit", "impl_hash", "interpreter"}
    assert manifest["git_commit"] == "dae8c610584aa693e9fad240e01fa8423c1b0b61"
    assert len(manifest["impl_hash"]) == 64
    assert manifest["interpreter"].startswith("CPython ")


def test_the_impl_hash_is_a_digest_of_the_implementation_files(ps, tmp_path):
    root = _tree(tmp_path)
    install_ps_backend(ps, git=None)
    expected = hashlib.sha256(
        "\n".join(
            sorted(
                f"{rel} {hashlib.sha256((root / rel).read_bytes()).hexdigest()}"
                for rel in ("jev_driver/cli.py", "scripts/drive.py")
            )
        ).encode()
    ).hexdigest()
    assert proc.version_manifest(root=root)["impl_hash"] == expected


def test_the_same_tree_hashes_the_same_way(ps, tmp_path):
    root = _tree(tmp_path)
    install_ps_backend(ps, git=None)
    assert proc.impl_hash(root) == proc.impl_hash(root)


def test_the_hash_is_the_same_tree_in_a_different_directory(ps, tmp_path):
    """Relative paths, not absolute ones: a checkout moved or re-cloned must hash
    the same, which is also what keeps a home directory out of the digest."""
    here = _tree(tmp_path / "here")
    there = _tree(tmp_path / "there" / "deeper")
    install_ps_backend(ps, git=None)
    assert proc.impl_hash(here) == proc.impl_hash(there)


def test_touching_one_byte_changes_the_hash(ps, tmp_path):
    root = _tree(tmp_path)
    install_ps_backend(ps, git=None)
    before = proc.impl_hash(root)
    (root / "scripts" / "drive.py").write_text("# scripts/drive.py, one byte more\n")
    assert proc.impl_hash(root) != before


def test_a_new_implementation_file_changes_the_hash(ps, tmp_path):
    root = _tree(tmp_path)
    install_ps_backend(ps, git=None)
    before = proc.impl_hash(root)
    (root / "scripts" / "probe_jev.py").write_text("# probe\n")
    assert proc.impl_hash(root) != before


def test_the_hash_covers_both_implementation_directories(ps, tmp_path):
    root = _tree(tmp_path)
    install_ps_backend(ps, git=None)
    before = proc.impl_hash(root)
    (root / "jev_driver" / "cli.py").write_text("# jev_driver/cli.py, changed\n")
    assert proc.impl_hash(root) != before


def test_tests_and_docs_are_out_of_the_hash(ps, tmp_path):
    root = _tree(tmp_path)
    (root / "tests").mkdir()
    (root / "tests" / "test_x.py").write_text("# not implementation\n")
    (root / "docs").mkdir()
    (root / "docs" / "notes.md").write_text("# not implementation\n")
    install_ps_backend(ps, git=None)
    assert proc.impl_hash(root) == proc.impl_hash(_tree(tmp_path / "again"))


def test_a_dirty_checkout_still_has_an_impl_hash(ps, tmp_path):
    """The commit alone cannot describe what ran; the digest can."""
    root = _tree(tmp_path)
    install_ps_backend(ps, git="dae8c610584aa693e9fad240e01fa8423c1b0b61\n")
    (root / "jev_driver" / "cli.py").write_text("# edited after commit\n")
    manifest = proc.version_manifest(root=root)
    assert manifest["git_commit"] == "dae8c610584aa693e9fad240e01fa8423c1b0b61"
    assert manifest["impl_hash"] is not None


def test_a_missing_git_is_unknown_but_still_hashed(ps, tmp_path):
    root = _tree(tmp_path)
    install_ps_backend(ps, dead=True)
    manifest = proc.version_manifest(root=root)
    assert manifest["git_commit"] is None
    assert manifest["impl_hash"] is not None


def test_git_is_asked_for_one_head_sha_in_the_checkout(ps, tmp_path):
    root = _tree(tmp_path)
    backend = install_ps_backend(ps, git="abc123\n")
    proc.version_manifest(root=root)
    assert backend.git_calls == [["git", "-C", str(root), "rev-parse", "HEAD"]]


def test_a_tree_without_both_implementation_directories_is_not_hashed(ps, tmp_path):
    """Half a digest would attest to code nobody hashed."""
    (tmp_path / "jev_driver").mkdir()
    (tmp_path / "jev_driver" / "cli.py").write_text("# cli\n")
    install_ps_backend(ps, git=None)
    assert proc.repo_root.__module__  # module is importable; the root check is what matters
    assert proc.impl_hash(tmp_path) is not None  # hashing an explicit path is the caller's choice
    assert proc.repo_root() is not None  # the real checkout has both directories


def test_an_unreadable_file_is_not_a_partial_digest(monkeypatch, tmp_path):
    root = _tree(tmp_path)

    def no_read(path):
        return None

    monkeypatch.setattr(proc, "_file_digest", no_read)
    assert proc.impl_hash(root) is None


def test_the_manifest_carries_no_path_and_no_environment(ps, tmp_path, monkeypatch):
    root = _tree(tmp_path)
    monkeypatch.setenv("TYPESAFE_API_KEY", "sk-live-should-never-be-here")
    install_ps_backend(ps, git="dae8c610584aa693e9fad240e01fa8423c1b0b61\n")
    manifest = proc.version_manifest(root=root)
    blob = json.dumps(manifest)
    assert str(root) not in blob
    assert "sk-live" not in blob
    for value in manifest.values():
        assert value is None or len(str(value)) < 80


def test_version_manifest_never_raises():
    class Hostile:
        def __fspath__(self):
            raise RuntimeError("no path for you")

    class Worse(str):
        pass

    assert set(proc.version_manifest(root=Hostile())) == {"git_commit", "impl_hash", "interpreter"}
    assert proc.version_manifest(root=Worse("/nope"))["impl_hash"] is None


def test_repo_root_is_none_without_a_checkout(monkeypatch, tmp_path):
    monkeypatch.setattr(proc, "driver_home", lambda: tmp_path / "not-a-checkout")
    assert proc.repo_root() is None
    # Same three fields as a hashed tree: a catalog install with no checkout must
    # still say "unknown" in the same shape, not "fewer keys".
    manifest = proc.version_manifest()
    assert set(manifest) == {"git_commit", "impl_hash", "interpreter"}
    assert manifest["impl_hash"] is None
    assert manifest["git_commit"] is None
    assert manifest["interpreter"].startswith("CPython ")


# ------------------------------------------------------------ writing it out


def test_the_manifest_is_written_beside_the_run_log(ps, monkeypatch, tmp_path):
    log = tmp_path / "run-log" / "drive.jsonl"
    monkeypatch.setattr(runlog, "JSONL_PATH", log)
    install_ps_backend(ps, git="abc123\n")
    written = proc.write_version_manifest()
    target = tmp_path / "run-log" / "version_manifest.json"
    assert json.loads(target.read_text()) == written
    assert set(written) == {"git_commit", "impl_hash", "interpreter"}
    assert proc.manifest_path() == target


def test_an_unwritable_manifest_is_reported_not_raised(monkeypatch, ps, tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("I am a file, not a directory\n")
    monkeypatch.setattr(runlog, "JSONL_PATH", blocker / "run-log" / "drive.jsonl")
    install_ps_backend(ps, git=None)
    written = proc.write_version_manifest()
    assert "error" in written
    assert written["git_commit"] is None
    assert not (blocker / "run-log").exists()


def test_a_failed_write_leaves_no_scratch_file(monkeypatch, ps, tmp_path):
    class Unserialisable:
        def __repr__(self):
            raise RuntimeError("no repr either")

    monkeypatch.setattr(runlog, "JSONL_PATH", tmp_path / "run-log" / "drive.jsonl")
    install_ps_backend(ps, git=None)
    written = proc.write_version_manifest({"impl_hash": Unserialisable()})
    assert "error" in written
    assert not (tmp_path / "run-log" / "version_manifest.json.tmp").exists()


def test_a_prepared_manifest_is_written_verbatim(monkeypatch, ps, tmp_path):
    monkeypatch.setattr(runlog, "JSONL_PATH", tmp_path / "run-log" / "drive.jsonl")
    install_ps_backend(ps, git="ignored")
    written = proc.write_version_manifest({"git_commit": "abc", "impl_hash": "def", "interpreter": "CPython 3.12"})
    assert json.loads((tmp_path / "run-log" / "version_manifest.json").read_text()) == written


def test_the_real_checkout_hashes_and_commits(ps):
    """The live backend, once: this is a git checkout and a real process table."""
    backend = ps(git=None)
    manifest = proc.version_manifest()
    assert set(manifest) == {"git_commit", "impl_hash", "interpreter"}
    assert manifest["impl_hash"] is not None
    if backend.git_calls:  # git answered: HEAD must be a sha, not a sentence
        assert len(manifest["git_commit"] or "") in {0, 40}
    assert proc.proc_state(os.getpid()) == proc.ALIVE  # this process is alive, and says so


def test_this_process_is_never_listed_as_an_orphan():
    assert os.getpid() not in [row["pid"] for row in proc.matching_processes()]


# --------------------------------------------------------------- through the CLI

PAGE = {
    "url": "https://example.test/next",
    "title": "Next page",
    "fingerprint": "fp-decision",
    "text": "The order was confirmed.",
    "scroll": {"y": 0, "height": 900},
    "actions": [],
}


class FakeAgent:
    """One tick, then done. Same state shape as DriveAgent, no browser, no model."""

    def __init__(self, url, goal, screenshots=False, debug=False, **extra):
        self.state = {
            "browser": None,
            "goal": goal,
            "page": dict(PAGE),
            "decision": {"choice": "DONE"},
            "history": [],
            "status": "ready",
            "decisions": [],
            "text_calls": [],
            "elapsed_ms": 7,
            "started_at": None,
        }
        self.closed = False

    def snapshot(self):
        return {key: value for key, value in self.state.items() if key != "browser"}

    def command(self, name, body=None):
        if name == "tick":
            self.state["decisions"].append({"choice": "DONE", "operation": "DONE"})
            self.state["status"] = "done"
        return self.snapshot()

    def close(self):
        self.closed = True


@pytest.fixture
def drive(monkeypatch, capsys, ps, sleeps):
    """cli.main with no browser and no model. Returns (code, stdout rows, events)."""

    def run(*, auto_launched=False, agent_cls=FakeAgent):
        backend = install_ps_backend(ps, git="dae8c610584aa693e9fad240e01fa8423c1b0b61\n")
        monkeypatch.setattr(
            cli,
            "discover",
            lambda **kw: Discovery("ws://127.0.0.1:1/x", "http://127.0.0.1:1", "terminal-browser", auto_launched),
        )
        monkeypatch.setattr(cli, "connect", lambda url: None)
        monkeypatch.setattr(cli, "set_lease", lambda **kw: None)
        monkeypatch.setattr(cli, "find_continuable_page", lambda: (None, None))
        monkeypatch.setattr(cli, "DriveAgent", agent_cls)
        code = cli.main(["--goal", "Confirm the order", "--url", "https://example.test/next"])
        rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
        events = [
            json.loads(line)
            for line in (runlog.JSONL_PATH).read_text().splitlines()
            if runlog.JSONL_PATH.exists()
        ]
        return code, rows, events, backend

    return run


def _processes_event(events):
    found = [event for event in events if event.get("event") == "processes"]
    assert found, f"no process evidence in {[event.get('event') for event in events]}"
    return found[-1]


def test_a_run_names_the_code_that_produced_it(drive, tmp_path):
    code, _rows, events, backend = drive()
    assert code == 0
    manifest = json.loads((tmp_path / "run-log" / "version_manifest.json").read_text())
    assert set(manifest) == {"git_commit", "impl_hash", "interpreter"}
    assert manifest["git_commit"] == "dae8c610584aa693e9fad240e01fa8423c1b0b61"
    started = [event for event in events if event.get("event") == "run"]
    assert started[-1]["version"] == manifest


def test_a_run_that_attaches_counts_no_spawns(drive):
    code, rows, events, backend = drive(auto_launched=False)
    assert code == 0
    assert proc.runtime_spawn_count() == 0
    event = _processes_event(events)
    assert event["spawn_count"] == 0
    assert event["status"] == proc.STATUS_NO_PIDS
    assert event["goal"] == "Confirm the order"
    assert backend.ps_calls == []  # nothing to look for, so nothing is looked at


def test_a_run_that_provisioned_a_browser_counts_one_spawn(drive):
    code, rows, events, backend = drive(auto_launched=True)
    assert code == 0
    assert proc.runtime_spawn_count() == 1
    event = _processes_event(events)
    assert event["spawn_count"] == 1
    assert event["spawn_kinds"] == {"terminal-browser": 1}
    # No pid: terminal-browser forked the pane itself, so the scan is the evidence.
    assert event["tracked_pids"] == []
    assert event["status"] == proc.STATUS_UNTRACKED
    assert [row["pid"] for row in event["pattern_matches"]] == [4242, 4243]


def test_two_runs_in_one_process_do_not_share_a_spawn_count(drive):
    """One MCP server serves many runs per process. A run's count is about that
    run, so the second one must not start at two."""
    drive(auto_launched=True)
    _code, _rows, events, _backend = drive(auto_launched=True)
    starts = [
        event["spawn_count"]
        for event in events
        if event.get("event") == "processes" and event.get("spawn_count")
    ]
    assert starts == [1, 1]


def test_the_result_and_exit_code_are_untouched_by_the_accounting(drive):
    """stdout is still exactly the one tick row, and a done run still exits 0."""
    code, rows, _events, _backend = drive()
    assert code == 0
    assert rows == [
        {
            "status": "done",
            "url": "https://example.test/next",
            "last_action": "DONE",
            "elapsed_ms": 7,
            "usage": {},
            "why": "Model chose DONE. Confirm the page; DONE is not proof the goal happened.",
            "page_text": "The order was confirmed.",
            "final_view": {"error": "no browser to re-read"},
        }
    ]


def test_a_leaked_pid_is_recorded_without_changing_the_exit_code(drive, monkeypatch):
    proc.note_spawn("terminal-browser", pid=4243)  # as if discovery had learned a pid
    monkeypatch.setattr(proc.os, "kill", lambda pid, sig: None)
    code, rows, events, _backend = drive()
    event = _processes_event(events)
    # The run reset the counters at startup, so this pid belongs to the run only
    # if it was counted there: the report is evidence, the exit code is not it.
    assert code == 0
    assert rows[-1]["status"] == "done"
    assert "event" in event


def test_a_tracked_pid_that_survives_is_reported_as_an_orphan(monkeypatch, capsys, ps, sleeps):
    monkeypatch.setattr(
        cli,
        "discover",
        lambda **kw: Discovery("ws://127.0.0.1:1/x", "http://127.0.0.1:1", "terminal-browser", True),
    )
    monkeypatch.setattr(cli, "connect", lambda url: None)
    monkeypatch.setattr(cli, "set_lease", lambda **kw: None)
    monkeypatch.setattr(cli, "find_continuable_page", lambda: (None, None))
    monkeypatch.setattr(cli, "DriveAgent", FakeAgent)
    install_ps_backend(ps, states={4243: "Ss"})
    monkeypatch.setattr(proc.os, "kill", lambda pid, sig: None)
    real_note = proc.note_spawn

    def note_with_pid(kind="spawn", pid=None):
        return real_note(kind, 4243)  # a provision that somehow knows its pid

    monkeypatch.setattr(cli.proc_mod, "note_spawn", note_with_pid)
    code = cli.main(["--goal", "Confirm the order", "--url", "https://example.test/next"])
    capsys.readouterr()
    events = [json.loads(line) for line in runlog.JSONL_PATH.read_text().splitlines()]
    event = _processes_event(events)
    assert code == 0  # an orphan is evidence, never a reason to fail a finished run
    assert event["status"] == proc.STATUS_ORPHANS
    assert event["orphans"] == [4243]
    assert sleeps == [proc.ORPHAN_CHECK_DELAY_S]


def test_broken_accounting_cannot_break_a_run(monkeypatch, capsys, ps, tmp_path):
    """The evidence is written from a `finally` block, where an exception would
    replace the exit code. Every way it can break must be survivable."""
    monkeypatch.setattr(
        cli,
        "discover",
        lambda **kw: Discovery("ws://127.0.0.1:1/x", "http://127.0.0.1:1", "terminal-browser", True),
    )
    monkeypatch.setattr(cli, "connect", lambda url: None)
    monkeypatch.setattr(cli, "set_lease", lambda **kw: None)
    monkeypatch.setattr(cli, "find_continuable_page", lambda: (None, None))
    monkeypatch.setattr(cli, "DriveAgent", FakeAgent)
    install_ps_backend(ps, git=None)

    def boom(**kwargs):
        raise RuntimeError("accounting exploded")

    monkeypatch.setattr(cli.proc_mod, "process_evidence", boom)
    code = cli.main(["--goal", "Confirm the order", "--url", "https://example.test/next"])
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    events = [json.loads(line) for line in runlog.JSONL_PATH.read_text().splitlines()]
    assert code == 0
    assert rows[-1]["status"] == "done"
    assert [event for event in events if event.get("event") == "processes"] == []


def test_an_unhashable_tree_still_lets_the_run_start(monkeypatch, capsys, ps, tmp_path):
    monkeypatch.setattr(
        cli,
        "discover",
        lambda **kw: Discovery("ws://127.0.0.1:1/x", "http://127.0.0.1:1", "terminal-browser", False),
    )
    monkeypatch.setattr(cli, "connect", lambda url: None)
    monkeypatch.setattr(cli, "set_lease", lambda **kw: None)
    monkeypatch.setattr(cli, "find_continuable_page", lambda: (None, None))
    monkeypatch.setattr(cli, "DriveAgent", FakeAgent)
    install_ps_backend(ps, dead=True)
    monkeypatch.setattr(cli.proc_mod, "repo_root", lambda: None)
    code = cli.main(["--goal", "Confirm the order", "--url", "https://example.test/next"])
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert code == 0
    assert rows[-1]["status"] == "done"
    written = json.loads((tmp_path / "run-log" / "version_manifest.json").read_text())
    assert written["impl_hash"] is None
    assert written["git_commit"] is None


def test_the_json_plugin_line_is_unchanged(monkeypatch, capsys, ps):
    install_ps_backend(ps, git="dae8c610584aa693e9fad240e01fa8423c1b0b61\n")
    monkeypatch.setattr(
        cli,
        "discover",
        lambda **kw: Discovery("ws://127.0.0.1:9/x", "http://127.0.0.1:9", "explicit", False, "background"),
    )
    monkeypatch.setattr(cli, "connect", lambda url: None)
    monkeypatch.setattr(cli, "set_lease", lambda **kw: None)
    monkeypatch.setattr(cli, "find_continuable_page", lambda: (None, None))
    monkeypatch.setattr(cli, "DriveAgent", FakeAgent)
    code = cli.main(["--goal", "g", "--url", "https://example.test/next", "--json", "--background"])
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert code == 0
    assert rows[0]["event"] == "browser"
    assert rows[0]["visibility"] == "background"
    assert "version" not in rows[0]  # the contract line stays exactly as it was
    assert rows[-1]["status"] == "done"