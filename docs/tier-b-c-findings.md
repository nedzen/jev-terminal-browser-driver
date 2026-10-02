# Findings: Tier 2–4 Batch B + Batch C

Branch `feat/tier-b-c`. Commits: `dae8c61` (Batch B), `6b1a6f8` (Batch C),
on top of `8ded6ed` (Batch A). Work items are numbered as in
`docs/upstream-ideas-backlog.md`.

Baseline verification at time of writing:

- `uv run pytest -q` → **446 passed** in 6.42s
- `uv run ruff check .` → **All checks passed!**
- `bun build opencode-plugin/jev-driver.ts --external "@opencode/plugin"`
  → Bundled 3 modules, 30.1 KB entry point (the host supplies
  `@opencode/plugin`; without `--external` bun cannot resolve it, which is
  expected for an OpenCode plugin file)

Test distribution after these commits:

| File | Tests | Introduced by |
| --- | --- | --- |
| `tests/test_processes.py` | 84 | Batch C |
| `tests/test_freshness.py` | 54 | Batch B |
| `tests/test_preflight.py` | 37 | Batch A |
| `tests/test_metrics.py` | 33 | Batch C |
| `tests/test_model_provenance.py` | 27 | Batch C |
| `tests/test_time_budget.py` | 20 | Batch B |
| `tests/test_final_view.py` | 12 | Batch A |

---

## Batch B (`dae8c61`) — freshness probe + time budget

### Tier 2 item 1 — read-only freshness probe

`jev_driver/browser.py`. `Browser.fresh()` (line 553) is the gate `act()`
calls before any input; `_probe_target()` (line 569) runs the same read-only
probe up to 3 extra times with settle sleeps from `PROBE_RETRIES`, and
`_probe_reason()` (line 747) reduces the answer to one of `PROBE_REASONS`
(line 31): `target_detached`, `target_changed`, `not_actionable`,
`not_writable`, `ok`.

The probe reads five booleans back (`attached`, `live`, `in_view`, `hit`,
`writable`) plus the page key and the node identity. Hit-testing is via
`document.elementFromPoint` at the target's rect centre; viewport and
connectivity come from `checkVisibility` plus a rect intersection.

Ordering is deliberate and asserted in the code comment at line 756: identity
is checked *last* for `click`/`select`, because a control that is covered or
disabled is still the control the model picked, and reporting its state beats
reporting it as a change. For `fill` the hit-test deliberately does **not**
gate: a field covered by its own label is focused and typed into (see
`_focus_covered_field`), so a fill needs `live` and `writable` only. That
keeps the covered-field fallback reachable.

On refusal, `act()` writes a `stale` event carrying both vocabularies:
`reason: field_changed|page_changed` (which guard failed) and
`probe_reason: <PROBE_REASONS value>` (why the target was unusable), then
raises `StalePage`.

Verification: `tests/test_freshness.py`, 54 tests. The last block runs the
real probe expression through **node** against a synthetic DOM harness
(`run_probe`, line 317) so the JS itself is exercised, not just the Python
reason mapping — including `elementFromPoint` returning an overlay, a
zero-size rect, an off-screen rect, `connected: false`, and a `window` with no
`__jevFast` cache.

### Tier 2 item 2 — per-decision time budget, double-checked

`jev_driver/drive_agent.py` lines 137–191. `time_budget_s` is an *inner*
deadline that starts at the first decision (`_start_time_budget`), so opening
the tab and reading the first page spends none of it. It is checked twice:

1. in `command("predict")` before the Jev call — an already-expired deadline
   stops without spending a model call;
2. in `command("act")` before input — a model call that outlived the deadline
   is **discarded unexecuted**, so the page is never mutated by a decision
   nobody waited for.

Both paths land in `_stop_time_budget()`, which sets `status: blocked`,
`stop_reason: time_budget`, discards the pending decision, writes a `blocked`
event with `reason: time_budget` and the `TIME_BUDGET_WHY` explanation, then
paints the HUD. `timeout_s` is untouched and stays the outer subprocess kill —
two different budgets, two different failure modes.

