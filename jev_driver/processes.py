"""Spawn accounting, orphan detection, and which code produced a run.

Stdlib only, and nothing in here raises. This module is evidence, not control
flow: a run that finished its work must not fail because a status check could
not answer.

Three jobs, one per run:

- **Spawn accounting.** ``note_spawn()`` is the seam a process-creating call
  site reports to, ``runtime_spawn_count()`` is how many this run created. Until
  a run counts its own spawns, "we started one browser" is an assumption.
- **Orphan detection.** close is detach-only (see ``browser.Browser.close``), so
  a leftover terminal-browser keeps a pane and a CDP port and nothing in the log
  mentions it. After close, ``orphan_report()`` asks ``ps`` who is still there.
- **Provenance.** ``version_manifest()`` names the code behind a run: git commit,
  a SHA-256 over the implementation files, and the interpreter.

Zombie semantics — the part worth reading twice
-----------------------------------------------
``os.kill(pid, 0)`` answers "is this pid in the process table", not "is this
process running". A child that has exited but has not been waited on is a
zombie: signal 0 succeeds, yet the process holds no pane, no socket, no CPU and
will never do anything again. Reading that as alive reports an orphan for a
process that already did its job — the commonest false positive available here,
because a driver spawns short-lived helpers that nobody reaps. So
``proc_state()`` reads the process state as well and answers ``zombie`` for it,
and ``orphan_report()`` counts only ``alive`` pids as orphans.

A zombie is not an orphan either: this run is closing, init reaps the corpse
then, and a dead process leaks nothing but a slot. The four answers are
``alive`` (running), ``zombie`` (exited, unreaped), ``dead`` (gone) and
``unknown`` (we were not allowed to look, or no status source exists) — and
``unknown`` deliberately never counts as clean, because "we could not check" is
not "there is nothing there".

A command-pattern match is evidence, not a verdict. The driver attaches to
panes it did not create and leaves them running on purpose, so ``ps`` rows land
in ``pattern_matches``, separately from the pids this run tracked itself. Only
those tracked pids can make a run report ``orphans``.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import platform
import subprocess
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from . import runlog
from .preflight import driver_home

# ps and git are local and answer in milliseconds; a loaded box must not turn a
# status check into a hang, so both are bounded and every failure is reported
# rather than raised.
PS_TIMEOUT_S = 3.0
GIT_TIMEOUT_S = 3.0
# A process tearing down right after close still answers signal 0 for a moment.
# Ask again after a beat so a clean exit is not recorded as a leak.
ORPHAN_CHECK_DELAY_S = 0.5

MANIFEST_NAME = "version_manifest.json"
# Implementation files that decide what a run does. Hashed, never read out.
HASHED_DIRS = ("jev_driver", "scripts")
DEFAULT_PATTERNS = ("terminal-browser",)
MAX_PATTERN_MATCHES = 20
MAX_TRACKED_PIDS = 64
MAX_COMMAND_CHARS = 200

ALIVE = "alive"
ZOMBIE = "zombie"
DEAD = "dead"
UNKNOWN = "unknown"

# First character of a ps STAT field. macOS marks an exited process "Z+", Linux
# spells a dead-but-listed process "X"/"x"; both start with Z or X.
ZOMBIE_LETTERS = ("Z", "X", "x")
# Everything else we are willing to call running. "I" is macOS idle-kernel
# thread, "W" is Linux paging, "U"/"P"/"K"/"T" are the BSD/older states.
RUNNING_LETTERS = ("R", "S", "D", "I", "T", "U", "W", "K", "P")

# Close-time report vocabulary. Only "orphans" is a leak verdict.
STATUS_ORPHANS = "orphans"
STATUS_UNKNOWN = "unknown"
STATUS_CLEAN = "clean"
STATUS_NO_PIDS = "no_pids"
STATUS_UNTRACKED = "untracked"


def _short(exc: BaseException) -> str:
    try:
        return (str(exc) or exc.__class__.__name__)[:200]
    except Exception:  # a hostile __str__ must not become a second failure
        return exc.__class__.__name__


# ---------------------------------------------------------------- subprocess


def run_cmd(argv: list[str], *, timeout: float) -> str | None:
    """stdout of one short-lived helper process, or None. Never raises.

    ``argv`` is a list, never a shell string: a repo path or a pattern can hold
    spaces, quotes and worse. Anything that goes wrong — no such binary, a
    non-zero exit, a timeout, a decode failure — is None, and the caller
    reports "could not check" rather than crashing a finished run.
    """
    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if completed.returncode != 0:
        return None
    try:
        return completed.stdout or ""
    except Exception:
        return None


def ps_output(args: list[str], *, timeout: float = PS_TIMEOUT_S) -> str | None:
    """``ps`` output, or None. ``-A -o`` is the one spelling both BSD/macOS ps
    and Linux procps accept; a pid-less, header-less column list keeps parsing
    identical on the two."""
    return run_cmd(["ps", *args], timeout=timeout)


def process_table(*, timeout: float = PS_TIMEOUT_S) -> list[dict]:
    """Every visible process as ``{"pid", "stat", "command"}``. Never raises.

    The kernel's own threads carry no useful command line; they are kept
    (they are part of an honest table) and filtered by the callers.
    """
    rows: list[dict] = []
    out = ps_output(["-A", "-o", "pid=,stat=,command="], timeout=timeout)
    for line in (out or "").splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3 or not parts[0].isdigit():
            continue
        rows.append(
            {
                "pid": int(parts[0]),
                "stat": parts[1][:4],
                "command": parts[2].strip()[:MAX_COMMAND_CHARS],
            }
        )
    return rows


def _patterns(patterns) -> tuple[str, ...]:
    """The caller's patterns as a non-empty tuple of strings, or the default.
    Never raises: an empty or hostile pattern list falls back rather than
    turning a status check into an error."""
    try:
        values = tuple(p for p in (patterns or ()) if p)
    except Exception:
        return DEFAULT_PATTERNS
    return values or DEFAULT_PATTERNS


def state_is_zombie(stat: str) -> bool:
    """True when a ps STAT field says the process has already exited."""
    text = (stat or "").strip()
    return bool(text) and text[0] in ZOMBIE_LETTERS


def matching_processes(
    patterns: tuple[str, ...] = DEFAULT_PATTERNS,
    *,
    exclude_pids: tuple[int, ...] = (),
    limit: int = MAX_PATTERN_MATCHES,
    timeout: float = PS_TIMEOUT_S,
) -> list[dict]:
    """Processes whose command line contains one of ``patterns``.

    Our own pid is always excluded: this module runs inside the driver, whose
    argv can carry a URL that matches. Matching is substring, deliberately — it
    is the portable thing that works whether the command is
    ``/home/u/.local/bin/terminal-browser`` or ``node .../terminal-browser.js``.
    """
    wanted = _patterns(patterns)
    skip = set(_clean_pids(exclude_pids)) | {os.getpid()}
    hits = [
        row
        for row in process_table(timeout=timeout)
        if row["pid"] not in skip and any(pattern in row["command"] for pattern in wanted)
    ]
    return hits[: max(0, int(limit))]


# ------------------------------------------------------------------ liveness


def _clean_pid(pid) -> int | None:
    """A usable pid, or None. pid 0 and negatives address a process *group*,
    so signalling them proves nothing about one process."""
    try:
        value = int(pid)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _clean_pids(pids) -> list[int]:
    """Deduplicated, ordered, bounded pid list. Anything unusable is dropped
    rather than reported: a typo must not become a signal to the wrong group."""
    out: list[int] = []
    for pid in pids or ():
        value = _clean_pid(pid)
        if value is not None and value not in out:
            out.append(value)
        if len(out) >= MAX_TRACKED_PIDS:
            break
    return out


def _signal_zero(pid: int) -> str:
    """What signal 0 alone can tell us: ``alive``, ``dead`` or ``unknown``.

    ESRCH means the pid is gone. EPERM means it exists but belongs to someone
    else — present, yet not ours to classify, so it stays unknown here.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return DEAD
    except OSError as exc:
        return DEAD if getattr(exc, "errno", None) == errno.ESRCH else UNKNOWN
    return ALIVE


