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

## How it works (summary)

The pipeline: `drive.py` sets a tab lease → the verbatim upstream `Agent` loop
runs `snapshot.js` in-page (element table + ≤6K visible text), asks Jev via
OpenRouter's decisions API (operation + target in one request, independent
heads, strict probability validation), acts via hit-tested raw CDP input, and
repeats until `DONE`/`BLOCKED` or the tick budget. A small LLM writes text
only for `TYPE_TEXT`. The full execution chain, the browser-discovery ladder,
and the CDP transport contract live in
[docs/architecture.md](docs/architecture.md).

## Install

Requirements: macOS or Linux, Python ≥ 3.12, [uv](https://docs.astral.sh/uv/),
the [terminal-browser](https://terminal-browser.dev) **program** on `PATH`
(not the terminal-browser skill), and `OPENROUTER_API_KEY`.

From Hermes, once this repo is installed as the `jev-driver` plugin, call the
`jev_drive` tool. The browser opens as a visible pane. Pass `debug: true` only
when you want score outlines. Pass `background: true` with `cdp_url` only when
you want to attach to a browser you already started hidden. This driver does
not launch a hidden browser.

```bash
git clone https://github.com/nedzen/jev-terminal-browser-driver
cd jev-terminal-browser-driver
uv sync
# OPENROUTER_API_KEY in the environment or ~/.hermes/.env
```

The plugin finds `scripts/drive.py` next to itself. It does not look in
`~/Projects`.

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

Each tick prints `{"status", "url", "last_action", "elapsed_ms", "usage", "why"}`.
With `--debug` it also prints `insight`.
Exit `0` on `done`, `1` on `blocked`/error/budget. **Model `DONE` is not
success** — check the URL or page text independently.

## Hermes plugin

The same loop is a native Hermes tool `jev_drive` (toolset `jev`) for
`hermes --tui`. The plugin process never imports `jev_driver`; it shells out
to `uv run python scripts/drive.py --json`. Desktop preview and headless
Chromium are out of scope.

```bash
./scripts/install_plugin.sh            # ~/.hermes/plugins/jev-driver
./scripts/install_plugin.sh work       # also ~/.hermes/profiles/work/plugins/
hermes plugins enable jev-driver
```

Named profiles do **not** inherit the default-home plugin dir — symlink each
profile you care about. `plugins.enabled` is per home.

Discovery is visible-or-fail. Order: explicit `--cdp` / `JEV_CDP_URL` →
a running terminal-browser pane (`ls` or the daemon SQLite record, so a
no-TTY Hermes subprocess can still see the pane) → `terminal-browser open
<url> --split right --no-merge` with every `HERDR_*` variable stripped, so
the split lands in the real terminal (cmux, ghostty, …) instead of a nested
herdr pane. If that cannot happen, the tool returns `blocked` and names the
reason. It never falls through to headless Chromium. It never calls
`Target.closeTarget` on a TUI tab.

`debug` is off unless Plugins settings or `debug: true` / `--debug` turn it on.
The owned tab then gets a collapsible bottom-right panel plus outlines:
chosen element in green, other candidates in red. The tool result adds `why`
and `insights` (operation, labeled target, confidence, top probabilities).
Model `DONE` is still not success.

Tool fields: `goal` (required), optional `url` (navigates the driver's
existing tab; a new tab only if that tab is gone), `target`, `max_steps`
(cap 30), `cdp_url`, `timeout_s`, `debug` (omit to use the plugin setting), `watch`.
`watch: true` does not change visibility; it yields if the user changes the
page. Prefer `jev_drive` over pasting snapshots into chat. Each run appends
to `~/.cache/jev-driver/drive.jsonl` and `drive.log` (goal, ranked hits,
blocked reason).

terminal-browser needs a kitty-graphics terminal (kitty, ghostty, wezterm,
tmux, vscode, cmux, supacode — not iTerm2/Terminal.app). Installing it does
not install a terminal. If opening the pane fails, the error includes
terminal-browser's own diagnostics.

Omit `--url` / `url` to stay on the current page of that same tab. The final
tool result includes `page_text` (visible snapshot text, max 2000 chars) on
`done`/`blocked`, and `browser.log` is the path of the JSONL log.

### Offline tests

```bash
uv run pytest        # offline; no network, no live browser
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

## Safety model (summary)

The driver shares a Chromium with your other tabs. Enforced in code, not
prompts: tab lease with denylist (`--tab new` default, `--target` opt-in),
detach-only close, no focus stealing, no viewport override, target filtering
(non-`page`/`chrome://`/`devtools://`/extensions/workers skipped), and never
`terminal-browser shutdown`. Full list with the crash stories behind each rule:
[docs/architecture.md](docs/architecture.md) § Safety model. Shared
cookies/profile: don't point the driver at logged-in sessions you don't want
automated.

## Docs index

| Doc | Covers |
|---|---|
| [docs/architecture.md](docs/architecture.md) | Execution chain, decision protocol, discovery ladder, CDP transport, safety model, visibility surfaces |
| [docs/README.md](docs/README.md) | Docs index |
| [docs/research/](docs/research/) | One current note (`TUI_ONLY_DISCOVERY_FIX.md`); the rest is historical |
| [docs/archive/iteration-1-cli/](docs/archive/iteration-1-cli/) | Pre-plugin iteration record (live-ops notes, N-way bench) |
| [docs/maintainer-notes.md](docs/maintainer-notes.md) | Maintainer notes. Not loaded by Hermes. |
| [docs/plugin-catalog-entry.yaml](docs/plugin-catalog-entry.yaml) | Draft catalog entry. Not submitted. |
| [HANDOFF.md](HANDOFF.md) | Session-continuation state (status, pitfalls, TODO) |

## What the agent sees

The agent contract is the `jev_drive` tool description. There is no skill to
enable, and the agent should not read the plugin source. `terminal-browser`
is the installed program that draws the pane. Its skill is not a dependency.

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