The kwarg is only forwarded when set (`cli.py` line 395), so an agent built
without it behaves exactly as before.

Plumbing: `--time-budget-s` on `scripts/drive.py` (rejected as
`--time-budget-s must be 1..{TIME_BUDGET_CAP}` before any browser work,
cli.py line 309), the same bound in `plugin/handler.py` `_budget(...)` line
299 (strict reject, never clamp — consistent with the other budgets), and the
same in `opencode-plugin/jev-driver.ts` at lines 89, 210, 500. The taxonomy
maps to `stopped_reason: "time_budget"` through `handler._stopped_reason`.

Verification: `tests/test_time_budget.py`, 20 tests, covering no-budget
unchanged behaviour, zero-means-no-budget, clock origin, both check points,
flag forwarding, pre-spawn rejection, cap agreement across layers, and
`timeout_s` still killing the subprocess.

---

## Batch C (`6b1a6f8`) — metrics, process accounting, provenance

### Tier 3 item 6 — `RunMetrics` per-run aggregate

New `jev_driver/metrics.py` (325 lines). `Metrics` is owned by the agent, one
per run, never global. `instrument_browser()` (line 297) times the browser's
own methods **in place** — `observe` → `observe` phase, `_observe_once`
(nested, counted once), `act` → `act`, `fresh` → `fresh`, `wait`/`sleep` →
`wait` — so phases the base loop spends inside its own code are counted too.
Each wrapper only delegates and re-raises; every recorder call goes through
`_book`, which swallows failures. `instrument_browser` is idempotent (guarded
by a `_jev_metrics` attribute read via `vars()`, not `getattr`, so a `Mock`
cannot fake being already-instrumented) and is a no-op without a browser.

Snapshot shape (`schema: 1`): `jev` calls + total/max/avg ms; `actions`
attempted/succeeded/failed overall and `by_kind`; `phases` for all four
phases; `stale` count; `text_helper`; `startup_ms`; `cleanup_ms`; `status`,
`error`, `finished`, `finished_at`. Every phase key is always present so two
runs diff cleanly.

Secret-free **by construction, not by scrubbing**: no recorder accepts page
text, a URL, a label, or a key. The only strings that can reach a snapshot come
from closed vocabularies (`_KINDS`, `_STATUSES`, `_ERROR_KINDS`) plus the
*type name* of an exception — `_error_kind` matches `_ERROR_HINTS` against
`type(error).__name__.lower()` and never reads the message. There is no door
for a hostile page to smuggle anything through.

`Metrics.write()` never raises (a metrics failure must not break a run),
writes to a sibling `.tmp` and renames, so a reader never sees half a file and
a failed write leaves the previous run's snapshot readable. Path defaults to
the run log's own directory (`metrics_path`).

Wiring: `DriveAgent.__init__` creates it and records startup; `instrument_browser`
is called when the browser appears; `record_jev` after each predict,
`record_text_helper` for helper calls (cached values are not calls),
`record_stale` at every stale site, and `_write_metrics` in cleanup records
cleanup ms, calls `finish(status, error)` (first call wins, so `close()` twice
is harmless) and writes once (`_metrics_written` latch).

Verification: `tests/test_metrics.py`, 33 tests.

### Tier 3 item 7 — spawn/orphan accounting + version manifest

New `jev_driver/processes.py` (676 lines), stdlib only, nothing raises. The
module docstring is the readable version of the design and is worth keeping in
sync with the code.

- **Spawn accounting.** `note_spawn(kind, pid=None)` is the seam
  process-creating call sites report to; `runtime_spawn_count()` answers how
  many this run created. `cli.py` calls `reset_spawns()` at the top of
  `main()` — the count is about *this run*, not the process lifetime, which
  matters because one MCP server process serves many runs.
