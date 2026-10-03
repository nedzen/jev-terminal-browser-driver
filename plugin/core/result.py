"""Tick rows and folded agent results from one field table. Stdlib only."""

from __future__ import annotations

import json
from dataclasses import dataclass

PAGE_TEXT_LIMIT = 2000


@dataclass(frozen=True)
class Field:
    """Declared field: optional truthy omit, optional length cap."""

    name: str
    truthy: bool = False
    limit: int | None = None

    def carries(self, value) -> bool:
        if value is None:
            return False
        return not self.truthy or bool(value)

    def cap(self, value):
        if self.limit is None or not isinstance(value, str):
            return value
        return value[: self.limit]


# What a tick row carries into the result, in order. Read by both builders.
PASSTHROUGH = (
    Field("page_text", limit=PAGE_TEXT_LIMIT),
    Field("why", truthy=True),
    Field("reason", truthy=True),
    Field("final_view"),
    Field("omitted_actions", truthy=True),
)

# What build_tick_row may add to a row. `error`, `degenerate` and `insight` stop
# here: the result folds them (the run's error, any tick's degeneracy, the whole
# insight trace) instead of copying the last row's copy.
TICK_OPTIONAL = PASSTHROUGH + (
    Field("error", truthy=True),
    Field("degenerate", truthy=True),
    Field("insight", truthy=True),
)


def build_tick_row(base: dict, **fields) -> dict:
    """One tick row: base keys always; optional fields only if declared."""
    row = dict(base)
    for field in TICK_OPTIONAL:
        value = fields.get(field.name)
        if field.carries(value):
            row[field.name] = field.cap(value)
    return row


def parse_json_lines(text: str) -> list[dict]:
    rows = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def sum_usage(rows: list[dict]) -> dict:
    total = {"input_tokens": 0, "output_tokens": 0, "cost": 0.0}
    for row in rows:
        usage = row.get("usage") or {}
        if not isinstance(usage, dict):
            continue
        total["input_tokens"] += int(usage.get("input_tokens") or 0)
        total["output_tokens"] += int(usage.get("output_tokens") or 0)
        total["cost"] += float(usage.get("cost") or 0)
    return total


def stopped_reason(status: str, reason, error) -> str:
    """Uniform stop taxonomy (model_done / budgets / no_page / model_blocked / error)."""
    if error == "timeout":
        return "time_budget"
    if error == "cancelled":
        return "cancelled"
    if status == "done":
        return "model_done"
    if reason == "max_steps":
        return "action_budget"
    if reason == "no_page":
        return "no_page"
    if status == "error":
        return "error"
    return "model_blocked"


def compact_result(rows: list[dict], exit_code: int, error: str | None = None, *, insights: bool = True) -> dict:
    """Fold tick rows into one agent result (insights off unless debug)."""
    meta = next((r for r in rows if r.get("event") == "browser"), {})
    ticks = [r for r in rows if r.get("status")]
    last = ticks[-1] if ticks else {}
    status = last.get("status") or ("error" if error else "blocked")
    error = error or last.get("error")
    if status not in {"done", "blocked", "error"}:
        status = "blocked"
    actions = [t.get("last_action") for t in ticks if t.get("last_action")]
    browser = {
        "source": meta.get("source"),
        "cdp_url": meta.get("cdp_url"),
        "auto_launched": bool(meta.get("auto_launched")),
        "visibility": meta.get("visibility")
        or ("terminal-browser-pane" if meta.get("source") == "terminal-browser" else "unknown"),
    }
    if meta.get("continuity"):
        browser["continuity"] = meta["continuity"]
    if meta.get("log"):
        browser["log"] = meta["log"]
    success = exit_code == 0 and status == "done" and not error
    out = {
        "success": success,
        "status": "error" if error and status != "blocked" else status,
        "final_url": last.get("url"),
        "actions": actions,
        "ticks": len(ticks),
        "usage": sum_usage(ticks),
        "browser": browser,
        "error": error,
    }
    # The declared carry-forward, so a row that gained a field cannot end here
    # without it: this loop is the whole contract, not a list to keep in step.
    for field in PASSTHROUGH:
        value = last.get(field.name)
        if field.carries(value):
            out[field.name] = field.cap(value)
    # DONE is a model choice, never an independent verification.
    out["verified"] = None
    if out["status"] == "done":
        out["outcome_verification"] = "unverified - DONE choice by model without independent check"
    else:
        out["outcome_verification"] = "not_applicable"
    out["stopped_reason"] = stopped_reason(out["status"], out.get("reason"), out.get("error"))
    if insights:
        rows_insights = [t["insight"] for t in ticks if isinstance(t.get("insight"), dict)]
        if rows_insights:
            out["insights"] = rows_insights
    if any(t.get("degenerate") for t in ticks):
        out["degenerate"] = True
    return out
