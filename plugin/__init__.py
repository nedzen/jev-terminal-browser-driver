"""Hermes agent plugin: native jev_drive tool (subprocess to the repo CLI)."""

from __future__ import annotations

from .handler import check_jev_drive, handle_jev_drive

DESCRIPTION = (
    "Drive a visible terminal-browser tab from Hermes TUI. One viewport at a "
    "time: forms, wizards, filters, logins, in-view navigation. Jev picks the "
    "operation and target, then acts. Not for scan/collect/rank over long pages "
    "(e.g. artificialanalysis.ai leaderboards) — use fetch/HTML/API. "
    "Do not ask it to take a screenshot. After it types, Press Enter is a separate "
    "action. Debug is on unless debug=false: the tab shows labeled candidates and why "
    "the run stopped, and the tool result includes an insights trace. "
    "Reuses the driver's own tab: a url navigates that tab instead of opening "
    "another one. A new tab is opened only when that tab is gone (30 min). "
    "watch=true only changes "
    "takeover: stop if the user changes the page. The pane is visible either way."
)

PARAMETERS = {
    "type": "object",
    "additionalProperties": False,
    "required": ["goal"],
    "properties": {
        "goal": {"type": "string", "description": "Natural-language goal for the current page."},
        "url": {
            "type": "string",
            "description": (
                "Navigate the driver's existing tab to this page. Omit to keep the "
                "current page. A new tab is opened only if the driver has no live tab."
            ),
        },
        "target": {"type": "string", "description": "Explicit CDP target id to attach (opt-in)."},
        "max_steps": {
            "type": "integer",
            "description": "Tick budget (default 12, hard cap 30).",
            "default": 12,
            "minimum": 1,
            "maximum": 30,
        },
        "cdp_url": {"type": "string", "description": "Explicit CDP websocket or http://host:port discovery URL."},
        "watch": {
            "type": "boolean",
            "description": (
                "If true, stop when the user changes the page (their action wins). "
                "The browser pane is visible either way. Default false."
            ),
            "default": False,
        },
        "timeout_s": {
            "type": "integer",
            "description": "Subprocess timeout in seconds (default 300, hard cap 900).",
            "default": 300,
            "minimum": 1,
            "maximum": 900,
        },
        "debug": {
            "type": "boolean",
            "description": (
                "Debug overlay plus an insights trace in the tool result "
                "(operation, labeled targets, confidence, why it stopped). "
                "Default true. Pass false to turn both off."
            ),
            "default": True,
        },
    },
}

# Hermes registry.get_definitions wraps entry.schema as the OpenAI `function` block.
# tool_search reads function.description and function.parameters — not ToolEntry.description
# and not a bare JSON Schema. Match google_meet/spotify: {name, description, parameters}.
SCHEMA = {
    "name": "jev_drive",
    "description": DESCRIPTION,
    "parameters": PARAMETERS,
}


def register(ctx) -> None:
    ctx.register_tool(
        name="jev_drive",
        toolset="jev",
        schema=SCHEMA,
        handler=handle_jev_drive,
        check_fn=check_jev_drive,
        description=DESCRIPTION,
        emoji="⚡",
    )
