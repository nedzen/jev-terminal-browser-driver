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
from scripts.live.chain import run_chain
from scripts.live.classify import classify
from scripts.live.driver import McpStdio
from scripts.live.metrics import CallMeter, build_run_record, waste_ticks
from scripts.live.redact import assert_no_amounts, redact_record
from scripts.live.regress import diff_run, is_regression
from scripts.live.spec import validate_manifest, validate_test
from scripts.live.taxonomy import (
    CAP_MANDATORY,
    CAP_OPTIONAL,
    CRASH,
    SEV_1,
    SEV_2,
    is_pass,
    is_terminal,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SLICE_DIR = Path("/tmp/wwwdrive-runs")
LEDGER = REPO_ROOT / "docs" / "live-learnings.md"
BASELINE_DIR = REPO_ROOT / "docs" / "live-baseline"

LEDGER_HEADER = """# Live-test learnings ledger

One row per run, appended by `scripts/live`. `outcome` is the v3.1 partition
(HIT / HIT-recovered / MISS / FALSE-DONE / BLOCKED-honest / BLOCKED-unjustified /
STALL / CRASH); `sev` is sev-1 (FALSE-DONE only, must stay 0) or sev-2. `bytes/call`
is the caller-ingested gate; driver Jev token totals are diagnosis only. Slices:
`/tmp/wwwdrive-runs/<run_id>.jsonl`. S6x rows carry presence-only values, never
amounts.

Columns: `url` is final_url on host+path only, `waste` is measured waste-ticks
(post-satisfaction + stale retries only -- the remainder has no oracle), `commit`
is abbreviated to 7. A `void` row is a CRASH retry attempt and does not count
toward the scoreboard. Free-text notes are in the run record and the JSONL slice,
not in this table: a wrapping paragraph in a ledger cell is unreadable.

| run_id | test | site | tier | cap | outcome | sev | cause | stop | url | ticks | waste | B/c | hash | commit | wall |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
"""

# Every drift-checked field the diff needs, in the order it is read.
BASELINE_FIELDS = (
    "run_id", "test_id", "outcome_class", "severity", "failure_cause", "stop_reason",
    "final_url", "final_url_host_path", "ticks", "waste_ticks", "bytes_per_call",
    "spec_hash", "commit",
)

# v3.1 F5: a CRASH voids its run and gets one retry; two consecutive crashes are a
# harness-error investigation and never a product MISS.
CRASH_RETRIES = 1

# v3.1 safety: a consequential-element hit aborts the suite and queues an owner
# flag rather than being scored, because the click may already have landed.
class SuiteAbort(RuntimeError):
    """A safety denylist was hit: the suite stops and the owner is flagged."""


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


def driver_state_dir() -> Path:
    """Where the driver keeps its lease state, read from the product itself.

    Imported lazily and by attribute rather than hardcoded here: the whole point
    of fix (1) is that this path is the product's to decide, and a copy of it in
    the harness is exactly the kind of second source that drifted before.
    """
    from jev_driver.browser import LAST_PAGE_PATH

    return Path(LAST_PAGE_PATH).parent


def spec_hash() -> str:
    """The decision prompt revision this run asked under."""
    from jev_driver.model import QUESTION_SPEC_HASH

    return QUESTION_SPEC_HASH


def _run_id(test_id: str) -> str:
    return f"{test_id}-{uuid.uuid4().hex[:10]}"


def _disposition_cell(row: dict) -> str:
    """The outcome cell, carrying any marker that changes how the row counts.

    A voided CRASH retry and a safety-abort are both ledger rows, and without a
    marker in the table a reader cannot tell them from scored runs -- which is
    exactly the confusion the scoreboard's void exclusion exists to prevent. The
    marker lives here rather than in a free-text notes column because a wrapping
    paragraph is unreadable in a scan table.
    """
    outcome = row.get("outcome_class") or ("PENDING" if row.get("needs_human_verdict") else "-")
    if row.get("void"):
        return f"{outcome} (void)"
    if row.get("owner_flag"):
        return f"{outcome} ({row['owner_flag']})"
    return str(outcome)


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
        row.get("matrix_cap"), _disposition_cell(row),
        row.get("severity") or "-", row.get("failure_cause") or "-",
        row.get("stop_reason") or "-",
        row.get("final_url_host_path") or "-", row.get("ticks"), row.get("waste_ticks"),
        row.get("bytes_per_call"),
        row.get("spec_hash"), (row.get("commit") or "")[:7], row.get("wall_s"),
    ]
    with ledger.open("a", encoding="utf-8") as handle:
        handle.write("| " + " | ".join("" if c is None else str(c) for c in cells) + " |\n")


