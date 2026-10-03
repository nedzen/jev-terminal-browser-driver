# Backlog: upstream MCP-PR ideas, Tiers 2–4

Source: analysis of browser-use/jev-ultrafast PRs #3 (goal-level MCP server),
#126 (SemIf local backend + MCP), #141 (Ego backend + MCP) against this repo
at branch `feat/multi-agent-mcp-opencode` (commit eda6d5a, Tier 1 done).
Fork point of this product is upstream `452c1ad`; upstream `main` has not
moved since (README-only). Full analysis lives in the session, not here —
this file is the build list.

Tier 1 (DONE, eda6d5a): verified:null + outcome_verification,
stopped_reason taxonomy, 2000-char page_text cap, strict budget validation
(reject, don't clamp), schema-guarantee test, stdout-discipline test.
Mirrored in plugin/handler.py and in the OpenCode TypeScript plugin (the
latter deleted in 32a5444; MCP is the single adapter).

## Tier 2 — robustness core

1. **Read-only freshness probe: retry-with-settle + hit-test + reasons**
   (PR #141 probe.js). DONE (Batch B): probe retries 3x with settle sleeps,
   viewport + elementFromPoint hit-test, writable check, reason taxonomy
   (target_detached/target_changed/not_actionable/not_writable/ok) in
   telemetry. Fill intentionally gated on live+writable only (covered-field
   fallback must stay reachable).
2. **Per-decision timeout, double-checked** (PR #3). DONE (Batch B):
   time_budget_s arg (inner deadline from first decision) checked before
   the Jev call AND before input; expired decisions discarded unexecuted
   with stop time_budget. timeout_s stays the outer kill.
3. **_final_view independent re-read** (PR #141). After DONE, re-observe and
   compare fingerprints; never trust the model's DONE claim. Parts exist
   (fingerprint/marker); wire into cli.py tick loop.
4. **Error bifurcation** (PR #141). One failed run must not end the session:
   bad args -> isError result, session lives; crash -> error + trace, session
   still lives. Audit scripts/mcp.py serve() + run paths.

## Tier 3 — observability

DONE (Batch C): RunMetrics aggregate + metrics.json; spawn/orphan accounting + version_manifest; decision provenance + question-spec hashes; timeline hardening was Batch A. Remaining: none.

5. **Startup preflight + no-browser status check** (PR #141 preflight.py).
   ~30-line pure function (keys present? terminal-browser on PATH?),
   called in DriveAgent/CLI startup, plus a status tool in scripts/mcp.py
   that costs no browser and no paid call.
6. **RunMetrics per-run aggregate** (PR #141 metrics.py). Dataclass owned by
   the agent: jev calls + latency, actions attempted/succeeded by kind,
   stale count, per-phase backend ms, text-helper calls/ms, startup/cleanup
   ms -> one metrics.json per run. runlog.py stays the event source.
7. **Spawn/orphan accounting + impl-hash per run** (PR #141 harness).
   runtime_spawn_count, delayed pid-liveness check after close, version
   manifest (git commit + SHA-256 over implementation files). Detach-only
   close makes orphans unlikely but silent today.
8. **Decision provenance + question-spec hashes** (PR #126). Hash questions.py
   prompts, record backend/model/version/decision_source per stage in runlog
   + drive output. Keeps future cloud-vs-local traces from conflating.
   Record usage for BOTH stages (theirs drops target usage — don't copy that).
9. **Timeline hardening** (PR #141). Secret regex + size caps/truncation in
   runlog write path. Copy their credential-redaction test.

## Tier 4 — decision-layer shims (local-backend future-proofing)

DONE (Batch C): per-head deterministic bypass with mixed/deterministic tagging; tolerant validation (0.05, decision/choice dual key) + optional bearer. Paging deferred until a backend needs it. Remaining: none.

10. **Single-candidate deterministic bypass** (PR #126). len==1 target skips
    the paid call; tag stage deterministic / combined mixed so fake 1.0
    confidence never pollutes calibration.
11. **Tolerant validation + optional bearer** (PR #126). 0.02 -> 0.05 sum
    tolerance, accept decision/choice dual key, omit Authorization when key
    empty (prerequisite for unauthenticated 127.0.0.1 backends).

## Architecture batch (feat/arch-core)

11. **plugin/core/ stdlib-only leaf** DONE: env walks (driver_home,
    has_decision_key, terminal_browser_installed, check_drive, LOG_DIR),
    budget validation (caps + budget/deny_names), and compact_result +
    stop taxonomy moved out of plugin/handler.py. handler.py is now the
    subprocess shell and re-exports the old names. `tests/test_core.py` walks
    the ASTs: core imports nothing outside stdlib + plugin.
12. **Single result builder** DONE: cli.tick_record emits through
    plugin.core.result.build_tick_row; tick row and agent result are two views
    of one field table (PASSTHROUGH). A novel declared field survives the round
    trip with one table edit — the dropped-field class that nearly killed
    final_view and omitted_actions.

13. **Run-log record in core** DONE (Batch B): plugin/core/trace.py owns the
    drive.jsonl record's field set (TRACE_FIELDS), cli.trace_fields delegates to
    build_trace_record. Byte-identical to the dict literal it replaced: same
    keys, same order, same nulls, verified against HEAD's implementation over
    16 input shapes at both the record and the written-log level.
    Separate table from result.PASSTHROUGH on purpose — a trace record writes
    explicit nulls where a row omits, and keeps its own page-text cap (1500) and
    ranked-head depth (8). write_event stays in jev_driver/runlog.py: it is the
    sanitize-and-append boundary, it also serves redact_for_wire (a property of
    an outgoing body, not of this record), and instances/metrics/model patch its
    module globals.

Known duplication left in place (out of this batch's scope):
jev_driver/preflight.py still spells out has_decision_key /
terminal_browser_installed / driver_home rather than importing core.env. The
answers must agree, but they are read differently (a PATH probe for the binary
vs. a file check), so folding them together would hide which one answered.
Tracked for a follow-up.

## Accepted direction (owner decision 2026-10-03; build after suite green)
- Plugin consumes the MCP server: one implementation (`scripts/mcp.py` over
  stdio) as the single source of truth for schemas/behavior; the Hermes
  plugin becomes a thin adapter (packaging, settings UI, availability gate)
  instead of mirroring schemas. Standalone MCP keeps working untouched.
  Eyes-open costs: plugin owns the server subprocess lifecycle (spawn,
  health, version skew, cross-boundary errors). Consistent with the
  subprocess-boundary direction below — this is adapter-over-subprocess,
  not in-process.

## Deliberately deferred

- Full step-session API (start/step/observe/close, PR #3): needs session
  registry + locking; conflicts with stateless subprocess model. Revisit
  after Tiers 2–3. Read-only progress via JSONL tail is the cheap interim.
- Candidate paging (PR #126): only needed if a backend caps option counts;
  systemone takes N-way in one request.
- Benchmark runner/scoring formula (PR #141): only when comparing two
  approaches. Fixtures + bench_nway.py + docs/test-plan.md cover today.
- UPLOAD_FILE (PR #189): separate capability, scope on demand.
- In-process MCP, Ego backend, cloud-env removal: explicitly rejected;
  our subprocess boundary + dual-backend direction stand.
- Parallel MCP drives (owner decision 2026-10-03: PARKED for later, after
  the live-test program; assessment delivered, no work now): Stage 0 —
  per-drive isolation (continuity keyed per session/tab, tab leasing with
  cross-process file locks, one pane/tab per drive, no tab-stealing);
  Stage 1 — parallel mcp.py loop (reader thread + worker pool + stdout
  mutex + cancellation routing); Stage 2 — resource/visibility limits.
  Test suite stays sequential regardless.
