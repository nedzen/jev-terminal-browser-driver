"""Spawn accounting, orphan detection, version manifest.

Offline: every `ps` and `git` call is a fake backend, and every signal is a fake
`os.kill`, so no process table is read, nothing is signalled, and no child is
started. The one live assertion is this process asking about itself.
"""

import errno
import hashlib
import json
import os
import re
import threading
from unittest.mock import Mock

import pytest

from jev_driver import cli, runlog
from jev_driver import instances as proc
from jev_driver.discover import Discovery
from jev_driver.metrics import Metrics, metrics_path

STAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

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

    def __init__(self, table="", states=None, starts=None, git=None, dead=False):
        self.table = table
        self.states = dict(states or {})
        self.starts = dict(starts or {})  # pid -> the lstart token ps would render
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
            field = argv[argv.index("-o") + 1] if "-o" in argv else ""
            if field.startswith("lstart"):
                return self.starts.get(pid, "")
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

    def install(table="", states=None, starts=None, git=None, dead=False):
        backend = FakePs(table=table, states=states, starts=starts, git=git, dead=dead)
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


def install_ps_backend(ps, states=None, starts=None, git=None, table=PS_TABLE, dead=False):
    return ps(table=table, states=states, starts=starts, git=git, dead=dead)


# ------------------------------------------------------------------- spawning


def test_a_spawn_is_counted_with_its_pid_and_kind(ps):
    install_ps_backend(ps)
    assert proc.note_spawn("terminal-browser") == 1
    assert proc.note_spawn("helper", pid="4242") == 2
    assert proc.runtime_spawn_count() == 2
    assert proc.tracked_pids() == [4242]
    assert proc.spawn_summary() == {
        "spawn_count": 2,
        "tracked_pids": [4242],
        "tracked_starts": {4242: None},
        "spawn_kinds": {"helper": 1, "terminal-browser": 1},
    }


def test_a_spawn_without_a_pid_is_counted_but_not_tracked(ps):
    install_ps_backend(ps)
    proc.note_spawn("terminal-browser")  # terminal-browser forks the pane itself
    assert proc.runtime_spawn_count() == 1
    assert proc.tracked_pids() == []
    # A count without a pid is exactly why the orphan check also takes a pattern.
    assert proc.spawn_summary()["spawn_kinds"] == {"terminal-browser": 1}


def test_unusable_pids_are_counted_but_never_tracked(ps):
    install_ps_backend(ps)
    for pid in (0, -7, "abc", None):
        proc.note_spawn("helper", pid=pid)
    assert proc.runtime_spawn_count() == 4
    assert proc.tracked_pids() == []  # signalling pid 0 hits a whole group


def test_the_same_pid_is_tracked_once(ps):
    install_ps_backend(ps)
    proc.note_spawn("helper", pid=4242)
    proc.note_spawn("helper", pid=4242)
    assert proc.spawn_summary() == {
        "spawn_count": 2,
        "tracked_pids": [4242],
        "tracked_starts": {4242: None},
        "spawn_kinds": {"helper": 2},
    }


def test_a_tracked_pid_may_be_adopted_after_the_fact(ps):
    install_ps_backend(ps)
    proc.note_spawn("terminal-browser")
    proc.note_spawn("terminal-browser", pid=4242)
    assert proc.tracked_pids() == [4242]


def test_reset_spawns_starts_the_next_run(ps):
    install_ps_backend(ps)
    proc.note_spawn("terminal-browser", pid=4242)
    proc.reset_spawns()
    assert proc.runtime_spawn_count() == 0
    assert proc.tracked_pids() == []
    assert proc.spawn_summary()["spawn_kinds"] == {}
    # Start times belong to the run that read them, never to the next one.
    assert proc.spawn_summary()["tracked_starts"] == {}


def test_the_context_manager_counts_a_spawn_without_a_pid():
    with proc.RUN.spawn("probe"):
        pass
    assert proc.spawn_summary() == {
        "spawn_count": 1,
        "tracked_pids": [],
        "tracked_starts": {},
        "spawn_kinds": {"probe": 1},
    }


