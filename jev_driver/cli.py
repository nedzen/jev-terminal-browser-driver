"""CLI: discover CDP, lease an owned tab, run ticks, print compact JSON."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from urllib.parse import urlsplit

from plugin.core.result import build_tick_row
from plugin.core.trace import build_trace_record, last_decision, target_labels, top_probs

from . import browser as browser_mod
from . import model as model_mod
from . import processes as proc_mod
from .browser import _log_continuity, find_continuable_page, set_lease
from .cdp import connect, list_browsers
from .discover import WatchUnavailable, discover
from .drive_agent import TIME_BUDGET_WHY, DriveAgent, _why
from .preflight import preflight
from .questions import MAX_STEPS
from .readiness import REASON_WHY, degenerate, unsupported_goal
from .runlog import JSONL_PATH, write_event
from .takeover import TAKEOVER_REASON, WatchAgent

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FIXTURE = (ROOT / "fixtures" / "click.html").resolve()
TIME_BUDGET_CAP = 900  # same ceiling as the outer timeout_s kill


def same_document(left, right) -> bool:
    """True when both URLs are the same page, ignoring a trailing slash and fragment."""

    def norm(raw):
        parts = urlsplit((raw or "").strip())
        path = parts.path.rstrip("/") or "/"
        return (parts.scheme.lower(), parts.netloc.lower(), path, parts.query)

    if not left or not right:
        return False
    return norm(left) == norm(right)


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
            "navigate": bool(url) and not same_document(url, existing_url),
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


def no_page_error(plan, url):
    """A call without url must reuse the remembered tab, never fall back to the local fixture."""
    if url or plan["tab"] != "new":
        return None
    reason = plan.get("continuity") or "no remembered tab"
    return f"No page to reuse ({reason}). Pass url to open one."


def trace_fields(snap: dict, rec: dict, *, goal: str) -> dict:
    """The run-log record for one tick. No request body, no API key.

    The shape belongs to plugin.core.trace: the log's field set is declared
    there, so a field is added in one place rather than to a dict literal here
    that the log reader has to be told about separately.
    """
    return build_trace_record(rec, last_decision(snap), goal=goal)


def tick_record(snap: dict, *, debug: bool = False, **overrides) -> dict:
    """One tick row, assembled by plugin.core.result.build_tick_row.

    The driver reads a tick here and an agent reads the folded result there, so
    the row is built by the same builder that folds it. `overrides` are the
    caller's fields (a max_steps budget, a time_budget stop, a final re-read);
    they win over the snapshot's and go through the same builder, which is what
    keeps the two sides from drifting.
    """
    history = snap.get("history") or []
    decisions = snap.get("decisions") or []
    last = history[-1] if history else None
    decision = decisions[-1] if decisions else None
    page = snap.get("page") or {}
    choice = (decision or {}).get("choice")
    stopped = snap.get("status") in {"done", "blocked"}
    if stopped and (choice in {"DONE", "BLOCKED"} or snap.get("stop_reason")):
        last_action = choice if choice in {"DONE", "BLOCKED"} else snap["status"].upper()
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
    fields = {
        "why": why,
        "reason": reason,
        "error": TAKEOVER_REASON if snap.get("takeover") else None,
        # snapshot.js caps the action list at 250; when it trims, say so. A
        # blocked run then reads as "the model could not see these", not "the
        # page had none".
        "omitted_actions": page.get("omitted_actions"),
        "degenerate": True if degenerate(decision) else None,
    }
    # Page text only on a stopping tick: mid-run it is 2K chars the agent pays
    # for on every step and never reads.
    if overrides.get("status") or snap.get("status") in {"done", "blocked"} or reason:
        fields["page_text"] = page.get("text") or ""
    if debug and (decision or last):
        labels = target_labels(decision)
        operation = (decision or {}).get("operation") or (last or {}).get("operation")
        target_key = (decision or {}).get("target")
        target = labels.get(target_key)
        if not target and operation in {"CLICK", "TYPE_TEXT", "SELECT"}:
            target = (last or {}).get("action")
        fields["insight"] = {
            "operation": operation,
            "confidence": (decision or {}).get("confidence"),
            "target": target,
            "why": why,
            "top_ops": top_probs((decision or {}).get("operation_probabilities")),
            "top_targets": top_probs((decision or {}).get("target_probabilities"), labels),
            "page_changed": None if last is None else last.get("page_changed"),
        }
    # An override that is None means "nothing to override": the snapshot's own
    # value stands. Empty values are real values and do override.
    fields.update({key: value for key, value in overrides.items() if value is not None})
    return build_tick_row(
        {
            "status": overrides.get("status") or snap.get("status"),
            "url": page.get("url"),
            "last_action": last_action,
            "elapsed_ms": snap.get("elapsed_ms"),
            "usage": usage,
        },
        **fields,
    )


def _final_view(snap: dict, browser) -> dict:
    """Re-read the page once after DONE and compare it with the decision-time read.

    A DONE choice is the model's claim, not evidence. This attaches the evidence
    and nothing else: the status never changes on account of it, and a failed
    re-read is reported rather than raised.
    """
    page = snap.get("page") or {}
    decisions = snap.get("decisions") or []
    decision = (decisions[-1] if decisions else None) or {}
    at_decision = decision.get("fingerprint") or page.get("fingerprint")
    if browser is None:
        return {"error": "no browser to re-read"}
    try:
        fresh = browser.observe(screenshot=False) or {}
        # Nothing below may raise: a browser that answers with anything but a page
        # reports like a failed re-read instead of escaping past this function.
        if not isinstance(fresh, dict):
            return {"error": f"observe returned {type(fresh).__name__}, not a page"}
    except Exception as exc:  # a probe must never turn a finished run into a crash
        return {"error": str(exc)[:200] or exc.__class__.__name__}
    seen = fresh.get("fingerprint")
    return {
        "page_changed_since_decision": None if not (at_decision and seen) else at_decision != seen,
        "url": fresh.get("url"),
        "title": fresh.get("title"),
    }


def _record_processes(goal) -> None:
    """Write the close-time process evidence: what this run spawned, and whether
    anything it started is still running after the detach-only close.

    Never raises and never returns a value. It runs from a `finally` block, where
    an exception would replace the run's exit code — accounting must be able to
    lose evidence, not the result. A zombie is not a leak, so the check reads the
    process state and not just `kill(pid, 0)`.
    """
    try:
        write_event(proc_mod.process_evidence(goal=goal))
    except Exception:
        return


def _instance_pid(ws_url):
    """The pid of the terminal-browser instance serving ``ws_url``, or None.

    `terminal-browser ls --all --json` reports every instance with its cdpPort
    and pid, so the port this run is driving identifies the process the
    auto-launch started. Without that pid the spawn is counted but untracked,
    and the close-time check can only fall back to a command-pattern scan —
    evidence about the box, never a verdict about this run.

    Every failure answers None, which is the state the orphan check already
    handled: a port we cannot read, no binary installed, an unreadable answer, or
    an instance `ls` cannot see from a no-TTY caller all leave the spawn
    counted and untracked rather than guessing at a pid.
    """
    try:
        port = urlsplit(ws_url or "").port
    except ValueError:
        return None
    if not port:
        return None
    try:
        rows = list_browsers().get("browsers") or []
    except Exception:
        return None
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            if int(row.get("cdpPort")) != port:
                continue
            pid = int(row.get("pid"))
        except (TypeError, ValueError):
            continue
        return pid if pid > 0 else None
    return None


def _metrics_of(agent):
    """This run's ``Metrics``, or None for an agent that has none. Never raises."""
    try:
        return getattr(agent, "metrics", None)
    except Exception:
        return None


