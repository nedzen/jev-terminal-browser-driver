# Architecture

wwwdrive drives a visible [terminal-browser](https://terminal-browser.dev) tab
with [Jev](https://docs.typesafe.ai/introduction) decisions. Agents speak MCP
(or Hermes native tools); they never import the driver.

## Layout

```
plugin/                 Hermes + MCP schemas; stdlib-only core/
  handler.py            spawns scripts/drive.py / read.py; folds JSON ticks
  core/                 env, budgets, result, trace — imported by both sides
scripts/mcp.py          stdio MCP server
scripts/drive.py        → jev_driver.cli
scripts/live/           S-batch harness + manifests/
jev_driver/
  cli.py                argparse; discover / lease / open / tick helpers
  drive_agent.py        observe→decide→act loop, finish/stop gates, HUD
  agent.py              compatibility alias: Agent = DriveAgent
  readiness.py          DONE thresholds, end_state_reached, Evidence+verdict()
  browser.py            Browser class; re-exports lease / probe / ops
  lease.py              tab lease, last-page memory, tab-open helpers
  probe.py              fingerprinting, read-only freshness probe
  ops.py                CDP read/act executor (browser_operation)
  discover.py           provision pane (cmux-first, else terminal-browser --split)
  instances.py          process evidence, spawn notes, herdr/root-terminal rules
  model.py              Jev choose() + typing helper
  questions.py          decision protocol text
```

Dependency rule: `plugin/core/` must stay stdlib-only (`tests/test_core.py`
AST-walks it). `jev_driver` may import `plugin.core`; Hermes must not need
`jev_driver` installed.

## One drive tick

1. **Lease** — attach remembered tab (`~/.cache/wwwdrive/last-page.json`) or
   provision a visible pane. No `url` and no remembered tab → `reason: no_page`
   (not a model BLOCKED).
2. **Observe** — `snapshot.js` in-page: interactive elements + ≤6K viewport text.
3. **Decide** — Jev returns one operation + target (DONE / BLOCKED / click / …).
4. **Act** — real CDP input; stale-page retry once; denylist skips consequential
   labels.
5. **Finish gates** (`readiness.verdict` + `DriveAgent` I/O):
   - two consecutive degenerate spreads → `low_confidence`
   - weak / zero-action DONE → reject; driver may auto-scroll (`history.auto`)
   - BLOCKED → look further (≤3 scrolls) or `end_state_reached` rescue
6. **Result** — `plugin.core.result.compact_result` folds tick rows; stop taxonomy
   in `stopped_reason()`.

## Finish / stop gates

| Gate | Rule |
|---|---|
| `DONE_MIN` | 0.6 |
| `ZERO_ACTION_DONE_MIN` | 0.95 when `model_action_count == 0` (auto-scrolls excluded) |
| Mid-band acted DONE | `[0.6, 0.8)` needs `goal_evidenced` |
| Nav / rescue | `end_state_reached`: moved_on, non-shell, whole-word goal tokens in title/text/URL-**path**/history (host excluded), progressed ≥ steps |
| Low confidence | 2 consecutive degenerate ticks |

Calibrated from live sev-1 rows — see [live-testing.md](live-testing.md) and
[decisions.md](decisions.md). Do not retune from one run.

## Continuity and panes

- Prefer re-attach; quarantine `last-page.json` between live tests.
- Inside cmux: provision via control socket at **root terminal** level, tabs
  after first pane — never nest inside herdr.
- Elsewhere: `terminal-browser open URL --split right --no-merge`.
- CDP: `new-tab` / detach-only close; never `Target.closeTarget` (TUI PageHost
  crash); websocket `suppress_origin=True`.

## Safety

- Shares the user’s logged-in profile; only drives its own tab.
- Skips `chrome://`, `devtools://`, extensions, workers.
- Credentials in visible text are redacted before the model request
  (`runlog.redact_for_wire`).
- Per-test denylists on R-tier live manifests.

## Envelope

Drive is a **one-viewport click-path actor**. Aggregation / extraction over tall
multi-viewport pages is out of envelope — use `read` with `scrolls`, or an API.
Desktop Hermes preview webview is not driveable; the product is TUI-visible only.

## Logs

- `~/.cache/wwwdrive/drive.log` / `drive.jsonl` — events, never raw secrets.
- Live slices: `/tmp/wwwdrive-runs/<run_id>.jsonl`.
- Live ledger: `docs/live-ledger.jsonl` (append-only).

## Where to go next

| Doc | Role |
|---|---|
| [live-testing.md](live-testing.md) | How to run tests + distilled live lessons |
| [decisions.md](decisions.md) | Why the gates and layout look like this |
| [known-issues.md](known-issues.md) | Open faults and gotchas |
| [roadmap.md](roadmap.md) | Open work and parked ideas |
| [../CHANGELOG.md](../CHANGELOG.md) | User-facing version history |
| [../AGENTS.md](../AGENTS.md) | Contributor working agreement |