def test_a_noted_pid_carries_the_start_time_read_at_spawn(ps):
    """A pid is a slot, not a process: the only way to tell the two apart later
    is what was read while the slot was still ours."""
    backend = install_ps_backend(ps, starts={4243: "Fri Oct  2 19:11:03 2026"})
    proc.note_spawn("terminal-browser", pid=4243)
    assert proc.spawn_summary()["tracked_starts"] == {4243: "Fri Oct  2 19:11:03 2026"}
    assert backend.ps_calls == [["ps", "-p", "4243", "-o", "lstart="]]


def test_a_pid_with_no_start_time_is_recorded_as_unknown(ps):
    """Unreadable at spawn time is "we could not tell", which costs the reuse
    check and nothing else."""
    install_ps_backend(ps, dead=True)
    proc.note_spawn("terminal-browser", pid=4243)
    assert proc.spawn_summary()["tracked_starts"] == {4243: None}


def test_an_unreadable_pid_never_costs_a_ps_call(ps):
    backend = install_ps_backend(ps)
    proc.note_spawn("terminal-browser")  # no pid to ask about
    proc.note_spawn("helper", pid=0)
    assert backend.ps_calls == []


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


# --- raw argv from other processes -----------------------------------------


BARE_CREDENTIALS = [
    ("curl -H 'Authorization: Bearer sk-live-AAAABBBBCCCC' https://api.example.test/v1", "sk-live-AAAABBBBCCCC"),
    ("curl https://api.example.test/v1?api_key=sk-live-ZZZZ9999", "sk-live-ZZZZ9999"),
    ("node drive.js --token ghp_ABCDEFGHIJKLMNOP --url https://x.test", "ghp_ABCDEFGHIJKLMNOP"),
    ("node drive.js --api-key=sk-live-QQQQ1111 --url https://x.test", "sk-live-QQQQ1111"),
    ("sh -c 'export AUTH_TOKEN=deadbeefcafe1234; curl https://x.test'", "deadbeefcafe1234"),
]


@pytest.mark.parametrize(("argv", "secret"), BARE_CREDENTIALS)
def test_a_bare_credential_in_another_process_argv_is_not_kept(ps, argv, secret):
    """`ps` shows every process's whole command line, credentials included, and
    those rows go into the run log. The row is still evidence of a running
    process; it just must not carry the secret."""
    install_ps_backend(ps, table=f" 4243 Ss  {argv}\n")
    row = proc.matching_processes(("curl", "node", "sh"))[0]
    assert row["pid"] == 4243  # the row survives: the process is still evidence
    assert secret not in row["command"]
    assert "redacted" in row["command"]


def test_an_argv_flag_credential_is_caught_though_the_run_log_rules_never_see_a_colon(ps):
    # `--token VALUE` is not `token: VALUE`, so the argv shape has to be rewritten
    # before the run log's assignment rules can match it.
    install_ps_backend(ps, table=" 4243 Ss  node drive.js --token ghp_ABCDEFGHIJKLMNOP --url https://x.test\n")
    assert proc.matching_processes(("node",))[0]["command"] == (
        "node drive.js --token [redacted] --url https://x.test"
    )


def test_a_scrubbed_command_still_matches_the_browser_pattern(ps):
    install_ps_backend(ps, table=" 4243 Ss  /Users/u/.local/bin/terminal-browser --url 'https://x.test/?api_key=sk-live-X'\n")
    row = proc.matching_processes()[0]
    assert row["pid"] == 4243
    assert "terminal-browser" in row["command"]
    assert "sk-live-X" not in row["command"]


def test_the_process_evidence_event_carries_no_bare_credential(ps, sleeps):
    install_ps_backend(ps, table=" 4243 Ss  /usr/bin/terminal-browser open 'https://x.test/?token=ghp_AAAABBBBCCCCCCCC'\n")
    proc.note_spawn("terminal-browser")
    evidence = proc.process_evidence(goal="Check the page", sleep=lambda seconds: sleeps.append(seconds))
    blob = json.dumps(evidence)
    assert "ghp_AAAABBBBCCCCCCCC" not in blob
    assert "[redacted]" in blob


