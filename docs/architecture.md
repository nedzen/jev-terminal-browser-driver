# Architecture

How `drive` / `drive.py` works: the execution chain, the decision
protocol, the CDP transport, and the safety model. Paths relative to the repo
root unless noted.

## The plugin/driver boundary

```
plugin/                      Hermes loads this; jev_driver is NOT importable here
  __init__.py                tool schemas + register(ctx)
  handler.py                 subprocess adapter: argv, the child, the JSON payload
  core/                      the stdlib-only leaf, imported by BOTH sides
    env.py                   driver_home, has_decision_key, terminal_browser_installed,
                             check_drive, LOG_DIR, log_handler_event
    budgets.py               caps + budget()/deny_names(): reject, never clamp
    result.py                build_tick_row() and compact_result(), one field table
scripts/drive.py → jev_driver/cli.py  (imports plugin.core.result to emit ticks)
scripts/mcp.py   → plugin.handler + jev_driver.preflight (MCP adapter)
```

The dependency runs one way. Hermes loads `plugin/` without `jev_driver`
installed, so nothing under `plugin/core/` may import outside the standard
library and `plugin/`; `tests/test_core.py` walks the ASTs to keep that true.
`jev_driver` importing `plugin.core.result` is the allowed direction — core is
stdlib-only, so it costs the driver no dependency.

`plugin/handler.py` is a shell: every rule it used to own now lives in core,
and it re-exports the public names (`compact_result`, `driver_home`,
`TIME_BUDGET_CAP`, …) because callers and tests hold those names.

### One result builder, one field table

A tick row (what the driver prints) and the agent result (what `compact_result`
folds those rows into) are two views of a single declaration, `PASSTHROUGH` in
`plugin/core/result.py`. `build_tick_row` and `compact_result` both read it, so a
field added there is carried by both with no second edit.

This is a bug class, not a style preference: `final_view` and
`omitted_actions` were each added to the row and nearly dropped on the way into
the result, each needing a separate regression test to notice.
`tests/test_core.py::test_a_novel_declared_field_survives_the_round_trip` pins
the property itself — a field nobody has written a case for still arrives.

A tick row carries `status`, `url`, `last_action`, `elapsed_ms`, `usage` plus
whatever `TICK_OPTIONAL` declares. `cli.tick_record(snap, **overrides)` passes
caller-owned fields (a `max_steps` budget, a `time_budget` stop, a final
re-read) through the same builder; an override of `None` means "nothing to
override" and leaves the snapshot's value alone. `degenerate` and `insight` are
row-only — the result aggregates them across ticks rather than copying the last
row's copy.

### The run-log record

`plugin/core/trace.py` holds the third record: what survives in
`~/.cache/wwwdrive/drive.jsonl` after the process exits. `cli.trace_fields`
delegates to `build_trace_record(row, decision, goal=goal)`, so the log's field
set is declared once (`TRACE_FIELDS`) instead of being a dict literal in the CLI.

It is a separate table from `PASSTHROUGH` because the two records disagree
where it would be least visible:

- a tick row and an agent result **omit** a field with nothing to say; a trace
  record writes the key with an explicit `null`. A log reader asks "was this
  run's reason recorded?", and `absent` does not answer that the way `null`
  does. `TraceField.present` is the flag that keeps the two apart, and
  `final_view` is the one conditional field — an empty one would claim the
  driver re-read the page when it never did.
- the trace caps page text at its own `TRACE_PAGE_TEXT` (1500) and reads ranked
  heads at `TRACE_PROBS_LIMIT` (8), against the row's 2000 and 4. The log is
  read by a human scanning a file; the row is read by an agent on every step.
- the row is the record's base, so a field added to a table is filled from the
  row by default and only the four the log computes itself (`event`, `goal`,
  `ranked_ops`, `ranked_targets`) are substituted.

`write_event` and the redaction vocabulary stay in `jev_driver/runlog.py`.
That module is the sanitize-and-append boundary, it also serves
`redact_for_wire` (a property of an outgoing request body, not of this record),
and `processes.py`, `metrics.py` and `model.py` reach into its module globals.
Moving it into the leaf both adapters load would put a wire concern there and
break every module that patches those globals. The seam is left where it is: the
core builds the record, `runlog` decides whether it may touch disk.

