#!/usr/bin/env python3
"""Probe OpenRouter decisions API with ultrafast's object instructions/criteria.

Run BEFORE writing jev_driver/model.py. Records winning body shape.
Does not import jev_ultrafast. Does not call chat/completions for Jev.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

URL = os.environ.get("DECISION_GATE_URL", "https://openrouter.ai/api/alpha/decisions")
MODEL = os.environ.get("DECISION_GATE_MODEL", "typesafe/jev-1.13")
ROOT = Path(__file__).resolve().parents[1]
NOTES = ROOT / "JEV_DRIVER_NOTES.md"


def load_key() -> str:
    for var in ("DECISION_GATE_API_KEY", "OPENROUTER_API_KEY"):
        k = os.environ.get(var, "").strip()
        if k:
            return k
    env = Path.home() / ".hermes" / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if line.startswith("OPENROUTER_API_KEY="):
                val = line.split("=", 1)[1].strip().strip('"').strip("'")
                if val:
                    return val
    raise SystemExit("no DECISION_GATE_API_KEY or OPENROUTER_API_KEY")


def post(body: dict) -> tuple[int, dict | str, int]:
    payload = json.dumps(body).encode()
    req = urllib.request.Request(
        URL,
        data=payload,
        headers={"Authorization": f"Bearer {load_key()}", "Content-Type": "application/json"},
        method="POST",
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read()
            ms = round((time.perf_counter() - started) * 1000)
            return resp.status, json.loads(raw), ms
    except urllib.error.HTTPError as e:
        ms = round((time.perf_counter() - started) * 1000)
        text = e.read().decode(errors="replace")[:2000]
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = text
        return e.code, parsed, ms


OBJECT_BODY = {
    "model": MODEL,
    "state": {
        "page": {"url": "https://example.test/", "title": "Search", "text": "Search Widget"},
        "elements": [
            {"index": "1", "label": "Search", "role": "textbox", "operations": ["TYPE_TEXT", "CLICK"]},
            {"index": "2", "label": "Widget", "role": "link", "operations": ["CLICK"]},
        ],
        "recent_actions": [],
    },
    "questions": {
        "operation": {
            "type": "choice",
            "instructions": {
                "goal": "Click the Widget link",
                "rules": "Advance the user's entire goal from the CURRENT page using one operation.",
            },
            "criteria": {
                "CLICK": {"covers": "Click an element or link", "not": "Typing"},
                "TYPE_TEXT": {"covers": "Enter text in a field", "not": "Clicking"},
                "DONE": {"covers": "Every requirement is visibly satisfied", "not": "Still need a click"},
                "BLOCKED": {"covers": "No supported operation can progress", "not": "A useful control is visible"},
                "WAIT": {
                    "covers": "Needed control is absent or results still loading",
                    "not": "A useful control is visible",
                },
            },
        }
    },
}


def stringify_guidance(value):
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True)


def stringify_body(body: dict) -> dict:
    out = json.loads(json.dumps(body))
    for q in out["questions"].values():
        q["instructions"] = stringify_guidance(q["instructions"])
        q["criteria"] = {k: stringify_guidance(v) for k, v in q["criteria"].items()}
    return out


def validate_choice(answer, ids) -> bool:
    try:
        probabilities = answer["probabilities"]
        numbers = [*probabilities.values(), answer["confidence"]]
        return (
            answer["choice"] in ids
            and set(probabilities) == set(ids)
            and all(type(n) in (int, float) and 0 <= n <= 1 for n in numbers)
            and abs(sum(probabilities.values()) - 1) < 0.02
            and probabilities[answer["choice"]] >= max(probabilities.values()) - 1e-6
        )
    except (KeyError, TypeError, ValueError):
        return False


def summarize(status: int, payload, ms: int, shape: str) -> dict:
    ids = set(OBJECT_BODY["questions"]["operation"]["criteria"])
    answer = {}
    valid = False
    if isinstance(payload, dict):
        answer = (payload.get("answers") or {}).get("operation") or {}
        valid = validate_choice(answer, ids)
    return {
        "shape": shape,
        "http_status": status,
        "latency_ms": ms,
        "valid_choice": valid,
        "choice": answer.get("choice"),
        "confidence": answer.get("confidence"),
        "usage": payload.get("usage") if isinstance(payload, dict) else None,
        "model": payload.get("model") if isinstance(payload, dict) else None,
        "error_excerpt": None
        if status == 200
        else (payload if isinstance(payload, str) else json.dumps(payload)[:800]),
    }


def main() -> None:
    object_status, object_payload, object_ms = post(OBJECT_BODY)
    results = [summarize(object_status, object_payload, object_ms, "object_instructions_and_criteria")]
    winning = "object_instructions_and_criteria"
    if object_status != 200:
        string_status, string_payload, string_ms = post(stringify_body(OBJECT_BODY))
        results.append(summarize(string_status, string_payload, string_ms, "stringified_guidance"))
        winning = "stringified_guidance" if string_status == 200 else "neither"

    lines = [
        "# Jev driver notes",
        "",
        "Working notes for the terminal-browser Jev driver. Probe recorded **before** the model adapter.",
        "",
        "## Probe (OpenRouter decisions)",
        "",
        f"- Endpoint: `{URL}`",
        f"- Model: `{MODEL}`",
        f"- Time: {time.strftime('%Y-%m-%d %H:%M:%S %z')}",
        f"- Winning body shape: **{winning}**",
        "- Chat/completions for Jev: not probed (rejected by OpenRouter for this model).",
        "",
        "```json",
        json.dumps(results, indent=2),
        "```",
        "",
    ]
    NOTES.write_text("\n".join(lines) + "\n")
    print(json.dumps({"winning": winning, "results": results}, indent=2))
    if winning == "neither":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
