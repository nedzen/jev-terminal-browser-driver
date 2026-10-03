"""The sequential runner: one test at a time, one ledger row per run.

v3 fixes the order of operations per test, and this module follows it literally
because the order is load-bearing: isolation first (a busy pane invalidates
everything after it), then the drive, then classification against the declared end
state, then the regression diff, then the row. A row is written whatever happened
-- a run that raised still gets one, because a run that left no trace is how a
suite ends up looking green over a test that never executed.

Slices land in `/tmp/wwwdrive-runs/<run_id>.jsonl` (v3 retention: pruned at 30
days or on a release tag) and rows in `docs/live-learnings.md`, one per run.

Stdlib only.
"""

from __future__ import annotations

import json
import subprocess
import time
import uuid
from pathlib import Path

from scripts.live import isolation
from scripts.live.classify import classify
from scripts.live.driver import DriverError, McpStdio, Stalled
from scripts.live.metrics import CallMeter, build_run_record
from scripts.live.redact import assert_no_amounts, redact_record
from scripts.live.regress import diff_run, is_regression
from scripts.live.spec import validate_manifest, validate_test

REPO_ROOT = Path(__file__).resolve().parents[2]
SLICE_DIR = Path("/tmp/wwwdrive-runs")
LEDGER = REPO_ROOT / "docs" / "live-learnings.md"
BASELINE_DIR = REPO_ROOT / "docs" / "live-baseline"

LEDGER_HEADER = """# Live-test learnings ledger

One row per run, appended by `scripts/live`. `classification` is one of HIT, MISS,
BLOCKED_HONEST (honest block on an unsatisfiable goal) -- a blocked stop on a
satisfiable goal is recorded as MISS, per v3. `bytes/call` is the caller-ingested
gate; driver Jev token totals are diagnosis only. Slices:
`/tmp/wwwdrive-runs/<run_id>.jsonl`. S6x rows carry presence-only values, never
amounts.

Columns: `result` is the classification (HIT / MISS / BLOCKED_HONEST), `url` is
final_url on host+path only, `commit` is abbreviated to 7.

| run_id | test | site | tier | result | stop_reason | url | ticks | bytes/call | spec_hash | commit | wall_s | notes |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
"""

# Every drift-checked field the diff needs, in the order it is read.
BASELINE_FIELDS = (
    "run_id", "test_id", "classification", "stop_reason", "final_url",
    "final_url_host_path", "ticks", "bytes_per_call", "spec_hash", "commit",
)


def git_commit(root: Path = REPO_ROOT) -> str:
    """The commit this run executed, pinned per run rather than read from a tag later."""
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10, check=False,
        )
        return (out.stdout or "").strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def spec_hash() -> str:
    """The decision prompt revision this run asked under."""
    from jev_driver.model import QUESTION_SPEC_HASH

    return QUESTION_SPEC_HASH


def _run_id(test_id: str) -> str:
    return f"{test_id}-{uuid.uuid4().hex[:10]}"


def _append_ledger(row: dict, ledger: Path | None = None) -> None:
    """Append one row, creating the ledger with its header if it is new.

    `ledger` resolves inside the function on purpose. As a default argument it
    would be bound at import, which means a caller that redirects the module
    constant is ignored -- and a self-test then appends its rows to the real
    `docs/live-learnings.md` instead of a tmp file.
    """
    ledger = Path(ledger) if ledger is not None else LEDGER
    ledger.parent.mkdir(parents=True, exist_ok=True)
    if not ledger.exists() or ledger.read_text(encoding="utf-8").strip() == "":
        ledger.write_text(LEDGER_HEADER, encoding="utf-8")
    cells = [
        row.get("run_id"), row.get("test_id"), row.get("site"), row.get("tier"),
        row.get("classification"), row.get("stop_reason") or "-",
        row.get("final_url_host_path") or "-", row.get("ticks"), row.get("bytes_per_call"),
        row.get("spec_hash"), (row.get("commit") or "")[:7], row.get("wall_s"),
        (row.get("notes") or "-").replace("|", "/"),
    ]
    with ledger.open("a", encoding="utf-8") as handle:
        handle.write("| " + " | ".join("" if c is None else str(c) for c in cells) + " |\n")


def write_slice(run_id: str, events, *, slice_dir: Path = SLICE_DIR, redact: str | None = None) -> Path:
    """The run's log slice, one JSON object per line, redacted when the test asked."""
    slice_dir.mkdir(parents=True, exist_ok=True)
    path = slice_dir / f"{run_id}.jsonl"
    with path.open("w", encoding="utf-8") as handle:
        for event in events or []:
            record = redact_record(dict(event), redact=redact)
            if redact:
                assert_no_amounts(record)
            handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    return path


def load_baseline(suite: str, test_id: str, *, baseline_dir: Path = BASELINE_DIR) -> dict | None:
    """The last green slice for this test, or None.

    Per test rather than per suite so a test added later starts with no baseline
    instead of being compared against an unrelated run.
    """
    path = Path(baseline_dir) / f"{suite}__{test_id}.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def save_baseline(record: dict, *, suite: str = "live", baseline_dir: Path = BASELINE_DIR) -> Path:
    """Promote a run to be this test's baseline.

    Named by `load_baseline`'s convention, and that sharing is the point: the two
    used to disagree, so a saved baseline was silently never found and every run
    reported "first run" forever.
    """
    Path(baseline_dir).mkdir(parents=True, exist_ok=True)
    path = Path(baseline_dir) / f"{suite}__{record['test_id']}.json"
    path.write_text(json.dumps({k: record.get(k) for k in BASELINE_FIELDS}, indent=2), encoding="utf-8")
    return path


