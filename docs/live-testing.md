# Live testing

## Offline (always)

```bash
uv sync --group dev
uv run pytest          # no network, no browser, no ~/.cache writes
uv run ruff check .
```

Expect ~970+ passed, a few skipped (live cmux pane checks). Failures that need
a real `terminal-browser` binary are bugs in test isolation — unit provision
tests must mock `resolve_terminal_browser`.

### Suite map

| Area | Files |
|---|---|
| Finish/stop gates | `test_low_confidence`, `test_end_state_guard`, `test_stop_verdict`, `test_stop_framing` |
| Agent loop / budgets | `test_time_budget`, `test_agent`, `test_freshness` |
| Plugin / MCP | `test_plugin_handler`, `test_mcp`, `test_adapter_parity`, `test_core` |
| Provisioning | `test_discover`, `test_cmux_provision` |
| Process evidence | `test_processes` (imports `jev_driver.instances`) |
| Live harness (fake MCP) | `scripts/live/test_harness.py` |

Full name index: [test-catalog.md](test-catalog.md).

### Mutation rule

New behaviour needs a test that fails when the production hunk is reverted.
Tautological tests that pass with or without the change should be deleted.

## Live S-batch

Requirements: `terminal-browser` on PATH, kitty-graphics terminal (or cmux),
`TYPESAFE_API_KEY`, idle pane (no other drive holding the lease). Quiescent
cmux workspace — ambient splits pollute leak assertions.

```bash
uv run python -m scripts.live --dry-validate
uv run python -m scripts.live                   # S2a, S4a-chain, S7c, S8b
uv run python -m scripts.live S2a S7c S4a-chain
uv run python -m scripts.live --ledger /tmp/ledger.jsonl
```

Manifests: `scripts/live/manifests/`. Prefer `S4a-chain` over bare `S4a` (a
single no-url drive after quarantine is `no_page`, not continuity).

The harness waits for `drive.jsonl` to go quiet (~2s) between tests and before
a CRASH retry — otherwise every test after the first fails isolation.

Slices → `/tmp/wwwdrive-runs/<run_id>.jsonl`.  
Ledger → [live-ledger.jsonl](live-ledger.jsonl) (append-only JSONL).

### Outcome classes

| Class | Meaning |
|---|---|
| HIT / HIT-recovered | End state matched (rescue path for the latter) |
| FALSE-DONE | **sev-1** — done without the declared end state (must stay 0) |
| BLOCKED-unjustified | Model blocked on a satisfiable goal (sev-2) |
| BLOCKED-honest | Blocked on an unsatisfiable goal |
| CRASH | Driver/harness error — includes `no_page` |
| STALL | Timeout / no tick progress |

Void CRASH retries are ledger rows but excluded from the scoreboard.

### Manual paste prompts

Ten host-facing prompts with pass lines live in git history under the former
`docs/test-plan.md` / `docs/mcp-agent-test-guide.md` (see [CHANGELOG](../CHANGELOG.md)
era). MCP smoke: `tools/list`, then `drive` with `{}` → `goal is required`.
Serial MCP: one `tools/call` at a time; no cancel mid-drive. No hot-reload after
pull — restart the server.

## Distilled learnings

### Scoreboard (window 2, post-isolation — counts)

From `docs/live-ledger.jsonl` with `window == 2` (24 scored rows):

| Outcome | n |
|---|---|
| HIT | 7 |
| FALSE-DONE | 2 |
| BLOCKED-unjustified | 12 |
| BLOCKED-honest | 1 |
| MISS | 2 |

Window 1 (~52 rows) is retained in the JSONL for history but superseded for
scoreboard purposes (pre-isolation / pre-sev-1 / pre-movedon gates).

Regenerate counts:

```bash
uv run python -c "from scripts.live.runner import summarize_ledger; print(summarize_ledger())"
```

### Per-site patterns

| Site | Pattern |
|---|---|
| CoinMarketCap | Nav HIT easy (named currency); exchange/rank goals → FALSE-DONE or unjustified-block |
| GitHub | Search/trending/commit often BLOCKED-unjustified; notifications HIT; release notes need `/releases/tag/vX.Y.Z` |
| X | Feed/home → MISS (budget) or unjustified-block; stable status URLs can HIT |
| Kalshi / Polymarket | Unjustified-block cluster; short loading pages need wait-before-scroll; dynamic event URLs |
| Wikipedia | Early FALSE-DONE → HIT after zero-action / evidence gates |

### False-done causes (sev-1)

1. Zero-action / premature DONE without end state → `ZERO_ACTION_DONE_MIN` 0.95
2. Auto-scroll laundering zero-action onto acted floor → `history.auto` (#23)
3. Host-name token match (`coin` ∈ `coinmarketcap.com`) → whole-word, host excluded (#26)
4. Mid-band acted DONE (0.6–0.8) without goal evidence → `goal_evidenced` required
5. Isolation VOID: re-attach inherited foreign tab → quarantine + log_dir match

### Threshold calibration evidence

- Legit already-satisfied: DONE 1.00 / 0.99 (S1d, S6a)
- False zero-action: 0.81 / 0.69 / 0.60 (S7b, S5b, M15)
- Mid-band wrong page: DONE 0.68 × 5 acts (S2a window-2)

Do not retune from a single run. Design write-ups: [decisions.md](decisions.md).

### Harness gotchas

- Live pane tests are ambient-sensitive; clean cmux = trustworthy green.
- `NESTED_CMUX` fixtures use fake paths — do not add socket-exists guards on
  `_in_cmux_context` (breaks routing tests).
- Routing tests must `delenv` `CMUX_*` / `HERDR_*` (or pass explicit env).
