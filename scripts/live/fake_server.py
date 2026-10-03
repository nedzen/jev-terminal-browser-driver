"""A scripted MCP stdio server, so the harness can be tested without a browser.

The self-tests need all four classifications, a stall, and a transport failure, and
none of those should cost a pane. This server speaks the same newline-delimited
JSON-RPC as `scripts/mcp.py`, answers `initialize` / `tools/list` / `tools/call`,
and returns whatever result the test scripted for the next drive.

The stall is the interesting one. A stall cannot be faked by returning slowly --
the harness watches the *run log* for tick progress while the call is outstanding,
so a scripted server has to write the log too, or the stall path is never actually
exercised. `STALL_SILENT` therefore writes nothing at all.

Stdlib only.
"""

from __future__ import annotations

import json
import subprocess
import sys

SERVER_NAME = "live-fake"
PROTOCOL = "2025-06-18"

# The end state every scripted scenario lands on, matching the IANA goal the
# self-tests declare. Fixed, so a scenario's classification depends on the
# scenario and not on the arguments the harness happened to send.
END_STATE_URL = "https://www.iana.org/help/example-domains"


def result_text(payload: dict) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
            "isError": payload.get("status") == "error"}


def drive_result(*, status: str, stop_reason: str | None, final_url: str | None = None,
                 final_view: str | None = None, **extra) -> dict:
    """A drive result in the shape `scripts/mcp.py` returns.

    `false_done` is expressed the only way the product can express it: the stop
    reason claims done while the caller sees no end state. The classifier has to
    catch that from the two halves, which is the behaviour under test.
    """
    # final_view is an object in a real drive result (url/title/flags) and carries
    # no body text, with the page text alongside it as `page_text`. A fake that
    # returned a ready-made string would let a text_present predicate pass against
    # the view and hide the very bug this models.
    view = final_view if isinstance(final_view, dict) else (
        {"url": final_url or "", "title": final_view or ""} if final_view else None
    )
    payload = {
        "success": status == "done",
        "status": status,
        "stopped_reason": stop_reason,
        "final_url": final_url,
        "final_view": view,
        "page_text": extra.get("page_text"),
        "verified": None,
        "actions": extra.get("actions", []),
    }
    payload.update({k: v for k, v in extra.items() if k != "actions"})
    return result_text(payload)


# The four scripted outcomes the suite needs.
HIT = "hit"
FALSE_DONE = "false_done"
HONEST_BLOCKED = "honest_blocked"
UNJUSTIFIED_BLOCKED = "unjustified_blocked"

# Writes nothing to the run log, so the caller's stall watcher sees no progress.
STALL_SILENT = "stall_silent"

# Never returns, so the caller's timeout path is what ends the run.
NEVER_RETURNS = "never_returns"

# v3.1 classes the old scenarios did not cover.
HIT_RECOVERED = "hit_recovered"
CRASH = "crash"
# A done stop on the end-state URL, with a consequential action label attached,
# so the manifest denylist has something real to catch.
CONSEQUENTIAL_HIT = "consequential_hit"
# A done stop with no end state anywhere: the sev-1 integrity case.
FALSE_DONE_SEV1 = "false_done_sev1"

# Returns a result, then writes an *unfinished* run into the log directory. The
# next drive in the chain must refuse to start, which is the S4 "no interleave"
# rule: a foreign session appearing between calls cannot be allowed to share the
# tab. Only the fake server can stage this -- a real driver would need another
# process to seize the pane mid-suite.
ISOLATION_VIOLATION = "isolation_violation"


HIT_AT_PREFIX = "hit@"

# `pagetext@<url>|<body text>`: a HIT at <url> whose page text is <body text>, with
# a dict final_view carrying no body. The only way a text_present predicate can be
# satisfied is if the harness actually threads page_text through.
PAGE_TEXT_AT_PREFIX = "pagetext@"


