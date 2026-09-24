---
name: jev-terminal-browser-driver
description: Drive a terminal-browser tab with Jev decisions.
---

# Jev terminal-browser driver

Run the jev-ultrafast loop against an **owned** tab on the shared terminal-browser Chromium. Jev picks operation + target; the script clicks/types. Do not paste snapshots into chat.

## When to Use

- A **single-viewport click-path** goal on a live page (forms, wizards, filters, logins, navigation) should be executed with cheap typed decisions, not a11y dumps in context.
- You already have terminal-browser open and need observe → choose → act → re-observe.
- **Preferred when the Hermes plugin is installed:** call the native `jev_drive` tool (do not paste snapshots into chat, do not shell out to `drive.py` yourself).
- This is Hermes `--tui` only. Every run drives a **visible** terminal-browser pane (the open one, or a split the driver opens). There is no headless mode and no desktop preview.
- `debug` defaults to true. Leave it on. The owned tab shows the decision, labeled candidates, and why the run stopped. The tool result includes `insights` and `why`. Pass `debug: false` only when the user asks for a quiet run.
- `watch: true` does not change visibility. It only yields if the user changes the page ("their mouse wins"). Omit it unless they are driving the same tab by hand.
- If the driver says terminal-browser is missing or the terminal cannot draw it, say that. Do not retry in a background browser.
- Pass `url` to navigate the driver's existing tab. Do not expect a new tab per call. A new tab is opened only when the driver has no live tab of its own (30 min). Omit `url` to stay on the current page.

Don't use for:

- Aggregation or extraction over long multi-viewport pages (scan / collect / rank rows). jev only sees the current viewport (≤6k text). Example: ranking models on artificialanalysis.ai — use fetch/HTML/API instead.
- One-off `eval` / screenshot / cookie inspection — use `terminal-browser action` instead.
- Mixing `@eN` refs from `terminal-browser action -- snapshot` with this loop. Those ids are not Jev's `eN`.

## Prerequisites

- `terminal-browser` installed. A pane may already be open; if not, the driver opens one split to the right. Kitty-graphics terminals only (kitty, ghostty, wezterm, tmux, vscode, cmux, supacode). Not iTerm2 or Terminal.app.
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

Each tick prints one JSON line `{status, url, last_action, elapsed_ms, usage, why}` and, unless `--no-debug`, an `insight` object (operation, labeled target, top probabilities, whether the page changed). Exit 0 = model chose DONE; still verify the URL independently. `--target <cdpTargetId>` attaches to a tab you name explicitly. Default `--tab new` opens a TUI tab and **detaches** on exit (does not `Target.closeTarget` — that destroys Electron `webContents` under the TUI).

## Pitfalls

- Never `terminal-browser shutdown` (kills every pane's browser).
- Never `Target.closeTarget` on a terminal-browser tab. The TUI's `PageHost.blurContent` then throws `TypeError: Object has been destroyed`.
- Default path never attaches to existing user tabs. Do not pass `--target` on hindsight/X/Laya tabs.
- No `setDeviceMetricsOverride`; pane viewport is used as-is.
- Choice cap is 255; snapshot already caps 250 actions.
- Model DONE is not success — check `url` (or page text) yourself. A DONE below 0.6 is returned as not done, with `reason` of `weak_done` or `shell`. Use `page_text` and `reason` instead of opening another browser tool.
- TYPE_TEXT uses OpenRouter chat-completions (`inception/mercury-2.5`); Jev uses `https://openrouter.ai/api/alpha/decisions`.

## Verification

Last JSON `url` matches the independent check (e.g. contains `widget`). `uv run pytest` is offline and must stay green without network.
