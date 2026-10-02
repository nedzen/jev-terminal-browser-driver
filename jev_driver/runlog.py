"""Append-only run log. No network, no browser.

Every record is sanitized before it touches disk (upstream PR
browser-use/jev-ultrafast#141):

- credentials are redacted, both secret-looking dict keys (``api_key``,
  ``password``, ``token``, ``secret``, ``bearer``, ``cookie``, ...) and
  secret-looking assignments inside free text (``?api_key=...``,
  ``"token": "ghp_..."``, ``Authorization: Bearer ...``);
- oversized strings (>200 chars) and lists (>20 items) are capped, shallowly
  and with a depth limit, so a runaway page dump cannot blow up the log;
- ``write_event`` never raises: hostile input (``None``, circular refs, huge
  blobs, unserializable objects, lone surrogates, a key whose ``str()`` blows
  up) is coerced, and disk errors are swallowed.

The caller's event dict is never mutated, so logging the same event twice
produces the same record (idempotent).
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

# Key names whose value is dropped wholesale, whatever it holds. Word
# boundaries keep benign names such as "input_tokens" or "bypassed" intact; one
# `prefix_` segment catches vendor-ish names (my_password, auth_token) without
# catching input_tokens, whose trailing `s` is not a word boundary.
_SECRET_KEY_RE = re.compile(
    r"api[-_]?key|private[-_]?key|(?:access|refresh|id|auth|session)[-_]?token|session[-_]?id"
    r"|[a-z0-9]+[-_]?(?:password|secret|token|cookie|credential)\b"
    r"|\b(?:tokens?|pass(?:word|wd)?|pwd|secrets?|auth(?:orization)?|bearers?|cookies?|credentials?)\b",
    re.IGNORECASE,
)
# A secret assignment inside free text: `api_key=sk-live-...`,
# `"token": "ghp_..."`, `AUTH_TOKEN: sk-...`, `my_password=hunter2`,
# `Authorization: Bearer sk-...`, `SESSION_ID=...`. The key may be quoted and
# carry one `prefix_` segment; the value may be quoted or an auth scheme.
_SECRET_ASSIGN_RE = re.compile(
    r"(?i)\b(?P<k>[\"']?(?:[a-z0-9]+[-_])?"
    r"(?:api[-_]?key|access[-_]?token|refresh[-_]?token|auth[-_]?token|session[-_]?token|session[-_]?id"
    r"|auth(?:orization)?|password|passwd|pwd|secret|token|bearer|cookie)[\"']?)"
    r"\s*[:=]\s*"
    r"(?:\"[^\"]*\"|'[^']*'|(?:bearer|basic|token)\s+)?[^\s,;&)\]}]+"
)
# A bare credential with no key in front of it.
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


def _scrub_text(text: str) -> str:
    """Redact secret-looking values in free text, then cap the length.

    Assignments first: `Authorization: Bearer sk-...` then becomes
    `Authorization=[redacted]` in one step. The bare-credential pass runs on
    what is left, so it never leaves a half-consumed `[redacted]` behind.
    """
    try:
        out = _SECRET_ASSIGN_RE.sub(lambda m: f"{m.group('k')}={REDACTED}", text)
        out = _BEARER_RE.sub(f"\\1 {REDACTED}", out)
    except Exception:
        return _clip(text)
    return _clip(out)


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
    """Append one line, ignoring anything the filesystem or the caller says.

    `errors="replace"` keeps a lone surrogate (page text can carry one) from
    turning the whole record into a UnicodeEncodeError and losing it.
    """
    try:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8", errors="replace") as handle:
            handle.write(line + "\n")
    except Exception:
        return


def write_event(event: dict, *, jsonl_path: Path | None = None, text_path: Path | None = None) -> None:
    """Append one JSON record and one readable line.

    Never raises and never mutates `event`: bad input is sanitized and disk
    errors are ignored, so a logging failure cannot break a run. Paths are used
    exactly as given; only a missing path falls back to the module default.
    """
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