## Envelope

`drive` is a **one-viewport click-path actor**: forms, wizards, filters,
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
   (remembered in `~/.cache/wwwdrive/last-page.json`) and navigates it only
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

   **Freshness, and the driver's own URL fragment.** Before any input the marker
   is compared against the one the decision was made from; a difference means the
   page moved, and the decision is discarded rather than executed. The marker
   embeds `location.href`, and the driver appends `#jev=<time_ns()>` to every new
   tab's URL so a re-opened tab is distinct in CDP's target list. That fragment
   is the driver's own bookkeeping, not site state, and it is not stable across
   calls - so both comparisons (`MARKER`, and `page_key`, which reaches
   `location.href` by a different route) normalize it away first. Without that, a
   difference the driver authored itself, with an unchanged `performance.timeOrigin`
   and an unchanged page, was reported as a stale page: the 2026-10-02T22:29 and
   22:41 runs on example.com ended `click_not_sent` after two `field_changed` on
   a link that was fully actionable. Only `jev=<digits>` inside the fragment is
   dropped; a site fragment (`#section`, `#/route/2`) is the site moving and
   still counts.
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

The driver keeps one tab. `~/.cache/wwwdrive/last-page.json` holds
`{targetId, url, source, browser_id, ts}` for the tab it created, including
fixture pages. The next run re-attaches to that target when it is still
alive, in the same browser, and younger than 30 minutes. Passing `url`
navigates that tab (`Page.navigate`). It does not open another one. A new
tab is created only when the pointer is missing, expired, or the target is
gone. Explicit `--target` still wins. A dead id is not replaced by an
unrelated tab; if another live tab has the same URL stem, that tab is
reused, otherwise a new tab is opened.

Runs append to `~/.cache/wwwdrive/drive.jsonl` and `drive.log`: the goal,
each tick's ranked operations and labeled targets, and a `blocked` record
with why and a short page excerpt.

### Hydration, scroll, repeat-guard

After navigate (and after click/select/fill), `Browser.observe` may re-read
the page up to 3 times if the action count is still low or visible text is
still growing (`HYDRATE_*` class attributes; tests inject a zero sleep).
A page with no sentence yet, only short labels, is not ready: reading
continues for up to 8 rounds. A `DONE` below 0.6, or a `DONE` on that
label-only page, is not success — unless the run has already navigated and
`DONE` is still the strongest operation, in which case `_reject_weak_done`
accepts it (`drive_agent.py:591`). That bypass is how a sub-0.6 `DONE` ends
a run that reached its end state, so the threshold binds only runs that never
moved off their start URL. A covered
fill target is focused and typed into. Fill freshness follows that field,
not the rest of the page, so a changing feed does not cancel a search box.
`Press Enter` is offered as its own action once a field can carry a
submit: either it holds text (the original rule — a filled field is one the
run already committed to), or it is a `searchbox`, which gets it with nothing
typed so a search page with no Search button still has a submit path. The
`searchbox` role comes from `input[type=search]` and nothing else, so it is a
fact about the element rather than a guess read off a label. Jev never implies
the key. Two decisions that still execute nothing stop the
run. The log records each act (`via` pointer, focus, wheel, or enter), each
rejected `DONE`, and each stale attempt. Stops carry `reason` plus the
visible text. Screenshot goals return that text immediately and do not act.
Scroll wheel delta is `innerHeight * 0.8` at CDP dispatch; `snapshot.js`
still reports 560. `DriveAgent` exempts advancing `SCROLL_*` from the
upstream 3-repeat hard-block (`agent.py` stays verbatim). Tick JSON may
include `degenerate: true` when the top operation probability is &lt; 0.6
with a &lt; 0.1 gap to the runner-up.

### Debug HUD

`--debug` follows the Hermes plugin setting `plugins.entries.wwwdrive.settings.debug` unless a call passes `debug` explicitly. It injects
`jev_driver/hud.js` into the **owned** tab only. The overlay root is `aria-hidden`, so snapshot.js does not index it. It is not `inert`, so the corner panel can be clicked.

