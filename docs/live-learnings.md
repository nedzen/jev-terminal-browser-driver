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
| S2b-145353b8b5 | S2b | github.com | R | mandatory | BLOCKED-unjustified | sev-2 | unjustified-block | model_blocked | github.com/ | 2 | 0 | 1847.0 | feca83330992f854 | 2da12a5 | 3.76 |
| S2b-88cde9906e | S2b | github.com | R | mandatory | BLOCKED-unjustified | sev-2 | unjustified-block | model_blocked | github.com/nedzen/jev-terminal-browser-driver | 1 | 0 | 2215.0 | feca83330992f854 | 2da12a5 | 1.88 |
| S2b-c30ada0579 | S2b | github.com | R | mandatory | BLOCKED-unjustified | sev-2 | unjustified-block | model_blocked | github.com/nedzen/jev-terminal-browser-driver | 1 | 0 | 2215.0 | feca83330992f854 | 2da12a5 | 0.73 |
| S2c-R-97c766ad27 | S2c-R | polymarket.com | R | mandatory | BLOCKED-unjustified | sev-2 | unjustified-block | model_blocked | polymarket.com/ | 6 | 0 | 849.0 | feca83330992f854 | fc91f2c | 8.1 |
| S2c-R-a1bf0d1770 | S2c-R | polymarket.com | R | mandatory | BLOCKED-unjustified | sev-2 | unjustified-block | model_blocked | polymarket.com/ | 5 | 0 | 980.0 | feca83330992f854 | fc91f2c | 5.07 |
| S3a-725f4bb5df | S3a | x.com | R | mandatory | MISS | sev-2 | wrong-end-state | action_budget | x.com/home | 13 | 0 | 1593.0 | feca83330992f854 | 1007df3 | 8.3 |
| S3a-07db7b4b5a | S3a | x.com | R | mandatory | BLOCKED-unjustified | sev-2 | unjustified-block | model_blocked | x.com/home | 5 | 0 | 1653.0 | feca83330992f854 | 1007df3 | 5.41 |
| S3b-e028dae50b | S3b | kalshi.com | R | mandatory | BLOCKED-unjustified | sev-2 | unjustified-block | model_blocked | kalshi.com/calendar | 2 | 0 | 969.0 | feca83330992f854 | 9ce1fe9 | 8.54 |
| S4a-4a14b4702c | S4a | github.com | R | mandatory | BLOCKED-unjustified | sev-2 | unjustified-block | model_blocked | kalshi.com/calendar | 5 | 0 | 2037.0 | feca83330992f854 | 9ce1fe9 | 2.92 |
| S4b-909fd7d95e | S4b | github.com | R | mandatory | BLOCKED-unjustified | sev-2 | unjustified-block | model_blocked | kalshi.com/calendar | 4 | 0 | 1311.0 | feca83330992f854 | 9ce1fe9 | 2.16 |
| S5a-f5ae22880b | S5a | github.com | R | mandatory | BLOCKED-unjustified | sev-2 | unjustified-block | model_blocked | github.com/trending | 3 | 0 | 2273.0 | feca83330992f854 | f39449d | 4.24 |
| S5b-a69a3c12bf | S5b | en.wikipedia.org | R | mandatory | FALSE-DONE | sev-1 | false-done | model_done | en.wikipedia.org/wiki/String_trimmer | 1 | 0 | 2934.0 | feca83330992f854 | f39449d | 1.21 |
| S5c-e77a5c3c9e | S5c | x.com | R | mandatory | MISS | sev-2 | wrong-end-state | action_budget | x.com/home | 13 | 0 | 1553.0 | feca83330992f854 | 551783c | 7.97 |
| S5d-b23409daae | S5d | github.com | R | mandatory | BLOCKED-unjustified | sev-2 | unjustified-block | model_blocked | github.com/search | 4 | 0 | 873.0 | feca83330992f854 | 551783c | 18.64 |
| S6a-83f8edf87b | S6a | github.com | R | mandatory | HIT | - | - | model_done | github.com/notifications | 1 | 0 | 1359.0 | feca83330992f854 | c6f1295 | 1.57 |
| S6b-664a2ed5f3 | S6b | x.com | R | mandatory | MISS | sev-2 | wrong-end-state | action_budget | x.com/home | 13 | 0 | 1623.0 | feca83330992f854 | c6f1295 | 8.4 |
| S6c-01bcf4eadb | S6c | kalshi.com | R | mandatory | BLOCKED-unjustified | sev-2 | unjustified-block | model_blocked | kalshi.com/markets/kxfeddecision/fed-meeting/kxfeddecision-26oct | 5 | 0 | 1587.0 | feca83330992f854 | fab40d6 | 4.77 |
| S7a-80feaef951 | S7a | github.com | R | mandatory | BLOCKED-unjustified | sev-2 | unjustified-block | model_blocked | github.com/search | 4 | 0 | 1620.0 | feca83330992f854 | fab40d6 | 6.07 |
| S7b-436881a53b | S7b | coinmarketcap.com | R | mandatory | FALSE-DONE | sev-1 | false-done | model_done | coinmarketcap.com/ | 1 | 0 | 2315.0 | feca83330992f854 | 48a2516 | 2.15 |
| S7c-2ceae347d3 | S7c | coinmarketcap.com | R | mandatory | HIT | - | - | model_done | coinmarketcap.com/ | 1 | 0 | 2413.0 | feca83330992f854 | 48a2516 | 0.79 |
| S8a-4feb1be05f | S8a | x.com | R | mandatory | BLOCKED-unjustified | sev-2 | unjustified-block | model_blocked | x.com/home | 2 | 0 | 1523.0 | feca83330992f854 | 48a2516 | 4.12 |
| S8b-6a98e5226a | S8b | github.com | R | mandatory | HIT | - | - | model_done | github.com/nedzen/jev-terminal-browser-driver/releases/tag/v1.1.0 | 7 | 0 | 2649.0 | feca83330992f854 | 48a2516 | 4.6 |
| S1a-ii-5bb6e31cc4 | S1a-ii | coinmarketcap.com | R | mandatory | HIT | - | - | model_done | coinmarketcap.com/currencies/tether | 2 | 0 | 1319.0 | feca83330992f854 | 48a2516 | 2.88 |
