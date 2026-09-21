# jev-terminal-browser-driver — docs index

This project is a **Hermes agent plugin** first: a native `jev_drive` tool that
drives a real browser with cheap typed decisions (Jev via OpenRouter) and
returns ~200-byte JSON per tick. A standalone CLI (`scripts/drive.py`) lives
underneath the plugin and is what the plugin shells out to.

## Current docs (start here)

| Doc | What it covers |
|---|---|
| `../README.md` | Project overview, install, usage, tool schema, visibility behavior, limitations, credits |
| `../SKILL.md` | The Hermes skill doc — when to use the tool vs plain `terminal-browser action`, pitfalls, verification |
| `../HANDOFF.md` | Session-continuation state: verified status, known pitfalls, open TODO, people/panes |
| `architecture.md` | How it works: execution chain, decision protocol, CDP transport, discovery ladder, safety model |

## Research (design evidence — read before changing architecture)

| Doc | Verdict it established |
|---|---|
| `research/DESKTOP_PLUGIN_RESEARCH.md` | Desktop app = same local Python backend; plugin loader parity TUI/Desktop; agent-browser engine facts |
| `research/DESKTOP_BUTTON_RESEARCH.md` | Desktop plugin UI surface (panes/palette/statusBar chips); preview toolbar is NOT pluggable; upstream slot PR deferred |
| `research/VISIBLE_BROWSER_RESEARCH.md` | Visibility verdicts: preview.open emit = the desktop path; true-drive into the webview forbidden-by-design; TUI watch via `terminal-browser open --split` |

## Archive — iteration 1 (CLI-era, before the plugin)

`archive/iteration-1-cli/` preserves the pre-plugin iteration's operational
record. Kept for history and for numbers/pitfalls that were verified then; some
run instructions predate the plugin and are superseded by the README.

| Doc | What it holds |
|---|---|
| `archive/iteration-1-cli/JEV_DRIVER_NOTES.md` | First live-ops notes: probe result, per-tick token costs, CDP/Electron pitfalls, N-way viewport notes |
| `archive/iteration-1-cli/bench_nway.md` | N-way element-choice bench (20/20 @ N≈20, 8/8 @ N≈120, latency + token cost) |

Note: the Electron/CDP pitfalls first recorded in the archive notes (no
`Target.createTarget` on terminal-browser's Electron; never
`Target.closeTarget` — TUI `PageHost` crash; CDP websocket needs
`suppress_origin=True`) are still true and are reflected in the README and the
`AGENT_BROWSER_ENGINE` scrub in `jev_driver/discover.py`.
