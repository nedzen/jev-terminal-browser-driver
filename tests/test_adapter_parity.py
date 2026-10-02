"""Adapter parity: the MCP server must serve the Hermes plugin schemas
verbatim (plugin/__init__.py is canonical). No browser.

There is exactly one adapter surface: scripts/mcp.py imports the canonical
schemas, so drift is structurally impossible — these tests pin that import
relationship instead of duplicating schema text.
"""

import pytest

from plugin import (
    DESCRIPTION,
    PARAMETERS,
    READ_DESCRIPTION,
    READ_PARAMETERS,
    STATUS_DESCRIPTION,
    STATUS_PARAMETERS,
)
from plugin.handler import TIME_BUDGET_CAP


@pytest.fixture()
def mcp_mod(load_mcp):
    return load_mcp("jev_mcp_parity")


def test_mcp_serves_canonical_schemas(mcp_mod):
    mod = mcp_mod
    tools = {t["name"]: t for t in mod._tool_list()}
    assert set(tools) == {"jev_drive", "jev_read", "jev_status"}
    assert tools["jev_drive"]["inputSchema"] == PARAMETERS
    assert tools["jev_read"]["inputSchema"] == READ_PARAMETERS
    assert tools["jev_drive"]["description"] == DESCRIPTION
    assert tools["jev_read"]["description"] == READ_DESCRIPTION
    assert tools["jev_status"]["inputSchema"] == STATUS_PARAMETERS
    assert tools["jev_status"]["description"] == STATUS_DESCRIPTION


def test_time_budget_offered_with_the_same_bounds():
    """time_budget_s is optional on jev_drive, 1..900, never on jev_read."""
    prop = PARAMETERS["properties"]["time_budget_s"]
    assert prop["type"] == "integer"
    assert (prop["minimum"], prop["maximum"]) == (1, 900)
    assert "default" not in prop  # absent means no inner deadline
    assert TIME_BUDGET_CAP == prop["maximum"]
    assert "timeout_s" in PARAMETERS["properties"]  # the outer kill stays a separate knob
    assert "time_budget_s" not in READ_PARAMETERS["properties"]  # jev_read never drives


def test_status_tool_parity(mcp_mod):
    """jev_status: canonical empty-object schema, same checks as preflight."""
    from jev_driver.preflight import CHECKS, FIXES

    mod = mcp_mod
    tools = {t["name"]: t for t in mod._tool_list()}
    assert tools["jev_status"]["inputSchema"] == STATUS_PARAMETERS
    assert STATUS_PARAMETERS == {"type": "object", "additionalProperties": False, "properties": {}}
    assert set(CHECKS) <= set(FIXES)  # every check names its fix
