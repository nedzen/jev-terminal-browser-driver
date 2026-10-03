"""Spawn accounting, orphan checks, code provenance, and root-terminal placement.

Stdlib only; evidence paths never raise. Pattern matches are evidence, not a leak verdict.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import platform
import re
import subprocess
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from . import runlog
from .preflight import driver_home

# Bounded local helpers; failures are reported, never raised.
PS_TIMEOUT_S = 3.0
GIT_TIMEOUT_S = 3.0
# Settle after close so a tearing-down process is not counted as a leak.
ORPHAN_CHECK_DELAY_S = 0.5

MANIFEST_NAME = "version_manifest.json"
HASHED_DIRS = ("jev_driver", "scripts")  # hashed, never read out

DEFAULT_PATTERNS = ("terminal-browser",)
MAX_PATTERN_MATCHES = 20
MAX_TRACKED_PIDS = 64
MAX_COMMAND_CHARS = 200

ALIVE = "alive"
DEAD = "dead"
UNKNOWN = "unknown"

# Rewrite `--token VALUE` so runlog's assignment redactor can see it.
_SECRET_FLAG_RE = re.compile(r"(?i)(--[a-z0-9][a-z0-9_-]*)([ \t]+|=)(\S+)")
_SECRET_FLAG_NAME_RE = re.compile(
    r"(?i)^--[a-z0-9]*[-_]?(?:api[-_]?key|keys?|tokens?|secrets?|passwords?|passwd|pwd|cookies?|credentials?)$"
)

STATUS_ORPHANS = "orphans"  # only this status is a leak verdict
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
    """stdout of one short-lived helper, or None. Never raises."""
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
    """``ps`` output, or None. ``-A -o`` is the portable BSD/Linux spelling."""
    return run_cmd(["ps", *args], timeout=timeout)


def _redact_flag(match) -> str:
    """Redact one ``--flag value`` pair when the flag name looks secret."""
    try:
        if _SECRET_FLAG_NAME_RE.match(match.group(1)):
            return f"{match.group(1)}{match.group(2)}{runlog.REDACTED}"
        return match.group(0)
    except Exception:
        return match.group(0)


def _scrub_command(value) -> str:
    """Credential-redact and length-cap a ps command string.

        Runlog first, then ``--flag value`` rewrite — reverse order can leave a
        half-consumed ``[redacted]`` marker.
    """
    text = str(value or "")
    try:
        return _SECRET_FLAG_RE.sub(_redact_flag, runlog._scrub_text(text))[:MAX_COMMAND_CHARS]
    except Exception:
        return text[:MAX_COMMAND_CHARS]


def process_table(*, timeout: float = PS_TIMEOUT_S) -> list[dict]:
    """Every visible process as ``{"pid", "stat", "command"}``. Never raises. Commands redacted here."""
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
                "command": _scrub_command(parts[2].strip()),
            }
        )
    return rows


def _patterns(patterns) -> tuple[str, ...]:
    """Non-empty pattern tuple, or the default. Never raises."""
    try:
        values = tuple(p for p in (patterns or ()) if p)
    except Exception:
        return DEFAULT_PATTERNS
    return values or DEFAULT_PATTERNS


def matching_processes(
    patterns: tuple[str, ...] = DEFAULT_PATTERNS,
    *,
    exclude_pids: tuple[int, ...] = (),
    limit: int = MAX_PATTERN_MATCHES,
    timeout: float = PS_TIMEOUT_S,
) -> list[dict]:
    """Processes whose (already-redacted) command contains one of ``patterns``."""
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
    """A usable pid, or None (0/negatives are process groups)."""
    try:
        value = int(pid)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _clean_pids(pids) -> list[int]:
    """Deduplicated, ordered, bounded pid list; drop unusable values."""
    out: list[int] = []
    for pid in pids or ():
        value = _clean_pid(pid)
        if value is not None and value not in out:
            out.append(value)
        if len(out) >= MAX_TRACKED_PIDS:
            break
    return out


def _signal_zero(pid: int) -> str:
    """``alive``, ``dead``, or ``unknown`` from signal 0 (EPERM -> unknown)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return DEAD
    except OSError as exc:
        return DEAD if getattr(exc, "errno", None) == errno.ESRCH else UNKNOWN
    return ALIVE


def proc_state(pid) -> str:
    """``alive``, ``dead``, or ``unknown`` via signal 0. Never raises."""
    value = _clean_pid(pid)
    if value is None:
        return UNKNOWN
    return _signal_zero(value)


def liveness_report(pids) -> dict:
    """Classify pids into always-present buckets. Never raises."""
    report: dict = {ALIVE: [], DEAD: [], UNKNOWN: [], "checked": 0}
    for pid in _clean_pids(pids):
        try:
            report[proc_state(pid)].append(pid)
        except Exception:  # a single unreadable pid must not lose the batch
            report[UNKNOWN].append(pid)
        report["checked"] += 1
    return report

# ------------------------------------------------------------------- spawning


