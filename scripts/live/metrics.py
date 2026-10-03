"""Caller-side byte accounting, and the run record the ledger row is built from.

v3's measurement contract (a) is explicit that the gate is *caller-ingested*
payload bytes: the JSON-serialized byte length of each MCP result object, summed
per run. Driver-side Jev token totals are recorded but never gated, because they
carry roughly 32% noise and a gate on them would flap.

Ticks are counted the way v3 defines them -- one emitted tick event that carried
an action attempt -- so a stale retry is visible in the log without inflating the
count. That distinction is the whole reason this is not `len(history)`.

Stdlib only.
"""

from __future__ import annotations

import json

from scripts.live.classify import host_and_path


def result_bytes(obj) -> int:
    """Serialized byte length of one MCP result object.

    Measured on the same JSON the caller received, with the separators the wire
    uses, so the number is reproducible from a slice without the server.
    """
    return len(json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def count_ticks(events) -> int:
    """Emitted tick events, per v3: one per tick that carried an action attempt.

    `act` is deliberately *not* counted alongside `tick`. The driver emits one of
    each per executed step, so counting both reports two ticks for one step. And
    `stale` is a decision that could not be executed -- v3's "stale retries logged,
    don't increment" -- so it is not a tick at all and is never in the count.

    Counting only `tick` is what makes both of those true by construction rather
    than by a filter that has to be kept in step with the event vocabulary.
    """
    return sum(1 for event in events or [] if event.get("event") == "tick")


def scroll_count(events) -> int:
    """Scroll actions actually dispatched, from the log rather than from history."""
    return sum(1 for event in events or [] if event.get("event") == "act" and event.get("kind") == "scroll")


class CallMeter:
    """Accumulates caller bytes per call, for one run.

    Held as an object rather than a dict so a run cannot forget to initialise the
    counter and silently report bytes/call as a division by the run's call count
    when it made no calls at all.
    """

    def __init__(self):
        self.calls: list[dict] = []
        self.total_bytes = 0

    def record(self, tool: str, result) -> int:
        size = result_bytes(result)
        self.calls.append({"tool": tool, "bytes": size})
        self.total_bytes += size
        return size

    @property
    def bytes_per_call(self) -> float:
        return round(self.total_bytes / len(self.calls), 2) if self.calls else 0.0

    def as_record(self) -> dict:
        return {
            "calls": len(self.calls),
            "total_bytes": self.total_bytes,
            "bytes_per_call": self.bytes_per_call,
            "per_call": list(self.calls),
        }


def build_run_record(*, run_id: str, test: dict, meter: CallMeter, decision_outcome: dict, events,
                     final_url: str | None, final_view: str | None, spec_hash: str, commit: str,
                     wall_s: float, error: str | None = None, notes: str = "") -> dict:
    """Everything v3 says a run must record, in one dict.

    The pinned `spec_hash` and `commit` are per run rather than per suite: the V2
    lesson was that a comparison across a prompt revision looks like a product
    change when it is only a different question being asked.
    """
    ticks = count_ticks(events)
    return {
        "run_id": run_id,
        "test_id": test["id"],
        "site": test["site"],
        "goal": test["goal"],
        "tier": test["tier"],
        "satisfiable": test["satisfiable"],
        "timeout_s": test["timeout_s"],
        "classification": decision_outcome["classification"],
        "why": decision_outcome.get("why"),
        "end_state_matched": decision_outcome.get("end_state_matched"),
        "false_done": decision_outcome.get("false_done", False),
        "unjustified_block": decision_outcome.get("unjustified_block", False),
        "stop_reason": decision_outcome.get("stop_reason"),
        "final_url": final_url,
        "final_url_host_path": host_and_path(final_url),
        "ticks": ticks,
        "scrolls": scroll_count(events),
        "bytes_total": meter.total_bytes,
        "bytes_per_call": meter.bytes_per_call,
        "calls": len(meter.calls),
        "spec_hash": spec_hash,
        "commit": commit,
        "wall_s": round(wall_s, 2),
        "error": error,
        "notes": notes,
        "redact": test.get("redact"),
    }