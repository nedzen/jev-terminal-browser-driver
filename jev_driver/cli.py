"""CLI: discover CDP, lease an owned tab, run ticks, print compact JSON."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .agent import Agent
from .browser import set_lease
from .cdp import connect
from .discover import discover
from .questions import MAX_STEPS

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
    return {
        "status": snap.get("status"),
        "url": page.get("url"),
        "last_action": last_action,
        "elapsed_ms": snap.get("elapsed_ms"),
        "usage": usage,
    }


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Drive a terminal-browser tab with Jev decisions.")
    parser.add_argument("--goal", required=True)
    parser.add_argument("--url", default=DEFAULT_FIXTURE.as_uri())
    parser.add_argument("--tab", choices=("new",), default="new")
    parser.add_argument("--target", dest="target_id", default=None, help="Attach to this CDP target id (explicit).")
    parser.add_argument("--browser", dest="browser_key", default=None)
    parser.add_argument("--max-steps", type=int, default=20)
    parser.add_argument("--navigate", action="store_true", help="With --target, also Page.navigate to --url.")
    parser.add_argument("--cdp", dest="cdp_url", default=None, help="Explicit CDP websocket or http discovery URL.")
    parser.add_argument("--json", action="store_true", help="Plugin contract: browser meta line, then JSON ticks.")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.max_steps < 1 or args.max_steps > MAX_STEPS:
        print(json.dumps({"status": "blocked", "error": f"--max-steps must be 1..{MAX_STEPS}"}), file=sys.stderr)
        return 1
    found = discover(explicit=args.cdp_url, launch_url=args.url)
    connect(found.ws_url)
    if args.json:
        print(
            json.dumps(
                {
                    "event": "browser",
                    "source": found.source,
                    "cdp_url": found.ws_url,
                    "auto_launched": found.auto_launched,
                }
            ),
            flush=True,
        )
    if args.target_id:
        set_lease(tab="target", target_id=args.target_id, browser_key=args.browser_key, navigate=args.navigate)
    else:
        set_lease(tab="new", browser_key=args.browser_key, navigate=True)
    agent = Agent(args.url, args.goal, screenshots=False)
    code = 1
    try:
        steps = 0
        snap = agent.snapshot()
        while agent.state["status"] not in {"done", "blocked"}:
            if steps >= args.max_steps:
                print(json.dumps({**tick_record(snap), "status": "blocked", "error": "max-steps"}))
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