What it shows: a green outline on the chosen element, red outlines on the other candidates, and a bottom-right panel (backdrop blur) that collapses to a one-line chip. Expanded, it shows the goal, why, ranked operations, ranked hits, recent steps, and token spend. The same trace is also written to `~/.cache/wwwdrive/drive.log`.

Under `--debug` the same facts are copied into each tick as `insight` and
aggregated on the result as `insights` plus a final `why`. Gating today is
MCP-only: `scripts/mcp.py` passes `insights` from an explicit opt-in
(`WWWDRIVE_DEBUG=1`), and the default result carries everything else
unchanged. The Hermes native path and the CLI always include the trace —
neither the plugin `debug` setting nor `--debug` gates it (a uniform gate
is an open follow-up, not this change). The ranked facts themselves always
reach `~/.cache/wwwdrive/drive.log`, so nothing is lost from the evidence
path. A `DONE` tick reports `last_action: DONE` and that decision's own
token usage, not the previous click.

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
- **`Press Enter` is scoped to search submits.** `browser_operation` handles
  `kind: "enter"` by dispatching a bare `keyDown`/`keyUp` with no node, no
  `evaluate`, and no hit-test — unlike click and fill, which resolve the node
  and hit-test it. It is therefore a *focus-scoped, untargeted* keypress: it
  lands on whatever the browser has focused, not on the field the model chose.
  Two consequences, both load-bearing:
  - Enter is offered on a `searchbox` even when empty, because submitting a
    search navigates to a result list and mutates nothing.
  - Enter is **not** offered on an empty non-search field. The audited-and-
    rejected widening was "any focused/fillable text field": that would put a
    blind keypress in reach of every comment box, checkout and message field on
    the page, where the same keypress submits the enclosing form. `deny_names`
    is not a mitigation for that — it filters by *label*, and the control's
    label is the constant `"Press Enter"` on every page, so a caller who wants
    Enter gone must deny that one string; there is no per-form granularity to
    configure. A `textbox` labelled "Search" and an autocomplete `combobox` are
    both excluded for the same reason: the label is a guess, and a combobox's
    Enter selects a suggestion that can carry a form with it.
  - Withdrawing the feature wholesale is possible and cheap: `deny_names:
    ["^Press Enter$"]` removes the control from the action space.
- Shared cookies/profile: the driver sees the browser's profile. Don't point
  it at logged-in sessions you don't want automated.

## Decision protocol notes

- The body `{model, state, questions}` goes to TypeSafe's `/v1/systemone`.
  It started on OpenRouter's decisions endpoint (`typesafe/jev-1.13`), which
  still works with `DECISION_GATE_URL` and `OPENROUTER_API_KEY`. Object
  instructions and object criteria values are accepted; no stringify
  fallback is needed.
- **The wire is redacted; the log's size caps are not.** Page text, element
  labels and the goal are third-party content, and a credential rendered into
  any of them (a filled token field, `api_key=…` in a settings dump) would
  otherwise be shipped to the provider. Both request bodies — `choose` and
  `field_text` — go through `runlog.redact_for_wire` first, which applies the
  run log's secret vocabulary and nothing else: no string clip, no list cap,
  no depth truncation, because clipping the evidence would shorten the prompt
  the decision is made from while still reporting success. The `request`
  recorded on a decision is the scrubbed body that actually went out.
- The reported `model` id is validated against a bounded plain-identifier
  pattern before the answer is acted on. It is provider-controlled and is
  recorded as provenance and rendered by the CLI, so free text in that slot is
  a provider writing into the run log. A missing or malformed id refuses the
  response instead of being recorded; the refusal does not echo the value.
- Context budget: 3 target heads × ~250 rows + 6K text + rules approach but
  survive the 32K input cap at N=120 with truncated criteria labels. Do not
  raise the 250-action cap. A two-call operation/target fallback exists in
  the design but was never needed; ship it only if a live overflow survives
  truncation.
- Local models (Laya/oMLX via a localhost `DECISION_GATE_URL`) remain future
  work: the N-way choice contract is unvalidated on local models.
