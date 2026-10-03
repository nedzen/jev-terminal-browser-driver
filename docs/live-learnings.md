# Live-test learnings ledger

One row per run, appended by `scripts/live`. `outcome` is the v3.1 partition
(HIT / HIT-recovered / MISS / FALSE-DONE / BLOCKED-honest / BLOCKED-unjustified /
STALL / CRASH); `sev` is sev-1 (FALSE-DONE only, must stay 0) or sev-2. `bytes/call`
is the caller-ingested gate; driver Jev token totals are diagnosis only. Slices:
`/tmp/wwwdrive-runs/<run_id>.jsonl`. S6x rows carry presence-only values, never
amounts.

Columns: `url` is final_url on host+path only, `waste` is measured waste-ticks
(post-satisfaction + stale retries only -- the remainder has no oracle), `commit`
is abbreviated to 7. A `void` row is a CRASH retry attempt and does not count
toward the scoreboard. Free-text notes are in the run record and the JSONL slice,
not in this table: a wrapping paragraph in a ledger cell is unreadable.

| run_id | test | site | tier | cap | outcome | sev | cause | stop | url | ticks | waste | B/c | hash | commit | wall |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| aa58e97d | S1a | coinmarketcap.com | R | mandatory | HIT | n/a | n/a | model_done | coinmarketcap.com/currencies/tether/ | 2 | 0 | n/m-CLI | feca8333 | 93a4210 | 3s |

Notes: t0 snapshot pinned #3 = Tether (coinmarketcap.com/currencies/tether/);
goal named it by name; final_url matches exactly; page shows Tether #3 + price.
B/c n/m: S1a executed via CLI (pre-runner-use), no MCP result bytes exist —
first runner-driven run will carry measured B/c. Slice:
/tmp/wwwdrive-runs/aa58e97dd1c04724bfea6badc23d0d01.jsonl (2 lines: start +
finish; tick payloads in drive.jsonl under run_id).
| S1a-196486ef94 | S1a | coinmarketcap.com | R | mandatory | HIT | - | - | model_done | coinmarketcap.com/currencies/tether | 2 | 0 | 1320.0 | feca83330992f854 | e13d8ff | 1.62 |
| S1a-97cfbc11a7 | S1a | coinmarketcap.com | R | mandatory | HIT | - | - | model_done | coinmarketcap.com/currencies/tether | 2 | 0 | 1320.0 | feca83330992f854 | e13d8ff | 1.42 |

Correction (§C loop on S1a): runner wrote ticks 0 (payload `ticks` count never
entered the event stream; string actions invisible to the denylist — second
live-use bug, fixed + tested in scripts/live). Ticks corrected to 2 per both
slices' payloads. Row 196486ef94 is the pre-fix-crash attempt's completed
drive (classification crashed post-drive); row 97cfbc11a7 the clean rerun —
both real drives, both HIT; the duplication is operator re-invocation, noted.
| S1b-b1b038e07d | S1b | coinmarketcap.com | R | mandatory | BLOCKED-unjustified | sev-2 | unjustified-block | model_blocked | - | 1 | 0 | 403.0 | feca83330992f854 | d369080 | 2.75 |
| S1b-8226f57c3e | S1b | coinmarketcap.com | R | mandatory | HIT | - | - | model_done | coinmarketcap.com/currencies/tether | 2 | 0 | 1417.0 | feca83330992f854 | d369080 | 3.44 |
| S1c-6de4b4dfb9 | S1c | github.com | R | mandatory | BLOCKED-unjustified | sev-2 | unjustified-block | model_blocked | github.com/nedzen/jev-terminal-browser-driver | 4 | 0 | 2303.0 | feca83330992f854 | f7999eb | 1.95 |
| S1c-67d8c4cf52 | S1c | github.com | R | mandatory | BLOCKED-honest | sev-2 | - | model_blocked | github.com/nedzen/jev-terminal-browser-driver | 2 | 0 | 2229.0 | feca83330992f854 | f7999eb | 1.0 |
| S1d-da4f36ceb0 | S1d | coinmarketcap.com/currencies/tether/ | R | mandatory | HIT | - | - | model_done | coinmarketcap.com/currencies/tether | 1 | 0 | 1597.0 | feca83330992f854 | 3109602 | 2.2 |
| S2a-d5e84c1a1e | S2a | coinmarketcap.com | R | mandatory | BLOCKED-unjustified | sev-2 | unjustified-block | model_blocked | coinmarketcap.com/currencies/chainlink | 6 | 0 | 2030.0 | feca83330992f854 | 3109602 | 5.85 |
