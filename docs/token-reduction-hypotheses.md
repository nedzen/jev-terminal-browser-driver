# Token-reduction hypotheses (browsing ingestion)

Goal: minimize tokens an agent ingests while browsing, without degrading
task accuracy. Status: HYPOTHESES — none implemented. Each needs A/B
measurement on the frozen bench (bench_nway.py + Wikipedia task) against
`metrics.json` per-tick input/output tokens before touching production.

## Baseline (already in place)

- `read` with `script` extracts JSON in-page with zero decision-model cost.
- `short_criterion` compresses targets (LABEL_MAX=80, VALUE_MAX=40).
- `page_text` capped at 2000 chars in adapter results.
- `insights` trace rides every tick (candidate for removal, see H2).

## H1 — Elide repeated page chrome (big win, low risk)

Ticks 2..N on one document resend identical headers/nav/footers.
Fingerprint static regions once per document, send once, reference by hash
until changed. Expected: 20-40% input-token cut on same-page runs.

## H2 — Drop insights from the default result (big win, low risk)

Gate the ranked operations/targets trace behind the debug flag. Agents do
not decide off it; it is pure debug payload on every tick.

## H3 — Trim history (big win, low risk)

`recent_actions[-10:]` + full criteria -> last 5 + compacted older steps.

## H4 — Delta snapshots (biggest win, needs validation)

Full snapshot on tick 1 + every Nth tick as keyframe; deltas between
(added/removed actions, changed text spans). Risk: backend may reason
worse without full context. A/B first — largest upside, do not ship
unmeasured.

## H5 — Goal-scoped text (needs validation)

Center page_text on the goal-relevant region (last action, above-the-fold
first) instead of head-truncating at 2000 chars. Same budget, higher
density.

## H6 — Unchanged-page marker (needs validation)

Fingerprint match with previous tick -> send `unchanged_since: N` instead
of the snapshot. Backend tolerance unknown; big payoff on wait-heavy pages.

## H7 — Tune LABEL_MAX/VALUE_MAX (small, safe)

80/40 are guesses. Sweep via bench_nway.py, keep accuracy flat.

## H8 — Read outline density modes (small, safe)

Headings-only vs full outline; current schema untuned since written.

## Field note (reviewer live probe, PR #10)

`omitted_actions` is viewport-gated in practice: snapshot.js filters to
viewport-visible elements, and a typical terminal pane holds ~90-150
actionable elements — the 250-cap trim only fires on substantially larger
viewports. If the field's purpose is diagnosing "the model could not see
these", the viewport filter (not the 250 cap) may be the more informative
limiter to surface someday (e.g. total vs visible counts).

## Execution order

H1 + H2 first (no model-behavior risk), each independently: implement,
measure on bench + live Wikipedia task, confirm success rate flat, commit
separately. H4 as a measured experiment only. Rest after.

## Measurement contract (locked 2026-10-03)

Gate on (a) caller-ingested payload tokens — result bytes per drive call
(the doc's actual goal: "tokens an agent ingests") — plus (b) suite green
and decision-sequence equivalence. Driver Jev totals are diagnosis only:
±32% run-to-run swing on identical action sequences makes them unusable
as a gate. H2 measured: 4,862 → 3,030 B per drive call (~458 tok).

## Follow-ups

- docs/architecture.md:120 vs apply_debug_setting: call-supplied debug is
  discarded by both adapters — doc says otherwise. Fix the claim.

## Measured evidence (stress session 2026-10-02, X + markets + CMC)

| task | outcome | ticks | in-tokens | out-tokens |
|---|---|---|---|---|
| like LoopCD post in feed | blocked (target left viewport, 3 scrolls + BLOCKED) | 4 | 18,096 | 1,736 |
| like top post in feed | blocked (click_not_sent) | 4 | 4,987 | 512 |
| like on stable post page | done | 2 | 10,933 | 1,102 |
| reply 2 words + emoji (typing flow) | done, verified verbatim | 6 | 18,035 | 1,736 |
| bookmark LoopCD in feed | blocked (same scroll pattern) | 4 | 17,852 | 1,715 |
| read+script extractions (feed, links, bookmarks, Kalshi, Polymarket, CMC table) | all success | 0 model calls | ~0 | ~0 |

Readings: (1) feed engagement fails 3/4 when the target isn't in the
initial viewport — ~18k tokens burned per failed attempt on scroll+block;
stable-page actions succeed in 2 ticks. Feed re-ranking defeats the
clicker, not snapshot size. (2) read+script is nearly free — confirms the
extract path as the collection default. (3) typing flow works end to end
(field_text + dialog) at ~18k/6 ticks. (4) kalshi.com/markets is a 404;
home page works. H1 (chrome elision) would NOT have saved the failed
likes — the cost was re-observation after ranking churn, which argues for
H4-keyframes/H6-markers plus a find-then-freeze (navigate to stable post
page first) policy instead.
