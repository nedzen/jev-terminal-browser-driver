"""Adapter parity: the MCP server and OpenCode plugin must stay in sync with
the Hermes plugin schemas (plugin/__init__.py is canonical). No browser."""

import re
from pathlib import Path

from plugin import (
    DESCRIPTION,
    PARAMETERS,
    READ_DESCRIPTION,
    READ_PARAMETERS,
    STATUS_DESCRIPTION,
    STATUS_PARAMETERS,
)

ROOT = Path(__file__).resolve().parents[1]


def test_mcp_serves_canonical_schemas():
    import importlib.util

    spec = importlib.util.spec_from_file_location("jev_mcp_parity", ROOT / "scripts" / "mcp.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    tools = {t["name"]: t for t in mod._tool_list()}
    assert tools["jev_drive"]["inputSchema"] == PARAMETERS
    assert tools["jev_read"]["inputSchema"] == READ_PARAMETERS
    assert tools["jev_drive"]["description"] == DESCRIPTION
    assert tools["jev_read"]["description"] == READ_DESCRIPTION
    assert tools["jev_status"]["inputSchema"] == STATUS_PARAMETERS
    assert tools["jev_status"]["description"] == STATUS_DESCRIPTION


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s)


def test_opencode_plugin_matches_canonical_schemas():
    ts = (ROOT / "opencode-plugin" / "jev-driver.ts").read_text()
    flat = re.sub(r'"\s*\+\s*"', "", ts)  # strip TS string-literal joins
    assert _norm(DESCRIPTION) in _norm(flat), "jev_drive description drifted"
    assert _norm(READ_DESCRIPTION) in _norm(flat), "jev_read description drifted"
    assert _norm(STATUS_DESCRIPTION) in _norm(flat), "jev_status description drifted"
    for name in ("jev_drive", "jev_read", "scripts/drive.py", "scripts/read.py"):
        assert name in ts, name

    def ts_spelling(v):
        if type(v) is bool:
            return "true" if v else "false"
        return str(v)

    for schema in (PARAMETERS, READ_PARAMETERS):
        for prop, spec in schema["properties"].items():
            for key in ("maximum", "minimum", "default"):
                if key in spec:
                    assert f"{key}: {ts_spelling(spec[key])}" in ts, f"{prop}.{key} drifted"
    assert 'required: ["goal"]' in ts
    # Tier 1 (upstream PR #3 ideas): verification honesty, stop taxonomy,
    # page-text cap, strict budget validation — mirrored in both adapters.
    for marker in ("verified", "stopped_reason", "outcome_verification", "PAGE_TEXT_LIMIT = 2000",
                   "must be an integer", "model_done", "action_budget", "time_budget", "model_blocked",
                   "final_view", "cancelled", "detached",
                   # Tier 2 #2: the optional in-loop budget, forwarded like the others.
                   "TIME_BUDGET_CAP = 900", "--time-budget-s"):
        assert marker in ts, marker


def _drive_parameters_block(ts: str) -> str:
    """The source of the drive PARAMETERS object, so a bound cannot drift unnoticed."""

    start = ts.index("const PARAMETERS = {")
    end = ts.index("const READ_DESCRIPTION")
    return ts[start:end]


def test_time_budget_is_offered_by_both_adapters_with_the_same_bounds():
    """time_budget_s is optional on jev_drive in both adapters, 1..900, never on jev_read."""
    from plugin.handler import TIME_BUDGET_CAP

    prop = PARAMETERS["properties"]["time_budget_s"]
    assert prop["type"] == "integer"
    assert (prop["minimum"], prop["maximum"]) == (1, 900)
    assert "default" not in prop  # absent means no inner deadline
    assert TIME_BUDGET_CAP == prop["maximum"]
    assert "timeout_s" in PARAMETERS["properties"]  # the outer kill stays a separate knob
    assert "time_budget_s" not in READ_PARAMETERS["properties"]  # jev_read never drives

    ts = (ROOT / "opencode-plugin" / "jev-driver.ts").read_text()
    block = _drive_parameters_block(ts)
    assert "time_budget_s: {" in block
    # Scope the bounds to this property: max_steps and timeout_s share them.
    prop_block = block.split("time_budget_s: {", 1)[1].split("\n    },", 1)[0]
    assert 'type: "integer"' in prop_block
    assert "minimum: 1" in prop_block
    assert "maximum: 900" in prop_block
    assert "default:" not in prop_block

    # Forwarded to the same CLI flag and validated with the same cap as Python.
    assert 'argv.push("--time-budget-s"' in ts
    assert "budgetInt(args.time_budget_s, 1, TIME_BUDGET_CAP" in ts


def _status_tool_body(ts: str) -> str:
    """The source of the single editor.add({...}) block that registers jev_status.

    Scoped to that call's braces so assertions about it say something about
    jev_status specifically, not about the whole file where spawn is legitimate.
    """
    marker = 'name: "jev_status"'
    start = ts.index(marker)
    open_brace = ts.rindex("{", 0, start)
    depth = 0
    for i in range(open_brace, len(ts)):
        if ts[i] == "{":
            depth += 1
        elif ts[i] == "}":
            depth -= 1
            if depth == 0:
                return ts[open_brace : i + 1]
    raise AssertionError("unbalanced braces after jev_status registration")


def test_status_tool_parity():
    """jev_status: both adapters expose it, both report the same four checks, and
    neither may drift into returning key material."""
    import importlib.util

    from jev_driver.preflight import CHECKS, FIXES

    spec = importlib.util.spec_from_file_location("jev_mcp_status_parity", ROOT / "scripts" / "mcp.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    # Canonical empty-object schema, both adapters.
    assert STATUS_PARAMETERS == {"type": "object", "additionalProperties": False, "properties": {}}
    tools = {t["name"]: t for t in mod._tool_list()}
    assert tools["jev_status"]["inputSchema"] == STATUS_PARAMETERS

    ts = (ROOT / "opencode-plugin" / "jev-driver.ts").read_text()
    assert 'name: "jev_status"' in ts
    assert "description: STATUS_DESCRIPTION" in ts
    assert "input: STATUS_PARAMETERS" in ts
    assert "JSON.stringify(preflight())" in ts

    # Same check names and same fixes, verbatim.
    for name in (*CHECKS, *FIXES):
        assert f'"{name}"' in ts, name
    for fix in FIXES.values():
        assert fix in ts, fix

    # jev_status must stay in-process. Scope the check to the tool's own body:
    # the file legitimately imports spawn for jev_drive and jev_read.
    body = _status_tool_body(ts)
    # Word boundaries: `execute` and `uv run` inside prose must not trip this.
    banned = [
        r"\bspawn\b", r"\bspawnSync\b", r"\brunSubprocess\b", r"\bexecSync\b",
        r"\bexecFile\b", r"\bexecFileSync\b", r"\bchild_process\b", r"\bBun\s*\.\s*\$",
        r"\bexec\s*\(", r"\buv\b", r"scripts/drive", r"scripts/read",
    ]
    spawnish = [pattern for pattern in banned if re.search(pattern, body)]
    assert not spawnish, f"jev_status must not shell out, matched: {spawnish}\n{body}"
    # ...and it must actually be the in-process status computation.
    assert "preflight()" in body, f"jev_status does not compute statuses in-process:\n{body}"
    assert "JSON.stringify(preflight())" in body, body

    # jev_drive and jev_read still spawn the CLI, so the ban above is scoped.
    for spawned in ("scripts/drive.py", "scripts/read.py"):
        assert spawned in ts, spawned
    assert "runSubprocess" in ts and "spawn" in ts
