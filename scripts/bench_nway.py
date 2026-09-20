#!/usr/bin/env python3
"""Jev-only N-way element choice bench on frozen observe() tables."""

from __future__ import annotations

import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from jev_driver.browser import Browser, set_lease  # noqa: E402
from jev_driver.model import choose  # noqa: E402

DATA = ROOT / "tests" / "data"
FIXTURE = ROOT / "fixtures" / "nway.html"
REPORT = ROOT / "bench_nway.md"


def freeze_one(n: int, target: int, dest: Path) -> dict:
    url = FIXTURE.resolve().as_uri() + f"?n={n}&target={target}"
    set_lease(tab="new", navigate=True)
    browser = Browser(url)
    try:
        page = browser.observe(screenshot=False)
    finally:
        browser.close()
    correct = next(a["id"] for a in page["actions"] if a.get("label") == "Target Item")
    payload = {
        "n": n,
        "target_index": target,
        "goal": "Click the link labeled Target Item.",
        "correct_id": correct,
        "page": {k: page[k] for k in ("url", "title", "text", "actions") if k in page},
    }
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(payload))
    return payload


def cases_from_snapshot(payload: dict, count: int) -> list[dict]:
    """Build `count` labeled cases by rotating which filler is the goal, plus the real target."""
    actions = payload["page"]["actions"]
    clicks = [a for a in actions if a["kind"] == "click" and a.get("label")]
    out = []
    # Always include the designed target.
    out.append(
        {
            "goal": payload["goal"],
            "correct_id": payload["correct_id"],
            "page": payload["page"],
            "n_choices": len([a for a in actions if a["kind"] == "click"]),
        }
    )
    fillers = [a for a in clicks if a["id"] != payload["correct_id"] and a["label"].startswith("Filler")]
    step = max(1, len(fillers) // max(1, count - 1))
    for action in fillers[::step]:
        if len(out) >= count:
            break
        page = json.loads(json.dumps(payload["page"]))
        out.append(
            {
                "goal": f"Click the link labeled {action['label']}.",
                "correct_id": action["id"],
                "page": page,
                "n_choices": len([a for a in page["actions"] if a["kind"] == "click"]),
            }
        )
    return out[:count]


def run_case(case: dict) -> dict:
    started = time.perf_counter()
    decision = choose(case["page"], case["goal"], [])
    latency = round((time.perf_counter() - started) * 1000)
    usage = decision.get("usage") or {}
    return {
        "goal": case["goal"],
        "correct_id": case["correct_id"],
        "choice": decision["choice"],
        "hit": decision["choice"] == case["correct_id"],
        "operation": decision["operation"],
        "latency_ms": decision.get("latency_ms") or latency,
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "cost": usage.get("cost"),
        "n_choices": case["n_choices"],
    }


def summarize(rows: list[dict], label: str) -> str:
    hits = sum(1 for r in rows if r["hit"])
    lat = sorted(r["latency_ms"] for r in rows)
    p50 = lat[len(lat) // 2]
    ins = [r["input_tokens"] for r in rows if r["input_tokens"] is not None]
    outs = [r["output_tokens"] for r in rows if r["output_tokens"] is not None]
    costs = [r["cost"] for r in rows if r["cost"] is not None]
    n_click = rows[0]["n_choices"] if rows else 0
    return "\n".join(
        [
            f"## {label}",
            "",
            f"- Cases: {len(rows)} (click-target N ≈ {n_click})",
            f"- Accuracy (consumed action id): {hits}/{len(rows)} = {hits / len(rows):.1%}",
            f"- p50 latency: {p50} ms",
            f"- Mean input tokens: {round(statistics.mean(ins), 1) if ins else 'n/a'}",
            f"- Mean output tokens: {round(statistics.mean(outs), 1) if outs else 'n/a'}",
            f"- Mean cost USD: {statistics.mean(costs) if costs else 'n/a'}",
            "",
        ]
    )


def main() -> int:
    small_path = DATA / "nway20.json"
    large_path = DATA / "nway120.json"
    if "--freeze" in sys.argv or not small_path.exists():
        print("freezing n=20 snapshot", file=sys.stderr)
        freeze_one(20, 7, small_path)
    if "--freeze" in sys.argv or not large_path.exists():
        print("freezing n=120 snapshot", file=sys.stderr)
        freeze_one(120, 40, large_path)
    small = json.loads(small_path.read_text())
    large = json.loads(large_path.read_text())
    small_cases = cases_from_snapshot(small, 20)
    large_cases = cases_from_snapshot(large, 8)
    print(f"running {len(small_cases)} N~20 cases", file=sys.stderr)
    small_rows = [run_case(c) for c in small_cases]
    print(f"running {len(large_cases)} N~100+ cases", file=sys.stderr)
    large_rows = [run_case(c) for c in large_cases]
    report = [
        "# Jev N-way element choice (OpenRouter decisions, no Laya)",
        "",
        "Accuracy is argmax of the **consumed** target head mapped to the executed action id.",
        "Snapshots come from `snapshot.js` via `Browser.observe` on `fixtures/nway.html`.",
        "",
        summarize(small_rows, "N ≈ 20 click targets (≥20 cases)"),
        summarize(large_rows, "N ≈ 100+ click targets"),
        "## Raw N≈20",
        "",
        "```json",
        json.dumps(small_rows, indent=2),
        "```",
        "",
        "## Raw N≈100+",
        "",
        "```json",
        json.dumps(large_rows, indent=2),
        "```",
        "",
    ]
    REPORT.write_text("\n".join(report))
    print(REPORT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
