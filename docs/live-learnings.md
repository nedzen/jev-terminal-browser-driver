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
