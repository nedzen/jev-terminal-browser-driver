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
| S4a-4a14b4702c | S4a | github.com | R | mandatory | VOID | - | harness-isolation | model_blocked | kalshi.com/calendar | 5 | 0 | 2037.0 | feca83330992f854 | 9ce1fe9 | 2.92 |
| S4b-909fd7d95e | S4b | github.com | R | mandatory | VOID | - | harness-isolation | model_blocked | kalshi.com/calendar | 4 | 0 | 1311.0 | feca83330992f854 | 9ce1fe9 | 2.16 |

VOID notes (§triaged audit 2026-10-03): both inherited S3b's Kalshi tab
(no-url re-attach + navigate:False) — never reached a GitHub page; verdicts
meaningless, excluded from rates. S4a manifest also fixed (empty
text_present was vacuous → human_judged). 07:0x window ran without isolation
guarantees (log_dir/quarantine silent no-ops; isolation_report hardcoded
True) — other rows stand but unguaranteed until re-run. S5b/S7b sev-1 rows
flagged for evidence re-read (landed on prior-run pages).
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
| M1-df39741241 | M1 | github.com | R | mandatory | CRASH (void) | sev-2 | harness-error | - | - | 0 | 0 | 0.0 | feca83330992f854 | a29bcec | 0.06 |
| M1-43b00b0f1c | M1 | github.com | R | mandatory | CRASH | sev-2 | harness-error | - | - | 0 | 0 | 0.0 | feca83330992f854 | a29bcec | 0.01 |
| M1-2f01e1cfc8 | M1 | github.com | R | mandatory | BLOCKED-unjustified | sev-2 | unjustified-block | model_blocked | github.com/notifications | 1 | 0 | 1214.0 | feca83330992f854 | a29bcec | 4.32 |
| M2-e64bbe4683 | M2 | github.com | R | mandatory | HIT | - | - | model_done | github.com/nedzen/jev-terminal-browser-driver/issues | 9 | 0 | 1471.0 | feca83330992f854 | a29bcec | 6.32 |
| M3-c11686d469 | M3 | coinmarketcap.com | R | mandatory | BLOCKED-unjustified | sev-2 | unjustified-block | model_blocked | coinmarketcap.com/ | 5 | 0 | 2286.0 | feca83330992f854 | a29bcec | 5.51 |
| M4-bcfbebd851 | M4 | github.com | R | mandatory | FALSE-DONE | sev-1 | false-done | model_done | github.com/nedzen/jev-terminal-browser-driver | 4 | 0 | 1415.0 | feca83330992f854 | a29bcec | 3.9 |
| M5-e3375bffa4 | M5 | x.com | R | mandatory | MISS | sev-2 | wrong-end-state | action_budget | x.com/home | 13 | 0 | 1325.0 | feca83330992f854 | 9cf85ac | 9.29 |
| M6-9f33e9e37f | M6 | kalshi.com | R | mandatory | BLOCKED-unjustified | sev-2 | unjustified-block | model_blocked | chromewebdata/ | 4 | 0 | 631.0 | feca83330992f854 | 9cf85ac | 15.26 |
| M6-67bb4a667a | M6 | kalshi.com | R | mandatory | BLOCKED-unjustified | sev-2 | unjustified-block | model_blocked | kalshi.com/ | 2 | 0 | 1809.0 | feca83330992f854 | 9cf85ac | 3.68 |
| M7-7dd0bfa1e7 | M7 | polymarket.com | R | mandatory | FALSE-DONE | sev-1 | false-done | model_done | polymarket.com/event/btc-updown-5m-1791012900 | 3 | 0 | 2982.0 | feca83330992f854 | 3a9beae | 3.84 |
| M8-ad106edd4e | M8 | github.com | R | mandatory | BLOCKED-unjustified | sev-2 | unjustified-block | model_blocked | github.com/search | 4 | 0 | 872.0 | feca83330992f854 | 3a9beae | 19.55 |
| M9-3072a8ca2d | M9 | github.com | R | mandatory | BLOCKED-unjustified | sev-2 | unjustified-block | model_blocked | github.com/nedzen/jev-terminal-browser-driver | 5 | 0 | 2831.5 | feca83330992f854 | fa1e157 | 7.47 |
| M11-0d81fab727 | M11 | wolframalpha.com | R | mandatory | BLOCKED-unjustified | sev-2 | unjustified-block | model_blocked | www.wolframalpha.com/ | 5 | 0 | 1859.0 | feca83330992f854 | fa1e157 | 10.27 |
| M12-4ddd1476f6 | M12 | flights.google.com | R | mandatory | MISS | sev-2 | wrong-end-state | action_budget | www.google.com/travel/flights | 13 | 0 | 1827.0 | feca83330992f854 | 3762a4e | 18.64 |
| M13-508657a185 | M13 | amazon.com | R | mandatory | BLOCKED-unjustified | sev-2 | unjustified-block | model_blocked | www.amazon.com/s | 5 | 0 | 2236.0 | feca83330992f854 | 3762a4e | 10.25 |
| M14-450398291c | M14 | arxiv.org | R | mandatory | FALSE-DONE | sev-1 | false-done | model_done | arxiv.org/list/cs.AI/new | 4 | 0 | 2883.0 | feca83330992f854 | 84599b0 | 3.69 |
| M15-2a3d60a83e | M15 | sec.gov | R | mandatory | FALSE-DONE | sev-1 | false-done | model_done | www.sec.gov/cgi-bin/browse-edgar | 1 | 0 | 3044.0 | feca83330992f854 | 84599b0 | 6.02 |
| M16-b26ddd4990 | M16 | huggingface.co | R | mandatory | FALSE-DONE | sev-1 | false-done | model_done | huggingface.co/datasets/osunlp/Online-Mind2Web/tree/main | 2 | 0 | 1826.0 | feca83330992f854 | 3856781 | 3.42 |
| M17-13e7a7fd7f | M17 | huggingface.co | R | mandatory | MISS | sev-2 | wrong-end-state | action_budget | huggingface.co/meta-llama/Llama-3-70B | 13 | 0 | 1560.0 | feca83330992f854 | 3856781 | 8.34 |
| M18-01ac591c9e | M18 | huggingface.co | R | mandatory | BLOCKED-unjustified | sev-2 | unjustified-block | model_blocked | huggingface.co/models | 4 | 0 | 2532.0 | feca83330992f854 | 3856781 | 4.18 |

