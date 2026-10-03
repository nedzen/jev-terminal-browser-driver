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
| H4 experiment | unassigned | CONDITIONAL design sign-off by reviewer (direction sound, gate under-specified; 7 conditions incl. frozen per-tick A/B, live all-10-green + tick counts, keyframe rationale, adversarial pages, suite+mutation, gated rollout, ceiling restated) — future H4 PR must prove each |

## Fleet (one writer per tree, workers never commit)

| Name | Tree | Role |
|---|---|---|
| megabosss2 | main checkout | merger/orchestrator (old megaboss wJ:p23 retired by owner, 2026-10-02 — token cost) |
| ceo | wJ:p20 | plenary supervisory/decision authority (owner out for good); successions logged in FLEEET.md |
| god | wJ:p2Z (ex-ceo) | owner's personal check-in point only — passive, never a report target |
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

## Next release (1.1.0) — SHIPPED 2026-10-03

- [x] Arch merged (PR #13) → reinstall verified → abridged release gate →
  tag v1.1.0 (`3a8121b`, pushed) → release notes published.
- Owner actions still pending: GitHub repo rename click; founder/intern
  profile re-link + settings re-entry.

## Release gate 1.1.0 (2026-10-03, CLOSED as-is per owner decision — owner out, authority CEO)

- Reinstall: PASS — `scripts/install_plugin.sh` re-run on ca1ce07 working
  tree; `~/.hermes/plugins/wwwdrive` symlink fresh, no stale jev-driver in
  the default home. OPEN (owner action, untouched): named profiles `founder`
  and `intern` still link the old `jev-driver` — needs the per-profile
  re-link + settings re-entry.
- Wire contract (raw stdio): PASS — initialize negotiates 2024-11-05,
  serverInfo wwwdrive **1.1.0** (version bumped in mcp.py + plugin.yaml +
  pyproject.toml + uv.lock), tools/list exactly {drive, read, status},
  `drive {}` → success:false / "goal is required" / verified:null,
  status → ready:true all checks ok, no key material.
- Suite + ruff: PASS — 657 passed, ruff clean (locked 0.16.8, repo root,
  re-verified on the final version-bumped tree before commit).
- Live read-only IANA drive: **NOT GREEN after 3 attempts** (read-only, no
  mutations; goal `Go to example.com and click the 'More information...' /
  'Learn more' link, then stop.`): run 1 — self-inflicted staleness loop
  (driver's `#jev=` URL marker changes the page between observe and act →
  `field_changed` ×2 → `click_not_sent`); run 2 — click FIRED, final_url
  `iana.org/help/example-domains` (the plan's end state) but model chose
  BLOCKED instead of DONE → `model_blocked`; run 3 (22:49:33Z,
  run_id `bcbce170`) — click FIRED, `stale 0`, same correct end URL,
  model again BLOCKED (`BLOCKED 0.62 | DONE 0.26`) → `model_blocked`.
  Suspects: backend model stop-protocol behavior (owner's configured backend
  changed since 1.0.0 shipped) + observe/act URL-marker mutation (possible
  pre-existing bug class). NO mid-release hotfixes.
- Owner decision (relayed via CEO, owner now OUT FOR GOOD): ship v1.1.0
  as-is on reinstall + wire-contract + suite 657 + ruff green, with this
  gap recorded honestly. No descope-by-default; this is an explicit
  owner override of the live-drive gate item only.
- Post-1.1.0 work (each needs its own cycle + reviewer sign-off):
  (a) stop-protocol mislabel investigation — V2 FROZEN PROBE DONE (main-ops,
  16/16 posts, no browser): variant A (failing wording) 0 DONE / 8 BLOCKED,
  DONE mean 0.1938 (0.17–0.22); variant B (good wording) 8 DONE / 0 BLOCKED,
  DONE mean 0.6475 (0.62–0.67). Non-overlapping (gap 0.40); 6-token input
  control (689 vs 683) proves only the goal string moved. H1 CONFIRMED —
  goal framing alone crosses DONE_MIN 0.6. H4 EXONERATED — model id
  jev-1.13.0 + spec hash + endpoint fixed across the probe, gap persists.
  H2 structural (no BLOCKED-side guard: `_reject_weak_done` exits for any
  non-DONE choice, `_look_further` needs targets — correct URL, no path to
  done). H3 geometry flake orthogonal + still open. `_moved_on` :591 hatch:
  the 21:30 gate-green run passed at DONE 0.59 < 0.6 via the escape, not the
  threshold. Incidental: reported confidence INVERTS vs chosen-option
  probability (blocked runs 0.56–0.67 vs done runs 0.24–0.35 — calibration
  trap); `model_version` null in responses (no server-side drift detection
  from response shape). V2 raw JSON PERSISTED at `/tmp/v2-frozen-probe.json`
  (16 samples, re-emitted verbatim, parsed + verified: 8×A_failing BLOCKED,
  8×B_good DONE; model jev-1.13.0, spec c81014bb333236aa, model_version null
  all 16). Caveat: request bodies + full frozen text not captured (only
  frozen_text_len 200) — re-emittable on request.
- V3 DONE_MIN-bypass map (arch, refs verified on disk): THREE paths to done
  below (or without) DONE_MIN — B1 `:591` `_moved_on` (any DONE argmax after
  any URL change; test pins acceptance at 0.36, no lower bound, URL-unchanged
  rejection unpinned); B2 confidence fallback (`readiness.py:78-81`: DONE
  absent from head + choice DONE → scored on `confidence` vs the same 0.6);
  `:463` `_would_undo` already-followed (no model DONE, no probability —
  deliberate, tested). Guard interaction: `_stop_low_confidence` first;
  `degenerate` (top<0.6, gap<0.1) stops diffuse DONE but not peaked
  confidently-mediocre DONE. Doc impact: `architecture.md:180` false as
  written (true only for never-navigated runs); release-test-plan §2 never
  records DONE p so Path-B greens pass unnoticed; battery rows 1–2 likely
  Path-B, rows 6/8/9 leave the threshold untested for non-navigating goals.
- ACCEPTED post-1.1.0 fix backlog (CEO final ruling 2026-10-03), priority
  order, PENDING→ACCEPTED (each: own cycle + reviewer sign-off): P1 DONE
  stop-framing (`questions.py:14` observable end-state criterion) — MERGED
  (PR #14, 76adb6a; reviewer approved after marker post; 665 green + ruff
  clean re-verified on merged main; merger probe: A DONE 0.19→0.50, B
  0.65→0.95 — partial mitigation, P2 still needed). Reviewer suggestions
  carried as follow-up: test file trailing newline; DONE-clause hedge for
  multi-step goals (P2 input). P2 H2 BLOCKED-side guard (correct-URL
  rescue) — MERGED (PR #15, 1dcbcf3; reviewer approved with marker; 691
  green + ruff clean re-verified on merged main; all-of guard,
  exhaustion-only hook, metrics vocab +1, no schema change); P3 bundled with P1 (see above);
  P4 H3 geometry (quantify `not_actionable`, then fix) — FILED;
  P5 `model_version` + confidence instrumentation — MERGED (PR #16, d21b4bb;
  reviewer approved with marker; 700 green + ruff clean re-verified on merged
  main; request-id provenance + BLOCKED-legibility logging, logging-only).
  P4 code (items 1–4) MERGED — see close-out below; items 5–6
  deferred follow-ups. No tags/releases for these (accumulate toward future
  1.1.1/1.2.0, CEO's call).
- P4 code (items 1–4) MERGED (PR #17, bb78ab1; reviewer approved with marker;
  729 green + ruff clean re-verified on merged main via scratch worktree;
  merger caught + closed a mutation gap pre-PR: _probe_reason ordering now
  pinned by integration test). Items 5–6 deferred follow-ups.
  (b) `#jev=` marker observe/act staleness (run 1 evidence, intermittent —
  did NOT reproduce on run 3) — filed, NOT started.
- Owner-only pending, untouched: GitHub repo rename click; `founder` /
  `intern` profile re-link + settings re-entry.

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
