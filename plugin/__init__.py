"""Hermes agent plugin: native jev_drive tool (subprocess to the repo CLI)."""

from __future__ import annotations

from .handler import check_jev_drive, handle_jev_drive

DESCRIPTION = (
    "Drive one visible browser tab. Call this tool and stop. Do not open another "
    "browser tool, do not take a screenshot, and do not read this plugin's source. "
    "One viewport: forms, wizards, filters, logins, and in-view clicks. "
    "Pass url to navigate the driver's own tab. Omit url to stay on the current page. "
    "A new tab is opened only when that tab is gone. "
    "background defaults false and must stay false unless the user asks for a hidden "
    "browser; it does not launch one, it only attaches to cdp_url. "
    "debug defaults false. Pass debug=true only when the user asks to see scores "
    "on the page. "
    "Read reason and page_text before deciding the tool failed. "
    "model_blocked: this target was refused, the tool can still click. "
    "click_not_sent or stale_page: the click was chosen and not sent; call the same goal once more. "
    "field_changed: the text field changed before typing. "
    "shell or weak_done: the page was not ready. "
    "unsupported: the goal asked for a screenshot; page_text is the result. "
    "Not for scan/collect/rank over long pages. "
    "watch=true only stops if the user changes the page. The pane is visible either way."
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
        "cdp_url": {
            "type": "string",
            "description": "CDP URL to attach. Ignored unless background is true.",
        },
        "background": {
            "type": "boolean",
            "description": (
                "Attach to cdp_url instead of the visible pane. Off by default. "
                "Set true only when the user asks for a hidden browser. "
                "Does not launch a hidden browser."
            ),
            "default": False,
        },
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
                "Draw score outlines on the page and include an insight trace. "
                "Default false. Pass true only when the user asks to see the scores."
            ),
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