## BASELINE WINDOW 2 (2026-10-03, post-fix clean re-run)
Prior 51 rows SUPERSEDED for scoreboard purposes (pre-isolation-fix window:
no isolation guarantees, pre-sev-1-gate, pre-movedon-gate, pre-scorer-fix).
History stays above; scoreboard counts only rows below this line.
| S1a-e7da7829b8 | S1a | coinmarketcap.com | R | mandatory | HIT | - | - | model_done | coinmarketcap.com/currencies/tether | 2 | 0 | 1294.0 | f8b13c650cc5ddf6 | ac7df8b | 5.97 |
| S1b-59a74df485 | S1b | coinmarketcap.com | R | mandatory | HIT | - | - | model_done | coinmarketcap.com/currencies/tether | 2 | 0 | 1295.0 | f8b13c650cc5ddf6 | ac7df8b | 2.97 |
| S1c-e60d8509c9 | S1c | github.com | R | mandatory | BLOCKED-honest | sev-2 | - | model_blocked | github.com/nedzen/jev-terminal-browser-driver | 4 | 0 | 2628.0 | f8b13c650cc5ddf6 | ac7df8b | 4.03 |
| S1d-efdb25761e | S1d | coinmarketcap.com/currencies/tether/ | R | mandatory | HIT | - | - | model_done | coinmarketcap.com/currencies/tether | 1 | 0 | 1567.0 | f8b13c650cc5ddf6 | ac7df8b | 2.8 |
| S2a-aee6ace2ec | S2a | coinmarketcap.com | R | mandatory | FALSE-DONE | sev-1 | false-done | model_done | coinmarketcap.com/exchanges/picol | 7 | 0 | 2294.0 | f8b13c650cc5ddf6 | 9953fd3 | 5.92 |
| S2b-9ddd1f7181 | S2b | github.com | R | mandatory | BLOCKED-unjustified | sev-2 | unjustified-block | model_blocked | github.com/nedzen/jev-terminal-browser-driver | 4 | 0 | 2516.0 | f8b13c650cc5ddf6 | 9953fd3 | 3.93 |
| S2c-R-e657345d5c | S2c-R | polymarket.com | R | mandatory | BLOCKED-unjustified | sev-2 | unjustified-block | model_blocked | polymarket.com/event/btc-updown-5m-1791018000 | 4 | 0 | 2693.0 | f8b13c650cc5ddf6 | 1b12992 | 6.19 |
| S3a-2a581d3ebc | S3a | x.com | R | mandatory | HIT | - | - | model_done | x.com/bfl_ai/status/2105734605621825738 | 6 | 0 | 1601.0 | f8b13c650cc5ddf6 | 1b12992 | 7.1 |
| S3b-bdd7a1671e | S3b | kalshi.com | R | mandatory | BLOCKED-unjustified | sev-2 | unjustified-block | model_blocked | kalshi.com/calendar | 3 | 0 | 974.0 | f8b13c650cc5ddf6 | 5982578 | 10.16 |
| S4a-abc943ff9a | S4a | github.com | R | mandatory | BLOCKED-unjustified | sev-2 | unjustified-block | model_blocked | - | 1 | 0 | 396.0 | f8b13c650cc5ddf6 | 5982578 | 0.36 |
| S6a-ec6720d378 | S6a | github.com | R | mandatory | HIT | - | - | model_done | github.com/notifications | 1 | 0 | 1382.0 | f8b13c650cc5ddf6 | 5982578 | 2.33 |
| S4a-19ca810b41 | S4a | github.com | R | mandatory | BLOCKED-unjustified | sev-2 | unjustified-block | model_blocked | - | 1 | 0 | 396.0 | f8b13c650cc5ddf6 | 5982578 | 0.31 |
| S5a-0f45676695 | S5a | github.com | R | mandatory | BLOCKED-unjustified | sev-2 | unjustified-block | model_blocked | github.com/trending | 3 | 0 | 2272.0 | f8b13c650cc5ddf6 | cc92cdd | 4.02 |
| S5b-a8361e4b33 | S5b | en.wikipedia.org | R | mandatory | HIT | - | - | model_done | en.wikipedia.org/wiki/String_trimmer | 6 | 0 | 2971.0 | f8b13c650cc5ddf6 | cc92cdd | 4.19 |
| S5c-90002fb24c | S5c | x.com | R | mandatory | MISS | sev-2 | wrong-end-state | action_budget | x.com/home | 13 | 0 | 1362.0 | f8b13c650cc5ddf6 | cc92cdd | 8.59 |
| S5d-fe62bec306 | S5d | github.com | R | mandatory | BLOCKED-unjustified | sev-2 | unjustified-block | model_blocked | github.com/search | 4 | 0 | 872.0 | f8b13c650cc5ddf6 | 4d89894 | 19.96 |
| S6b-1bb3ac146a | S6b | x.com | R | mandatory | MISS | sev-2 | wrong-end-state | action_budget | x.com/home | 13 | 0 | 1235.0 | f8b13c650cc5ddf6 | 4d89894 | 8.21 |
| S6c-5435e787ef | S6c | kalshi.com | R | mandatory | BLOCKED-unjustified | sev-2 | unjustified-block | model_blocked | kalshi.com/calendar | 4 | 0 | 1938.0 | f8b13c650cc5ddf6 | 4d89894 | 5.82 |