def _bind_run(agent, goal) -> None:
    """Give the run's counters the goal digest, so metrics.json is attributable.

    Before this, a snapshot knows its own ``run_id`` but not which run that is.
    Never raises: telemetry identity is not worth a run.
    """
    metrics = _metrics_of(agent)
    if metrics is None:
        return
    try:
        metrics.bind_run(goal)
    except Exception:
        return


def _note_stop(agent) -> None:
    """Record why the run stopped, as a token, before close freezes the snapshot.

    The reason lives in the agent's state and the snapshot is written by
    ``close()``, so a deadline stop recorded after that would read as a blocked
    run with no error kind at all — indistinguishable from a blocked run that
    stopped for any other reason. Never raises.
    """
    metrics = _metrics_of(agent)
    if metrics is None:
        return
    try:
        reason = (getattr(agent, "state", None) or {}).get("stop_reason")
        if reason is not None:
            metrics.record_stop(reason)
    except Exception:
        return


def _run_event(identity, *, stage: str, agent=None) -> dict:
    """The per-run record: which code, which tab, and this run's counters.

    Written twice, once when the run is identified and once when it finishes,
    both carrying the same ``run_id`` inside the embedded snapshot. The first
    copy is what survives a run that is killed before it can close; the second
    is the run's numbers in the append-only log, where an overwritten
    metrics.json cannot reach them. ``metrics`` is absent for an agent that has
    no counters, which is every test double and no real run.
    """
    record = {"event": "run", "stage": stage, **(identity or {})}
    metrics = _metrics_of(agent)
    if metrics is None:
        return record
    try:
        record["metrics"] = metrics.snapshot()
    except Exception:
        return record
    return record


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Drive a terminal-browser tab with Jev decisions.")
    parser.add_argument("--goal", default=None)
    parser.add_argument(
        "--check", action="store_true", help="Print startup statuses as JSON and exit. Needs no --goal."
    )
    parser.add_argument("--url", default=None, help="Page to open. Omit to re-attach to the last driven page.")
    parser.add_argument("--tab", choices=("new",), default="new")
    parser.add_argument("--target", dest="target_id", default=None, help="Attach to this CDP target id (explicit).")
    parser.add_argument("--browser", dest="browser_key", default=None)
    parser.add_argument("--max-steps", type=int, default=20)
    parser.add_argument(
        "--time-budget-s",
        dest="time_budget_s",
        type=int,
        default=None,
        help=(
            f"Stop this run after N seconds of deciding (1..{TIME_BUDGET_CAP}). Checked before "
            "every model call and before every click or type; a decision that outlives it is "
            "discarded. Omit for no inner deadline, and note timeout_s stays the outer kill. "
            "The clock starts at the first decision, not at tab creation."
        ),
    )
    parser.add_argument("--navigate", action="store_true", help="With --target, also Page.navigate to --url.")
    parser.add_argument(
        "--deny-name",
        dest="deny_names",
        action="append",
        default=None,
        help=(
            "Never offer an element whose name matches this regex to the model (repeatable). "
            "A denied element gets no index, so it cannot be chosen, clicked, or typed into."
        ),
    )
    parser.add_argument("--cdp", dest="cdp_url", default=None, help="Explicit CDP websocket or http discovery URL.")
    parser.add_argument(
        "--background",
        action="store_true",
        help="Attach to --cdp instead of a visible pane. Does not launch a hidden browser.",
    )
    parser.add_argument("--json", action="store_true", help="Plugin contract: browser meta line, then JSON ticks.")
    parser.add_argument("--watch", action="store_true", help="Drive a visible terminal-browser pane.")
    parser.add_argument(
        "--debug",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Draw score outlines and include an insight trace. Off unless the user asks.",
    )
    args = parser.parse_args(argv)
    # --goal is required to drive, but not to ask whether driving is possible.
    if not args.check and not (args.goal or "").strip():
        parser.error("the following arguments are required: --goal (or pass --check)")
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.check:
        # Statuses only: no browser, no model call, no key material. Exit 0 either
        # way so a shell script can read the JSON without treating a missing
        # dependency as a crash.
        print(json.dumps(preflight()))
        return 0
    if args.max_steps < 1 or args.max_steps > MAX_STEPS:
        print(json.dumps({"status": "blocked", "error": f"--max-steps must be 1..{MAX_STEPS}"}), file=sys.stderr)
        return 1
    if args.time_budget_s is not None and not (1 <= args.time_budget_s <= TIME_BUDGET_CAP):
        err = f"--time-budget-s must be 1..{TIME_BUDGET_CAP}"
        print(json.dumps({"status": "blocked", "error": err}), file=sys.stderr)
        return 1
    # Installed before the agent is built: the denylist is read by every decision
    # this run makes, and a pattern that cannot compile is rejected here rather
    # than after a browser and a paid call.
    try:
        model_mod.set_deny_names(args.deny_names)
    except ValueError as exc:
        print(json.dumps({"status": "blocked", "error": str(exc)}), file=sys.stderr)
        return 1
    url = args.url
    launch = url or DEFAULT_FIXTURE.as_uri()
    # Spawn accounting starts here, so the count is about THIS run and not about
    # the process lifetime — one MCP server serves many runs per process.
    proc_mod.reset_spawns()
    try:
        found = discover(
            explicit=args.cdp_url,
            launch_url=launch,
            watch=args.watch,
            background=args.background,
        )
    except WatchUnavailable as exc:
        print(json.dumps({"status": "blocked", "error": str(exc)}), flush=True)
        return 1
    if found.auto_launched:
        # This run provisioned the pane, so it started one browser process.
        # terminal-browser forks that pane itself, so the pid comes from
        # `ls --all --json` matched by the port this run is driving. Without it
        # the spawn is counted but untracked, and the close-time check can only
        # answer with a command-pattern scan — evidence, not a verdict.
        proc_mod.note_spawn("terminal-browser", _instance_pid(found.ws_url))
    agent = None
    identity: dict | None = None
    # One try for everything from here to the end of the run: the close-time
    # evidence and the counters are written from the finally below, so a path
    # that returns early — no page to reuse, an unsupported goal, an agent that
    # would not open — has to be inside it or the run would leave no evidence at
    # all. Exit codes and printed rows are unchanged by where the try begins.
    try:
        connect(found.ws_url)
        visibility = "background" if args.background else "terminal-browser-pane"
        continuable = (None, None)
        dropped = None
        if not args.target_id:
            _log_continuity("lookup")
            continuable = find_continuable_page()
            dropped = None if continuable[0] else browser_mod.LAST_CONTINUITY
        plan = choose_lease(
            url=url,
            target_id=args.target_id,
            continuable=continuable,
            default_url=launch,
            navigate_explicit=args.navigate,
            dropped=dropped,
        )
        missing = no_page_error(plan, url)
        if missing:
            write_event({"event": "blocked", "goal": args.goal, "error": missing, "continuity": plan.get("continuity")})
            print(json.dumps({"status": "blocked", "error": missing, "reason": "no_page"}), flush=True)
            return 1
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
        # Name the code that produced this run, in the run log's own directory and
        # in the run's evidence line. Additive: a tree that cannot be hashed or a log
        # directory that cannot be written is reported inside the manifest, never
        # raised, and the run goes on unchanged.
        manifest = proc_mod.write_version_manifest()
        identity = {
            "goal": args.goal,
            "url": url,
            "continuity": continuity or ("new-tab" if plan["tab"] == "new" else "target"),
            "target_id": plan["target_id"],
            "source": found.source,
            "visibility": visibility,
            "version": manifest,
        }
        agent_cls = WatchAgent if args.watch else DriveAgent
        # Only pass the kwarg when asked, so an agent built without the flag is unchanged.
        extra = {} if args.time_budget_s is None else {"time_budget_s": args.time_budget_s}
        agent = agent_cls(agent_url, args.goal, screenshots=False, debug=args.debug, **extra)
        _bind_run(agent, args.goal)

        def emit(snap, rec):
            print(json.dumps(rec), flush=True)
            write_event(trace_fields(snap, rec, goal=args.goal))

        # Written before the first decision, so a run killed mid-drive still says
        # what it was aiming at and which code was answering.
        write_event(_run_event(identity, stage="start", agent=agent))

        refused = unsupported_goal(args.goal)
        if refused:
            snap = agent.snapshot()
            snap["status"] = "blocked"
            snap["stop_reason"] = refused
            agent.state["status"] = "blocked"
            agent.state["stop_reason"] = refused
            rec = tick_record(snap, debug=args.debug, page_text=((snap.get("page") or {}).get("text") or ""))
            emit(snap, rec)
            return 1

        code = 1
        try:
            steps = 0
            snap = agent.snapshot()
            while agent.state["status"] not in {"done", "blocked"}:
                if steps >= args.max_steps:
                    kinds = {item.get("kind") for item in snap.get("history") or []}
                    why = REASON_WHY["max_steps"]
                    if kinds and kinds <= {"scroll", "wait"}:
                        why = REASON_WHY["scroll_only"]
                    rec = tick_record(
                        snap,
                        debug=args.debug,
                        status="blocked",
                        error="max-steps",
                        reason="max_steps",
                        why=why,
                        page_text=(snap.get("page") or {}).get("text") or "",
                    )
                    if args.debug and isinstance(rec.get("insight"), dict):
                        rec["insight"]["why"] = why
                    emit(snap, rec)
                    return 1
                snap = agent.command("tick")
                steps += 1
                budget_stop = snap.get("stop_reason") == "time_budget"
                rec = tick_record(
                    snap,
                    debug=args.debug,
                    # The stop taxonomy reads error=="timeout" as stopped_reason
                    # time_budget, so an in-loop deadline reports like the outer
                    # timeout_s kill — without killing anything.
                    error="timeout" if budget_stop else None,
                    why=TIME_BUDGET_WHY if budget_stop else None,
                    final_view=_final_view(snap, (agent.state or {}).get("browser"))
                    if snap.get("status") == "done"
                    else None,
                )
                emit(snap, rec)
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
        _note_stop(agent)
        try:
            if agent is not None:
                agent.close()
        finally:
            # Nested so that neither record can be lost to the other failing: a
            # close that raises still leaves the run's numbers and its process
            # evidence in the log, and the close's own exception still propagates
            # exactly as it did when this was a single finally.
            try:
                if agent is not None:
                    write_event(_run_event(identity, stage="finish", agent=agent))
            finally:
                _record_processes(args.goal)


if __name__ == "__main__":
    raise SystemExit(main())
