# Live-test learnings ledger

One row per run, appended by `scripts/live`. `classification` is one of HIT, MISS,
BLOCKED_HONEST (honest block on an unsatisfiable goal) -- a blocked stop on a
satisfiable goal is recorded as MISS, per v3. `bytes/call` is the caller-ingested
gate; driver Jev token totals are diagnosis only. Slices:
`/tmp/wwwdrive-runs/<run_id>.jsonl`. S6x rows carry presence-only values, never
amounts.

Columns: `result` is the classification (HIT / MISS / BLOCKED_HONEST), `url` is
final_url on host+path only, `commit` is abbreviated to 7.

| run_id | test | site | tier | result | stop_reason | url | ticks | bytes/call | spec_hash | commit | wall_s | notes |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