- **Orphan detection.** `browser.close()` is detach-only (it calls
  `Target.detachFromTarget` and nothing else; see the comment at
  `browser.py` line 628 — never `Target.closeTarget`, which destroys Electron
  webContents out from under `ViewRegistry`). So a leftover terminal-browser
  keeps a pane and a CDP port and nothing in the log mentions it.
  `orphan_report()` asks `ps` after a `ORPHAN_CHECK_DELAY_S = 0.5` beat (a
  process tearing down right after close still answers signal 0 for a moment).
- **Zombie semantics.** The load-bearing part. `os.kill(pid, 0)` answers "is
  this pid in the process table", not "is this process running". A child that
  exited but was never waited on is a zombie: signal 0 succeeds, but it holds
  no pane, no socket, no CPU, and will never act again. Reading that as alive
  reports an orphan for a process that already did its job — the commonest
  false positive available, because a driver spawns short-lived helpers nobody
  reaps. `proc_state()` (line 257) reads the process state as well
  (`ZOMBIE_LETTERS` for `Z`/`X`/`x`; `RUNNING_LETTERS` for everything else
  we're willing to call running, including macOS `I` idle-kernel threads and
  Linux `W` paging) and answers `zombie`. Four outcomes: `alive`, `zombie`,
  `dead`, `unknown`. `unknown` **never counts as clean** — "we could not check"
  is not "there is nothing there".
- **Pattern matches are evidence, not a verdict.** The driver attaches to
  panes it did not create and leaves them running on purpose, so `ps` rows land
  in `pattern_matches` (capped at `MAX_PATTERN_MATCHES = 20`, command text
  clipped to `MAX_COMMAND_CHARS = 200`) separately from `tracked_pids`
  (`MAX_TRACKED_PIDS = 64`). Only tracked pids can make a run report `orphans`.
- **Provenance.** `version_manifest()` (line 610) is exactly three keys:
  `git_commit` (`git rev-parse HEAD`, or None), `impl_hash` (SHA-256 over
  `jev_driver/*.py` + `scripts/*.py`, ordered by relative path so the digest
  never depends on directory listing order), `interpreter`. The hash is
  present precisely because the commit is not enough — a dirty checkout runs
  code that commit does not describe. `repo_root()` requires *both*
  implementation directories to exist: hashing a tree missing `scripts/` would
  yield a digest meaning "half the code", which is worse than saying unknown.
  `write_version_manifest()` never raises; a read-only home directory comes
  back under `error` and the run goes on. One file, overwritten per run, plus
  the manifest embedded in the run's own `event: run` line — that per-run copy
  is what makes a run attributable after the checkout has moved on.

Wiring: `cli.py` line 379 writes the manifest and includes it in the `run`
event; `_record_processes()` (cli.py line 238) writes `process_evidence()`
from a `finally` block, where an exception would replace the run's exit code,
so accounting is allowed to lose evidence but never the result.

Verification: `tests/test_processes.py`, 84 tests — the largest file in the
suite, and deliberately so: this is the module where a wrong answer is
silently wrong.

### Tier 3 item 8 — decision provenance + question-spec hashes

`jev_driver/model.py`. `question_spec_hash(prompts)` (line 33) hashes the
prompt texts in `questions.py`; `QUESTION_SPEC_HASH` (line 51) is computed at
import. Every decision now carries `backend`, `model_version`,
`question_spec_hash`, `decision_source`, `calibration_surface`,
`bypassed_heads` (every head answered in-process, not just the executed one,
so "mixed" is auditable without replaying the request), and a per-head
`stages` map where `stages[head]["backend"]` is `"deterministic"` or
`"model"`.

The point of the whole exercise is line 274's comment: a consumer calibrating
on confidence reads which surface produced the number, so `"deterministic"`
figures are recognisably ours however plausible they look. Usage is recorded
for **both** stages — upstream drops target usage and this deliberately does
not copy that.

### Tier 4 items 10 and 11 — decision-layer shims

