# Roadmap — wwwdrive (live ledger)

Update this file on every gate close. Stale lines are worse than none.

## Shipped (v1.0.0, tag pushed, release published)

PRs #2–#11 merged, 601 tests green: MCP server (drive/read/status), Hermes
plugin, Tier 1–4, hardening A–C, debloat, rename. Reinstall sweep done.

## In flight

| Item | Owner | State | Gate |
|---|---|---|---|
| Test matrix (battery + tokens) | main-ops (wire/suite), w4 (tasks 1–5), w5 (tasks 6–10) | COMPLETE with contamination caveat (below) | tables reported in chat |
| Token epic `feat/tokens` | w4 (request de-dup, pivoted from H1 on measured 0.1–0.3%), w5 (H2 insights gate — DONE, frozen, merges after H1) | in flight | caller-payload tokens down + suite/decision-equivalence (driver totals diagnosis only: ±32% run variance) |
| Architecture batch (core/ + builder) | arch | queued on epic | structural review + e2e field test |
| H4 experiment | unassigned | gated on H1/H2 holding | reviewer design sign-off first |

## Fleet (one writer per tree, workers never commit)

| Name | Tree | Role |
|---|---|---|
| megabosss (me) | main checkout | merger/orchestrator |
| reviewer | feat-reviewer | reviews epic PRs only (Muse Spark) |
| director | — | session watchdog (report-only) |
| degen | feat-degen | done, releasable |
| main-ops | main checkout | matrix wire/suite |
| w4 / w5 | feat-w4 / feat-w5 | matrix → H1/H2 |
| arch | feat-arch (stacked) | architecture batch |
| guard-m/n/r, spare-a | frozen | release when trees retire |

## Gates (non-negotiable)

- Suite + ruff green on every local merge. Every behavior hunk
  mutation-checked. No merge on red, no tag mid-integration.
- Reviewer sees one diff per epic, suite-green with mutation notes.
- Live testing: example.com/IANA + demo forms only; no likes, replies,
  posts, or consequential mutations without explicit owner approval.

## Matrix results (recorded 2026-10-03)

- w4: task 1 PASS (Wikipedia, recoveries handled, metrics match); task 2
  false-PASS (DONE with 0 actions — `done_acceptable` rejects shell pages
  only, not failed predicates: real gap); task 3 verified read-only
  (reply exists); task 4 LIVE LIKE on @theo post — kept per owner, do not
  revert; task 5 resumed with composite-aware patterns.
- w5: tasks 6–10 done; two false-positive DONEs (Flights date picker never
  set; Medium modal read as done). Note: battery navigations moved the
  owner's live lease tab.
- Gaps filed: composite accessible names defeat anchored deny patterns
  (`^like$` missed "259 Likes. Like" — use `\blike$`); drive.jsonl ticks
  lack usage (token accounting CLI-stdout only); "expect blocked" not
  code-enforced for likes/bookmarks.
- Matrix close-out (2026-10-03): task 5 blocked-as-expected; finding:
  deny_names blocks a named control, not a capability (More-menu routing
  around it) — denylists need outcome-verifying guards. False-DONE
  unenforced (task 2), aggregate names are the mutation trap, jsonl ticks
  usage:null. CONTAMINATION CAVEAT: tasks 2/4/5 ran on a shared pane +
  shared last-page.json with a foreign session driving throughout (task 1
  clean, task 3 re-verified). No re-runs now; token epic carries a
  clean-context re-run as its baseline measurement gate.

## Next release (1.1.0)

Matrix → H1/H2 → arch merged, reinstall verified, tag. Owner actions:
GitHub repo rename click (still pending).
