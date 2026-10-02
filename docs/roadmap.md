# Roadmap — wwwdrive (live ledger)

Update this file on every gate close. Stale lines are worse than none.

## Shipped (v1.0.0, tag pushed, release published)

PRs #2–#11 merged, 601 tests green: MCP server (drive/read/status), Hermes
plugin, Tier 1–4, hardening A–C, debloat, rename. Reinstall sweep done.

## In flight

| Item | Owner | State | Gate |
|---|---|---|---|
| Test matrix (battery + tokens) | main-ops (wire/suite), w4 (tasks 1–5), w5 (tasks 6–10) | COMPLETE with contamination caveat (below) | tables reported in chat |
| Token epic `feat/tokens` | w4 (request de-dup), w5 (H2 insights gate) | MERGED (PR #12) — H1 bench −17.5/−21.4%, H2 −1.8KB/call, 625 green | caller-payload gate held; driver totals diagnosis-only |
| Architecture batch (core/ + builder) | arch (wR:p1) | MERGED (PR #13, ca1ce07) — batches A+B, one epic review, 657 green | §6 verify done (suite/ruff/mutation, merger spot-checks B5×2/B6/B8 killed); reviewer approved after M1 withdrawn (delta verdict, 2026-10-02) |
| H4 experiment | unassigned | gated on H1/H2 holding | reviewer design sign-off first |

## Fleet (one writer per tree, workers never commit)

| Name | Tree | Role |
|---|---|---|
| megabosss2 | main checkout | merger/orchestrator (old megaboss wJ:p23 retired by owner, 2026-10-02 — token cost) |
| ceo | wJ:p2Z | oversight: watches verdicts/panes, co-owns finish pipeline |
| reviewer | feat-reviewer (wS:p1) | reviews epic PRs only (Muse Spark 1.3 Contributor; pane formerly mislabeled guard-r, renamed) |
| degen | feat-degen | done, releasable |
| main-ops | main checkout | matrix wire/suite |
| w4 / w5 | feat-w4 / feat-w5 | frozen-done (token epic merged, PR #12) |
| arch | feat-arch (stacked) | done — architecture batch merged |
| guard-m/n, spare-a | frozen | release when trees retire |

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

Arch merged (PR #13) → reinstall verified → abridged release gate
(fresh-install parity, wire contract, one live read-only IANA/example.com
drive, suite+ruff) → tag v1.1.0 → release notes. Owner actions:
GitHub repo rename click (still pending).

## Process notes (2026-10-03)

- PR #13 minors follow-up (reviewer-approved, same pattern as a24042f):
  m1 stale `cli._target_labels` comment; m2 `label_of` duplicated in
  core/trace.py + drive_agent.py (4 lines, kept deliberately); m3 Hermes
  gating doc overclaim in docs/architecture.md; m4 runlog `_text_lines`
  renderer pin (declared fields reach JSONL, not the human log line).
- Reviewer protocol rule learned: lint extractions must carry the root
  pyproject.toml — without it ruff misresolves src roots and misclassifies
  `plugin.core.*` as third-party, producing false I001s (M1, withdrawn).
- Merger corollary: lint/suite claims are only evidence with the locked
  binary (uv.lock ruff 0.16.8) run from the repo root on the PR head.