def _state_letter(pid: int, *, timeout: float = PS_TIMEOUT_S) -> str | None:
    """First character of the process state, or None when unreadable.

    ``ps -o state= -p <pid>`` is the one portable status read: Linux has
    ``/proc/<pid>/stat`` and macOS has nothing, so a second source would be a
    second platform's bug surface for no gain.
    """
    out = ps_output(["-p", str(pid), "-o", "state="], timeout=timeout)
    for line in (out or "").splitlines():
        text = line.strip()
        if text:
            return text[0].upper()
    return None


def proc_state(pid, *, timeout: float = PS_TIMEOUT_S) -> str:
    """``alive``, ``zombie``, ``dead`` or ``unknown`` for one pid. Never raises.

    Two questions, in this order, because neither alone is enough:

    1. ``kill(pid, 0)`` — is the pid still in the table? No means ``dead``, and
       no status lookup is worth running.
    2. the process state — is the table entry a running process or a corpse?
       ``Z`` means the process has already exited; a zombie is dead work, not
       leaked work, and saying ``alive`` here is the false positive this whole
       function exists to avoid.

    ``unknown`` covers a pid we may not signal and a machine with no readable
    state. It is not a softer ``alive``: the caller has to be able to say it
    could not check.
    """
    value = _clean_pid(pid)
    if value is None:
        return UNKNOWN
    probe = _signal_zero(value)
    if probe == DEAD:
        return DEAD
    state = _state_letter(value, timeout=timeout)
    if state is not None:
        if state in ZOMBIE_LETTERS:
            return ZOMBIE
        if state in RUNNING_LETTERS:
            # A readable state outranks EPERM: we may not signal the pid, but
            # the state we just read says it is running.
            return ALIVE
        return UNKNOWN
    return ALIVE if probe == ALIVE else UNKNOWN


