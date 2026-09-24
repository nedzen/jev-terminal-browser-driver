#!/usr/bin/env python3
"""Read the driver's tab: outline, or a caller script. No Jev loop."""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from jev_driver import browser as browser_mod  # noqa: E402
from jev_driver.browser import Browser, _log_continuity, find_continuable_page, set_lease  # noqa: E402
from jev_driver.cdp import connect  # noqa: E402
from jev_driver.cli import DEFAULT_FIXTURE, choose_lease, no_page_error  # noqa: E402
from jev_driver.discover import WatchUnavailable, discover  # noqa: E402
from jev_driver.page_read import read_page  # noqa: E402
from jev_driver.runlog import write_event  # noqa: E402


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Read structured data from the visible tab.")
    parser.add_argument("--url", default=None)
    parser.add_argument("--script", default=None)
    parser.add_argument("--scrolls", type=int, default=0)
    parser.add_argument("--target", dest="target_id", default=None)
    parser.add_argument("--cdp", dest="cdp_url", default=None)
    parser.add_argument("--background", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--debug", action=argparse.BooleanOptionalAction, default=False)
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    launch = args.url or DEFAULT_FIXTURE.as_uri()
    try:
        found = discover(explicit=args.cdp_url, launch_url=launch, background=args.background)
    except WatchUnavailable as exc:
        print(json.dumps({"success": False, "status": "blocked", "error": str(exc)}), flush=True)
        return 1
    connect(found.ws_url)
    continuable = (None, None)
    dropped = None
    if not args.target_id:
        _log_continuity("lookup")
        continuable = find_continuable_page()
        dropped = None if continuable[0] else browser_mod.LAST_CONTINUITY
    plan = choose_lease(
        url=args.url,
        target_id=args.target_id,
        continuable=continuable,
        default_url=launch,
        dropped=dropped,
    )
    missing = no_page_error(plan, args.url)
    if missing:
        write_event({"event": "read", "success": False, "error": missing, "continuity": plan.get("continuity")})
        print(json.dumps({"success": False, "status": "blocked", "error": missing}), flush=True)
        return 1
    set_lease(tab=plan["tab"], target_id=plan["target_id"], navigate=plan["navigate"])
    try:
        browser = Browser(plan["agent_url"])
        browser.debug = args.debug
        browser.restore_hud()
        result = read_page(browser, args.script, args.scrolls)
        browser.restore_hud()
    except (ValueError, RuntimeError, TimeoutError) as exc:
        result = {"success": False, "status": "blocked", "error": str(exc)}
    visibility = "background" if args.background else "terminal-browser-pane"
    result["browser"] = {"source": found.source, "visibility": visibility}
    if plan.get("continuity"):
        result["browser"]["continuity"] = plan["continuity"]
    outline = result.get("outline")
    write_event(
        {
            "event": "read",
            "url": result.get("url") or args.url,
            "scrolls": args.scrolls,
            "script": (args.script or "")[:400] or None,
            "success": bool(result.get("success")),
            "error": result.get("error"),
            "continuity": plan.get("continuity"),
            "outline": len(outline) if isinstance(outline, list) else None,
        }
    )
    print(json.dumps(result), flush=True)
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    raise SystemExit(main())
