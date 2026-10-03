# Known faults (recorded 2026-10-03 for the next agent — all verified on disk/logs)

## Product faults (jev-terminal-browser-driver)
1. **Visible provisioning fails from Hermes-TUI-in-cmux.** `terminal-browser
   open --split` relies on adapter detection, which loses inside the Hermes
   TUI (and worse, nests the browser in a herdr pane). Proven live:
   `cmux new-split right --command 'terminal-browser open <url>'` works and
   lands at root level. Fix in flight: PR #27 (lifecycle + root-terminal
   rule + cmux-first ordering) — verify merge state before building on it.
   Rule: root terminal level ALWAYS, tabs-per-drive after first provision,
   never herdr panes, every provisioning test closes what it opens.
2. **Stray background browsers linger.** Reads attach to invisible instances
   (`terminal-browser ls --all` showed strays); no TTL/reaping. Ordered in
   the lifecycle cycle: visible-by-default, idle-TTL with lease exemption,
   owner-opened browsers exempt.
3. **Stale pane titles mislead** (e.g. megabosss2 kept "Third IANA drive
   attempt" for hours). Cosmetic but caused a real owner complaint.
4. **CLI ergonomics:** `--split-dir=right` (guessed flag) fails silently-ish;
   correct is `--split right`. Consider typo tolerance or clearer `open --help`.
5. **Enter-submit gated too narrow** (press_enter only after filled value in
   snapshot; zero executions ever) — M8/S5d unjustified blocks. PR #25 merged
   the searchbox half; form-field half DECLINED with audit (do not revive
   silently).
6. **Reviewer IANA ritual retired** (a live trivial-page verification ran
   07:54 post-#23). Reviewer is wire-only now; standing redirect must ship
   in every review dispatch. Trivial pages retired suite-wide (v4 scope rule).

## Scoreboard faults (open, from 52-row ledger)
- 24 BLOCKED-unjustified (common cause diagnosed; per-site cycles pending —
  re-verify not_actionable-with-P4-live + M7 unreconciled FIRST, premises
  went stale once already).
- 7 FALSE-DONE rows stand as exhibits; gates merged (#23 zero-action 0.95,
  #26 movedon goal-progress); next window must show zero new (window-2
  already added S7c — loop it).
- X-cluster: no code lever (enumeration + budget-outs) → re-scope-or-defer,
  not more retries. S4a/b VOIDED (quarantine breach, fixed #22).
- N=10s pending on flaky winners; M9 runnable (multi-call merged #21);
  H4 implementation open; V2 re-run open; confidence/model_version gaps open.

## Fleet faults (process, for the next supervisor)
- **Name drift breaks supervision** (watches key on agent names): megabosss2
  drifted to `megaboss2` mid-flight; pin names, report any rename instantly.
- **Pane moves kill watches**: megaboss wJ:p2Y→wJ:p33, god wJ:p2Z→wJ:p34.
  Re-arm loops after any move; settle notifications reach ONLY the arming
  session.
- **Ledger-before-gate**: FLEEET.md once recorded a sweep complete before it
  executed — record on gate close, never in advance.
- **Merge discipline**: suite-green + mutation kills + reviewer marker per
  epic; the single exception was owner-ordered (#27 merged on override with
  verdict pending — check whether the verdict later demanded fix-forward).
- **Silent observer**: god (wJ:p34) gets zero routine traffic; all reporting
  terminates at ceo (wJ:p20). Frozen workers get routing notes only on
  reactivation. W-tier held for owner; X never automated.