def run_test(client: McpStdio, test: dict, *, log_dir=None, suite: str = "live",
             baseline_dir: Path = BASELINE_DIR, ledger: Path | None = None) -> dict:
    """One test, end to end. Always returns a record, including on failure.

    Every failure mode here is a recorded outcome rather than an exception: a
    transport error, a stall and a timeout are all things v3 wants scored and
    ledgered, and a runner that raised would lose exactly the runs worth reading.
    """
    run_id = _run_id(test["id"])
    # Normalized here rather than trusting the caller: run_test is reachable from
    # a self-test and a script, and a dict missing timeout_s should fail the run
    # loudly rather than raise KeyError halfway through building its record.
    test = validate_test(test)
    meter = CallMeter()
    events: list = []
    error = None
    timed_out = False
    stalled = False
    final_url = None
    final_view = None
    started = time.perf_counter()

    # Isolation before anything else: a busy pane invalidates the whole run.
    try:
        isolation.assert_pane_idle(log_dir) if log_dir else isolation.assert_pane_idle()
        quarantine = isolation.quarantine_last_page(log_dir) if log_dir else isolation.quarantine_last_page()
    except isolation.IsolationError as exc:
        outcome = classify(
            expected=test["expected"], satisfiable=test["satisfiable"], stop_reason=None,
            final_url=None, final_view=None, error=f"isolation: {exc}",
        )
        record = build_run_record(
            run_id=run_id, test=test, meter=meter, decision_outcome=outcome, events=events,
            final_url=None, final_view=None, spec_hash=spec_hash(), commit=git_commit(),
            wall_s=time.perf_counter() - started, error=f"isolation: {exc}",
            notes="refused before the drive",
        )
        record["quarantined_target"] = None
        _append_ledger(record, ledger)
        return record

    try:
        response = client.call(
            "drive",
            {"goal": test["goal"], "url": test.get("url"), "max_steps": test.get("max_steps")},
            log_dir=log_dir, stall_s=test.get("stall_s"), timeout_s=test.get("timeout_s"),
        )
        payload = McpStdio.payload(response)
        meter.record("drive", payload)
        final_url = payload.get("final_url")
        final_view = payload.get("final_view")
        events = [{"event": "drive_result", "result": payload}]
    except Stalled as exc:
        stalled = True
        error = str(exc)
    except DriverError as exc:
        error = str(exc)

    wall = time.perf_counter() - started
    outcome = classify(
        expected=test["expected"], satisfiable=test["satisfiable"],
        stop_reason=(events[0]["result"].get("stopped_reason") if events and events[0].get("result") else None),
        final_url=final_url, final_view=final_view, stalled=stalled, timed_out=timed_out, error=error,
    )

    record = build_run_record(
        run_id=run_id, test=test, meter=meter, decision_outcome=outcome, events=events,
        final_url=final_url, final_view=final_view, spec_hash=spec_hash(), commit=git_commit(),
        wall_s=wall, error=error,
    )
    record["quarantined_target"] = (quarantine or {}).get("targetId")

    slice_path = write_slice(run_id, events, redact=test.get("redact"))
    record["slice"] = str(slice_path)

    baseline = load_baseline(suite, test["id"], baseline_dir=baseline_dir)
    diff = diff_run(record, baseline)
    record["diff"] = diff
    record["regression"] = is_regression(diff)

    _append_ledger(record, ledger)
    return record


def run_suite(manifest: dict, *, client=None, log_dir=None, suite: str = "live",
              baseline_dir: Path = BASELINE_DIR, ledger: Path | None = None) -> dict:
    """Every test in the manifest, sequentially, in manifest order.

    A manifest that fails validation stops the suite before any browser time,
    which is the difference between a typo costing a second and costing a pane.
    """
    checked = validate_manifest(manifest)
    owns_client = client is None
    client = client or McpStdio()
    if owns_client:
        client.start()
    try:
        records = [run_test(client, test, log_dir=log_dir, suite=suite,
                            baseline_dir=baseline_dir, ledger=ledger)
                   for test in checked["tests"]]
    finally:
        if owns_client:
            client.close()
    return scoreboard(checked["suite"], records)


def scoreboard(suite: str, records: list[dict]) -> dict:
    """The per-suite numbers v3 asks for.

    Satisfiable goals only for the hit rate, because an honest block on an
    unsatisfiable goal is not a hit and counting it as a miss would make the rate
    meaningless in both directions.
    """
    satisfiable = [r for r in records if r.get("satisfiable")]
    unsatisfiable = [r for r in records if not r.get("satisfiable")]
    hits = [r for r in satisfiable if r["classification"] == "HIT"]
    honest = [r for r in unsatisfiable if r["classification"] == "BLOCKED_HONEST"]
    false_done = [r for r in records if r.get("false_done")]
    unjustified = [r for r in records if r.get("unjustified_block")]
    return {
        "suite": suite,
        "runs": len(records),
        "satisfiable": len(satisfiable),
        "unsatisfiable": len(unsatisfiable),
        "hits": len(hits),
        "honest_blocked": len(honest),
        "hit_rate": round(len(hits) / len(satisfiable), 4) if satisfiable else None,
        "false_done": len(false_done),
        "unjustified_blocks": len(unjustified),
        "regressions": [r["run_id"] for r in records if r.get("regression")],
        "records": records,
    }