"""Hermes agent plugin: native jev_drive tool (subprocess to the repo CLI)."""

from __future__ import annotations

from .handler import check_jev_drive, handle_jev_drive, handle_jev_read

DESCRIPTION = (
    "Click, type, select, and scroll in one visible browser tab until a goal is done. "
    "Use it for actions: open a menu, fill a form, press a button, like a post, follow a link. "
    "Do not open another browser tool, take a screenshot, or read this plugin's source. "
    "Write the goal as the whole task with its end state, for example "
    "'Open Bookmarks from the left nav; done when the Bookmarks timeline shows'. "
    "Several steps in one goal are fine. Do not split a task into one call per click. "
    "To look at, list, or collect what is on the page, call jev_read instead; "
    "it scrolls too (scrolls=N), so do not call jev_drive just to scroll and see more. "
    "Pass url to navigate the driver's own tab. Omit url to stay on the current page. "
    "The result has status (done or blocked), actions, why, reason, and page_text. "
    "Reasons: max_steps, the budget ran out, check page_text and continue with a narrower goal. "
    "click_not_sent or stale_page, the page moved before input; retry the same goal once. "
    "toggle_undo, the next click would have undone an earlier one, so the first click worked. "
    "model_blocked, the target is not visible here; scroll with jev_read or pass a url. "
    "shell or weak_done, the page was still loading. "
    "no_page, there is no driver tab; pass url. "
    "Do not repeat a goal that was blocked twice; report what page_text shows. "
    "background must stay false unless the user asks for a hidden browser. "
    "The debug overlay is a plugin setting, not an argument."
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

READ_DESCRIPTION = (
    "Read the same visible tab jev_drive uses, without clicking. "
    "Use it to see what is on the page, list posts, or collect JSON. "
    "Omit script to get an outline of articles, headings, times, and links. "
    "Then call again with script: an expression, a function, or a body with return, "
    "for example `[...document.querySelectorAll('article')].map(a => a.innerText.slice(0, 280))`. "
    "The value must be JSON-serializable. "
    "scrolls moves down the page before the script runs (max 15) so one call can cover a long feed. "
    "Pass url to navigate that tab first. Omit url to read the current page."
)

READ_PARAMETERS = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "url": {"type": "string", "description": "Navigate the driver's tab here before reading. Omit to stay."},
        "script": {
            "type": "string",
            "description": (
                "JavaScript expression, function, or body with return. JSON-serializable result. "
                "Omit for an outline."
            ),
        },
        "scrolls": {
            "type": "integer",
            "description": "How many viewport scrolls to run before reading. Default 0, max 15.",
            "default": 0,
            "minimum": 0,
            "maximum": 15,
        },
        "timeout_s": {
            "type": "integer",
            "description": "Subprocess timeout in seconds (default 300, hard cap 900).",
            "default": 300,
            "minimum": 1,
            "maximum": 900,
        },
    },
}

READ_SCHEMA = {"name": "jev_read", "description": READ_DESCRIPTION, "parameters": READ_PARAMETERS}


def apply_debug_setting(payload: dict, setting) -> dict:
    """The plugin setting is the only debug switch. A model-supplied flag is ignored."""
    out = dict(payload)
    out.pop("debug", None)
    out["debug"] = bool(setting)
    return out


def register(ctx) -> None:
    def handle(args=None, **kwargs):
        payload = dict(args if isinstance(args, dict) else kwargs)
        payload = apply_debug_setting(payload, ctx.get_config("debug", False))
        return handle_jev_drive(payload)

    ctx.register_tool(
        name="jev_drive",
        toolset="jev",
        schema=SCHEMA,
        handler=handle,
        check_fn=check_jev_drive,
        description=DESCRIPTION,
        emoji="⚡",
    )
    def handle_read(args=None, **kwargs):
        payload = dict(args if isinstance(args, dict) else kwargs)
        payload = apply_debug_setting(payload, ctx.get_config("debug", False))
        return handle_jev_read(payload)

    ctx.register_tool(
        name="jev_read",
        toolset="jev",
        schema=READ_SCHEMA,
        handler=handle_read,
        check_fn=check_jev_drive,
        description=READ_DESCRIPTION,
        emoji="📄",
    )