- **Single-candidate deterministic bypass** (model.py line 237). An operation
  head whose candidate set has `len == 1` skips the paid call; its answer is
  synthesized locally and tagged `BACKEND_DETERMINISTIC`, the stage's
  `source: bypass`. When some heads bypassed and others did not,
  `decision_source` is `"mixed"` and `bypassed_heads` lists which. The
  operation head is always the model's, so `backend` at the top level is
  `deterministic` only when the executed choice came from a bypass. A
  deterministic head carrying fake `1.0` confidence is exactly what would
  pollute calibration, which is why it is tagged rather than merely made.
- **Tolerant validation + optional bearer.** `PROBABILITY_TOLERANCE = 0.05`
  (was 0.02), a dual `decision`/`choice` key is accepted, and
  `Authorization: Bearer` is attached only when a key exists (model.py line
  110) — the prerequisite for unauthenticated 127.0.0.1 local backends.

Verification: `tests/test_model_provenance.py`, 27 tests. `tests/test_agent.py`
was updated for the intended bypass contract rather than the old behaviour.

---

## Batch A context (`8ded6ed`)

Recorded here because B and C depend on it and the backlog doc for Batch C
refers back to it.

- **Tier 2 item 3, `_final_view` independent re-read** — `cli.py` line 197.
  After DONE, re-observe and compare fingerprints against the decision-time
  read. Evidence only: the status never changes on account of it, and every
  failure mode (no browser, `observe` returned a non-dict, a CDP error) is
  reported inside the object rather than raised. Surfaced through
  `compact_result` (handler.py line 192) and the TS plugin (line 297).
- **Tier 3 item 5, preflight + status** — `jev_driver/preflight.py` (127
  lines, stdlib only, side-effect free: no browser launch, no CDP probe, no
  model call, nothing written to disk). Four checks in report order —
  `decision_key`, `terminal_browser`, `driver_home`, `python_env` — each
  `ok`/`missing`, plus `ready`, `missing`, and a `fixes` hint per missing
  check. A key is reported present or missing; its value is never read into
  the result. It deliberately mirrors `handler.py` (`has_decision_key`,
  `terminal_browser_installed`, `check_jev_drive`) so `jev_status` and the
  Hermes gate agree, and `handler.py` keeps its own copy **on purpose**:
  Hermes loads it without `jev_driver`, so neither module may import the
  other. Exposed as `scripts/drive.py --check` and the `jev_status` MCP tool,
  which is served by the MCP and OpenCode adapters only — never a Hermes
  native tool (`plugin/__init__.py` line 158).
- **Tier 3 item 9, timeline hardening** — `runlog.py`: credential redaction
  for both secret-looking dict keys and free-text assignments
  (`Authorization: Bearer sk-...` becomes `Authorization=[redacted]` in one
  step, with the bare-credential pass running afterwards so no half-consumed
  marker is left), plus size caps `MAX_STRING=200`, `MAX_LIST=20`,
  `MAX_KEYS=64`, `MAX_DEPTH=6`, `[circular]`/`[truncated]` guards, and a
  never-raising `write_event` with falsy-path handling.
- **Tier 2 item 4, error bifurcation** — `scripts/mcp.py`: an unknown tool is
  rejected before any handler runs and is not logged as a server fault; a
  handler exception becomes a JSON-RPC internal error and the stdio loop keeps
  serving; unserializable payloads are answered rather than fatal.
  `_log_fault` records only the exception *class*, never the message, because a
  message can carry a URL with a key in it and the log is append-only.

---

## Live verification

All MCP results below were produced through a real `scripts/mcp.py` server
over stdio, not by calling the handler in-process.

