"""Sequential drive chains: drive 1 -> drive 2 in one test, one lease.

M9 continuity chains (and S4a/S4b generally) need two `drive` calls inside a
single test: the first opens a page, the second continues *without* a url so the
driver has to re-attach. The runner previously issued exactly one drive per test,
so such a manifest was declarable but unrunnable -- the gap the roadmap calls F5.

Three rules make a chain a chain rather than two unrelated tests:

- **Back-to-back, no interleave.** Every drive runs inside one attempt and one
  lease. The pane-idle check runs *before each* drive, so a foreign session that
  appears between them stops the chain instead of quietly sharing the tab.
- **No quarantine between calls.** `last-page.json` is quarantined once, before
  the chain, and never between drives: renaming it mid-chain is exactly what
  destroys the re-attach the second drive depends on. The check between drives is
  a read-only idle assertion for that reason.
- **A chain budget on top of the per-call timeouts.** Three calls that each fit
  inside their own timeout can still overrun the suite, and only a chain-level
  budget notices.

Session linkage is recorded from what the result actually carries. The drive
result exposes `browser.continuity` and `auto_launched` but *not* the CDP
`session_id`, so the tab `target_id` is recorded as the linkage proxy -- same
targetId across drives is the evidence of a re-attach, and its absence is
evidence against one. That gap is stated in the record rather than papered over.

Stdlib only, no browser: the transport is the same `McpStdio` the single-drive path
uses, and the fake server can script every branch below.
"""

from __future__ import annotations

import time
from pathlib import Path

from scripts.live import isolation
from scripts.live.classify import classify, host_and_path
from scripts.live.driver import CallTimeout, DriverError, McpStdio, Stalled
from scripts.live.metrics import CallMeter, consequential_hits
from scripts.live.spec import DEFAULT_TIMEOUT_S, STALL_S
from scripts.live.taxonomy import BLOCKED_HONEST, CRASH, MISS, STALL


def _linkage(payload: dict) -> dict:
    """What the result says about which tab answered, and whether it re-attached."""
    browser = (payload or {}).get("browser") or {}
    return {
        "continuity": browser.get("continuity"),
        "auto_launched": browser.get("auto_launched"),
        "cdp_url": browser.get("cdp_url"),
        "source": browser.get("source"),
    }


def _target_id(log_dir) -> str | None:
    """The tab the driver last observed, read from the log it maintains.

    This is the linkage proxy the module docstring describes: the result does not
    carry a CDP session id, so the tab identity is what proves drive 2 continued
    in drive 1's tab rather than opening a second one.
    """
    page = isolation.read_last_page(log_dir)
    return (page or {}).get("targetId")


def _call_one(client: McpStdio, step: dict, *, log_dir, timeout_s, stall_s) -> dict:
    """One drive call. Returns its payload plus how it ended; never raises."""
    outcome: dict = {"error": None, "stalled": False, "timed_out": False, "crashed": False,
                     "payload": None}
    try:
        response = client.call(
            "drive",
            {"goal": step["goal"], "url": step.get("url"), "max_steps": step.get("max_steps")},
            log_dir=log_dir, stall_s=stall_s, timeout_s=timeout_s,
        )
        outcome["payload"] = McpStdio.payload(response)
    except Stalled as exc:
        outcome["stalled"] = True
        outcome["error"] = str(exc)
    except CallTimeout as exc:
        outcome["timed_out"] = True
        outcome["error"] = str(exc)
    except DriverError as exc:
        outcome["crashed"] = True
        outcome["error"] = str(exc)
    return outcome