def write_slice(run_id: str, events, *, slice_dir, redact: str | None = None) -> Path:
    """The run's log slice, one JSON object per line, redacted when the test asked.

    `slice_dir` is a required keyword with no default. It was `slice_dir=SLICE_DIR`
    before, and that default was the defect: every self-test run wrote a slice into
    the production `/tmp/wwwdrive-runs` because `run_test` never passed one. A
    default here is a silent write to a real directory, so there isn't one --
    forgetting to pass it is now a TypeError at the call site.
    """
    slice_dir = Path(slice_dir)
    slice_dir.mkdir(parents=True, exist_ok=True)
    path = slice_dir / f"{run_id}.jsonl"
    with path.open("w", encoding="utf-8") as handle:
        for event in events or []:
            record = redact_record(dict(event), redact=redact)
            if redact:
                assert_no_amounts(record)
            handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    return path


def load_baseline(suite: str, test_id: str, *, baseline_dir=None) -> dict | None:
    """The last green slice for this test, or None.

    Per test rather than per suite so a test added later starts with no baseline
    instead of being compared against an unrelated run.
    """
    path = Path(baseline_dir or BASELINE_DIR) / f"{suite}__{test_id}.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def save_baseline(record: dict, *, suite: str = "live", baseline_dir=None) -> Path:
    """Promote a run to be this test's baseline.

    Named by `load_baseline`'s convention, and that sharing is the point: the two
    used to disagree, so a saved baseline was silently never found and every run
    reported "first run" forever.
    """
    baseline_dir = Path(baseline_dir or BASELINE_DIR)
    baseline_dir.mkdir(parents=True, exist_ok=True)
    path = baseline_dir / f"{suite}__{record['test_id']}.json"
    path.write_text(json.dumps({k: record.get(k) for k in BASELINE_FIELDS}, indent=2), encoding="utf-8")
    return path


def _events_from_payload(payload: dict) -> list[dict]:
    """The drive result plus its actions as `act` events.

    The result carries the action labels the run touched; flattening them means
    the safety denylist and the waste-tick accounting read one stream instead of
    each reaching into the payload separately. The driver's own tick count is
    materialized as `tick` events (one per executed step) so tick-derived
    measures read the same stream; string actions become act events with the
    label as the only known field.
    """
    events = [{"event": "drive_result", "result": payload}]
    try:
        ticks = int(payload.get("ticks") or 0)
    except (TypeError, ValueError):
        ticks = 0
    events.extend({"event": "tick"} for _ in range(max(ticks, 0)))
    for action in payload.get("actions") or []:
        if isinstance(action, dict):
            events.append({"event": "act", "kind": action.get("kind"),
                           "label": action.get("label") or action.get("target")})
        elif isinstance(action, str):
            events.append({"event": "act", "kind": None, "label": action})
    return events


def run_test(client: McpStdio, test: dict, *, log_dir, slice_dir, suite: str = "live",
             baseline_dir=None, ledger: Path | None = None) -> dict:
    """One test, end to end, with v3.1's CRASH retry rule.

    A CRASH voids its attempt and gets exactly one retry; two consecutive crashes
    are escalated as a harness-error investigation and are never reported as a
    product MISS. Voided attempts still get a ledger row -- marked `void` -- because
    a crash that leaves no trace is indistinguishable from a test that never ran.

    Every other failure mode is a recorded outcome rather than an exception.
    """
    test = validate_test(test)
    for attempt in range(1, CRASH_RETRIES + 2):
        record = _attempt(client, test, log_dir=log_dir, slice_dir=slice_dir, suite=suite,
                          attempt=attempt, baseline_dir=baseline_dir, ledger=ledger)
        if record["outcome_class"] != CRASH:
            _append_ledger(record, ledger)
            return record
        if attempt <= CRASH_RETRIES:
            record["void"] = True
            record["notes"] = "CRASH attempt voided and retried once (v3.1 F5)"
            _append_ledger(record, ledger)
            continue
        record["escalation"] = "harness-error-investigation"
        record["notes"] = "two consecutive crashes: harness-error investigation, never a product MISS"
        _append_ledger(record, ledger)
        return record
    raise AssertionError("unreachable: the retry loop always returns")


