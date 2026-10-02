# Changelog

All notable user-facing changes, newest first. Version numbers follow
[semver](https://semver.org); the project was published as `jev-driver` at
0.1.0 and is published as `wwwdrive` from 1.0.0.

## 1.0.0

### Renamed

- The product is **wwwdrive**. The MCP server reports `wwwdrive`, the Hermes
  plugin directory is `~/.hermes/plugins/wwwdrive`, and the Hermes plugin id
  (so the settings key) is `wwwdrive`.
- Tools lost their prefix and are now bare under that namespace: `jev_drive` →
  `drive`, `jev_read` → `read`, `jev_status` → `status`. A host that
  namespaces MCP tools shows them as `mcp__wwwdrive__drive` and friends. A
  host with a native `read` and no namespacing can collide; that is called out
  in the README.
- `JEV_DRIVER_HOME` → `WWWDRIVE_HOME` and `JEV_DEBUG` → `WWWDRIVE_DEBUG`. Both
  old names are still read as fallbacks, so existing configs keep working.
- Logs, the remembered tab, and the overlay's remembered panel state moved to
  `~/.cache/wwwdrive`. Anything in `~/.cache/jev-driver` is orphaned and is
  never read again; the README says so.
- The rename is a clean break for tool names and the plugin directory: no
  compatibility alias, no leftover symlink. Re-register the MCP server and
  re-enter plugin settings once.

### Added

- **MCP server for every host.** One stdlib-only stdio server
  (`scripts/mcp.py`) exposes the same three tools to Hermes, OpenCode, omp,
  Grok, Claude Code/Crush, and anything else that speaks MCP — no per-host
  code, no new dependencies. Tool schemas are imported from `plugin/`, so the
  Hermes and MCP surfaces cannot drift; a parity test fails the suite if they
  do.
- **`status`**: a preflight tool that reports whether this machine can drive a
  browser at all, with no browser, no spend, and no key material — every field
  is `ok` or `missing`, plus the fixes. The same checks back `python
  scripts/drive.py --check`.
- **Honest results.** Every result now carries `verified: null` and an
  `outcome_verification` note: the model's DONE is a choice, never an
  independent check. `stopped_reason` gives a small stop taxonomy
  (`model_done`, `action_budget`, `time_budget`, `model_blocked`, `error`,
  `cancelled`) instead of a free-form `reason` alone, and `page_text` is
  capped so a page dump cannot flood the agent's context.
- **Budgets that reject rather than clamp.** `max_steps`, `time_budget_s`,
  and `timeout_s` are validated strictly before anything runs, so a caller
  learns the budget it got instead of silently getting a different one.
- **`time_budget_s`**: an inner deadline measured from the first decision and
  checked before every model call *and* before every click or type. A decision
  that outlives it is discarded without acting on the page. `timeout_s` stays
  the outer kill.
- **Final-view probe.** After DONE the driver re-observes the page and returns
  a fingerprint of what it believes it finished on, so a claimed success can
  be checked instead of trusted.
- **Freshness retries.** The read-only freshness probe now retries with settle
  waits and hit-tests the target, and reports why it gave up
  (`target_detached`, `target_changed`, `not_actionable`, `not_writable`).
- **Run metrics and process accounting.** Each run writes a `metrics.json`
  aggregate (decision latency, actions attempted vs succeeded, stale count,
  per-phase timings, text-helper calls), counts spawns, checks for orphans
  after cleanup, and records a version manifest (git commit plus a hash over
  the implementation files) so a trace can be tied to the code that produced
  it.
- **Decision provenance.** The run log records the backend, model, and prompt
  hashes behind every decision, and usage for both the decision stage and the
  text helper.
- **A hardened log.** Every record is redacted (secret-looking keys and
  assignments), size-capped, and coerced; `write_event` cannot raise, and the
  same event written twice produces the same record.
- **`read` can attach in the background**, like `drive` did: `background: true`
  plus a `cdp_url` the caller already holds. Neither tool launches a hidden
  browser.
- **Test-plan and agent guides** for driving the server live and pasting the
  release checklist.

### Changed

- The debug overlay is on by default, because a visible browser you cannot
  explain is worse than a busy one. Opt out with `WWWDRIVE_DEBUG=0` over MCP
  (`JEV_DEBUG=0` still works) or with the plugin setting. As before, the model
  cannot change it.
- Timeouts and driver crashes are logged as handler events, so a run that died
  outside the driver's own logging still leaves a record.
- Error handling in the MCP server is split: a client's mistake (unknown tool,
  bad arguments) is answered as such and never runs a handler, while anything
  the server itself hits answers `internal error` and keeps the stdio loop
  alive. Only the exception class is logged, never its message, which can
  carry a URL with a key in it.

### Fixed

- Detached drive subprocesses are killed as a process group, so a 900-second
  call cannot leave an orphan driving your browser.
- A cancelled run is distinguishable from a timeout.
- A stale fill cache no longer crosses same-labeled fields (upstream #191).
- Retries are tightened so a retry cannot re-send an action the page already
  rejected.

### Removed

- The OpenCode TypeScript plugin. MCP is the single adapter, so the schemas
  live in one place instead of two that can drift.
- Dead code and near-duplicate helpers, and shared test fakes in place of
  per-file copies.

### Migration

1. Re-register the MCP server under the name `wwwdrive`.
2. If you used the Hermes plugin: delete the old `jev-driver` plugin link,
   re-run `scripts/install_plugin.sh`, and re-enter the settings under
   **Plugins → wwwdrive**.
3. Update your agent's prompts or scripts to call `drive`, `read`, `status`.
4. Optionally delete `~/.cache/jev-driver` — it is no longer read.
