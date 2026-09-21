"""Hermes agent plugin: native jev_drive tool (subprocess to the repo CLI)."""

from __future__ import annotations

from .handler import check_jev_drive, handle_jev_drive

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["goal"],
    "properties": {
        "goal": {"type": "string", "description": "Natural-language goal for the current page."},
        "url": {"type": "string", "description": "Page to open in an owned tab (file:// or https://)."},
        "target": {"type": "string", "description": "Explicit CDP target id to attach (opt-in)."},
        "max_steps": {
            "type": "integer",
            "description": "Tick budget (default 12, hard cap 30).",
            "default": 12,
            "minimum": 1,
            "maximum": 30,
        },
        "cdp_url": {"type": "string", "description": "Explicit CDP websocket or http://host:port discovery URL."},
        "timeout_s": {
            "type": "integer",
            "description": "Subprocess timeout in seconds (default 300, hard cap 900).",
            "default": 300,
            "minimum": 1,
            "maximum": 900,
        },
    },
}


def register(ctx) -> None:
    ctx.register_tool(
        name="jev_drive",
        toolset="jev",
        schema=SCHEMA,
        handler=handle_jev_drive,
        check_fn=check_jev_drive,
        description=(
            "Run a goal in a real browser: observe visible elements, let Jev pick "
            "operation+target, act. Prefer this over dumping snapshots into chat."
        ),
        emoji="⚡",
    )
