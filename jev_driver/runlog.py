"""Append-only run log. Sanitize before disk; never raise; never mutate the event.

Redacts secret keys/assignments, caps oversized values, and exposes
``redact_for_wire`` (same vocabulary, no size caps) for request bodies.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

LOG_DIR = Path.home() / ".cache" / "wwwdrive"
JSONL_PATH = LOG_DIR / "drive.jsonl"
TEXT_PATH = LOG_DIR / "drive.log"

MAX_STRING = 200  # chars kept per string value
MAX_LIST = 20  # items kept per list value
MAX_KEYS = 64  # keys kept per object
MAX_DEPTH = 6  # nesting levels walked
REDACTED = "[redacted]"

# Secret key names (word boundaries keep e.g. input_tokens intact).
_SECRET_KEY_RE = re.compile(
    r"api[-_]?key|private[-_]?key|(?:access|refresh|id|auth|session)[-_]?token|session[-_]?id"
    r"|[a-z0-9]+[-_]?(?:password|secret|token|cookie|credential)\b"
    r"|\b(?:tokens?|pass(?:word|wd)?|pwd|secrets?|auth(?:orization)?|bearers?|cookies?|credentials?)\b",
    re.IGNORECASE,
)
_SECRET_ASSIGN_RE = re.compile(
    r"(?i)\b(?P<k>[\"']?(?:[a-z0-9]+[-_])?"
    r"(?:api[-_]?key|access[-_]?token|refresh[-_]?token|auth[-_]?token|session[-_]?token|session[-_]?id"
    r"|auth(?:orization)?|password|passwd|pwd|secret|token|bearer|cookie)[\"']?)"
    r"\s*[:=]\s*"
    r"(?:\"[^\"]*\"|'[^']*'|(?:bearer|basic|token)\s+)?[^\s,;&)\]}]+"
)
_BEARER_RE = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{6,}")

def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def _one_line(value, limit=400) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"

def _clip(text: str) -> str:
    return text if len(text) <= MAX_STRING else text[: MAX_STRING - 1] + "…"

def _redact_secrets(text: str) -> str:
    """Redact secret-looking values in free text (assignments, then bare bearer)."""
    try:
        out = _SECRET_ASSIGN_RE.sub(lambda m: f"{m.group('k')}={REDACTED}", text)
        return _BEARER_RE.sub(f"\\1 {REDACTED}", out)
    except Exception:
        return text

def _scrub_text(text: str) -> str:
    """Redact secret-looking values in free text, then cap the length."""
    return _clip(_redact_secrets(text))

def _sanitize(value, key: str = "", depth: int = 0, seen=frozenset()) -> object:
    """Return a JSON-safe, redacted, size-capped copy. Never raises."""
    if key and _SECRET_KEY_RE.search(key):
        return REDACTED
    if isinstance(value, str):
        return _scrub_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if depth >= MAX_DEPTH:
        return "[truncated]"
    marker = id(value)
    if marker in seen:
        return "[circular]"
    nested = seen | {marker}
    if isinstance(value, dict):
        out = {}
        try:
            items = list(value.items())[:MAX_KEYS]
        except Exception:
            return f"<unserializable {type(value).__name__}>"
        for raw_key, item in items:
            try:
                child = _one_line(raw_key, MAX_STRING)
            except Exception:
                # One key whose str() explodes must not cost us the record.
                child = f"<unserializable {type(raw_key).__name__}>"
            out[child] = _sanitize(item, key=child, depth=depth + 1, seen=nested)
        return out
    if isinstance(value, (list, tuple, set, frozenset)):
        try:
            items = list(value)[:MAX_LIST]
        except Exception:
            return f"<unserializable {type(value).__name__}>"
        return [_sanitize(item, key=key, depth=depth + 1, seen=nested) for item in items]
    try:
        return _scrub_text(str(value))
    except Exception:
        return f"<unserializable {type(value).__name__}>"

def _record(event) -> dict:
    """One JSON-safe record. `ts` is ours; a caller cannot forge it."""
    if isinstance(event, dict):
        payload: object = event
    elif event is None:
        payload = {"event": "event"}
    else:
        payload = {"event": "event", "detail": event}
    sanitized = _sanitize(payload)
    if not isinstance(sanitized, dict):
        sanitized = {"event": "event", "detail": sanitized}
    record = {"ts": _stamp()}
    record.update({key: value for key, value in sanitized.items() if key != "ts"})
    return record

# Nesting a wire payload is walked to. Bodies the driver builds sit about seven
# deep, so this is a backstop against a pathological structure, not a policy.
MAX_WIRE_DEPTH = 12

def redact_for_wire(value, key: str = "", depth: int = 0) -> object:
    """Redact secrets in a request body without size caps. Never raises."""
    if key and _SECRET_KEY_RE.search(key):
        return REDACTED
    if isinstance(value, str):
        return _redact_secrets(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, dict):
        if depth >= MAX_WIRE_DEPTH:
            return "[truncated]"
        try:
            items = list(value.items())
        except Exception:
            return _redact_secrets(str(value))
        out = {}
        for raw_key, item in items:
            try:
                child = _one_line(raw_key, MAX_STRING)
            except Exception:
                child = f"<unserializable {type(raw_key).__name__}>"
            out[child] = redact_for_wire(item, key=child, depth=depth + 1)
        return out
    if isinstance(value, (list, tuple, set, frozenset)):
        if depth >= MAX_WIRE_DEPTH:
            return "[truncated]"
        try:
            items = list(value)
        except Exception:
            return _redact_secrets(str(value))
        return [redact_for_wire(item, key=key, depth=depth + 1) for item in items]
    try:
        return _redact_secrets(str(value))
    except Exception:
        return f"<unserializable {type(value).__name__}>"

def _ranked(pairs) -> str:
    if not isinstance(pairs, (list, tuple)) or not pairs:
        return "—"
    bits = []
    for item in pairs[:MAX_LIST]:
        if isinstance(item, dict):
            bits.append(f"{_one_line(item.get('name'), 80)} {_one_line(item.get('p'), 40)}")
        else:
            bits.append(_one_line(item, 80))
    return " | ".join(bits)

def _text_lines(record: dict) -> str:
    kind = record.get("event") or "event"
    bits = [record["ts"], kind]
    if record.get("goal"):
        bits.append(f"goal={_one_line(record['goal'], 180)!r}")
    if record.get("status"):
        bits.append(f"status={_one_line(record['status'], 60)}")
    if record.get("last_action"):
        bits.append(f"action={_one_line(record['last_action'], 80)}")
    if record.get("url"):
        bits.append(f"url={_one_line(record['url'], 160)}")
    if record.get("why"):
        bits.append(f"why={_one_line(record['why'], 240)}")
    if record.get("error"):
        bits.append(f"error={_one_line(record['error'], 240)}")
    if record.get("reason"):
        bits.append(f"reason={_one_line(record['reason'], 60)}")
    if record.get("kind"):
        bits.append(f"kind={_one_line(record['kind'], 40)}")
    if record.get("label"):
        bits.append(f"label={_one_line(record['label'], 80)}")
    if record.get("via"):
        bits.append(f"via={_one_line(record['via'], 40)}")
    if record.get("typed"):
        bits.append(f"typed={_one_line(record['typed'], 80)!r}")
    if record.get("continuity"):
        bits.append(f"continuity={_one_line(record['continuity'], 40)}")
    if record.get("scrolls") is not None:
        bits.append(f"scrolls={_one_line(record['scrolls'], 20)}")
    if record.get("script"):
        bits.append(f"script={_one_line(record['script'], 180)!r}")
    lines = [" ".join(bits)]
    if record.get("ranked_ops") is not None or record.get("ranked_targets") is not None:
        lines.append(f"  ops: {_ranked(record.get('ranked_ops'))}")
        lines.append(f"  targets: {_ranked(record.get('ranked_targets'))}")
    if kind == "blocked" and record.get("page_text"):
        lines.append(f"  page: {_one_line(record['page_text'], 400)}")
    return "\n".join(lines)

def _append(path: Path | str, line: str) -> None:
    """Append one line; ignore FS errors; replace lone surrogates."""
    try:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8", errors="replace") as handle:
            handle.write(line + "\n")
    except Exception:
        return

def write_event(event: dict, *, jsonl_path: Path | None = None, text_path: Path | None = None) -> None:
    """Append JSON + text lines. Never raises; never mutates ``event``."""
    try:
        record = _record(event)
        try:
            line = json.dumps(record, ensure_ascii=False, default=str)
        except Exception:
            line = json.dumps({"ts": record["ts"], "event": "event", "error": "unserializable record"})
        _append(JSONL_PATH if jsonl_path is None else jsonl_path, line)
        _append(TEXT_PATH if text_path is None else text_path, _text_lines(record))
    except Exception:
        return