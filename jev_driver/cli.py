"""CLI: discover CDP, lease an owned tab, run ticks, print compact JSON."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .browser import LAST_CONTINUITY, _log_continuity, find_continuable_page, set_lease
from .cdp import connect
from .discover import WatchUnavailable, discover
from .drive_agent import DriveAgent, _why
from .questions import MAX_STEPS
from .readiness import REASON_WHY, unsupported_goal
from .runlog import JSONL_PATH, write_event
from .takeover import TAKEOVER_REASON, WatchAgent

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FIXTURE = (ROOT / "fixtures" / "click.html").resolve()


def _criterion_label(text, fallback):
    raw = str(text or fallback or "")
    if raw.startswith("[") and "]" in raw:
        raw = raw.split("]", 1)[1]
    return raw.split(";")[0].strip()[:80]


def _target_labels(decision):
    questions = ((decision or {}).get("request") or {}).get("questions") or {}
    op_key = ((decision or {}).get("operation") or "").lower() + "_target"
    criteria = (questions.get(op_key) or {}).get("criteria") or {}
    return {key: _criterion_label(text, key) for key, text in criteria.items()}


def _top_probs(probs, labels=None, limit=4):
    labels = labels or {}
    items = sorted((probs or {}).items(), key=lambda kv: -float(kv[1] or 0))[:limit]
    return [{"name": labels.get(key, key), "p": round(float(val), 3)} for key, val in items]


def choose_lease(*, url, target_id, continuable, default_url, navigate_explicit=True, dropped=None):
    """Decide which tab to drive.

    An explicit target id wins. Otherwise reuse the driver's own live tab and
    navigate it when a url was passed. Open a new tab only when that tab is gone.
    """
    if target_id:
        return {
            "tab": "target",
            "target_id": target_id,
            "navigate": navigate_explicit,
            "agent_url": url or default_url,
            "continuity": None,
        }
    existing_id, existing_url = continuable or (None, None)
    if existing_id:
        return {
            "tab": "target",
            "target_id": existing_id,
            "navigate": bool(url),
            "agent_url": url or existing_url or default_url,
            "continuity": "re-attach",
        }
    return {
        "tab": "new",
        "target_id": None,
        "navigate": True,
        "agent_url": url or default_url,
        "continuity": None if url else dropped,
    }


def trace_fields(snap: dict, rec: dict, *, goal: str) -> dict:
    """Fields worth keeping after the process exits. No request body, no API key."""
    decisions = snap.get("decisions") or []
    decision = decisions[-1] if decisions else {}
    labels = _target_labels(decision or {})
    status = rec.get("status")
    error = rec.get("error")
    if error or status == "blocked":
        kind = "blocked"
    elif status == "done":
        kind = "done"
    else:
        kind = "tick"
    return {
        "event": kind,
        "goal": goal,
        "status": status,
        "url": rec.get("url"),
        "last_action": rec.get("last_action"),
        "why": rec.get("why"),
        "error": error,
        "degenerate": True if rec.get("degenerate") else None,
        "ranked_ops": _top_probs((decision or {}).get("operation_probabilities"), limit=8),
        "ranked_targets": _top_probs((decision or {}).get("target_probabilities"), labels, limit=8),
        "page_text": (rec.get("page_text") or "")[:1500] or None,
        "reason": rec.get("reason"),
    }


def tick_record(snap: dict, *, debug: bool = False) -> dict:
    history = snap.get("history") or []
    decisions = snap.get("decisions") or []
    last = history[-1] if history else None
    decision = decisions[-1] if decisions else None
    page = snap.get("page") or {}
    choice = (decision or {}).get("choice")
    terminal = snap.get("status") in {"done", "blocked"} and choice in {"DONE", "BLOCKED"}
    if terminal:
        last_action = choice
        usage = (decision or {}).get("usage") or {}
    elif last:
        last_action = last.get("action")
        usage = last.get("usage") or {}
    elif snap.get("status") in {"done", "blocked"}:
        last_action = snap["status"].upper()
        usage = (decision or {}).get("usage") or {}
    else:
        last_action = None
        usage = {}
    reason = snap.get("stop_reason")
    why = _why(snap.get("status"), decision, history, reason)
    if not why and last and snap.get("status") not in {"done", "blocked"}:
        changed = last.get("page_changed")
        bit = "page changed" if changed is True else "page unchanged" if changed is False else ""
        why = f"{last.get('operation') or last.get('kind') or 'acted'} {last.get('action') or ''}".strip()
        if bit:
            why += f" ({bit})"
    rec = {
        "status": snap.get("status"),
        "url": page.get("url"),
        "last_action": last_action,
        "elapsed_ms": snap.get("elapsed_ms"),
        "usage": usage,
    }
    if why:
        rec["why"] = why
    if reason:
        rec["reason"] = reason
    if rec["status"] in {"done", "blocked"} or reason:
        rec["page_text"] = (page.get("text") or "")[:2000]
    if snap.get("takeover"):
        rec["error"] = TAKEOVER_REASON
    if degenerate(decision):
        rec["degenerate"] = True
    if debug and (decision or last):
        labels = _target_labels(decision)
        operation = (decision or {}).get("operation") or (last or {}).get("operation")
        target_key = (decision or {}).get("target")
        target = labels.get(target_key)
        if not target and operation in {"CLICK", "TYPE_TEXT", "SELECT"}:
            target = (last or {}).get("action")
        rec["insight"] = {
            "operation": operation,
            "confidence": (decision or {}).get("confidence"),
            "target": target,
            "why": why,
            "top_ops": _top_probs((decision or {}).get("operation_probabilities")),
            "top_targets": _top_probs((decision or {}).get("target_probabilities"), labels),
            "page_changed": None if last is None else last.get("page_changed"),
        }
    return rec


def degenerate(decision) -> bool:
    if not decision:
        return False
    probs = decision.get("operation_probabilities") or {}
    if not probs:
        return False
    ranked = sorted(probs.values(), reverse=True)
    top = ranked[0]
    gap = top - (ranked[1] if len(ranked) > 1 else 0)
    return top < 0.6 and gap < 0.1


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Drive a terminal-browser tab with Jev decisions.")
    parser.add_argument("--goal", required=True)
    parser.add_argument("--url", default=None, help="Page to open. Omit to re-attach to the last driven page.")
    parser.add_argument("--tab", choices=("new",), default="new")
    parser.add_argument("--target", dest="target_id", default=None, help="Attach to this CDP target id (explicit).")
    parser.add_argument("--browser", dest="browser_key", default=None)
    parser.add_argument("--max-steps", type=int, default=20)
    parser.add_argument("--navigate", action="store_true", help="With --target, also Page.navigate to --url.")
    parser.add_argument("--cdp", dest="cdp_url", default=None, help="Explicit CDP websocket or http discovery URL.")
    parser.add_argument("--json", action="store_true", help="Plugin contract: browser meta line, then JSON ticks.")
    parser.add_argument("--watch", action="store_true", help="Drive a visible terminal-browser pane.")
    parser.add_argument(
        "--debug",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Inject a debug HUD and include an insight trace (default on; --no-debug to disable).",
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.max_steps < 1 or args.max_steps > MAX_STEPS:
        print(json.dumps({"status": "blocked", "error": f"--max-steps must be 1..{MAX_STEPS}"}), file=sys.stderr)
        return 1
    url = args.url
    launch = url or DEFAULT_FIXTURE.as_uri()
    try:
        found = discover(explicit=args.cdp_url, launch_url=launch, watch=args.watch)
    except WatchUnavailable as exc:
        print(json.dumps({"status": "blocked", "error": str(exc)}), flush=True)
        return 1
    connect(found.ws_url)
    visibility = found.visibility or ("terminal-browser-pane" if found.source == "terminal-browser" else "headless")
    continuable = (None, None)
    dropped = None
    if not args.target_id:
        _log_continuity("lookup")
        continuable = find_continuable_page()
        dropped = None if continuable[0] else LAST_CONTINUITY
    plan = choose_lease(
        url=url,
        target_id=args.target_id,
        continuable=continuable,
        default_url=launch,
        navigate_explicit=args.navigate,
        dropped=dropped,
    )
    set_lease(
        tab=plan["tab"],
        target_id=plan["target_id"],
        browser_key=args.browser_key,
        navigate=plan["navigate"],
    )
    agent_url = plan["agent_url"]
    continuity = plan["continuity"]
    if args.json:
        meta = {
            "event": "browser",
            "source": found.source,
            "cdp_url": found.ws_url,
            "auto_launched": found.auto_launched,
            "visibility": visibility,
            "log": str(JSONL_PATH),
        }
        if continuity:
            meta["continuity"] = continuity
        print(json.dumps(meta), flush=True)
    write_event(
        {
            "event": "run",
            "goal": args.goal,
            "url": url,
            "continuity": continuity or ("new-tab" if plan["tab"] == "new" else "target"),
            "target_id": plan["target_id"],
            "source": found.source,
            "visibility": visibility,
        }
    )
    agent_cls = WatchAgent if args.watch else DriveAgent
    agent = None
    agent = agent_cls(agent_url, args.goal, screenshots=False, debug=args.debug)

    def emit(snap, rec):
        print(json.dumps(rec), flush=True)
        write_event(trace_fields(snap, rec, goal=args.goal))

    if unsupported_goal(args.goal):
        snap = agent.snapshot()
        snap["status"] = "blocked"
        snap["stop_reason"] = "unsupported"
        agent.state["status"] = "blocked"
        agent.state["stop_reason"] = "unsupported"
        rec = tick_record(snap, debug=args.debug)
        rec["page_text"] = ((snap.get("page") or {}).get("text") or "")[:2000]
        emit(snap, rec)
        return 1

    code = 1
    try:
        steps = 0
        snap = agent.snapshot()
        while agent.state["status"] not in {"done", "blocked"}:
            if steps >= args.max_steps:
                rec = {**tick_record(snap, debug=args.debug), "status": "blocked", "error": "max-steps"}
                rec["reason"] = "max_steps"
                rec["why"] = REASON_WHY["max_steps"]
                if args.debug and isinstance(rec.get("insight"), dict):
                    rec["insight"]["why"] = rec["why"]
                rec["page_text"] = ((snap.get("page") or {}).get("text") or "")[:2000]
                emit(snap, rec)
                return 1
            snap = agent.command("tick")
            steps += 1
            emit(snap, tick_record(snap, debug=args.debug))
        code = 0 if agent.state["status"] == "done" else 1
        return code
    except (ValueError, RuntimeError, TimeoutError) as exc:
        print(json.dumps({"status": "blocked", "error": str(exc)}), file=sys.stderr)
        write_event(
            {
                "event": "blocked",
                "goal": args.goal,
                "status": "blocked",
                "error": str(exc),
                "why": str(exc),
                "url": url,
            }
        )
        return 1
    finally:
        if agent is not None:
            agent.close()


if __name__ == "__main__":
    raise SystemExit(main())
