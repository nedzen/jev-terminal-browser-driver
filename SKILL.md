---
name: jev-terminal-browser-driver
description: Drive a terminal-browser tab with Jev decisions.
---

# Jev terminal-browser driver

Run the jev-ultrafast loop against an **owned** tab on the shared terminal-browser Chromium. Jev picks operation + target; the script clicks/types. Do not paste snapshots into chat.

## When to Use

- A goal on a live page should be executed with cheap typed decisions, not a11y dumps in context.
- You already have terminal-browser open and need observe → choose → act → re-observe.

Don't use for:

- One-off `eval` / screenshot / cookie inspection — use `terminal-browser action` instead.
- Mixing `@eN` refs from `terminal-browser action -- snapshot` with this loop. Those ids are not Jev's `eN`.

## Prerequisites

- terminal-browser running (`~/.local/bin/terminal-browser ls --all --json` shows a `cdpPort`).
- `OPENROUTER_API_KEY` or `DECISION_GATE_API_KEY` (env or `~/.hermes/.env`).
- From this repo: `uv sync`.

## How to Run

```bash
uv run python scripts/drive.py \
  --goal 'Click the Widget link' \
  --url "file://$(pwd)/fixtures/click.html" \
  --tab new \
  --max-steps 5
```

Each tick prints one JSON line `{status, url, last_action, elapsed_ms, usage}`. Exit 0 = model chose DONE; still verify the URL independently. `--target <cdpTargetId>` attaches to a tab you name explicitly. Default `--tab new` opens a TUI tab and **detaches** on exit (does not `Target.closeTarget` — that destroys Electron `webContents` under the TUI).

## Pitfalls

- Never `terminal-browser shutdown` (kills every pane's browser).
- Never `Target.closeTarget` on a terminal-browser tab. The TUI's `PageHost.blurContent` then throws `TypeError: Object has been destroyed`.
- Default path never attaches to existing user tabs. Do not pass `--target` on hindsight/X/Laya tabs.
- No `setDeviceMetricsOverride`; pane viewport is used as-is.
- Choice cap is 255; snapshot already caps 250 actions.
- Model DONE is not success — check `url` (or page text) yourself.
- TYPE_TEXT uses OpenRouter chat-completions (`inception/mercury-2.5`); Jev uses `https://openrouter.ai/api/alpha/decisions`.

## Verification

Last JSON `url` matches the independent check (e.g. contains `widget`). `uv run pytest` is offline and must stay green without network.
