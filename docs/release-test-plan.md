# Release test plan — wwwdrive 1.0.0

Runs after the rename merges and local consumers are reinstalled. Every
item live, wall-clock noted, evidence pasted. Goal: stable version gate.

## 0. Fresh install (README-only)

Follow README install on a clean consumer with no prior config. Record
every step that deviates from the doc; fix the doc, not the memory.

## 1. MCP wire contract (raw stdio, no browser)

- initialize negotiates a supported protocol version.
- tools/list returns exactly {drive, read, status} with canonical schemas.
- drive with {} → goal-required error, stopped_reason error, verified null,
  isError true.
- status → ready:true, all checks ok, no key material.

## 2. Live drives (visible pane)

- IANA task (example.com → Learn more): status done, final_url on iana.org,
  stopped_reason model_done, final_view present, verified null.
- Wikipedia task (search string trimmer → article): done + final_view.
- Starved budget (max_steps 1): blocked + action_budget, reason preserved.
- Bad budgets (99 / 1.5 / 0): rejected pre-spawn, isError, no browser launched.
- time_budget_s:1 on IANA task: blocked + time_budget between steps.

## 3. Read path

- read example.com (outline) → success.
- read article + script (first paragraph) → JSON string.
- Background read (background + cdp_url of shared browser) → success,
  source explicit.

## 4. Debug overlay

- Live drive with default settings: frosted panel bottom-right, step count,
  operation/confidence, green target ring. Screenshot or describe.
- Opt-out (WWWDRIVE_DEBUG=0): no panel, drive unaffected.

## 5. Agent matrix (same IANA + Wikipedia tasks each)

Hermes (MCP reinstall verified) → OpenRouter session → omp → grok →
dsh reviewer via direct channel. Record per-agent: success, ticks,
input/output tokens (from usage + metrics.json), wall-clock, anomalies.

## 6. Suite + lint + build

pytest all green, ruff clean. (No TS build — plugin deleted.)

## 7. Token baseline for H1/H2

Record per-tick input/output tokens for the Wikipedia task into a table
(append below). This is the before-number for the reduction work.

| date | task | ticks | in-tokens | out-tokens | wall-clock | notes |
|---|---|---|---|---|---|---|
| | | | | | | |

## Sign-off

All green + reviewer approval on the release PR → tag v1.0.0.

## Capability battery (human-parity contract)

Every hypothesis and every fix is gated on this battery: all 10 green AND
total tokens down, or it reverts. End state is matched on final URL +
visible evidence, never DONE alone. Baselines fill in as measured.

| # | task | site | status | ticks | in-tok | out-tok | wall |
|---|---|---|---|---|---|---|---|
| 1 | search + open article | Wikipedia | ✅ done | 4 | 8,983 | 820 | — |
| 2 | stable like | X post page | ✅ done | 2 | 10,933 | 1,102 | — |
| 3 | exact-text reply | X post page | ✅ done, verified verbatim | 6 | 18,035 | 1,736 | — |
| 4 | feed like below fold | X timeline | ❌ blocked ×2 (re-rank churn) | 4 | ~18k/5k | — | — |
| 5 | bookmark via share menu | X timeline | ❌ blocked (same pattern) | 4 | 17,852 | 1,715 | — |
| 6 | form with validation (invalid→valid) | TBD safe form | unmeasured | | | | |
| 7 | infinite-scroll collection (20 posts) | partial: 13/20 in one read+8 scrolls, read-only, no model loop | | | | |
| 8 | date picker | Flights/booking | unmeasured | | | | |
| 9 | login-wall detour | TBD | unmeasured | | | | |
| 10 | cross-site chain (trend→market→odds) | Polymarket | ✅ done, read-only, no model loop | 0 | ~0 | ~0 | — |

Loop: run battery → log faults/edge cases → fix → re-run battery to prove
it. Faults discovered live here until fixed.