@dataclass
class SpawnTracker:
    """Per-run spawn counters; not lock-protected (one loop owns each tracker)."""

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
    """Noted spawns this run (helpers/ps/git are not counted)."""
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
    """Whether this run left a process running. Never raises.

        Sleeps only when there is something to check. Only ``orphans`` is a leak;
        ``unknown`` is never clean. Pattern matches are evidence, not a verdict.
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
        live = liveness_report(tracked) if tracked else liveness_report([])
        matches = matching_processes(wanted, exclude_pids=tuple(tracked), timeout=timeout) if looked else []
        orphans = list(live[ALIVE])
        if orphans:
            status = STATUS_ORPHANS
            detail = f"{len(orphans)} process(es) this run started are still running"
        elif live[UNKNOWN]:
            status = STATUS_UNKNOWN
            detail = f"{len(live[UNKNOWN])} pid(s) could not be classified; clean is unproven"
        elif tracked:
            status = STATUS_CLEAN
            detail = f"{len(tracked)} tracked pid(s) dead"
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
    """Close-time spawn/orphan record. Never raises (safe for ``finally``)."""
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
    """Checkout root when both hashed dirs exist; else None."""
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
    """Top-level ``*.py`` under hashed dirs, ordered by relative path."""
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
    """SHA-256 of sorted ``"<relpath> <sha256>"`` lines, or None if incomplete.

        Files are hashed and dropped — no content/path/env reaches the digest.
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
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def git_commit(root) -> str | None:
    """``git rev-parse HEAD``, or None when unknown."""
    try:
        out = run_cmd(["git", "-C", str(root), "rev-parse", "HEAD"], timeout=GIT_TIMEOUT_S)
    except Exception:
        return None
    commit = (out or "").strip().splitlines()
    return commit[0].strip() if commit and commit[0].strip() else None


def interpreter() -> str:
    """Implementation and version only (no interpreter path)."""
    try:
        return f"{platform.python_implementation()} {platform.python_version()}".strip()
    except Exception:
        return "unknown"


def version_manifest(*, root=None) -> dict:
    """``git_commit``, ``impl_hash``, ``interpreter`` — secret-free provenance."""
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
    """Manifest path beside the run log, or None."""
    try:
        return Path(runlog.JSONL_PATH).parent / MANIFEST_NAME
    except Exception:
        return None


def _scratch_name(target: Path) -> Path:
    """Per-writer scratch path (pid+uuid) so concurrent renames cannot clash."""
    try:
        return target.with_name(f"{target.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    except Exception:
        return target.with_name(f"{target.name}.tmp")


def write_version_manifest(manifest: dict | None = None, *, path=None) -> dict:
    """Write the manifest beside the run log. Never raises; errors go in ``error``."""
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
        scratch = _scratch_name(target)
        scratch.write_text(json.dumps(body, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        scratch.replace(target)
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


# --------------------------------------------------------- root-terminal placement

# Herdr traces that do not use the HERDR_ prefix.
NON_PREFIXED_HERDR_VARS = ("SSH_AUTH_SOCK", "TERM_PROGRAM", "TERM_PROGRAM_VERSION")

_HERDR_SOCKET_MARKERS = ("herdr",)


def is_nested_in_herdr(env=None) -> bool:
    """True when this process is running inside a herdr pane."""
    env = os.environ if env is None else env
    return bool(env.get("HERDR_PANE_ID"))


def scrubbed_env(env=None) -> dict:
    """Child env with herdr traces removed (prefix and non-prefixed vars).

        Drop `SSH_AUTH_SOCK` rather than blank it. Leave `PWD`/`OLDPWD` alone.
    """
    env = os.environ if env is None else env
    kept = {}
    for key, value in env.items():
        if key.startswith("HERDR_"):
            continue
        if key in NON_PREFIXED_HERDR_VARS and _mentions_herdr(value):
            continue
        kept[key] = value
    return kept


def _mentions_herdr(value) -> bool:
    text = str(value or "").lower()
    return any(marker in text for marker in _HERDR_SOCKET_MARKERS)


def root_terminal_blocker(env=None) -> str | None:
    """Human-readable reason provisioning would nest inside herdr, or None.

        terminal-browser can only split the current surface, not open a root tab.
    """
    env = os.environ if env is None else env
    if not is_nested_in_herdr(env):
        return None
    outer = env.get("CMUX_SURFACE_ID") and "cmux" or env.get("TERM_PROGRAM") or "the outer terminal"
    return (
        f"refusing to provision inside a herdr pane: terminal-browser splits the current "
        f"surface, and this surface belongs to herdr, so the browser would nest inside the "
        f"agent's own pane. This is only reached when cmux cannot be addressed directly (no "
        f"CMUX_SOCKET_PATH + CMUX_WORKSPACE_ID); in a cmux workspace the cmux route runs "
        f"instead and creates the split at cmux level. Otherwise, open a root-level tab in "
        f"{outer} and run the drive there (cmux new-workspace --command ...). Scrubbing "
        f"HERDR_* is not the fix: it changes which adapter terminal-browser picks but not "
        f"which surface it splits."
    )
