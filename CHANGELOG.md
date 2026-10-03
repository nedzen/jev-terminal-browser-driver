# Changelog

All notable user-facing changes, newest first. Version numbers follow
[semver](https://semver.org); the project was published as `jev-driver` at
0.1.0 and is published as `wwwdrive` from 1.0.0.

## 1.1.0

### Changed

- **Insights trace explicit-only on MCP** (~1.8KB/call saved by default): the
  ranked operations/targets trace needs `WWWDRIVE_DEBUG=1`; the debug overlay
  default is unchanged.
- **`WWWDRIVE_REQUEST_DEDUP` opt-in**: request-assembly de-duplication (bench
  −17.5/−21.4% input, accuracy flat), env-gated, default off.

### Internal

- `plugin/core/` stdlib-only leaf (env, budgets, result builder, trace fields).
- One result field table shared by tick rows and compact results.
- Run-log record shape declared once in `plugin/core/trace.py`.

## 1.0.0

### Renamed

- Product is **wwwdrive** (MCP server name, Hermes plugin id, settings key).
- Tools are bare: `drive`, `read`, `status` (hosts may show
  `mcp__wwwdrive__drive`).
- `JEV_DRIVER_HOME` → `WWWDRIVE_HOME`, `JEV_DEBUG` → `WWWDRIVE_DEBUG` (old names
  still read as fallbacks).
- Cache moves to `~/.cache/wwwdrive` (`~/.cache/jev-driver` is orphaned).
- Clean break for tool names and plugin directory — re-register MCP and plugin
  settings once.

### Added

- MCP stdio server for every host (`scripts/mcp.py`); schemas from `plugin/`.
- **`status`** preflight (no browser, no spend).
- Honest results: `verified: null`, stop taxonomy, capped `page_text`.
- Strict budgets (`max_steps`, `time_budget_s`, `timeout_s`) — reject, never clamp.
- Inner `time_budget_s` from first decision; outer `timeout_s` remains the kill.
- Final-view re-observe after DONE; freshness probe retries with reasons.
- Run metrics, spawn/orphan evidence, version manifest, decision provenance.
- Hardened redacted log; `read` background attach (never launches hidden browser).

### Changed

- Debug overlay on by default (`WWWDRIVE_DEBUG=0` to opt out).
- MCP error bifurcation: bad client args vs internal error (class only logged).

### Fixed

- Process-group kill for detached drives; cancelled ≠ timeout.
- Stale fill cache no longer crosses same-labeled fields (upstream #191).

### Removed

- OpenCode TypeScript plugin (MCP is the single adapter).

### Migration

1. Re-register the MCP server as `wwwdrive`.
2. Hermes: delete old `jev-driver` plugin link, re-run `scripts/install_plugin.sh`,
   re-enter **Plugins → wwwdrive**.
3. Call `drive` / `read` / `status`.
4. Optionally delete `~/.cache/jev-driver`.