def run_chain(client: McpStdio, test: dict, *, log_dir, chain_budget_s=None) -> dict:
    """Run every drive in the chain, sequentially, and return the chain record.

    The record carries a per-drive row for each call, the chain verdict, and
    `stopped_at` -- the 1-based index of the drive where the chain stopped, which
    is M9's "report where it stopped". `stopped_at` is None only when every drive
    ran to completion.
    """
    steps = test.get("drives") or []
    chain_budget = chain_budget_s if chain_budget_s is not None else test.get("chain_budget_s")
    default_timeout = test.get("timeout_s") or DEFAULT_TIMEOUT_S
    default_stall = test.get("stall_s") or STALL_S

    drives: list[dict] = []
    meter = CallMeter()
    started = time.perf_counter()
    stopped_at = None
    stop_reason_detail = None
    isolation_violation = None
    budget_exhausted = False

    for index, step in enumerate(steps, start=1):
        # Read-only idle check before every drive: a foreign session that appears
        # between calls must stop the chain, not share the tab. Deliberately not a
        # quarantine -- see the module docstring.
        try:
            isolation.assert_pane_idle(log_dir, quiet_s=0)
        except isolation.IsolationError as exc:
            isolation_violation = str(exc)
            stopped_at = index
            stop_reason_detail = "isolation-violation"
            break

        if chain_budget is not None and (time.perf_counter() - started) >= chain_budget:
            budget_exhausted = True
            stopped_at = index
            stop_reason_detail = "chain-budget-exhausted"
            break

        call_started = time.perf_counter()
        result = _call_one(
            client, step,
            log_dir=log_dir,
            timeout_s=step.get("timeout_s", default_timeout),
            stall_s=step.get("stall_s", default_stall),
        )
        payload = result["payload"] or {}
        before_bytes = meter.total_bytes
        if payload:
            meter.record("drive", payload)

        elapsed = round(time.perf_counter() - call_started, 3)
        terminal = (
            ("stalled" if result["stalled"] else
             "timed_out" if result["timed_out"] else
             "crashed" if result["crashed"] else None)
        )
        drives.append({
            "drive": index,
            "goal": step["goal"],
            "url": step.get("url"),
            "note": step.get("note"),
            "timeout_s": step.get("timeout_s", default_timeout),
            "stall_s": step.get("stall_s", default_stall),
            "status": payload.get("status"),
            "stopped_reason": payload.get("stopped_reason"),
            "final_url": payload.get("final_url"),
            "final_url_host_path": host_and_path(payload.get("final_url")),
            "final_view": payload.get("final_view"),
            "ticks": payload.get("ticks"),
            "waste_ticks": 0,
            # This drive's own bytes, not the running total: the chain is the
            # byte meter, so per-call figures have to be per-drive deltas or
            # bytes/call collapses to the last call's size.
            "bytes": meter.total_bytes - before_bytes,
            "bytes_cumulative": meter.total_bytes,
            "elapsed_s": elapsed,
            "terminal": terminal,
            "error": result["error"],
            "target_id": _target_id(log_dir),
            "linkage": _linkage(payload),
            "actions": payload.get("actions") or [],
        })

        if terminal:
            stopped_at = index
            stop_reason_detail = terminal
            break

    completed = len(drives) if stopped_at is None else stopped_at - 1
    chain_wall = round(time.perf_counter() - started, 3)
    last = drives[-1] if drives else {}

    record = {
        "drives": drives,
        "drive_count": len(steps),
        "completed_drives": completed,
        "stopped_at": stopped_at,
        "stop_detail": stop_reason_detail,
        "budget_exhausted": budget_exhausted,
        "chain_budget_s": chain_budget,
        "chain_wall_s": chain_wall,
        "isolation_violation": isolation_violation,
        "session_ids": [d["target_id"] for d in drives],
        "bytes_total": meter.total_bytes,
        "bytes_per_call": meter.bytes_per_call,
        "final_url": last.get("final_url"),
        "final_url_host_path": last.get("final_url_host_path"),
        "final_view": last.get("final_view"),
        "stopped_reason": last.get("stopped_reason"),
        "ticks": sum(int(d.get("ticks") or 0) for d in drives),
        "consequential_hits": [
            hit
            for d in drives
            for hit in consequential_hits(
                [{"event": "act", "kind": a.get("kind"), "label": a.get("label")}
                 for a in d["actions"] if isinstance(a, dict)],
                deny_actions=test.get("deny_actions") or (),
                deny_elements=test.get("deny_elements") or (),
            )
        ],
        # No progress oracle exists, so waste-ticks for a chain is only the stale
        # retries each drive reported; the unmeasurable remainder stays unmeasured.
        "waste_ticks": sum(int(d.get("waste_ticks") or 0) for d in drives),
    }
    record["outcome"] = _chain_verdict(record, test)
    return record


def _chain_verdict(record: dict, test: dict) -> dict:
    """The chain's single outcome, with M9's PARTIAL semantics.

    M9 maps COMPLETED->HIT, PARTIAL->MISS with `stopped_at` + cause, BLOCKED per
    the partition and FALSE-DONE->sev-1. Two cases are resolved explicitly:

    - A chain that stopped *after* completing at least one drive is PARTIAL: real
      progress was made and then lost, and calling that a terminal class would
      discard the fact that drive 1 worked.
    - A chain that stopped at drive 1 with nothing completed keeps the terminal
      class, because a chain that never started is not partial. Reporting it as
      PARTIAL/MISS would blame the product for a stall that is a harness-visible
      timeout.
    """
    stopped_at = record["stopped_at"]
    detail = record["stop_detail"]
    completed = record["completed_drives"]

    if stopped_at is not None and completed == 0 and detail in {"stalled", "timed_out", "crashed"}:
        outcome = classify(
            expected=test["expected"], satisfiable=test["satisfiable"], stop_reason=None,
            final_url=None, final_view=None,
            stalled=detail == "stalled", timed_out=detail == "timed_out",
            crashed=detail == "crashed",
            human_judged=test.get("human_judged", False),
        )
        outcome["partial"] = False
        return outcome

    if stopped_at is not None:
        cause = {
            "stalled": "stall",
            "timed_out": "timeout",
            "crashed": "crash",
            "isolation-violation": "harness-error",
            "chain-budget-exhausted": "timeout",
        }.get(detail, "wrong-end-state")
        return {
            "outcome_class": MISS,
            "severity": "sev-2",
            "partial": True,
            "stopped_at": stopped_at,
            "failure_cause": cause,
            "needs_human_verdict": False,
            "false_done": False,
            "stop_reason": record.get("stopped_reason"),
            "end_state_matched": False,
            "why": (f"PARTIAL: stopped at drive {stopped_at}/{record['drive_count']} "
                    f"({detail}) after completing {completed}"),
        }

    # Every drive ran. Score the chain against the test's declared end state.
    outcome = classify(
        expected=test["expected"], satisfiable=test["satisfiable"],
        stop_reason=record.get("stopped_reason"),
        final_url=record.get("final_url"), final_view=record.get("final_view"),
        human_judged=test.get("human_judged", False),
        waste_ticks=record.get("waste_ticks") or 0,
    )
    outcome["partial"] = False
    return outcome


def blocked_on_last_drive(record: dict) -> bool:
    """Whether the chain ended on a block, which the verdict already accounts for."""
    return bool(record.get("drives")) and record["drives"][-1].get("stopped_reason") in {
        reason for reason in ("model_blocked", "weak_done", "shell", "max_steps", "time_budget")
    }


__all__ = [
    "BLOCKED_HONEST",
    "CRASH",
    "STALL",
    "run_chain",
    "blocked_on_last_drive",
    "Path",
]