# jev-terminal-browser-driver — docs index

This project is a **Hermes agent plugin** first: a native `jev_drive` tool that
drives a real browser with cheap typed decisions (Jev via OpenRouter) and
returns ~200-byte JSON per tick. A standalone CLI (`scripts/drive.py`) lives
underneath the plugin and is what the plugin shells out to.

Scope is **Hermes `--tui` + a visible terminal-browser pane**. Headless
Chromium and the Hermes desktop preview are not part of the product.

## Read these

| Doc | What it covers |
|---|---|
| `../README.md` | Install, CLI, plugin fields, the visible-or-fail discovery rule |
| `../SKILL.md` | What the Hermes agent should do: when to call `jev_drive`, debug on, pitfalls |
| `architecture.md` | Execution chain, decision protocol, CDP transport, current discovery ladder, safety model |
| `research/TUI_ONLY_DISCOVERY_FIX.md` | Why the headless ladder was removed, and the HERDR-scrub / daemon-DB mechanism |

## Do not treat these as the current contract

They record decisions that were later dropped (desktop preview, headless
agent-browser, a zap button). Useful as history. If they disagree with
`architecture.md`, architecture wins.

| Doc | What it actually is |
|---|---|
| `../HANDOFF.md` | Session notes from 2026-09-21. The top banner says what is stale. People/pane ids are dead. |
| `research/DESKTOP_PLUGIN_RESEARCH.md` | Desktop-app parity investigation. Not implemented as a supported path. |
| `research/DESKTOP_BUTTON_RESEARCH.md` | Desktop toolbar button. Not in scope. |
| `research/VISIBLE_BROWSER_RESEARCH.md` | How we learned the desktop webview cannot be driven. The TUI conclusion is now the whole product. |
| `research/JEV_DRIVE_BLOCKED_DEBUG_20260921.md` | Why extraction-over-a-long-page goals block. Still true: one viewport only. |

## Archive — iteration 1 (CLI-era, before the plugin)

`archive/iteration-1-cli/` preserves the pre-plugin iteration's operational
record. Kept for history and for numbers/pitfalls that were verified then; some
run instructions predate the plugin and are superseded by the README.

| Doc | What it holds |
|---|---|
| `archive/iteration-1-cli/JEV_DRIVER_NOTES.md` | First live-ops notes: probe result, per-tick token costs, CDP/Electron pitfalls, N-way viewport notes |
| `archive/iteration-1-cli/bench_nway.md` | N-way element-choice bench (20/20 @ N≈20, 8/8 @ N≈120, latency + token cost) |

Note: the Electron/CDP pitfalls first recorded in the archive notes are still
true: no `Target.createTarget` on terminal-browser's Electron; never
`Target.closeTarget` (TUI `PageHost` crash); CDP websocket needs
`suppress_origin=True`. The `AGENT_BROWSER_ENGINE` scrub described in those
notes was removed with the headless ladder.