def liveness_report(pids, *, timeout: float = PS_TIMEOUT_S) -> dict:
    """Classify a batch of pids. Never raises.

    The four buckets are always present and always lists, so a caller can read
    ``report["alive"]`` without a guard; ``checked`` says how many pids went in.
    """
    report: dict = {ALIVE: [], ZOMBIE: [], DEAD: [], UNKNOWN: [], "checked": 0}
    for pid in _clean_pids(pids):
        try:
            report[proc_state(pid, timeout=timeout)].append(pid)
        except Exception:  # a single unreadable pid must not lose the batch
            report[UNKNOWN].append(pid)
        report["checked"] += 1
    return report


# ------------------------------------------------------------------- spawning


@dataclass
class SpawnTracker:
    """Spawn counters for one run.

    ``note()`` is the seam: the call site that starts a process reports the pid
    when it has one. One run loop spawns, so this is deliberately not a
    lock-protected counter and does not pretend otherwise; a run that forks
    worker threads must give each its own tracker.
    """

    count: int = 0
    tracked: list[int] = field(default_factory=list)
    kinds: dict = field(default_factory=dict)

    def note(self, kind: str = "spawn", pid=None) -> int:
        """Count one spawn and return the new total. Never raises."""
        try:
            self.count += 1
            label = str(kind or "spawn")[:64]
            self.kinds[label] = self.kinds.get(label, 0) + 1
            value = _clean_pid(pid)
            if value is not None and value not in self.tracked:
                self.tracked.append(value)
            return self.count
        except Exception:
            return self.count

    @contextmanager
    def spawn(self, kind: str = "spawn", pid=None):
        """Count a spawn around a block, for call sites that cannot report a pid."""
        self.note(kind, pid)
        yield self

    def snapshot(self) -> dict:
        """The counters as a log-safe dict. Never raises."""
        try:
            return {
                "spawn_count": int(self.count),
                "tracked_pids": list(self.tracked),
                "spawn_kinds": dict(sorted(self.kinds.items())),
            }
        except Exception:
            return {"spawn_count": 0, "tracked_pids": [], "spawn_kinds": {}}

    def reset(self) -> None:
        """Start a new run's accounting. A process serves many runs (the MCP
        server), and a run's spawn count is about that run, not about uptime."""
        self.count = 0
        self.tracked = []
        self.kinds = {}


RUN = SpawnTracker()


def note_spawn(kind: str = "spawn", pid=None) -> int:
    """Count a spawn this run started. Returns the running total."""
    return RUN.note(kind, pid)


def runtime_spawn_count() -> int:
    """How many processes this run started. 0 means none — or that no call site
    reported one, which is why the tracker counts kinds as well as pids."""
    return RUN.count


def tracked_pids() -> list[int]:
    """Pids this run started and knows. A pid-less spawn is counted but not
    tracked, and is the reason the orphan check also takes a command pattern."""
    return list(RUN.tracked)


def spawn_summary() -> dict:
    """Counters as a log-safe dict."""
    return RUN.snapshot()


def reset_spawns() -> None:
    """Begin a new run's accounting."""
    RUN.reset()


# ------------------------------------------------------------------ orphans


def _sleep(seconds: float) -> None:
    """The settle wait before an orphan check. One named place so a test can
    replace it without touching the `time` module every other caller shares."""
    try:
        time.sleep(max(0.0, float(seconds)))
    except Exception:
        return