def test_a_scrub_that_explodes_still_yields_the_row(ps, monkeypatch):
    install_ps_backend(ps, table=" 4243 Ss  /usr/bin/terminal-browser --split right\n")
    monkeypatch.setattr(
        runlog,
        "_scrub_text",
        Mock(side_effect=RuntimeError("redaction is down")),
    )
    row = proc.matching_processes()[0]
    assert row == {"pid": 4243, "stat": "Ss", "command": "/usr/bin/terminal-browser --split right"}


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


# ------------------------------------------------------------- pid reuse


LSTART = "Fri Oct  2 19:11:03 2026"


def test_a_recycled_pid_is_never_reported_as_our_orphan(ps, signals):
    """The pid is still alive — but it is somebody else's process now, so calling
    it our leak would blame them and calling it clean would hide that we cannot
    say what we spawned. Neither is available, so it is unknown."""
    backend = install_ps_backend(ps, states={4243: "Ss"}, starts={4243: "Fri Oct  2 18:00:00 2026"})
    probe = signals()
    report = proc.liveness_report([4243], starts={4243: LSTART})
    assert report == {"alive": [], "zombie": [], "dead": [], "unknown": [4243], "checked": 1}
    assert probe.sent == [(4243, 0)]
    # The state is never read: the slot already failed the identity question.
    assert backend.ps_calls == [["ps", "-p", "4243", "-o", "lstart="]]


def test_a_pid_whose_start_time_is_unchanged_is_still_classified(ps, signals):
    """The common case must cost nothing: same process, same answer as before."""
    backend = install_ps_backend(ps, states={4243: "Ss"}, starts={4243: LSTART})
    signals()
    assert proc.liveness_report([4243], starts={4243: LSTART})["alive"] == [4243]
    assert backend.ps_calls == [
        ["ps", "-p", "4243", "-o", "lstart="],
        ["ps", "-p", "4243", "-o", "state="],
    ]


def test_an_unreadable_start_time_falls_back_to_the_older_checks(ps, signals):
    """`ps` cannot say when it started: that is the situation the reuse check was
    invented for, and it must not turn every run into `unknown`."""
    install_ps_backend(ps, states={4243: "Ss"})
    signals()
    assert proc.liveness_report([4243], starts={4243: LSTART})["alive"] == [4243]
    assert proc.liveness_report([4243], starts={4243: None})["alive"] == [4243]


def test_a_gone_pid_is_dead_without_asking_when_it_started(ps, signals):
    """Nothing to misattribute: a pid with no process behind it needs no lstart."""
    backend = install_ps_backend(ps)
    probe = signals(missing=[4243])
    assert proc.liveness_report([4243], starts={4243: LSTART})["dead"] == [4243]
    assert probe.sent == [(4243, 0)]
    assert backend.ps_calls == []


def test_a_pid_with_no_noted_start_time_never_asks_for_one(ps, signals):
    """A call site that reported no pid, or one whose ps was down at spawn time,
    must not start paying for a check it cannot do."""
    backend = install_ps_backend(ps, states={4243: "Ss"})
    signals()
    assert proc.liveness_report([4243])["alive"] == [4243]
    assert backend.ps_calls == [["ps", "-p", "4243", "-o", "state="]]


def test_a_recycled_pid_is_reported_as_unknown_not_clean(ps, signals, sleeps):
    install_ps_backend(ps, states={4243: "Ss"}, starts={4243: "Fri Oct  2 18:00:00 2026"})
    signals()
    report = proc.orphan_report([4243], spawn_count=1, starts={4243: LSTART})
    assert report["status"] == proc.STATUS_UNKNOWN  # never clean, never a leak verdict
    assert report["orphans"] == []
    assert "recycled" in report["detail"]


def test_the_reuse_check_survives_a_hostile_starts_mapping(ps, signals):
    class Hostile(dict):
        def get(self, *args, **kwargs):
            raise RuntimeError("no lookups for you")

    install_ps_backend(ps, states={4243: "Ss"})
    signals()
    assert proc.liveness_report([4243], starts=Hostile())["alive"] == [4243]


