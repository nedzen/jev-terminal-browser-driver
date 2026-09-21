# Architecture

How `jev_drive` / `drive.py` works: the execution chain, the decision
protocol, the CDP transport, and the safety model. Paths relative to the repo
root unless noted.

## Pipeline

```
scripts/drive.py
  → set tab lease (new | explicit --target)     # safety, before anything
  → Agent(url, goal)                            # jev-ultrafast agent.py, verbatim
       Browser.observe   → snapshot.js run in-page via CDP Runtime.evaluate
                           (element table + ≤6K visible text + freshness guards)
       model.choose      → POST openrouter.ai/api/alpha/decisions
                           {model: typesafe/jev-1.13, state, questions}
                           one request, independent heads:
                             operation ∈ CLICK|TYPE_TEXT|SELECT|SCROLL_UP|
                                         SCROLL_DOWN|WAIT|DONE|BLOCKED
                             click_target / type_text_target / select_target
                           (only the selected operation's head can execute)
       Browser.act       → Input.dispatchMouseEvent / insertText on the
                           owned CDP session, hit-tested before input
       TYPE_TEXT only    → small LLM (inception/mercury-2.5 via OpenRouter
                           /chat/completions) writes the field value
```

## Execution chain in detail

1. **Tab lease.** `drive.py` sets a module-level lease on `jev_driver.browser`
   *before* constructing `Agent`. Default `--tab new` opens a TUI-visible tab
   via `terminal-browser new-tab` (terminal-browser's Electron) or
   `Target.createTarget` (system Chrome, where the daemon runs headless) and
   attaches by `targetId`. `--target <id>` attaches to a tab you name
   explicitly. The driver never attaches to tabs it didn't create.
2. **Observation.** `jev_driver/snapshot.js` (upstream, verbatim) runs inside
   the page: indexes visible interactive elements (buttons, links, inputs,
   selects, ARIA roles), assigns stable IDs (`e1…e250`), records per-element
   guards (value/checked/context) and a page marker, and reads at most 6,000
   chars of visible text. Actions are capped at 250.
3. **Decision.** `jev_driver/model.py` posts the state + goal to OpenRouter's
   decisions endpoint. The response must be a full probability distribution
   over the offered choices (sum ≈ 1, argmax == choice) or nothing executes.
   Independent question heads are a safety property: a `CLICK` decision
   physically cannot consume a `TYPE_TEXT` or `SELECT` target id.
4. **Action.** `jev_driver/browser.py` re-validates the target immediately
   before input (connected, visible, enabled, not covered, geometry on-screen)
   and executes via raw CDP input events. Mutations are never retried; stale
   pages raise and the loop re-observes instead. After date/combobox-class
   clicks, a bounded overlay wait stabilizes the observed element set (the
   date-picker/autocomplete stall class) before the next predict.
5. **Loop.** `jev_driver/agent.py` (upstream, verbatim) repeats
   observe → choose → act until the model picks `DONE`/`BLOCKED` or
   `--max-steps`. Model `DONE` is *not* treated as success — verify the
   resulting URL or page content yourself.

### Session continuity

Omitting `--url` / `url` re-attaches to the previously driven tab **in the
same browser** (`~/.cache/jev-driver/last-page.json` holds
`{targetId, url, source, browser_id, ts}`). Continuity requires the same
`discover()` source and `browser_id` (ws host:port), a live target (or
URL-stem match on this `/json/list`), and a pointer younger than 30 minutes.
Legacy files and cross-daemon ids fail closed: a new tab / default fixture,
never an arbitrary live tab. Pass a non-empty `url` when switching sites.

### Takeover (watch mode)

`jev_driver/takeover.py` defines `WatchAgent` — a thin subclass that keeps
`agent.py` verbatim and changes only the tick policy: `browser.fresh()` is
checked before predict, after predict, and on `StalePage`; any mismatch while
watching yields `blocked` with reason "user took over the browser" instead of
re-observing and continuing. The user's mouse wins; the driver never
force-navigates a surface the user moved.

## Discovery ladder (which browser gets driven)

`jev_driver/discover.py` resolves a CDP endpoint in this order:

1. Explicit `--cdp` URL, or `JEV_CDP_URL` / `BROWSER_CDP_URL` env.
2. A running terminal-browser instance (`terminal-browser ls --all --json`
   → `cdpPort`).
3. The agent-browser daemon (`~/.agent-browser`, session `jev-driver`) —
   covers headless sessions and Hermes' own `browser` toolset engine.
4. Loopback probe `127.0.0.1:9222–9330` (`/json/version`).
5. Auto-provision: launch a headless `agent-browser --session jev-driver`
   session (system Chrome) and re-discover.

**Watch branch** (`watch=true` / `--watch`): terminal-browser is the
environment authority (there is NO herdr/cmux assumption — herdr is one of
eight supported terminals). If a TB instance is running → attach. Else if the
TB binary is on PATH → `terminal-browser open <url> --split right` (TB splits
herdr/cmux/kitty/ghostty/wezterm/tmux/vscode/supacode panes natively via
inherited `HERDR_*` env) and poll for its port (~30s). Else raise
`WatchUnavailable` with the install pointer, and surface TB's own diagnostics
(unsupported terminal, missing kitty graphics, etc.). Never fall through to
headless when watch was requested.

Env hygiene: `discover._child_env()` strips `AGENT_BROWSER_ENGINE` unless it
names `chrome` / `lightpanda` — Hermes injects `~/.hermes/.env` into session
env, and a hash/empty value there makes agent-browser reject every launch
(verified live; see `archive/iteration-1-cli/JEV_DRIVER_NOTES.md`).

## CDP transport

`jev_driver/cdp.py` is a minimal websocket CDP client. It discovers the port
dynamically (never hardcode it), fetches `/json/version` →
`webSocketDebuggerUrl` (a bare `ws://host:port` does not connect), and
connects with **`suppress_origin=True`** (Electron returns 403 otherwise). It
preserves the upstream helper's contract: unwrapped `result` payloads,
`RuntimeError` on CDP errors, no `sessionId` on `Target.*` methods, session id
only on `Runtime./Page./Input./Emulation.*` after a flatten attach.

## Visibility surfaces

- **Desktop:** the plugin handler (in-process) emits Hermes' own
  `preview.open` desktop-ui event (`tools/desktop_ui.py`), so the preview pane
  opens automatically with the driven URL. No plugin.js needed; no CDP attach
  to the webview (forbidden-by-design — see
  `research/VISIBLE_BROWSER_RESEARCH.md`). The preview shows the page in the
  desktop's own webview partition; the agent drives its own browser session
  (cookies live there) — pages behind logins look logged-out in the preview.
- **TUI:** when a terminal-browser pane is running, the ladder attaches to it
  and the user watches live (`watch` mode, Round 2). terminal-browser renders
  only in kitty-graphics-protocol terminals (kitty, ghostty, wezterm, tmux,
  vscode, cmux, supacode, herdr — NOT iTerm2/Terminal.app); installing
  terminal-browser does not install a terminal.
- **Headless:** default for cron/background; the daemon session persists for
  reuse.

## Safety model

The driver shares a Chromium with your other tabs. Enforced in code, not
prompts:

- **Tab lease**: default `--tab new` only; `--target` is an explicit opt-in.
- **Detach-only close**: `Browser.close()` detaches the CDP session but never
  calls `Target.closeTarget` — on terminal-browser's Electron runtime that
  destroys `webContents` under the TUI's `ViewRegistry` and crashes
  `PageHost.blurContent` with `TypeError: Object has been destroyed`.
  (Discovered the hard way; upstream's `Target.createTarget` is also
  unsupported there — TUI tabs are opened via `terminal-browser new-tab`.)
- **No focus stealing**: never `Target.activateTarget`; focus emulation is
  enabled on the owned session only (keeps background rAF alive without
  switching the visible tab).
- **No viewport override**: `Emulation.setDeviceMetricsOverride` is skipped —
  the pane's real viewport is used as-is.
- **Target filtering**: non-`page` targets, `chrome://`, `devtools://`,
  `chrome-extension://`, and workers are skipped. `target=_blank` pop-ups do
  not join the session and are treated as `BLOCKED`.
- **Never** run `terminal-browser shutdown` — it kills every pane's browser.
- Shared cookies/profile: the driver sees the browser's profile. Don't point
  it at logged-in sessions you don't want automated.

## Decision protocol notes

- The TypeSafe-shaped body `{model, state, questions}` is sent to
  OpenRouter's decisions endpoint (`typesafe/jev-1.13`); object instructions
  and object criteria values won in the probe (HTTP 200) — no stringify
  fallback needed.
- Context budget: 3 target heads × ~250 rows + 6K text + rules approach but
  survive the 32K input cap at N=120 with truncated criteria labels. Do not
  raise the 250-action cap. A two-call operation/target fallback exists in
  the design but was never needed; ship it only if a live overflow survives
  truncation.
- Local models (Laya/oMLX via a localhost `DECISION_GATE_URL`) remain future
  work: the N-way choice contract is unvalidated on local models.