def orphan_report(
    pids=(),
    *,
    spawn_count: int | None = None,
    patterns: tuple[str, ...] = DEFAULT_PATTERNS,
    delay_s: float = ORPHAN_CHECK_DELAY_S,
    sleep=None,
    timeout: float = PS_TIMEOUT_S,
) -> dict:
    """Did this run leave a process of its own running? Never raises.

    ``delay_s`` is the delayed part: right after close, a process that is
    tearing down still answers signal 0, so an immediate check reports a leak
    that resolves itself. The sleep only happens when there is something to
    look at — a run that spawned nothing cannot have left an orphan, and making
    every attach pay a sleep to prove it would be theatre.

    ``spawn_count`` decides whether the command-pattern scan is worth running:
    a run that counted no spawn reports ``no_pids`` and scans nothing, because
    every terminal-browser on the box is somebody's pane and none of them is
    this run's verdict. ``sleep`` replaces the settle wait; None uses ``_sleep``.

    ``status`` is one of:

    - ``orphans`` — a tracked pid is alive. The only leak verdict.
    - ``unknown`` — nothing alive, but a pid could not be classified. Not clean.
    - ``clean`` — every tracked pid is dead or a zombie (both cost nothing:
      a corpse is reaped by init when this process exits).
    - ``untracked`` — a spawn was counted but no pid is known, so the pattern
      scan is all the evidence there is.
    - ``no_pids`` — nothing spawned, nothing tracked, nothing to check.
    """
    wanted = _patterns(patterns)
    report: dict = {
        "status": STATUS_NO_PIDS,
        "counted_spawns": 0,
        "tracked_pids": [],
        "delay_s": float(delay_s),
        "patterns": list(wanted),
        "liveness": liveness_report([]),
        "orphans": [],
        "pattern_matches": [],
        "detail": None,
    }
    try:
        tracked = _clean_pids(pids)
        counted = int(spawn_count) if spawn_count is not None else len(tracked)
        looked = bool(tracked) or counted > 0
        if looked and delay_s > 0:
            (sleep or _sleep)(delay_s)
        live = liveness_report(tracked, timeout=timeout) if tracked else liveness_report([])
        matches = (
            matching_processes(wanted, exclude_pids=tuple(tracked), timeout=timeout) if looked else []
        )
        orphans = list(live[ALIVE])
        if orphans:
            status = STATUS_ORPHANS
            detail = f"{len(orphans)} process(es) this run started are still running"
        elif live[UNKNOWN]:
            status = STATUS_UNKNOWN
            detail = f"{len(live[UNKNOWN])} pid(s) could not be classified; clean is unproven"
        elif tracked:
            status = STATUS_CLEAN
            detail = f"{len(tracked)} tracked pid(s) dead or reaped"
        elif looked:
            status = STATUS_UNTRACKED
            detail = "spawn counted without a pid; pattern matches are not a leak verdict"
        else:
            status = STATUS_NO_PIDS
            detail = "this run started no processes"
        report.update(
            {
                "status": status,
                "counted_spawns": counted,
                "tracked_pids": tracked,
                "liveness": live,
                "orphans": orphans,
                "pattern_matches": matches,
                "detail": detail,
            }
        )
    except Exception as exc:
        # The only thing worse than a leaked process is a run that dies asking.
        report["status"] = STATUS_UNKNOWN
        report["detail"] = f"orphan check failed: {_short(exc)}"
    return report


def process_evidence(*, goal=None, **kwargs) -> dict:
    """The one record a run writes at close: what it spawned, what survived.

    Never raises — a caller in a ``finally`` block depends on that, because an
    exception there would replace the run's exit code.
    """
    try:
        summary = spawn_summary()
        report = orphan_report(
            summary["tracked_pids"],
            spawn_count=summary["spawn_count"],
            **kwargs,
        )
        return {"event": "processes", "goal": goal, **summary, **report}
    except Exception as exc:
        return {"event": "processes", "goal": goal, "error": _short(exc)}


# ---------------------------------------------------------------- provenance


def repo_root() -> Path | None:
    """The checkout this run is executing, or None.

    Both implementation directories must exist: hashing a tree where
    ``scripts/`` is missing would produce a digest that means "half the code",
    which is worse than saying unknown.
    """
    try:
        root = Path(driver_home())
    except Exception:
        return None
    try:
        if all((root / name).is_dir() for name in HASHED_DIRS):
            return root
    except OSError:
        return None
    return None


def implementation_files(root) -> list[Path]:
    """Top-level ``*.py`` of the implementation directories, ordered by their
    relative path so the caller never depends on directory listing order."""
    try:
        base = Path(root)
        found = [child for name in HASHED_DIRS for child in sorted((base / name).glob("*.py")) if child.is_file()]
        return sorted(found, key=lambda path: path.relative_to(base).as_posix())
    except Exception:
        return []