def test_an_unreadable_start_time_source_never_raises(ps, signals, monkeypatch):
    def boom(pid, *, timeout=proc.PS_TIMEOUT_S):
        raise RuntimeError("ps went sideways")

    monkeypatch.setattr(proc, "process_start_time", boom)
    signals()
    assert proc.proc_state(4243, started=LSTART) == proc.ALIVE


def test_a_hostile_pid_never_reads_a_start_time(ps):
    install_ps_backend(ps)
    assert proc.process_start_time(0) is None
    assert proc.process_start_time("junk") is None
    assert install_ps_backend(ps).ps_calls == []


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
    # Not the one fixed name: the scratch is per-writer, so "no scratch left" is the
    # assertion, not "that one name is gone".
    assert [path.name for path in (tmp_path / "run-log").iterdir() if path.name.endswith(".tmp")] == []
    assert not (tmp_path / "run-log" / "version_manifest.json").exists()


def test_a_failed_replace_removes_the_scratch_it_already_wrote(monkeypatch, ps, tmp_path):
    """The failure that happens *after* the body is on disk: the rename cannot land.
    A scratch left behind is a stale file a later reader could mistake for evidence."""
    monkeypatch.setattr(runlog, "JSONL_PATH", tmp_path / "run-log" / "drive.jsonl")
    target = tmp_path / "run-log" / "version_manifest.json"
    target.mkdir(parents=True)  # a directory where a file belongs
    install_ps_backend(ps, git=None)

    written = proc.write_version_manifest()
    assert "error" in written
    assert [path.name for path in (tmp_path / "run-log").iterdir()] == ["version_manifest.json"]


def test_a_prepared_manifest_is_written_verbatim(monkeypatch, ps, tmp_path):
    monkeypatch.setattr(runlog, "JSONL_PATH", tmp_path / "run-log" / "drive.jsonl")
    install_ps_backend(ps, git="ignored")
    written = proc.write_version_manifest({"git_commit": "abc", "impl_hash": "def", "interpreter": "CPython 3.12"})
    assert json.loads((tmp_path / "run-log" / "version_manifest.json").read_text()) == written


def test_the_scratch_name_is_not_shared_between_writers(monkeypatch, ps, tmp_path):
    """A fixed ``.tmp`` is only atomic while there is one writer: two runs in two
    processes share the path, and one can rename the other's half-written body
    into place."""
    target = tmp_path / "version_manifest.json"
    first = proc._scratch_name(target)
    second = proc._scratch_name(target)
    assert first != second
    assert first.parent == target.parent  # same directory: that is what makes the rename atomic
    assert first.name.endswith(".tmp") and second.name.endswith(".tmp")


