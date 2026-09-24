# Architecture

How `jev_drive` / `drive.py` works: the execution chain, the decision
protocol, the CDP transport, and the safety model. Paths relative to the repo
root unless noted.

## Envelope

`jev_drive` is a **one-viewport click-path actor**: forms, wizards, filters,
logins, in-view navigation. Verified successes (click fixture, Google Flights
form flow) are all single-viewport. Aggregation/extraction over long
multi-viewport pages ("scan, collect, rank" — e.g. artificialanalysis.ai
leaderboards) is out of envelope; use fetch/HTML/API. Findings:
`docs/research/JEV_DRIVE_BLOCKED_DEBUG_20260921.md`.

## Pipeline

```
scripts/drive.py
  → set tab lease (new | explicit --target)     # safety, before anything
  → Agent(url, goal)                            # jev-ultrafast agent.py, verbatim
       Browser.observe   → snapshot.js run in-page via CDP Runtime.evaluate
                           (element table + ≤6K visible text + freshness guards)
       model.choose      → POST api.typesafe.ai/v1/systemone
                           {model: jev-1.13.0, state, questions}
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
   *before* constructing `Agent`. It re-attaches to the driver's own tab
   (remembered in `~/.cache/jev-driver/last-page.json`) and navigates it only
   when `url` names another page. Only when that tab is gone does it open a
   TUI-visible tab via `terminal-browser new-tab`. With no `url` and no tab it
   returns `no_page` instead of guessing.
   `Target.createTarget` is not available on this Electron. `--target <id>`
   attaches to a tab you name explicitly. The driver never attaches to tabs
   it didn't create.
2. **Observation.** `jev_driver/snapshot.js` (upstream, verbatim) runs inside
   the page: indexes visible interactive elements (buttons, links, inputs,
   selects, ARIA roles), assigns stable IDs (`e1…e250`), records per-element
   guards (value/checked/context) and a page marker, and reads at most 6,000
   chars of visible text. Actions are capped at 250.
3. **Decision.** `jev_driver/model.py` posts the state + goal to TypeSafe
   (`https://api.typesafe.ai/v1/systemone`, model `jev-1.13.0`, bearer
   `TYPESAFE_API_KEY`). `DECISION_GATE_URL` overrides the endpoint. The response must be a full probability distribution
   over the offered choices (sum ≈ 1, argmax == choice) or nothing executes.
   Independent question heads are a safety property: a `CLICK` decision
   physically cannot consume a `TYPE_TEXT` or `SELECT` target id.
4. **Action.** `jev_driver/browser.py` re-validates the target immediately
   before input (connected, visible, enabled, not covered, geometry on-screen)
   and executes via raw CDP input events. Mutations are never retried; stale
   pages raise and the loop re-observes instead. After date/combobox-class
   clicks, a bounded overlay wait stabilizes the observed element set (the
   date-picker/autocomplete stall class) before the next predict.
5. **Recovery (`jev_driver/drive_agent.py`).** Freshness is per kind: scroll,
   wait, and DONE need only the same document; a click needs the same target
   with numbers ignored (live counts and relative times tick constantly); a
   fill needs the same field. A covered target is scrolled into view once.
   BLOCKED on a loading page waits; elsewhere it scrolls up to three times to
   look for the target. A toggle whose label or checked state changed after
   our click is not clicked again (`toggle_undo`). A same-label link that
   already led to a new URL is not followed again (`already_followed`),
   except pagination. A top-ranked DONE is accepted once the run has reached a
   new URL. After `Page.navigate` the driver waits for the new document, and
   observation waits up to 10 s for a document that is still loading.
5. **Loop.** `jev_driver/agent.py` (upstream, verbatim) repeats
   observe → choose → act until the model picks `DONE`/`BLOCKED` or
   `--max-steps`. Model `DONE` is *not* treated as success — verify the
   resulting URL or page content yourself.

### Session continuity

The driver keeps one tab. `~/.cache/jev-driver/last-page.json` holds
`{targetId, url, source, browser_id, ts}` for the tab it created, including
fixture pages. The next run re-attaches to that target when it is still
alive, in the same browser, and younger than 30 minutes. Passing `url`
navigates that tab (`Page.navigate`). It does not open another one. A new
tab is created only when the pointer is missing, expired, or the target is
gone. Explicit `--target` still wins. A dead id is not replaced by an
unrelated tab; if another live tab has the same URL stem, that tab is
reused, otherwise a new tab is opened.

Runs append to `~/.cache/jev-driver/drive.jsonl` and `drive.log`: the goal,
each tick's ranked operations and labeled targets, and a `blocked` record
with why and a short page excerpt.

### Hydration, scroll, repeat-guard

