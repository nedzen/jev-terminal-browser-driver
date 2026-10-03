"""Adapter contracts: MCP descriptions match the plugin; product bounds hold.

scripts/mcp.py imports plugin schemas, so inputSchema equality is structural.
Descriptions are still asserted: a hand-edited MCP list must not drift from
plugin/__init__.py.
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


def test_each_mcp_tool_description_matches_the_plugin(mcp_mod):
    """Every MCP tool description is the plugin's canonical string."""
    tools = {t["name"]: t for t in mcp_mod._tool_list()}
    assert set(tools) == {"drive", "read", "status"}
    assert tools["drive"]["description"] == DESCRIPTION
    assert tools["read"]["description"] == READ_DESCRIPTION
    assert tools["status"]["description"] == STATUS_DESCRIPTION


def test_time_budget_offered_with_the_same_bounds():
    """time_budget_s is optional on drive, 1..900, never on read."""
    prop = PARAMETERS["properties"]["time_budget_s"]
    assert prop["type"] == "integer"
    assert (prop["minimum"], prop["maximum"]) == (1, 900)
    assert "default" not in prop  # absent means no inner deadline
    assert TIME_BUDGET_CAP == prop["maximum"]
    assert "timeout_s" in PARAMETERS["properties"]  # the outer kill stays a separate knob
    assert "time_budget_s" not in READ_PARAMETERS["properties"]  # read never drives


def test_status_tool_takes_no_args_and_every_check_names_a_fix(mcp_mod):
    """status is an empty-object tool; every preflight check names its fix."""
    from jev_driver.preflight import CHECKS, FIXES

    tools = {t["name"]: t for t in mcp_mod._tool_list()}
    assert "status" in tools
    assert STATUS_PARAMETERS == {"type": "object", "additionalProperties": False, "properties": {}}
    assert set(CHECKS) <= set(FIXES)
