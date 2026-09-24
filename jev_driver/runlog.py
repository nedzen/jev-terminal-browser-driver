"""Append-only run log. No network, no browser."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

LOG_DIR = Path.home() / ".cache" / "jev-driver"
JSONL_PATH = LOG_DIR / "drive.jsonl"
TEXT_PATH = LOG_DIR / "drive.log"


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _one_line(value, limit=400) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _ranked(pairs) -> str:
    if not pairs:
        return "—"
    return " | ".join(f"{item.get('name')} {item.get('p')}" for item in pairs)


def write_event(event: dict, *, jsonl_path: Path = JSONL_PATH, text_path: Path = TEXT_PATH) -> None:
    """Append one JSON record and one readable line. Disk errors are ignored."""
    try:
        record = {"ts": _stamp(), **event}
        jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        with jsonl_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        kind = record.get("event") or "event"
        bits = [record["ts"], kind]
        if record.get("goal"):
            bits.append(f"goal={_one_line(record['goal'], 180)!r}")
        if record.get("status"):
            bits.append(f"status={record['status']}")
        if record.get("last_action"):
            bits.append(f"action={_one_line(record['last_action'], 80)}")
        if record.get("url"):
            bits.append(f"url={_one_line(record['url'], 160)}")
        if record.get("why"):
            bits.append(f"why={_one_line(record['why'], 240)}")
        if record.get("error"):
            bits.append(f"error={_one_line(record['error'], 240)}")
        if record.get("reason"):
            bits.append(f"reason={record['reason']}")
        if record.get("kind"):
            bits.append(f"kind={record['kind']}")
        if record.get("label"):
            bits.append(f"label={_one_line(record['label'], 80)}")
        if record.get("via"):
            bits.append(f"via={record['via']}")
        if record.get("typed"):
            bits.append(f"typed={_one_line(record['typed'], 80)!r}")
        lines = [" ".join(bits)]
        if record.get("ranked_ops") is not None or record.get("ranked_targets") is not None:
            lines.append(f"  ops: {_ranked(record.get('ranked_ops'))}")
            lines.append(f"  targets: {_ranked(record.get('ranked_targets'))}")
        if kind == "blocked" and record.get("page_text"):
            lines.append(f"  page: {_one_line(record['page_text'], 400)}")
        with text_path.open("a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
    except OSError:
        return
