# How to test

## Offline (always)

```bash
uv sync --group dev
uv run pytest          # no network, no browser, no ~/.cache writes
uv run ruff check .
```

Expect ~1000 passed, a few skipped (live cmux pane checks). Failures that
need a real `terminal-browser` binary are bugs in test isolation — unit
provision tests must mock `resolve_terminal_browser`.

### What the suite covers

| Area | Files |
|---|---|
| Finish/stop gates | `test_low_confidence`, `test_end_state_guard`, `test_stop_verdict`, `test_stop_framing` |
| Agent loop / budgets | `test_time_budget`, `test_agent`, `test_freshness` |
| Plugin / MCP | `test_plugin_handler`, `test_mcp`, `test_core` |
| Provisioning | `test_discover`, `test_cmux_provision` |
| Process evidence | `test_processes`, `test_lifecycle` (both import `jev_driver.instances`) |
| Live harness (fake MCP) | `scripts/live/test_harness.py` |

Full name index: [test-catalog.md](test-catalog.md).

## Live S-batch (needs a pane + keys)

Requirements: `terminal-browser` on PATH, kitty-graphics terminal (or cmux),
`TYPESAFE_API_KEY`, idle pane (no other drive holding the lease).

```bash
uv run python -m scripts.live --dry-validate    # offline schema check
uv run python -m scripts.live                   # all default manifests
uv run python -m scripts.live S2a S7c S4a-chain
uv run python -m scripts.live --ledger /tmp/live-ledger.md   # optional override
```

Manifests live in `scripts/live/manifests/`. The default suite prefers
`S4a-chain` over bare `S4a` (a single no-url drive after quarantine is
`no_page`, not continuity).

Slices → `/tmp/wwwdrive-runs/`. Ledger rows append to
[live-learnings.md](live-learnings.md).

### Interpreting outcomes

| Class | Meaning |
|---|---|
| HIT / HIT-recovered | End state matched (rescue path for the latter) |
| FALSE-DONE | sev-1 — done without the declared end state |
| BLOCKED-unjustified | Model blocked on a satisfiable goal |
| BLOCKED-honest | Blocked on an unsatisfiable goal |
| CRASH | Driver/harness error — includes `no_page` |
| STALL | Timeout / no tick progress |

## Manual paste prompts

Host-facing checklist (ten prompts): [test-plan.md](test-plan.md).
Agent-driven paste version: [mcp-agent-test-guide.md](mcp-agent-test-guide.md).

## Mutation rule (contributors)

New behaviour needs a test that fails when the production hunk is reverted.
Tautological tests that pass with or without the change should be deleted.