def _file_digest(path: Path) -> str | None:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def impl_hash(root) -> str | None:
    """SHA-256 over the sorted ``"<relpath> <sha256>"`` lines of the
    implementation files, or None when the tree cannot be vouched for.

    Order-independent by construction (the lines are sorted), so the same tree
    hashes the same however the filesystem enumerates it, and one changed byte
    in one file changes the digest. Secret-free by construction too: a file is
    hashed and dropped — no content, no absolute path and no environment
    variable reaches the digest, so hashing a tree that contains a key leaks
    nothing.

    None is a real answer, not a fallback: no checkout, or a file we cannot
    read. A partial digest would attest to code we never hashed.
    """
    base = Path(root)
    lines: list[str] = []
    for path in implementation_files(base):
        digest = _file_digest(path)
        if digest is None:
            return None
        try:
            rel = path.relative_to(base).as_posix()
        except ValueError:
            return None
        lines.append(f"{rel} {digest}")
    if not lines:
        return None
    # implementation_files() already ordered them by relative path, so the digest
    # does not depend on how the filesystem enumerates the directory.
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def git_commit(root) -> str | None:
    """``git rev-parse HEAD`` at the checkout, or None.

    A tree that is not a git checkout, a git that is not installed, or a
    repository with no commits all answer the same way, and all mean "unknown",
    which the impl_hash covers independently.
    """
    try:
        out = run_cmd(["git", "-C", str(root), "rev-parse", "HEAD"], timeout=GIT_TIMEOUT_S)
    except Exception:
        return None
    commit = (out or "").strip().splitlines()
    return commit[0].strip() if commit and commit[0].strip() else None


def interpreter() -> str:
    """"CPython 3.12.4". Version and implementation only: an interpreter path
    carries a home directory, which is not a secret but is not evidence."""
    try:
        return f"{platform.python_implementation()} {platform.python_version()}".strip()
    except Exception:
        return "unknown"


def version_manifest(*, root=None) -> dict:
    """Which code produced a run. Exactly three keys, nothing else.

    - ``git_commit`` — ``git rev-parse HEAD``, or None when unknown.
    - ``impl_hash`` — SHA-256 over ``jev_driver/*.py`` + ``scripts/*.py``, or
      None when the tree cannot be hashed. Present precisely because the commit
      is not enough: a dirty checkout runs code that commit does not describe.
    - ``interpreter`` — implementation and version.

    Secret-free by construction: three short strings derived from public
    inputs, with no path, argv or environment value among them.
    """
    try:
        base = Path(root) if root is not None else repo_root()
        if base is None:
            return {"git_commit": None, "impl_hash": None, "interpreter": interpreter()}
        return {
            "git_commit": git_commit(base),
            "impl_hash": impl_hash(base),
            "interpreter": interpreter(),
        }
    except Exception:
        return {"git_commit": None, "impl_hash": None, "interpreter": "unknown"}


def manifest_path() -> Path | None:
    """Where the manifest belongs: the run log's own directory, i.e. this run's
    evidence. None when the log path is unusable."""
    try:
        return Path(runlog.JSONL_PATH).parent / MANIFEST_NAME
    except Exception:
        return None


def write_version_manifest(manifest: dict | None = None, *, path=None) -> dict:
    """Write the manifest beside the run log and return what was written.

    Never raises: a read-only home directory must not cost the run its exit
    code, so a failed write comes back under ``error`` and the caller decides
    what to do with it.

    One file, overwritten per run, says "this is the code on disk right now".
    The manifest inside each run's own log line is what makes that run's code
    attributable after the checkout has moved on.
    """
    try:
        body = dict(manifest) if isinstance(manifest, dict) else version_manifest()
    except Exception:
        body = {"git_commit": None, "impl_hash": None, "interpreter": "unknown"}
    target = Path(path) if path is not None else manifest_path()
    scratch = None
    try:
        if target is None:
            raise OSError("no run log directory to write a manifest into")
        target.parent.mkdir(parents=True, exist_ok=True)
        scratch = target.with_name(target.name + ".tmp")
        scratch.write_text(json.dumps(body, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        scratch.replace(target)  # a reader sees the old file or the new one, never half of one
        scratch = None
    except Exception as exc:
        body["error"] = _short(exc)
    finally:
        if scratch is not None:
            try:
                Path(scratch).unlink()
            except OSError:
                pass
    return body