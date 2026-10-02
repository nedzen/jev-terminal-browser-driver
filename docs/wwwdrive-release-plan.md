# PARKED PLAN — wwwdrive rename + v1.0.0 release (+ pending merges)

Parked awaiting reviewer verdicts on PR #5 (fill-cache) and PR #7 (debloat).
Do NOT start until both are merged. Owner decisions recorded below are final.

## Decisions (owner, locked)

- New name: **wwwdrive**. Tools are **bare under the server namespace**:
  `drive`, `read`, `status` (clients show `mcp__wwwdrive__drive`, etc.).
- Bare-`read` collision note goes in the README (hosts with a native `read`
  and no namespacing).
- Version: **1.0.0**. Consumer-agnostic positioning: Hermes, OpenCode, omp,
  Grok, Claude Code/Crush, anything speaking MCP — list them in the README.
- GitHub repo renamed to `wwwdrive` (owner clicks in settings).

## Order (strict — no stacking on open work)

1. Merge PR #5, merge PR #7 (reviewer verdicts pending; watchers armed).
2. Micro-leftovers onto main: backlog one-liner, `_Time` merge, width-pin
   tests (from debloat worker report).
3. Rename branch off clean main: server name, tool names, Hermes plugin
   name/toolset, `JEV_DRIVER_HOME` -> `WWWDRIVE_HOME` (fallback to old),
   `~/.cache/jev-driver` -> `~/.cache/wwwdrive`, docs, CHANGELOG.md (new),
   version 1.0.0 in plugin.yaml + pyproject.
4. Reinstall sweep, verified live per consumer: Hermes default profile
   (plugin + mcp_servers entries, symlink), herdr test agent (same profile),
   hunt for other local consumers first.
5. Repo rename in settings, remotes updated.
6. Tag v1.0.0 + GitHub release notes from changelog. No PyPI.

## Context pointers

- Backlog: docs/upstream-ideas-backlog.md. Findings: findings doc was
  deleted in debloat (see git history) — Tier 2-4 evidence lives in PR
  bodies #2/#3 and the test suite.
- Working agreement: AGENTS.md (worktrees per worker, mutation-proven
  tests, merger commits).
- Reviewer channel: direct CDP via terminal-browser instance (port
  discovery: `terminal-browser ls --all --json`), prompt box
  `div.uV2eYG_input`, Enter submits. Survives MCP restarts.
- Open follow-ups (non-blocking): HUD bypass-tag rendering, dated metrics
  history, benchmark runner, UPLOAD_FILE upstream #189.
