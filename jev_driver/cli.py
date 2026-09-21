"""CLI: discover CDP, lease an owned tab, run ticks, print compact JSON."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .browser import LAST_CONTINUITY, _log_continuity, find_continuable_page, set_lease
from .cdp import connect
from .discover import WatchUnavailable, discover
from .drive_agent import DriveAgent
from .questions import MAX_STEPS
from .takeover import TAKEOVER_REASON, WatchAgent

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FIXTURE = (ROOT / "fixtures" / "click.html").resolve()


def tick_record(snap: dict) -> dict:
    history = snap.get("history") or []
    decisions = snap.get("decisions") or []
    last = history[-1] if history else None
    decision = decisions[-1] if decisions else None
    page = snap.get("page") or {}
    last_action = None
    if last:
        last_action = last.get("action")
    elif snap.get("status") in {"done", "blocked"}:
        last_action = snap["status"].upper()
    usage = (last or {}).get("usage") or (decision or {}).get("usage") or {}
    rec = {
        "status": snap.get("status"),
        "url": page.get("url"),
        "last_action": last_action,
        "elapsed_ms": snap.get("elapsed_ms"),
        "usage": usage,
    }
    if rec["status"] in {"done", "blocked"}:
        rec["page_text"] = (page.get("text") or "")[:2000]
    if snap.get("takeover"):
        rec["error"] = TAKEOVER_REASON
    if degenerate(decision):
        rec["degenerate"] = True
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
    parser.add_argument("--debug", action="store_true", help="Inject a debug HUD in the owned tab.")
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
    continuity = None
    if args.target_id:
        set_lease(tab="target", target_id=args.target_id, browser_key=args.browser_key, navigate=args.navigate)
        agent_url = url or launch
    elif url is None:
        _log_continuity("empty-url")
        existing_id, existing_url = find_continuable_page()
        if existing_id:
            set_lease(tab="target", target_id=existing_id, browser_key=args.browser_key, navigate=False)
            agent_url = existing_url or launch
            continuity = "re-attach"
        else:
            set_lease(tab="new", browser_key=args.browser_key, navigate=True)
            agent_url = DEFAULT_FIXTURE.as_uri()
            continuity = LAST_CONTINUITY
    else:
        set_lease(tab="new", browser_key=args.browser_key, navigate=True)
        agent_url = url
    if args.json:
        meta = {
            "event": "browser",
            "source": found.source,
            "cdp_url": found.ws_url,
            "auto_launched": found.auto_launched,
            "visibility": visibility,
        }
        if continuity:
            meta["continuity"] = continuity
        print(json.dumps(meta), flush=True)
    agent_cls = WatchAgent if args.watch else DriveAgent
    agent = agent_cls(agent_url, args.goal, screenshots=False, debug=args.debug)
    code = 1
    try:
        steps = 0
        snap = agent.snapshot()
        while agent.state["status"] not in {"done", "blocked"}:
            if steps >= args.max_steps:
                rec = {**tick_record(snap), "status": "blocked", "error": "max-steps"}
                rec["page_text"] = ((snap.get("page") or {}).get("text") or "")[:2000]
                print(json.dumps(rec))
                return 1
            snap = agent.command("tick")
            steps += 1
            print(json.dumps(tick_record(snap)), flush=True)
        code = 0 if agent.state["status"] == "done" else 1
        return code
    except (ValueError, RuntimeError, TimeoutError) as exc:
        print(json.dumps({"status": "blocked", "error": str(exc)}), file=sys.stderr)
        return 1
    finally:
        agent.close()


if __name__ == "__main__":
    raise SystemExit(main())
