# Architecture (≈10 minutes)

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
  cli.py                argparse, lease, tick loop entry
  drive_agent.py        finish/stop gates, HUD, retries
  agent.py              upstream-verbatim act/predict loop (do not merge)
  readiness.py          DONE thresholds, end_state_reached, Evidence+verdict()
  browser.py            Browser class; re-exports lease/probe/ops for callers
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

## Finish / stop (the gates that matter)

| Gate | Threshold / rule |
|---|---|
| `DONE_MIN` | 0.6 |
| `ZERO_ACTION_DONE_MIN` | 0.95 when `model_action_count == 0` (auto-scrolls excluded) |
| Nav / rescue bypass | `end_state_reached`: moved_on, not shell, whole-word goal tokens in title/text/URL-**path**/history (host excluded), progressed actions ≥ steps |
| Low confidence | 2 consecutive degenerate ticks |

Calibrated from live sev-1 rows (see `docs/live-learnings.md` and module
comments in `readiness.py`). Do not retune from one run.

## Continuity and panes

- Prefer re-attach; quarantine `last-page.json` between live tests.
- Inside cmux: provision via control socket at **root terminal** level, tabs
  after first pane — never nest inside herdr.
- Elsewhere: `terminal-browser open URL --split right --no-merge`.

## Safety

- Shares the user’s logged-in profile; only drives its own tab.
- Skips `chrome://`, `devtools://`, extensions, workers.
- Credentials in visible text are redacted before the model request
  (`runlog.redact_for_wire`).
- Per-test denylists on R-tier live manifests.

## Logs

- `~/.cache/wwwdrive/drive.log` / `drive.jsonl` — events, never raw secrets.
- Live slices: `/tmp/wwwdrive-runs/<run_id>.jsonl`.

## Where to go next

| Doc | Role |
|---|---|
| [how-to-test.md](how-to-test.md) | Offline pytest + live S-batch |
| [known-faults.md](known-faults.md) | Open product/fleet faults |
| [live-learnings.md](live-learnings.md) | Scoreboard ledger (raw rows) |
| [roadmap.md](roadmap.md) / [upstream-ideas-backlog.md](upstream-ideas-backlog.md) | Plans |
| [archive/](archive/) / [research/](research/) | Historical intel (later synthesis pass) |
| [archive/architecture-long.md](archive/architecture-long.md) | Pre-handoff long form of this doc |
