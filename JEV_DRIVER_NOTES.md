# Jev driver notes

Working notes for the terminal-browser Jev driver. Probe recorded **before** the model adapter.

## How to run

```bash
cd /Users/marius/Projects/jev-terminal-browser-driver
uv sync
uv run python scripts/drive.py \
  --goal 'Click the Widget link' \
  --url "file://$(pwd)/fixtures/click.html" \
  --tab new --max-steps 5
```

Auth: `DECISION_GATE_API_KEY` else `OPENROUTER_API_KEY` (env or `~/.hermes/.env`).
Jev: `POST https://openrouter.ai/api/alpha/decisions` model `typesafe/jev-1.13`.
TYPE_TEXT: OpenRouter `/chat/completions`, `inception/mercury-2.5`, `TEXT_MODEL_REASONING=none`.
CDP port: `terminal-browser ls --all --json` → `cdpPort` (not hardcoded). Browser websocket from `/json/version` → `webSocketDebuggerUrl`. Handshake must **omit Origin** (`suppress_origin=True`) or Electron returns 403.

Offline: `uv run pytest`. Do not import `jev_ultrafast`.

## Probe (OpenRouter decisions)

Recorded **before** `jev_driver/model.py` existed.

- Endpoint: `https://openrouter.ai/api/alpha/decisions`
- Model: `typesafe/jev-1.13`
- Time: 2026-09-21 00:49:00 +0530
- Winning body shape: **object_instructions_and_criteria** (HTTP 200). No stringify fallback.
- Chat/completions for Jev: not probed (OpenRouter rejects this model there).

```json
[
  {
    "shape": "object_instructions_and_criteria",
    "http_status": 200,
    "latency_ms": 429,
    "valid_choice": true,
    "choice": "CLICK",
    "confidence": 1,
    "usage": {
      "input_tokens": 602,
      "output_tokens": 55,
      "cost": 2.5284e-05
    },
    "model": "typesafe/jev-1.13-20260917",
    "error_excerpt": null
  }
]
```

Shipped adapter: same `{model, state, questions}` body; object `instructions`; **short string** criteria (decision (a) truncation; rich rows live in `state.elements`). One request, independent operation + per-op target heads. No two-call fallback (no 32k overflow after truncation).

## E2E (independent URL check, not model DONE)

- One tick, `fixtures/click.html`, goal “Click the Widget link”: last `url` `…/click.html#widget`, `last_action` Widget, then DONE. ~1.2 s, ~1121 input tokens, ~$4.7e-5 per decision.
- Multi-tick, `fixtures/search.html`: TYPE_TEXT Query → CLICK Go → CLICK Widget result → `…#widget-result`. ~2.6 s. TYPE_TEXT via Mercury on OpenRouter.
- Zero screenshots (`screenshots=False`).
- User target ids for hindsight (`FD0ED7B5…`) and Laya (`127.0.0.1:8790`) were not closed.

## Token cost per tick (live)

| Step | input_tokens | output_tokens | cost USD | latency_ms |
|---|---:|---:|---:|---:|
| Probe 5-option choice | 602 | 55 | 0.000025 | 429 |
| Click-fixture decision | 1121 | 76 | 0.000047 | ~400 |
| Search-fixture (per head, ~3 acts) | 1533–1672 | 108–114 | ~0.000066 | ~400 |
| Bench N≈20 mean | 2550 | 214 | 0.000107 | p50 451 |
| Bench N≈120 mean (8 cases) | 10672 | 1035 | 0.000448 | p50 514 |

No 32k overflow at N=120 (~11k input). Two-call fallback not used.

## CDP / shared browser

- `Target.createTarget` on this Electron: **Not supported**. Tabs are opened with `terminal-browser new-tab --browser <key>`.
- **Do not `Target.closeTarget`** on those tabs. Chromium destroys `webContents` while terminal-browser `ViewRegistry` still has a `PageHost`. Next focus/blur throws:

  `TypeError: Object has been destroyed` at `PageHost.blurContent` (`setActive` → `blurContent`).

  `Browser.close()` only `Target.detachFromTarget`. TUI tabs stay until the human closes them.
- Do not `Target.activateTarget` / `--follow` (steals focus and is on the crash path).
- No `Emulation.setDeviceMetricsOverride` (pane ~2176 px tall). Keep `setFocusEmulationEnabled` on the owned session.
- CDP websocket: `suppress_origin=True` (403 otherwise).
- `Target.setDiscoverTargets` on connect. `/json/list` is the reliable page list; TUI `ls --json` can show ghost rows after a CDP close.
- Denylist: never attach to `hindsight.vectorize.io` unless `--target` is explicit.

## Viewport / N-way

`snapshot.js` only indexes on-screen nodes. A 120-link single column overflowed the pane (**N≈51**). Compact `column-count` + attaching to an **existing** leftover tab (no `new-tab`, no `closeTarget`) produced **N=120** clicks at 575×1058. See `bench_nway.md`.

## Hermes plugin (feature/hermes-plugin)

- Symlink: `~/.hermes/plugins/jev-driver` → repo `plugin/`.
- `plugins.enabled` includes `jev-driver` in default `~/.hermes/config.yaml` (left installed).
- Offline tests: 45 passed (`uv run pytest`). Auto-provision ladder covered by `tests/test_discover.py`.
- `hermes plugins list` was started; it spun up local llama-server and dumped the full catalog (truncated before user plugins in the captured head). Discovery of the symlink is filesystem-confirmed. A full `hermes chat -q` jev_drive round-trip was not recorded here (llama-server startup noise); HQ can run:

  `hermes chat -q 'Use jev_drive to click the Widget link on the click fixture.'`

- Headless auto-provision: unit-tested (`--session jev-driver`). Not re-launched in this session (avoid extra Chromium after the PageHost crash).

## What we will not do

- `terminal-browser shutdown`
- `api.typesafe.ai`
- Laya
- Hardcoded TYPE_TEXT values
- Mixing agent-browser `@eN` with Jev `eN`