def _attempt(client: McpStdio, test: dict, *, log_dir, slice_dir, suite: str, attempt: int,
             baseline_dir, ledger: Path | None = None) -> dict:
    """One drive attempt.

    Returns its record and does not append to the ledger on the normal paths:
    `run_test` owns those writes, because only it knows whether this attempt is the
    scored one or a voided retry, and two writers each appending is how a run lands
    in the ledger twice.

    The one exception is the safety abort below, which raises: there `run_test`
    never regains control, so the row is written here or the abort leaves no trace.
    """
    run_id = _run_id(test["id"])
    meter = CallMeter()
    events: list = []
    error = None
    final_url = None
    final_view = None
    started = time.perf_counter()

    # Isolation before anything else: a misdirected log_dir or a busy pane
    # invalidates the whole run. The state-dir assertion comes first because every
    # other isolation step is meaningless without it -- that ordering is what let a
    # misdirected directory report success all the way into a live row.
    try:
        state_dir = driver_state_dir()
        isolation.assert_log_dir_matches(log_dir, state_dir)
        isolation.assert_pane_idle(log_dir)
        # `driver_state_dir=` here is defence in depth, not the load-bearing check:
        # `assert_log_dir_matches` above has already guaranteed log_dir *is* the
        # driver's state directory, so quarantine's loud branch cannot fire from
        # here. It is passed anyway because this call is the one that would silently
        # no-op if that assertion were ever removed -- and no test covers that
        # wiring specifically (the two failures are indistinguishable by type). Do
        # not read this line as a substitute for the assertion.
        quarantine = isolation.quarantine_last_page(log_dir, driver_state_dir=state_dir)
        report = isolation.isolation_report(log_dir, driver_state_dir=state_dir)
    except isolation.IsolationError as exc:
        outcome = classify(
            expected=test["expected"], satisfiable=test["satisfiable"], stop_reason=None,
            final_url=None, final_view=None, error=f"isolation: {exc}",
        )
        record = build_run_record(
            run_id=run_id, test=test, meter=meter, decision_outcome=outcome, events=events,
            final_url=None, final_view=None, spec_hash=spec_hash(), commit=git_commit(),
            wall_s=time.perf_counter() - started, error=f"isolation: {exc}",
            notes="refused before the drive", attempt=attempt,
        )
        record["quarantined_target"] = None
        return record

    chain = None
    try:
        # One chain, every drive in it, sequentially. A single-drive test takes
        # this path too, so there is exactly one code path for the transport.
        chain = run_chain(client, test, log_dir=log_dir)
    except SuiteAbort:
        raise
    events = []
    if chain and chain["drives"]:
        for entry in chain["drives"]:
            events.append({"event": "drive_result", "drive": entry["drive"],
                           "result": {"status": entry["status"],
                                      "stopped_reason": entry["stopped_reason"],
                                      "final_url": entry["final_url"],
                                      "final_view": entry["final_view"],
                                      "page_text": entry.get("page_text"),
                                      "ticks": entry["ticks"]}})
            events.extend({"event": "tick"} for _ in range(int(entry.get("ticks") or 0)))
            events.extend({"event": "act", "kind": a.get("kind"), "label": a.get("label")}
                          for a in entry["actions"] if isinstance(a, dict))
            if entry["terminal"]:
                error = entry["error"]
        final_url = chain.get("final_url")
        final_view = chain.get("final_view")
        # The chain is the byte meter for a multi-drive test; rebuild this attempt's
        # meter from its per-drive deltas so the record's bytes/call stays the
        # caller-ingested measurement contract (a) describes.
        meter.calls = [{"tool": "drive", "bytes": d["bytes"]} for d in chain["drives"]]
        meter.total_bytes = chain["bytes_total"]

    wall = time.perf_counter() - started
    # The chain owns the verdict. Re-deriving it here from the last payload would be
    # a second opinion on the same question, and for a chain those two disagree the
    # moment drive 1 of 2 is where it stopped.
    outcome = dict(chain["outcome"]) if chain else classify(
        expected=test["expected"], satisfiable=test["satisfiable"], stop_reason=None,
        final_url=None, final_view=None, error=error,
        human_judged=test.get("human_judged", False),
        waste_ticks=waste_ticks(events)["waste_ticks"],
        failure_cause=test.get("failure_cause"), anomaly_tags=test.get("anomaly_tags"),
    )
    if chain:
        # A declared failure cause never overrides what the chain observed.
        outcome["failure_cause"] = outcome.get("failure_cause") or test.get("failure_cause")

    record = build_run_record(
        run_id=run_id, test=test, meter=meter, decision_outcome=outcome, events=events,
        final_url=final_url, final_view=final_view, spec_hash=spec_hash(), commit=git_commit(),
        wall_s=wall, error=error, attempt=attempt,
    )
    record["quarantined_target"] = (quarantine or {}).get("targetId")
    record["isolation"] = report
    if chain:
        record["chain"] = chain
        record["stopped_at"] = chain["stopped_at"]
        record["stop_detail"] = chain["stop_detail"]
        record["partial"] = chain["outcome"].get("partial", False)
        record["session_ids"] = chain["session_ids"]
        record["chain_budget_s"] = chain["chain_budget_s"]
        record["completed_drives"] = chain["completed_drives"]
        record["drive_count"] = chain["drive_count"]
        if chain["isolation_violation"]:
            record["notes"] = f"chain stopped: {chain['isolation_violation']}"

    # v3.1 safety: a consequential element aborts the suite rather than being
    # scored, because the click may already have landed on a live site.
    if record["consequential_hits"]:
        record["owner_flag"] = "consequential-element"
        record["notes"] = f"denylist hit: {[h['value'] for h in record['consequential_hits']]}"
        _append_ledger(record, ledger)
        raise SuiteAbort(f"consequential element on {test['id']}: {record['notes']}")

    slice_path = write_slice(run_id, events, slice_dir=slice_dir, redact=test.get("redact"))
    record["slice"] = str(slice_path)

    baseline = load_baseline(suite, test["id"], baseline_dir=baseline_dir)
    diff = diff_run(record, baseline)
    record["diff"] = diff
    record["regression"] = is_regression(diff)

    return record


