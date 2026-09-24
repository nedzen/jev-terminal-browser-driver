# Maintainer notes

Hermes does not load this file. The agent contract is the `jev_drive` tool
description in `plugin/__init__.py`.

Do not tell the agent to read the terminal-browser skill or this repository's
Python. `terminal-browser` is the program on `PATH` that draws the pane.

Defaults: a visible pane, debug outlines off. `background: true` only attaches
to a `cdp_url` the caller already has. It does not launch a hidden browser.
