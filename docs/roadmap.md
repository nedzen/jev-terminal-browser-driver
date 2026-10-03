# Roadmap

Shipped releases: **v1.0.0** (rename to wwwdrive, MCP-first) and **v1.1.0**
(insights explicit-only, opt-in request de-dup). Details: [CHANGELOG.md](../CHANGELOG.md).

## Open product work

| Item | Notes |
|---|---|
| H4 delta snapshots | Largest remaining token lever; needs A/B + conditional sign-off (7 conditions). See also H3/H5–H8 below |
| BLOCKED-unjustified cluster | Per-site cycles; re-verify not_actionable + live premises before new gates |
| X-cluster | Re-scope or defer — not more retries |
| Stray-browser TTL | Open; do not ship an unwired reaper |
| `_final_view` in the tick loop | Independent re-read already exists post-DONE; wiring into the live tick path is still open |
| Error bifurcation audit | Bad args vs crash; session must stay alive |

## Token hypotheses (status)

| ID | Idea | Status |
|---|---|---|
| H1 | Request-assembly de-dup | **Shipped** (`WWWDRIVE_REQUEST_DEDUP`, opt-in) |
| H2 | Insights explicit-only | **Shipped** (~1.8KB/call) |
| H3 | History trim | Open |
| H4 | Delta snapshots | Open — biggest; feed re-rank costs dominate failed X likes |
| H5 | Goal-scoped page text | Open |
| H6 | Unchanged-page marker | Open |
| H7 | LABEL/VALUE sweep | Open |
| H8 | Outline density | Open |

**Hard rules:** gate on caller payload B/c + suite + decision-sequence
equivalence; do not optimize raw Jev spend; any sequence/end-state delta = revert.

## Accepted direction / parked

- Hermes plugin stays a thin adapter over the MCP subprocess (do not invert).
- Parallel multi-drive MCP: **parked**.
- Step-session API, candidate paging, UPLOAD_FILE: deferred.
- In-process MCP, Ego backend, removing cloud-env: **rejected**.

## Process (durable)

- One writer per worktree; workers never commit; merger runs suite green after
  each integrate.
- Epic PRs over per-batch reviews; mutation checks on new behaviour.
- Owner-only: GitHub rename / profile re-link when needed.
