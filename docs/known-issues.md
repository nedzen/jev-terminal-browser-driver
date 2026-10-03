# Known issues

Open faults and durable gotchas. Process/fleet seating details are omitted —
only rules that still affect the product or how you test it.

## Product

| ID | Issue | Status |
|---|---|---|
| P1 | Visible provisioning fails from Hermes-TUI-in-cmux (nests in herdr). Rule: **root terminal always**, tabs after first pane, never herdr. | Mitigated by cmux-first + root-terminal blocker; re-verify in live cmux |
| P2 | Stray background browsers; no idle TTL / reaping | Open — do not half-wire a reaper |
| P3 | Stale pane titles mislead operators | Cosmetic / open |
| P4 | CLI `--split-dir=right` fails; correct is `--split right` | Open ergonomics |
| P5 | Enter-submit form-field half declined (searchbox shipped). Do not revive silently | Declined |
| S1 | Large BLOCKED-unjustified cluster (GitHub search/trending, Kalshi, Polymarket, …) | Open sev-2 |
| S2 | FALSE-DONE exhibits; gates #23/#26 + mid-band evidence shipped — next window must add **zero** new sev-1 | Watch |
| S3 | X-cluster: no code lever (enumeration + budget-outs) → re-scope or defer | Open |
| S4 | Flaky N=10s winners; H4 token experiment still open | Open |

## Gotchas (will bite you)

1. **Model DONE is a claim** — check `final_url` / `page_text` / `final_view`.
2. **Short loading pages** omit `scroll_down` until taller than the viewport;
   BLOCKED must **wait/hydrate**, not rescue immediately (Kalshi / Polymarket / X).
3. **Live isolation:** quarantine `last-page.json` between tests; harness waits
   for log quiet (~2s) between manifests. Ambient cmux splits pollute leak tests.
4. **Never `Target.closeTarget`** on terminal-browser Electron (TUI PageHost crash).
5. **Do not add exists-checks** on cmux socket detection in unit tests (fake paths).
6. **Env isolation:** tests asserting routing must clear `CMUX_*` / `HERDR_*`.
7. **Ledger-before-gate is forbidden** — record outcomes when the gate closes.
8. **W-tier** owner-held; **X-tier** never automated.

## Reviewer posture

Wire + suite + mutation first. Trivial-page live rituals are retired. Run a
cheaper observe-only check before theorizing. See historical review protocol in
git (`docs/review-protocol.md` pre-synthesis) if you need the HTML verdict markers.
