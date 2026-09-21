"""Hermes agent plugin: native jev_drive tool (subprocess to the repo CLI)."""

from __future__ import annotations

from .handler import check_jev_drive, handle_jev_drive

DESCRIPTION = (
    "Run a single-viewport click-path goal in a real browser (forms, wizards, "
    "filters, logins, navigation): observe visible elements, let Jev pick "
    "operation+target, act. Not for scan/collect/rank over long pages "
    "(e.g. artificialanalysis.ai leaderboards) — use fetch/HTML/API. "
    "Prefer this over dumping snapshots into chat. Pass watch=true when the "
    "user asks to see the browser. Omit url to continue the last driven tab "
    "in the same browser (30 min). Pass a non-empty url when switching sites."
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
                "Page to open in a new owned tab. Omit to continue the last driven "
                "tab in the same browser (30 min). Pass a non-empty url when switching sites."
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
                "If true, drive a visible terminal-browser pane (needs a kitty-graphics "
                "terminal: kitty/ghostty/wezterm/tmux/vscode/cmux/supacode/herdr). Default false."
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
            "description": "Inject a driver-owned debug HUD (outlines + ranking) in the owned tab. Default false.",
            "default": False,
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
