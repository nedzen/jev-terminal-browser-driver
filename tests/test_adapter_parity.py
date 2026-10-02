"""Adapter parity: the MCP server and OpenCode plugin must stay in sync with
the Hermes plugin schemas (plugin/__init__.py is canonical). No browser."""

import re
from pathlib import Path

from plugin import DESCRIPTION, PARAMETERS, READ_DESCRIPTION, READ_PARAMETERS

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


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s)


def test_opencode_plugin_matches_canonical_schemas():
    ts = (ROOT / "opencode-plugin" / "jev-driver.ts").read_text()
    flat = re.sub(r'"\s*\+\s*"', "", ts)  # strip TS string-literal joins
    assert _norm(DESCRIPTION) in _norm(flat), "jev_drive description drifted"
    assert _norm(READ_DESCRIPTION) in _norm(flat), "jev_read description drifted"
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
