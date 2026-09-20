# jev-terminal-browser-driver

<!-- VIDEO: drag docs/demo.mp4 into the GitHub web editor while editing this
README, then replace the HTML comment below with the generated
https://github.com/user-attachments/assets/... URL on its own line.
The caption below assumes the video sits right above it. -->
<!-- PASTE-VIDEO-URL-HERE -->

*Real run — Google Flights, Zürich → London, one-way, Oct 15 2026: 14
autonomous ticks, 8.4 s, ~$0.0025 in Jev spend, zero screenshots.*

**Drive a real browser with a cheap typed-decision model — no screenshots, no
a11y dumps, no 50K-token snapshots.**

A port of [browser-use/jev-ultrafast](https://github.com/browser-use/jev-ultrafast)'s
agent loop to [terminal-browser](https://terminal-browser.dev) (a Chromium
rendered in your terminal), with all decisions answered by
[Jev](https://docs.typesafe.ai/introduction) (`typesafe/jev-1.13`) via
**OpenRouter's decisions API** — no TypeSafe API key required.

One CLI call runs a full observe → choose → act → re-observe loop and prints
one compact JSON line per tick. Your agent's context receives ~200 bytes per
step instead of an element dump.

```
{"status": "ready", "url": "…/flights/search?tfs=…", "last_action": "Search",
 "elapsed_ms": 8378, "usage": {"input_tokens": 4173, "output_tokens": 312,
 "cost": 0.000175266}}
```

Real run (Google Flights, Zürich → London, one-way, Oct 15 2026): the loop
filled both comboboxes, switched Round trip → One way, picked the date in the
calendar, and hit Search — **14 ticks, 8.4 s, ~$0.0025 in Jev spend**, zero
screenshots, zero snapshots in the agent's context.

## How it works

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

### Execution chain in detail

1. **Tab lease.** `drive.py` sets a module-level lease on `jev_driver.browser`
   *before* constructing `Agent`. Default `--tab new` opens a TUI-visible tab
   via `terminal-browser new-tab` and attaches by `targetId`. `--target <id>`
   attaches to a tab you name explicitly. The driver never attaches to tabs it
   didn't create.
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
   pages raise and the loop re-observes instead.
5. **Loop.** `jev_driver/agent.py` (upstream, verbatim) repeats
   observe → choose → act until the model picks `DONE`/`BLOCKED` or
   `--max-steps`. Model `DONE` is *not* treated as success — verify the
   resulting URL or page content yourself.

### CDP transport

`jev_driver/cdp.py` is a minimal websocket CDP client. It discovers the port
dynamically (`terminal-browser ls --all --json` → `cdpPort`; don't hardcode
it), fetches `/json/version` → `webSocketDebuggerUrl` (a bare `ws://host:port`
does not connect), and connects with **`suppress_origin=True`** (Electron
returns 403 otherwise). It preserves the upstream helper's contract:
unwrapped `result` payloads, `RuntimeError` on CDP errors, no `sessionId` on
`Target.*` methods, session id only on `Runtime./Page./Input./Emulation.*`
after a flatten attach.

## Install

Requirements: macOS (or Linux), Python ≥ 3.12, [uv](https://docs.astral.sh/uv/),
a running [terminal-browser](https://terminal-browser.dev) instance, and an
OpenRouter API key.

```bash
git clone <this-repo> jev-terminal-browser-driver
cd jev-terminal-browser-driver
uv sync
# auth: DECISION_GATE_API_KEY, else OPENROUTER_API_KEY in env or ~/.hermes/.env
```

## Usage

```bash
# One-tick smoke test on a local fixture
uv run python scripts/drive.py \
  --goal 'Click the Widget link' \
  --url "file://$(pwd)/fixtures/click.html" \
  --tab new --max-steps 5

# A real multi-step task
uv run python scripts/drive.py \
  --goal 'Search one-way flights from Zurich to London departing October 15, 2026, one adult, economy. Stop when matching flight options are visible.' \
  --url 'https://www.google.com/travel/flights?hl=en' \
  --tab new --max-steps 14

# Attach to a specific tab you own (explicit opt-in)
uv run python scripts/drive.py --goal '...' --target <cdpTargetId>
```

Each tick prints `{"status", "url", "last_action", "elapsed_ms", "usage"}`.
Exit `0` on `done`, `1` on `blocked`/error/budget. **Model `DONE` is not
success** — check the URL or page text independently.

### Offline tests

```bash
uv run pytest        # 36 tests, no network, no live browser
```

## Measured performance

| Scenario | Result |
|---|---|
| N-way click choice, N ≈ 20 (20 cases) | 20/20 correct, p50 451 ms, ~2.5K in-tokens, ~$0.0001/decision |
| N-way click choice, N ≈ 120 (8 cases) | 8/8 correct, p50 514 ms, ~10.7K in-tokens, ~$0.00045/decision |
| Google Flights end-to-end (14 ticks) | 8.4 s wall, ~$0.0025 total Jev spend |
| Context cost per tick (agent side) | one ~200-byte JSON line |

No 32K context overflow observed at N=120 — the single-request multi-question
body with truncated criteria labels holds. (A two-call operation/target
fallback exists in the design but is not shipped; it is only needed if a live
overflow survives truncation.)

## Safety model

The driver shares a Chromium with your other tabs. Enforced in code, not
prompts:

- **Tab lease**: default `--tab new` only; `--target` is an explicit opt-in.
- **Detach-only close**: `Browser.close()` detaches the CDP session but never
  calls `Target.closeTarget` — on terminal-browser's Electron runtime that
  destroys `webContents` under the TUI's `ViewRegistry` and crashes
  `PageHost.blurContent` with `TypeError: Object has been destroyed`.
  (Discovered the hard way; upstream's `Target.createTarget` is also
  unsupported there — tabs are opened via `terminal-browser new-tab`.)
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

## Hermes skill

`SKILL.md` is a Hermes Agent skill (`description` ≤ 60 chars, when-to-use
vs plain `terminal-browser action` snapshots, pitfalls, verification). The
key instruction: the agent runs `scripts/drive.py` and reads JSON lines — it
never pulls the element table or a screenshot into its context.

## Local models (future work)

Because every decision is a single request to a configurable
`DECISION_GATE_URL`, pointing it at a local typed-decision server (e.g. a
Laya/oMLX endpoint on localhost, as supported by
[decision-gate](https://github.com/nedzen/decision-gate)) should work with a
URL/model swap — free and private per decision at 0.03 s latency.

**This use-case is not addressed yet.** The N-way operation/target choice
protocol (choice heads with object criteria, full probability distributions)
is a materially harder contract than decision-gate's binary gating, and we
have not validated that a local model can carry it at the accuracy this
driver needs. We may address it in the future; until then, Jev on OpenRouter
is the only supported decision backend.

## Credits

Built on [browser-use/jev-ultrafast](https://github.com/browser-use/jev-ultrafast)
(© 2026 Browser Use, MIT). `jev_driver/snapshot.js`, `jev_driver/agent.py`,
and `jev_driver/questions.py` are used verbatim from upstream under the MIT
License; `browser.py`, `model.py`, and the decision transport are reworked for
terminal-browser and OpenRouter's decisions API. See `LICENSE` and the headers
in those files.

## License

MIT — see [LICENSE](LICENSE).