def scripted_result(scenario: str, *, url: str = END_STATE_URL, **kwargs) -> dict:
    """The drive result for a named scenario.

    Named rather than parameterised because the self-tests assert on behaviour,
    and a test that says "the false-done case" is readable where one that says
    "drive_result(status='done', stop_reason='model_done')" is not.

    The end-state URL is fixed rather than echoed from the request: a scripted
    outcome has to be self-contained, or the harness would score its own input
    instead of the scenario it was handed.
    """
    if scenario == HIT:
        return drive_result(status="done", stop_reason="model_done", final_url=url,
                            final_view=kwargs.get("final_view", "Example Domains"))
    if scenario.startswith(PAGE_TEXT_AT_PREFIX):
        url, _, body = scenario[len(PAGE_TEXT_AT_PREFIX):].partition("|")
        return drive_result(status="done", stop_reason="model_done", final_url=url,
                            final_view=None, page_text=body)
    if scenario.startswith(HIT_AT_PREFIX):
        # A HIT that lands on a caller-chosen URL, so a chain test can declare its
        # own end state instead of every test having to expect the IANA fixture.
        return drive_result(status="done", stop_reason="model_done",
                            final_url=scenario[len(HIT_AT_PREFIX):],
                            final_view=kwargs.get("final_view", "Release notes"))
    if scenario == FALSE_DONE:
        # Claims done while sitting somewhere else: the end state is absent, which
        # is what makes it false-done rather than merely unfinished.
        return drive_result(status="done", stop_reason="model_done",
                            final_url="https://example.com/", final_view=None)
    if scenario == HONEST_BLOCKED:
        return drive_result(status="blocked", stop_reason="model_blocked", final_url=url,
                            final_view=kwargs.get("final_view", "No news here"))
    if scenario == UNJUSTIFIED_BLOCKED:
        return drive_result(status="blocked", stop_reason="model_blocked", final_url=url,
                            final_view=kwargs.get("final_view", "Example Domains"))
    if scenario == STALL_SILENT:
        return None
    if scenario == NEVER_RETURNS:
        return None
    if scenario == CRASH:
        # Handled in serve(), not here: CRASH is a transport fault, not a result.
        raise ValueError("CRASH is a transport fault and must not reach scripted_result")
    if scenario == HIT_RECOVERED:
        return drive_result(status="done", stop_reason="end_state_reached",
                            final_url=END_STATE_URL, final_view="Example Domains",
                            actions=[{"kind": "click", "label": "Learn more"}])
    if scenario == CONSEQUENTIAL_HIT:
        return drive_result(status="done", stop_reason="model_done",
                            final_url=END_STATE_URL, final_view="Example Domains",
                            actions=[{"kind": "click", "label": "Buy Bitcoin"}])
    if scenario == FALSE_DONE_SEV1:
        return drive_result(status="done", stop_reason="model_done",
                            final_url="https://example.com/", final_view=None)
    raise ValueError(f"unknown scenario {scenario!r}")


def serve(scenarios, *, log_dir=None):
    """Serve `scenarios` (a list, one per drive call, in order).

    stdout is the only channel the harness reads, so diagnostics go to stderr.
    """
    queue = list(scenarios)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": None,
                                         "error": {"code": -32700, "message": "parse error"}}) + "\n")
            sys.stdout.flush()
            continue
        method = message.get("method")
        msg_id = message.get("id")
        if msg_id is None:
            continue

        if method == "initialize":
            result = {"protocolVersion": PROTOCOL, "capabilities": {"tools": {}},
                      "serverInfo": {"name": SERVER_NAME, "version": "0"}}
        elif method == "tools/list":
            result = {"tools": [{"name": name, "description": "", "inputSchema": {"type": "object",
                             "properties": {}}} for name in ("drive", "read", "status")]}
        elif method == "tools/call":
            scenario = queue.pop(0) if queue else HIT
            if scenario == CRASH:
                # A bare newline: the client's readline gets something it cannot
                # parse, which is a DriverError and therefore CRASH. Deliberately
                # not an error *payload* -- a returned error result would be
                # scored MISS, and the point of this scenario is the transport
                # failing. The server stays alive so the retry can be served.
                sys.stdout.write("\n")
                sys.stdout.flush()
                continue
            if scenario in (STALL_SILENT, NEVER_RETURNS):
                # Deliberately no response: the caller's stall/timeout watcher is
                # the only thing that can end this, which is the point.
                if scenario == NEVER_RETURNS:
                    sys.stderr.write("never_returns: holding the call open\n")
                    sys.stderr.flush()
                continue
            if scenario == ISOLATION_VIOLATION:
                if log_dir:
                    from pathlib import Path as _Path
                    _dir = _Path(log_dir)
                    _dir.mkdir(parents=True, exist_ok=True)
                    with (_dir / "drive.jsonl").open("a", encoding="utf-8") as _fh:
                        _fh.write(json.dumps({"event": "run", "stage": "start",
                                              "metrics": {"run_id": "foreign-session"}}) + "\n")
                result = scripted_result(HIT)
            else:
                result = scripted_result(scenario)
        elif method == "ping":
            result = {}
        else:
            sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg_id,
                                         "error": {"code": -32601, "message": f"unknown method: {method}"}}) + "\n")
            sys.stdout.flush()
            continue

        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg_id, "result": result}) + "\n")
        sys.stdout.flush()
    return 0


def spawn(scenarios, *, log_dir=None):
    """Start the fake server as a subprocess, the same shape McpStdio expects."""
    payload = json.dumps({"scenarios": list(scenarios), "log_dir": str(log_dir) if log_dir else None})
    return subprocess.Popen(
        [sys.executable, "-c", _ENTRY, payload],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1,
    )


_ENTRY = """
import json, sys
sys.path.insert(0, %r)
from scripts.live.fake_server import serve
config = json.loads(sys.argv[1])
raise SystemExit(serve(config["scenarios"], log_dir=config["log_dir"]))
""" % str(__import__("pathlib").Path(__file__).resolve().parents[2])