# Decisions

Short ADR-style entries. Newest relevant decisions first. Full history lives in
git; this file keeps the *why* that product code no longer narrates.

## Stop gates (sev-1 calibration)

### D1 — Zero-action DONE floor 0.95

- **Context:** Live sev-1 rows: legitimate already-satisfied DONEs scored 1.00 /
  0.99 (S1d, S6a); false zero-action DONEs scored 0.81 / 0.69 / 0.60 (S7b, S5b,
  M15) — all ≥ `DONE_MIN` (0.6).
- **Decision:** `ZERO_ACTION_DONE_MIN = 0.95` when `model_action_count == 0`.
- **Why:** A zero-action DONE claims the page was already the end state →
  near-certainty. Prefer a false blocked (sev-2) over a false done (sev-1).
- **Rejected:** Raising `DONE_MIN` globally; accepting mid-0.90 genuine
  zero-action DONEs (accepted false-negative risk).

### D2 — Auto-scrolls do not count as model actions (#23)

- **Context:** After a rejected zero-action DONE, look-further auto-scroll
  entered history and laundered the next DONE onto the acted floor (S7c).
- **Decision:** Driver corrective scrolls are tagged `history.auto = True`;
  `model_action_count` ignores them.
- **Rejected:** Counting all history kinds equally.

### D3 — Whole-word goal evidence; host excluded (#26)

- **Context:** Token `coin` matched host `coinmarketcap.com`; wrong-page DONE
  cleared the nav bypass (S2a).
- **Decision:** Whole-word match; URL **path/query** only (never host) as
  evidence, plus title/text/history labels.
- **Rejected:** Substring `token in evidence`; full URL including host.

### D4 — Mid-band acted DONEs need goal evidence

- **Context:** S2a window-2 accepted DONE 0.68 after five actions on the wrong
  exchange page under plain `DONE_MIN`.
- **Decision:** Acted DONEs in `[0.6, 0.8)` require `goal_evidenced`
  (`ACTED_DONE_EVIDENCE_MAX = 0.8`) inside `verdict()`.
- **Rejected then reversed:** Declining this as breaking DONE_MIN-after-action
  (M16 unit table) — live S2a forced the tighten. Policy lives in `verdict()`,
  not in bare `done_acceptable`.

### D5 — Collapse finish/stop into Evidence + verdict()

- **Context:** Finish/stop logic was scattered; BLOCKED look-further order
  drifted (short loading pages without `scroll_down` blocked instead of waiting).
- **Decision:** `readiness.Evidence` + `verdict()` own policy; `DriveAgent`
  owns I/O (`_hydrate` / `_auto_scroll` shared by weak DONE and BLOCKED).
- **Rejected:** Treating missing `scroll_down` as immediate give-up; rebuilding
  Evidence via `**ev.__dict__` to force rescue branches.

### D6 — BLOCKED end-state rescue is one-sided

- **Context:** Model chose BLOCKED on a page that already matched the goal;
  callers looped on `model_blocked`.
- **Decision:** Rescue to done only when `end_state_reached` (moved_on, non-shell,
  every step evidenced, progressed ≥ steps).
- **Why:** False done is worse than honest blocked.

## Product boundary

### D7 — TUI-visible only; never headless

- **Context:** Discovery ladder fell through to agent-browser / headless when
  Herdr env hid panes; sites (x.com) 403’d headless; nested herdr panes hid the
  browser from the user.
- **Decision:** Explicit CDP → running TB pane → daemon DB → provision visible
  split → **raise**. Never launch a hidden browser.
- **Rejected:** agent-browser daemon, loopback port scan, quieter fallbacks.
  Scrubbing `HERDR_*` alone is insufficient inside a herdr pane — refuse via
  `instances.root_terminal_blocker`.

### D8 — Desktop Hermes preview is not driveable

- **Context:** Packaged app closes remote debugging; no plugin door to
  `webContents.debugger`.
- **Decision:** Do not CDP-drive the desktop preview webview. Product is the
  visible terminal-browser pane.
- **Rejected:** Desktop true-drive; mirror iframe as the primary contract.

### D9 — Drive is a one-viewport click-path actor

- **Context:** Tall multi-viewport extraction goals (e.g. leaderboard scrape)
  always hard-BLOCKED (no cross-tick memory, hydration races, scroll vs budget).
- **Decision:** Out of envelope → use `read` with `scrolls`, or an API.
- **Rejected:** Accumulation scratchpad as default product shape.

### D10 — CDP / Electron pitfalls (still true)

- Open tabs via `terminal-browser new-tab`; **detach-only** close.
- Never `Target.closeTarget` / `activateTarget` / device-metrics override
  (TUI `PageHost` crash).
- CDP websocket needs `suppress_origin=True`.
- Driver `#jev=<ns>` fragment is stripped from freshness markers.

## Layout (this handoff)

### D11 — Module seams

- **Decision:** `browser.py` + `lease` / `probe` / `ops`; `DriveAgent` owns the
  loop (`agent.py` is `Agent = DriveAgent`); `instances.py` folds former
  processes+lifecycle; drop unused live `regress` / baseline path.
- **Why:** Reviewable seams; one loop owner; less dead surface.
- **Rejected:** Production reaper without a caller; keeping dual Agent/
  DriveAgent as separate upstream-verbatim forever.

## Adapter & measurement

### D12 — MCP imports plugin schemas; descriptions still asserted

- Schemas cannot drift by construction; description strings are still parity-
  tested so a hand-edited MCP list cannot silently diverge.

### D13 — Gate metric is caller-ingested bytes/call

- Jev token totals vary ±32% and are diagnosis-only. Scoreboard gate is
  payload B/c + suite green + decision-sequence equivalence.
- Shipped: H1 request de-dup (`WWWDRIVE_REQUEST_DEDUP`, opt-in); H2 insights
  explicit-only (~1.8KB/call saved).

### D14 — Enter-submit stays narrow

- Offer Enter for filled fields or `searchbox` role only. Broadening to any
  focused textbox was declined (blind submit on comments/checkout).