After navigate (and after click/select/fill), `Browser.observe` may re-read
the page up to 3 times if the action count is still low or visible text is
still growing (`HYDRATE_*` class attributes; tests inject a zero sleep).
A page with no sentence yet, only short labels, is not ready: reading
continues for up to 8 rounds. A `DONE` below 0.6, or a `DONE` on that
label-only page, is not success. A covered
fill target is focused and typed into. Fill freshness follows that field,
not the rest of the page, so a changing feed does not cancel a search box.
Once a field holds text, `Press Enter` is offered as its own action. Jev
never implies the key. Two decisions that still execute nothing stop the
run. The log records each act (`via` pointer, focus, wheel, or enter), each
rejected `DONE`, and each stale attempt. Stops carry `reason` plus the
visible text. Screenshot goals return that text immediately and do not act.
Scroll wheel delta is `innerHeight * 0.8` at CDP dispatch; `snapshot.js`
still reports 560. `DriveAgent` exempts advancing `SCROLL_*` from the
upstream 3-repeat hard-block (`agent.py` stays verbatim). Tick JSON may
include `degenerate: true` when the top operation probability is &lt; 0.6
with a &lt; 0.1 gap to the runner-up.

### Debug HUD

`--debug` follows the Hermes plugin setting `plugins.entries.jev-driver.settings.debug` unless a call passes `debug` explicitly. It injects
`jev_driver/hud.js` into the **owned** tab only. The overlay root is `aria-hidden`, so snapshot.js does not index it. It is not `inert`, so the corner panel can be clicked.

What it shows: a green outline on the chosen element, red outlines on the other candidates, and a bottom-right panel (backdrop blur) that collapses to a one-line chip. Expanded, it shows the goal, why, ranked operations, ranked hits, recent steps, and token spend. The same trace is also written to `~/.cache/jev-driver/drive.log`.

The same facts are copied into each tick as `insight` and aggregated on the
plugin result as `insights` plus a final `why`. A `DONE` tick reports
`last_action: DONE` and that decision's own token usage, not the previous click.

### Takeover (watch mode)

`jev_driver/takeover.py` defines `WatchAgent` — a thin subclass that keeps
`agent.py` verbatim and changes only the tick policy: `browser.fresh()` is
checked before predict, after predict, and on `StalePage`; any mismatch while
watching yields `blocked` with reason "user took over the browser" instead of
re-observing and continuing. The user's mouse wins; the driver never
force-navigates a surface the user moved.

## Discovery ladder (which browser gets driven)

Scope is Hermes `--tui` plus a visible terminal-browser pane. No headless
Chromium, no agent-browser daemon, no loopback scan, no desktop preview.
`jev_driver/discover.py` resolves a CDP endpoint in this order:

1. Explicit `--cdp` URL, or `JEV_CDP_URL` / `BROWSER_CDP_URL` env.
2. A running terminal-browser instance (`terminal-browser ls --all --json`
   → `cdpPort`).
3. The terminal-browser daemon SQLite record
   (`~/.local/share/terminal-browser-*/terminal-browser.db`), because `ls`
   from a no-TTY Hermes subprocess cannot see a pane that belongs to another
   terminal. The port is checked against `/json/version` before use.
4. Provision a visible pane: `terminal-browser open <url> --split right
   --no-merge`. The child env has every `HERDR_*` variable removed so the
   split is created by the real terminal (cmux, ghostty, kitty, …) and not
   nested inside a herdr pane. The CDP port comes from the JSON record
   `open` prints.
5. Raise `WatchUnavailable`. There is no quieter fallback.

`watch=true` uses that same ladder. The only extra behavior is takeover:
if the page changes under the driver, the run stops with "user took over
the browser" instead of continuing. See `docs/research/TUI_ONLY_DISCOVERY_FIX.md`
for why the old headless ladder was removed. The older writeup in
`archive/iteration-1-cli/JEV_DRIVER_NOTES.md` that mentions
`discover._child_env()` and `AGENT_BROWSER_ENGINE` describes code that is
gone.

## CDP transport

`jev_driver/cdp.py` is a minimal websocket CDP client. It discovers the port
dynamically (never hardcode it), fetches `/json/version` →
`webSocketDebuggerUrl` (a bare `ws://host:port` does not connect), and
connects with **`suppress_origin=True`** (Electron returns 403 otherwise). It
preserves the upstream helper's contract: unwrapped `result` payloads,
`RuntimeError` on CDP errors, no `sessionId` on `Target.*` methods, session id
only on `Runtime./Page./Input./Emulation.*` after a flatten attach.

## Visibility

One surface: a terminal-browser pane in a kitty-graphics terminal (kitty,
ghostty, wezterm, tmux, vscode, cmux, supacode — not iTerm2 or Terminal.app).
Installing terminal-browser does not install a terminal. The Hermes desktop
preview pane is not used; research on it
(`research/VISIBLE_BROWSER_RESEARCH.md`,
`research/DESKTOP_PLUGIN_RESEARCH.md`,
`research/DESKTOP_BUTTON_RESEARCH.md`) is historical and not the current
contract.

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

- The body `{model, state, questions}` goes to TypeSafe's `/v1/systemone`.
  It started on OpenRouter's decisions endpoint (`typesafe/jev-1.13`), which
  still works with `DECISION_GATE_URL` and `OPENROUTER_API_KEY`. Object
  instructions and object criteria values are accepted; no stringify
  fallback is needed.
- Context budget: 3 target heads × ~250 rows + 6K text + rules approach but
  survive the 32K input cap at N=120 with truncated criteria labels. Do not
  raise the 250-action cap. A two-call operation/target fallback exists in
  the design but was never needed; ship it only if a live overflow survives
  truncation.
- Local models (Laya/oMLX via a localhost `DECISION_GATE_URL`) remain future
  work: the N-way choice contract is unvalidated on local models.