- `jev_status` → `ready: true`. No browser, no paid call.
- `jev_drive` on IANA ("click the Learn more link, done when the example
  domains page shows") → `status: done`, and
  `final_view.page_changed_since_decision: false`. The independent re-read
  confirms the page actually changed as the DONE decision claimed.
- `jev_drive` on Wikipedia (complex multi-step drive followed by a read) →
  `done` with `final_view` present. That the post-DONE shift is reported
  honestly rather than laundered into a `verified: true` is the point of the
  item.
- `jev_drive` with `time_budget_s: 1` → `status: blocked`,
  `stopped_reason: time_budget`. The inner deadline fires *between steps*, so
  the pending decision is discarded unexecuted rather than the page being
  mutated after the caller gave up.
- Per run, `metrics.json` (the IANA run recorded 2 jev calls) and
  `version_manifest.json` (git commit + `impl_hash`) were written next to the
  run log.

Hermes-agent live tests, all passing: visible-pane drive, shared-browser
attach via `cdp_url` + `background: true`, a background `jev_read`, and a
complex Wikipedia drive-plus-read task.

**Operational note:** MCP servers do not hot-reload. After pulling new code
the host session must be restarted (or `scripts/mcp.py` killed) or tool
schemas and handlers stay on the old code. This is recorded in
`docs/mcp-agent-test-guide.md` §7 and is the single most likely cause of a
"my change did nothing" report.

---

## Known limits carried over

1. **Probe-before-close is unpinned.** `act()` gates on `fresh()`, but there is
   no re-probe between the probe passing and the CDP input landing. The window
   is real but small, and it is the reason a stale refusal still happens at
   all rather than silently corrupting the page.
2. **TS parity is presence-markers, not semantic equality.**
   `tests/test_adapter_parity.py` checks the canonical descriptions appear in
   `jev-driver.ts` (after stripping string-literal joins), that schema
   `minimum`/`maximum`/`default` values match, and that markers such as
   `verified`, `stopped_reason`, `outcome_verification`, `PAGE_TEXT_LIMIT =
   2000`, `TIME_BUDGET_CAP = 900`, `--time-budget-s` are present. That catches
   drift and renames. It does not prove the TS adapter computes those fields
   the same way — only that the strings and bounds agree.
3. **The MCP server is serial.** One `tools/call` at a time; during a
   300–900s drive the server cannot read stdin, so `notifications/cancelled`
   is only seen after the call returns. Out-of-band cancellation means
   SIGTERMing the drive subprocess (it runs in its own session) or relying on
   per-call `timeout_s` / `max_steps`. A client SIGKILL of the server orphans
   the drive subprocess.
4. **Truncation is silent in the JSONL.** `runlog` clips strings at 200 chars
   with `…` and marks deep values `[truncated]`, with no flag saying a value
   was cut. Readers of the log must know the caps exist; there is no
   `page_text_truncated: true` sibling to check.
5. **`page_text` is capped at 200 chars in the run log** (the
   `MAX_STRING` above) even though the adapter-facing result carries up to
   `PAGE_TEXT_LIMIT = 2000` and the tick record up to 1500. Three different
   caps on three different surfaces, deliberately — but it means the JSONL is
   not sufficient on its own to diagnose a page-content problem.

---

## Open follow-ups

1. **Surface bypass tags in the debug HUD.** `DriveAgent._hud_payload()`
   (drive_agent.py line 650) reports `decision` probabilities, target marks,
   history steps, usage and `degenerate` — but nothing from `backend`,
   `decision_source`, `bypassed_heads`, or `question_spec_hash`. The overlay
   can therefore show a confident-looking number without saying the head was
   answered in-process. The data is already on the decision record; it only
   needs rendering, and it is the same honesty problem the taxonomy fields
   solved at the adapter boundary.
2. **Dated metrics history.** `metrics.json` and `version_manifest.json` are
   both single files, overwritten per run. They answer "what did this run do"
   well and "how has this changed" not at all. A dated-per-run layout is the
   prerequisite for any before/after comparison or for a regression gate.
3. **Benchmark runner / scoring formula.** Deferred by decision, not by
   oversight (backlog "Deliberately deferred"): only worth building when two
   approaches need comparing. Today `fixtures/` + `scripts/bench_nway.py` +
   `docs/test-plan.md` cover it. Worth revisiting alongside #2, since dated
   metrics are what a runner would consume.

Explicitly still deferred: full step-session API (needs a registry and
locking, conflicts with the stateless subprocess model), candidate paging
(systemone takes N-way in one request), `UPLOAD_FILE` (separate capability),
in-process MCP, Ego backend, cloud-env removal (all rejected; the subprocess
boundary and dual-backend direction stand).