def run_suite(manifest: dict, *, client=None, log_dir, slice_dir, suite: str = "live",
              baseline_dir=None, ledger: Path | None = None) -> dict:
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
        records = [run_test(client, test, log_dir=log_dir, slice_dir=slice_dir, suite=suite,
                            baseline_dir=baseline_dir, ledger=ledger)
                   for test in checked["tests"]]
    finally:
        if owns_client:
            client.close()
    return scoreboard(checked["suite"], records)


def scoreboard(suite: str, records: list[dict]) -> dict:
    """The per-suite numbers v3.1 asks for.

    Voided CRASH retries are excluded from every rate: they are attempts, not
    outcomes, and counting them would let a crashing harness depress its own hit
    rate. Pending human verdicts are reported separately rather than folded into any
    class, because an unclassified run is neither a pass nor a failure.

    Optional matrix cells are counted apart from mandatory ones because they never
    gate -- a suite of optional cells that all fail is not a red suite.
    """
    scored = [r for r in records if not r.get("void")]
    pending = [r for r in scored if r.get("outcome_class") is None]
    classified = [r for r in scored if r.get("outcome_class") is not None]
    mandatory = [r for r in classified if (r.get("matrix_cap") or CAP_MANDATORY) == CAP_MANDATORY]
    optional = [r for r in classified if r.get("matrix_cap") == CAP_OPTIONAL]

    def rate(rows, predicate):
        return round(sum(1 for r in rows if predicate(r)) / len(rows), 4) if rows else None

    sev1 = [r for r in classified if r.get("severity") == SEV_1]
    sev2 = [r for r in classified if r.get("severity") == SEV_2]
    return {
        "suite": suite,
        "runs": len(records),
        "voided": len(records) - len(scored),
        "pending_human": [r["run_id"] for r in pending],
        "classified": len(classified),
        "mandatory_cells": len(mandatory),
        "optional_cells": len(optional),
        "by_class": {cls: sum(1 for r in classified if r["outcome_class"] == cls)
                     for cls in sorted({r["outcome_class"] for r in classified})},
        "hits": sum(1 for r in classified if is_pass(r["outcome_class"])),
        "hit_rate": rate(classified, lambda r: is_pass(r["outcome_class"])),
        "sev1_count": len(sev1),
        "sev1_run_ids": [r["run_id"] for r in sev1],
        "sev2_count": len(sev2),
        "sev2_rate": rate(classified, lambda r: r.get("severity") == SEV_2),
        "terminal": [r["run_id"] for r in classified if is_terminal(r["outcome_class"])],
        "waste_ticks_total": sum(r.get("waste_ticks") or 0 for r in scored),
        "regressions": [r["run_id"] for r in records if r.get("regression")],
        "escalations": [r["run_id"] for r in records if r.get("escalation")],
        "site_fingerprints": sorted(
            {r["site_fingerprint"] for r in records if r.get("site_fingerprint")}),
        "records": records,
    }