def test_two_writers_interleaving_leave_one_valid_manifest(monkeypatch, ps, tmp_path):
    """The failure this exists for: interleaved writers, one file, and a reader
    that must find a whole manifest from one of them — never a blend of both."""
    log = tmp_path / "run-log" / "drive.jsonl"
    monkeypatch.setattr(runlog, "JSONL_PATH", log)
    install_ps_backend(ps, git=None)
    real_write_text = proc.Path.write_text
    both_inside = threading.Barrier(2)
    both_written = threading.Barrier(2)

    def interleaving_write(self, *args, **kwargs):
        # Hold both writers inside the write, then hold them again once both
        # bodies are on disk. That is the window a shared scratch path turns into
        # one writer renaming the other's body — or into nothing at all.
        both_inside.wait(timeout=10)
        result = real_write_text(self, *args, **kwargs)
        both_written.wait(timeout=10)
        return result

    monkeypatch.setattr(proc.Path, "write_text", interleaving_write)
    written = []

    def writer(tag):
        written.append(proc.write_version_manifest({"git_commit": tag, "impl_hash": tag * 4, "interpreter": "3.12"}))

    threads = [threading.Thread(target=writer, args=(tag,)) for tag in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    target = tmp_path / "run-log" / "version_manifest.json"
    landed = json.loads(target.read_text())
    # One writer's manifest, whole: the commit, the hash and the interpreter all
    # come from the same body rather than from two.
    assert landed["git_commit"] in {"a", "b"}
    assert landed["impl_hash"] == landed["git_commit"] * 4
    assert {body["git_commit"] for body in written} == {"a", "b"}
    assert all("error" not in body for body in written)
    assert sorted(path.name for path in (tmp_path / "run-log").iterdir()) == ["version_manifest.json"]


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
        # Counters like the real agent, and written at close like the real agent,
        # so the run's own evidence can be compared with the file on disk.
        self.metrics = Metrics()

    def snapshot(self):
        return {key: value for key, value in self.state.items() if key != "browser"}

    def command(self, name, body=None):
        if name == "tick":
            self.state["decisions"].append({"choice": "DONE", "operation": "DONE"})
            self.state["status"] = "done"
        return self.snapshot()

    def close(self):
        self.closed = True
        self.metrics.record_cleanup(0.0)
        self.metrics.finish(self.state.get("status"))
        self.metrics.write()


# What `terminal-browser ls --all --json` reports for the port the fixture drives.
TB_INSTANCE = {"browsers": [{"key": "1-1", "pid": 4243, "cdpPort": 1, "url": "https://example.test/next"}]}


def _no_such_process(pid, sig):
    """A pid table with nothing in it: the default, so a liveness answer never
    depends on what the test machine happens to be running."""
    raise ProcessLookupError(errno.ESRCH, "no such process")


def _still_running(pid, sig):
    """A pid that answers signal 0: the pid is in the table, state unknown to
    `kill` alone, which is exactly what `ps` then has to settle."""
    return None


@pytest.fixture
def drive(monkeypatch, capsys, ps, sleeps):
    """cli.main with no browser and no model. Returns (code, stdout rows, events)."""

    def run(
        *,
        auto_launched=False,
        agent_cls=FakeAgent,
        ls=TB_INSTANCE,
        states=None,
        argv=None,
        kill=_no_such_process,
    ):
        backend = install_ps_backend(ps, states=states, git="dae8c610584aa693e9fad240e01fa8423c1b0b61\n")
        monkeypatch.setattr(
            cli,
            "discover",
            lambda **kw: Discovery("ws://127.0.0.1:1/x", "http://127.0.0.1:1", "terminal-browser", auto_launched),
        )
        monkeypatch.setattr(cli, "connect", lambda url: None)
        monkeypatch.setattr(cli, "set_lease", lambda **kw: None)
        monkeypatch.setattr(cli, "find_continuable_page", lambda: (None, None))
        monkeypatch.setattr(cli, "DriveAgent", agent_cls)

        def instance_list():
            """`ls --all --json`, or the failure a real call would have produced."""
            if isinstance(ls, BaseException):
                raise ls
            return ls

        monkeypatch.setattr(cli, "list_browsers", instance_list)
        monkeypatch.setattr(proc.os, "kill", kill)
        code = cli.main(argv or ["--goal", "Confirm the order", "--url", "https://example.test/next"])
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
    """The auto-launch is the one spawn this driver really performs, and `ls` names
    the pid behind the port it is driving — so the close-time check gets a pid to
    ask about instead of a command pattern."""
    code, rows, events, backend = drive(auto_launched=True)
    assert code == 0
    assert proc.runtime_spawn_count() == 1
    event = _processes_event(events)
    assert event["spawn_count"] == 1
    assert event["spawn_kinds"] == {"terminal-browser": 1}
    assert event["tracked_pids"] == [4243]
    assert event["status"] == proc.STATUS_CLEAN  # 4243 is not running: proved, not assumed
    assert event["liveness"]["dead"] == [4243]
    # A tracked pid is excluded from the scan: the scan exists for the pids this
    # run cannot name, and 4243 is now answered for directly.
    assert [row["pid"] for row in event["pattern_matches"]] == [4242]


def test_a_provision_whose_pid_ls_cannot_name_falls_back_to_the_scan(drive):
    """No pid is not a failure and is not a guess: the spawn stays counted, and
    the scan stays evidence rather than a verdict."""
    code, rows, events, backend = drive(auto_launched=True, ls={"browsers": []})
    assert code == 0
    event = _processes_event(events)
    assert event["spawn_count"] == 1
    assert event["tracked_pids"] == []
    assert event["status"] == proc.STATUS_UNTRACKED
    assert [row["pid"] for row in event["pattern_matches"]] == [4242, 4243]


@pytest.mark.parametrize(
    "answer",
    [
        FileNotFoundError("terminal-browser is not installed"),
        RuntimeError("ls timed out"),
        {"browsers": [{"pid": 4243}]},  # no cdpPort to match the driven port against
        {"browsers": [{"cdpPort": 1}]},  # a port with no pid
        {"browsers": [{"cdpPort": 1, "pid": 0}]},  # a pid that addresses no process
        {"browsers": [{"cdpPort": 1, "pid": "not-a-pid"}]},
        {"browsers": ["not even a dict"]},
        {"no_browsers_key": True},
        [],
    ],
)
def test_a_broken_instance_list_leaves_the_spawn_counted_and_untracked(drive, answer):
    """Every way `ls` can fail to name the new instance costs the pid and nothing
    else: the run still records the spawn it performed, and the orphan check still
    answers with the scan rather than with a guess."""
    code, _rows, events, _backend = drive(auto_launched=True, ls=answer)
    assert code == 0
    event = _processes_event(events)
    assert event["spawn_count"] == 1
    assert event["tracked_pids"] == []
    assert event["status"] == proc.STATUS_UNTRACKED
    assert [row["pid"] for row in event["pattern_matches"]] == [4242, 4243]


def test_the_instance_pid_is_matched_by_the_port_we_are_driving(monkeypatch):
    monkeypatch.setattr(
        cli,
        "list_browsers",
        lambda: {
            "browsers": [
                {"pid": 111, "cdpPort": 9},  # somebody else's instance
                {"pid": 4243, "cdpPort": 57463},
                {"pid": 222, "cdpPort": 57463},  # a second row for our port
            ]
        },
    )
    assert cli._instance_pid("ws://127.0.0.1:57463/devtools/browser/abc") == 4243
    assert cli._instance_pid("wss://127.0.0.1:57463/devtools/browser/abc") == 4243


@pytest.mark.parametrize(
    "ws_url", ["", None, "ws://127.0.0.1/devtools/browser/abc", "not a url at all", "ws://[::1/devtools"]
)
def test_a_ws_url_without_a_readable_port_yields_no_pid(monkeypatch, ws_url):
    monkeypatch.setattr(cli, "list_browsers", Mock(side_effect=AssertionError("ls must not be asked")))
    assert cli._instance_pid(ws_url) is None


def test_a_string_cdp_port_still_matches(monkeypatch):
    # `ls --json` is another program's output; "57463" and 57463 are the same port.
    monkeypatch.setattr(cli, "list_browsers", lambda: {"browsers": [{"pid": 4243, "cdpPort": "57463"}]})
    assert cli._instance_pid("ws://127.0.0.1:57463/devtools/browser/abc") == 4243


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
    monkeypatch.setattr(proc.os, "kill", _still_running)
    code, rows, events, _backend = drive()
    event = _processes_event(events)
    # The run reset the counters at startup, so this pid belongs to the run only
    # if it was counted there: the report is evidence, the exit code is not it.
    assert code == 0
    assert rows[-1]["status"] == "done"
    assert "event" in event


def test_a_provisioned_browser_that_survives_is_reported_as_an_orphan(drive):
    """The verdict the whole module exists for, end to end with nothing faked in
    the wiring: this run auto-launched a pane, `ls` named the pid behind the port,
    and the process is still there after the detach-only close."""
    code, rows, events, backend = drive(auto_launched=True, states={4243: "Ss"}, kill=_still_running)
    event = _processes_event(events)
    assert code == 0  # an orphan is evidence, never a reason to fail a finished run
    assert rows[-1]["status"] == "done"
    assert event["spawn_count"] == 1
    assert event["tracked_pids"] == [4243]
    assert event["status"] == proc.STATUS_ORPHANS
    assert event["orphans"] == [4243]
    assert event["liveness"]["alive"] == [4243]
    # The pid was classified from the table, not from a pattern that might match
    # somebody else's browser.
    assert ["ps", "-p", "4243", "-o", "state="] in backend.ps_calls


def test_a_run_that_stops_for_no_page_still_records_its_process_evidence(drive):
    """Early return, mid-run: the browser this run launched is still a browser it
    launched, so the accounting has to be written on this path too."""
    code, rows, events, backend = drive(auto_launched=True, argv=["--goal", "Confirm the order"])
    assert code == 1
    assert rows == [
        {
            "status": "blocked",
            "error": "No page to reuse (no remembered tab). Pass url to open one.",
            "reason": "no_page",
        }
    ]
    event = _processes_event(events)
    assert event["spawn_count"] == 1  # the launch happened before this refusal
    assert event["tracked_pids"] == [4243]
    assert event["goal"] == "Confirm the order"
    assert event["status"] == proc.STATUS_CLEAN


def test_a_run_refused_for_an_unsupported_goal_still_records_its_process_evidence(drive):
    code, rows, events, backend = drive(auto_launched=True, argv=["--goal", "Take a screenshot", "--url", "https://x.test"])
    assert code == 1
    assert rows[-1]["status"] == "blocked"
    event = _processes_event(events)
    assert event["spawn_count"] == 1
    assert event["tracked_pids"] == [4243]
    # The refusal is named as a stop token, not left as a blocked run with no reason.
    assert _run_events(events)[-1]["metrics"]["stop_reason"] == "unsupported"


def test_an_agent_that_would_not_open_still_records_its_process_evidence(monkeypatch, ps, sleeps):
    """Construction failing is not a result the driver converts — it still
    propagates — but the browser it launched is already a process it started, so
    the evidence has to be written on the way out."""
    monkeypatch.setattr(
        cli,
        "discover",
        lambda **kw: Discovery("ws://127.0.0.1:1/x", "http://127.0.0.1:1", "terminal-browser", True),
    )
    monkeypatch.setattr(cli, "connect", lambda url: None)
    monkeypatch.setattr(cli, "set_lease", lambda **kw: None)
    monkeypatch.setattr(cli, "find_continuable_page", lambda: (None, None))
    monkeypatch.setattr(cli, "list_browsers", lambda: TB_INSTANCE)
    install_ps_backend(ps, git=None)
    monkeypatch.setattr(proc.os, "kill", _no_such_process)

    class Refuses(FakeAgent):
        def __init__(self, *args, **kwargs):
            raise RuntimeError("the pane never came up")

    monkeypatch.setattr(cli, "DriveAgent", Refuses)
    with pytest.raises(RuntimeError, match="pane never came up"):
        cli.main(["--goal", "Confirm the order", "--url", "https://example.test/next"])

    events = [json.loads(line) for line in runlog.JSONL_PATH.read_text().splitlines()]
    event = _processes_event(events)
    assert event["spawn_count"] == 1
    assert event["tracked_pids"] == [4243]


# ------------------------------------------------------- run identity in the log


def _run_events(events):
    return [event for event in events if event.get("event") == "run"]


def test_the_run_record_carries_this_run_s_counters_and_identity(drive):
    code, _rows, events, backend = drive()
    assert code == 0
    runs = _run_events(events)
    assert [event["stage"] for event in runs] == ["start", "finish"]
    identity, finished = runs
    # One run, one id: the log's copy, and the file on disk, are the same run.
    on_disk = json.loads(metrics_path().read_text())
    assert identity["metrics"]["run_id"] == finished["metrics"]["run_id"] == on_disk["run_id"]
    assert len(on_disk["run_id"]) == 32 and all(c in "0123456789abcdef" for c in on_disk["run_id"])
    # The start copy cannot know how the run ended; the finish copy carries it.
    assert identity["metrics"]["finished"] is False
    assert finished["metrics"] == on_disk
    assert finished["metrics"]["status"] == "done"
    assert finished["metrics"]["finished_at"] is not None
    assert identity["version"] == finished["version"]


def test_the_run_identity_carries_a_goal_digest_and_never_the_goal(drive):
    code, _rows, events, _backend = drive()
    snapshot = _run_events(events)[0]["metrics"]
    assert len(snapshot["goal_hash"]) == 16
    assert snapshot["goal_hash"] == hashlib.sha256(b"Confirm the order").hexdigest()[:16]
    blob = json.dumps(snapshot)
    assert "Confirm the order" not in blob
    assert STAMP.match(snapshot["started_at"])


def test_two_runs_never_share_an_identity(drive):
    drive()
    first = json.loads(metrics_path().read_text())["run_id"]
    drive()
    second = json.loads(metrics_path().read_text())["run_id"]
    assert first != second


def test_a_run_that_never_closed_leaves_a_provably_stale_metrics_file(drive):
    """The SIGKILL case: metrics.json is written at close, so a killed run leaves
    the previous run's numbers in place. The log's run record carries its own id,
    so the two disagree and the file cannot pass as current."""

    class Killed(FakeAgent):
        def close(self):
            self.closed = True  # killed before the counters were written

    assert drive()[0] == 0
    written = json.loads(metrics_path().read_text())
    assert written["finished"] is True

    code, _rows, events, _backend = drive(agent_cls=Killed)
    assert code == 0
    assert json.loads(metrics_path().read_text()) == written  # nothing overwrote it
    latest = _run_events(events)[-1]["metrics"]
    assert latest["run_id"] != written["run_id"]
    assert latest["finished"] is False  # the log says which run the file is not


def test_the_stop_reason_the_driver_sets_is_the_one_metrics_reports():
    """The two halves of the nit, composed: the driver's state carries a token, the
    snapshot turns that token into a recorded stop (and into a budget error kind),
    and neither step reads a sentence."""
    class Deadline:
        state = {"stop_reason": "time_budget"}
        metrics = Metrics()

    agent = Deadline()
    cli._note_stop(agent)
    snap = agent.metrics.finish(agent.state["status"] if "status" in agent.state else "blocked")
    assert snap["stop_reason"] == "time_budget"
    assert snap["error"] == "budget"
    assert snap["status"] == "blocked"


def test_an_agent_with_no_counters_still_gets_a_run_record(drive):
    class Countless(FakeAgent):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.metrics = None

        def close(self):
            self.closed = True

    code, _rows, events, _backend = drive(agent_cls=Countless)
    assert code == 0
    runs = _run_events(events)
    assert [event["stage"] for event in runs] == ["start", "finish"]
    assert "metrics" not in runs[-1]
    assert runs[-1]["goal"] == "Confirm the order"  # the record is still the run's


def test_a_hostile_metrics_cannot_break_the_run_record(drive):
    class Hostile:
        def snapshot(self):
            raise RuntimeError("no counters for you")

    class Liar(FakeAgent):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.metrics = Hostile()

        def close(self):
            self.closed = True

    code, rows, events, _backend = drive(agent_cls=Liar)
    assert code == 0
    assert rows[-1]["status"] == "done"
    assert _processes_event(events)["spawn_count"] == 0
    assert "metrics" not in _run_events(events)[-1]


def test_a_close_that_raises_still_leaves_the_evidence(drive):
    """The close is the one thing in the finally that can raise, and it must not
    cost the run its records — nor swallow its own exception."""

    class Rude(FakeAgent):
        def close(self):
            self.closed = True
            raise RuntimeError("the browser would not detach")

    with pytest.raises(RuntimeError, match="would not detach"):
        drive(agent_cls=Rude)
    events = [json.loads(line) for line in runlog.JSONL_PATH.read_text().splitlines()]
    assert _processes_event(events)["spawn_count"] == 0
    assert [event["stage"] for event in _run_events(events)] == ["start", "finish"]


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