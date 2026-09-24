# DEBUG FINDINGS — jev_drive hard-BLOCKED on data-extraction goals (2026-09-21)

Debugged live against `https://artificialanalysis.ai/models` (multi-section, 19,416px-tall
SPA leaderboard). Every `jev_drive` call returned `status: "blocked"`. Full per-tick trace
captured via a direct `Agent` run (script: `~/.hermes/cache/scratch/jev_trace.py`).

## TL;DR

jev-drive is not broken — it is a **one-viewport actor by design**, and it was given an
**aggregation goal** (scan a 653-row table spread over ~35 viewport-heights and rank rows).
Under that goal type it converges to `BLOCKED` by construction, not by bug. No known-issue
coverage in HANDOFF.md; all four root causes below are new findings.

## Per-tick trace (real run, headless daemon)

| Tick | Decision | Conf (top op) | runner-up | Observation |
|---|---|---|---|---|
| 1 | BLOCKED | **0.99** | WAIT 0.01 | Pre-hydration page: 21 actions, no table content. Defeat declared on a blank-ish page. |
| 2 | SCROLL_DOWN | 0.58 | BLOCKED 0.23 | scroll_y 0 → 560. Page height 19,416px. |
| 3 | SCROLL_DOWN | 0.57 | CLICK 0.22 | scroll_y 560. |
| 4 | SCROLL_DOWN | **0.49** | BLOCKED 0.44 | Confidence collapsed to a coin flip. |
| 5 | CLICK e65 | 0.59 | BLOCKED 0.25 | Grasping at whatever is visible. |
| 6 | BLOCKED | 0.40 | CLICK 0.39 | Coin flip → repeat-guard / give-up. |

## Root causes (priority order)

### 1. No cross-tick accumulation (architectural)
`choose()` (`jev_driver/model.py:115`) receives only: current viewport text (≤6,000 chars,
`snapshot.js:85-93`), the element list, and the last 10 history **labels**
(`model.py:139-141`). There is no memory of content from previous viewports. Any goal of the
form "scan the page, collect/rank items, then decide" is unsatisfiable: rows scrolled past
cease to exist. The agent cannot follow the intended scan → rank-links → load-context →
execute flow, because "load into context" has no channel.

### 2. Hydration race on SPAs
Tick 1 observed the page mid-hydration (21 actions, no data) and chose BLOCKED at 0.99.
`snapshot.js` emits no "content settled" signal, and `NEXT_ACTION`
(`jev_driver/questions.py:13`) actively discourages WAIT ("Recent WAIT actions are not
evidence of loading. Prefer a useful visible control over WAIT."). On JS-heavy SPAs the
first decision is systematically poisoned.

### 3. Scroll granularity vs. page height vs. tick budget
Scroll delta is a fixed 560px (`snapshot.js:103`). On a 19k px page that is ~35 ticks; the
plugin default max_steps is 12-14 and the hard cap 30. The table is never reached, let alone
traversed. HANDOFF already notes the tick-budget squeeze on Google Flights (recommended
14→16); extraction pages are an order of magnitude worse.

### 4. Repeat-guard kills long scroll journeys
`agent.py:154-158`: three consecutive actions with `page_changed=False` (and kind≠wait) →
hard `status="blocked"`. Combined with #3, any journey long enough to need many scrolls has
a high chance of dying mid-way even when progress is being made (e.g. virtualized lists that
don't change `location.href` or trigger the fingerprint for a tick).

## Diagnostic signal worth exposing

**Confidence collapse predicts death 2-3 ticks early.** In the trace, the run was already
dead at tick 4 (SCROLL 0.49 vs BLOCKED 0.44). Heuristic: if top operation probability < ~0.6
AND the top-two gap < ~0.1, the goal is unsatisfiable in the current context — surface that
in the plugin result (e.g. `degenerate: true`) instead of letting the agent flail to BLOCKED.

## Fix options (for the dev to weigh)

1. **Minimal (config-level, no design change):** raise the 6k text cap (snapshot.js is
   "verbatim from upstream" — a raise breaks verbatim-ness; consider an env override) and
   increase scroll delta (560 → ~1.5×viewport). Helps navigation-ish goals; does NOT fix #1.
2. **Design fix — accumulation scratchpad:** driver-side persistent buffer; each tick append
   extracted candidates (links/rows matching goal keywords) and inject the buffer into the
   next `choose()` state (`model.py` body). This is the only change that makes the intended
   scan→rank→execute flow possible. Touches agent.py + model.py + questions.py.
3. **Task-routing fix (no code):** document in SKILL.md/README that jev_drive is for
   click-path goals (forms, wizards, filters, logins). Data-extraction/aggregation over long
   pages should go to fetch/HTML or an API instead. This matches the verified success cases
   in HANDOFF (click fixture, Google Flights form flow) — both single-viewport click paths.
4. **Cheap robustness wins regardless of path:**
   - Wait-for-hydration: after navigate, re-observe once if `n_actions < threshold` or text
     length grows between two observations, before letting the model decide.
   - Scale scroll delta to viewport height (`innerHeight * 0.8`) instead of fixed 560.
   - Exempt `scroll_down` from the 3-repeat guard when `scroll.y` actually advanced.
   - Add `degenerate` confidence signal to tick_record / plugin output.

## Repro

- Plugin calls: any `jev_drive` with url `https://artificialanalysis.ai/models` and a
  "find/rank Qwen models" goal → blocked (4 independent runs).
- Full-trace repro: `.venv/bin/python` + script at
  `~/.hermes/cache/scratch/jev_trace.py` (instantiates `Agent` directly, dumps per-tick
  operation probabilities, scroll position, viewport text head; 12-tick cap).
- Contrast (works): single-viewport click-path goals — see HANDOFF "Desktop-parity test".
