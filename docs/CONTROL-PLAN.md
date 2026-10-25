# Control plan — wwwdrive 1.2.0 (MCP-first, one tab, one run)

Written 2026-10-25 after a read-only audit of the working tree, both unmerged
branches, a built wheel, and the offline suite. Every number below was produced
in that session; nothing is from memory.

## Progress (2026-10-25, same session)

- **M1 done, locally:** `main` fast-forwarded through all 18 `refactor` commits.
  Each commit was gated in a scratch worktree: 971–1012 passed, 3 skipped,
  ruff clean at every one. No push.
- **M2 done, locally:** `ce6a830` — packaged MCP server, `wwwdrive-mcp` entry
  point, module-based adapters, single version source, wheel excludes,
  `status` reports `package`. Main is now `0a49c4b` (test fix). Verified from a
  clean venv installed wheel: initialize, tools/list, status ready, `drive {}`
  → "goal is required".
- **Gate hygiene done:** product suite is the default gate
  (`testpaths = ["tests"]`): 833 passed, 3 skipped, **8.4 s**. The live harness
  is a separate command (`pytest scripts/live`): 147 passed, **5.8 s**. The
  60 s regression was one test waiting out `wait_pane_idle` (30 s × 2 retries);
  the refusal is now injected and the wait's own timeout stays clock-pinned.
  Total gate: 74 s → ~14 s.
- **Still open:** the 3 extra commits on `feat/slim-continue` (harness split,
  `hud`/`act_retry`/`model_wire` split, docs handoff); M3–M6; live gate.

## 1. State of the world (verified)

| Fact | Evidence |
|---|---|
| Offline suite green on `main` | 1012 passed, 3 skipped, 14.4 s |
| Ruff clean on `main` | `uv run ruff check .` → all checks passed |
| Offline suite green on `feat/slim-continue` | 973 passed, 3 skipped, 74.3 s |
| Slimming work is **not on main** | `origin/main` at `b564350`; PR #29 draft open (`refactor`, 18 commits); local `feat/slim-continue` has 21 commits (18 + 3 session commits) |
| Local branches | 41; only 3 carry unmerged work (`refactor` 18, `feat/slim-continue` 21, `plan/wwwdrive-release` 1) |
| Product kernel size | `jev_driver` 6,747 + `plugin` 1,078 + entry scripts 370 ≈ 8.2k lines |
| Lab size | `scripts/live` ≈ 4.7k lines, 155 self-tests, inside the default `pytest` run |
| Test mass | 886 test functions / 12.5k lines, ~2:1 vs product code |
| Docs | 22 markdown files / 3.9k lines, plus `docs/research` and `docs/archive` |
| **A built wheel cannot drive** | installed in a clean venv: `driver_home` → `.../site-packages/plugin/core`, `preflight.ready=false`, `run_drive()` → `drive.py missing` |
| **The wheel has no MCP entry point** | `entry_points.txt` = `wwwdrive = jev_driver.cli:main` only; no `scripts/` in the wheel |
| Wheel ships agent debris | untracked `plugin/.crush/crush.db` (128K) and `plugin/.crush/logs/` are packaged |
| Version is duplicated | `1.1.0` in `pyproject.toml`, `plugin.yaml`, `plugin/plugin.yaml`, `scripts/mcp.py`, `uv.lock` |
| MCP is serial and blocks stdin | `scripts/mcp.py` docstring; default `timeout_s` 300, cap 900 |
| DONE has three sub-threshold paths | `docs/roadmap.md` V3 map: `_moved_on` bypass, confidence fallback (`readiness.py`), `already_followed` |
| Repository's own live ledger | 24 BLOCKED-unjustified (sev-2), 7 FALSE-DONE exhibits (sev-1) |
| Two implementations of one truth | `plugin/core/env.py` and `jev_driver/preflight.py` both answer "can this machine drive", pinned to agree by tests |

## 2. Why it got away — five mechanisms

1. **Three products, one tree.** The shipped driver (~8k), the live-test lab
   (~5k), and the agent-fleet process (roadmap, ledgers, 41 branches, review
   protocol) share one suite, one docs directory, and one release gate. Reading
   the repo means reading all three at once.
2. **Guard accretion.** Every incident added a guard, a bypass, a stop reason,
   and a paragraph of incident narrative. Nothing was ever removed. The DONE
   gate is now three paths plus a rescue, and the prose is the spec.
3. **Rearranging instead of deleting.** The slimming branch is real work (net
   −3.5k lines, 24 modules under 600 lines, 9 docs) but it added abstractions
   (`Evidence`/`verdict`, `instances`, `ops`, `probe`) and changed stop policy
   in the same motion. It was never merged, so what the owner reads and what
   runs diverge.
4. **MCP treated as an adapter, not the product.** The release target
   ("any agent") is served by a subprocess that re-executes a repo script; the
   package therefore only works from a checkout. No notebook state, no entry
   point, no wheel smoke test.
5. **No surface budget.** Nothing fails when a module doubles, when a tool
   gains a tenth argument, or when docs double. Agents optimise against tests,
   and tests were the only gate.

## 3. The target: definition of 1.2.0

One sentence: **`wwwdrive` is one installable MCP stdio server that drives one
visible terminal-browser tab, one run at a time, and returns a compact result.**

In scope:

- `pipx/uvx install` → `wwwdrive-mcp` on PATH → any MCP client registers one
  command. No checkout, no `--directory`, no `uv run` per call.
- Tools: `drive`, `read`, `status`. `drive` starts a run; results are compact.
- One tab, one run, explicitly serial. A second concurrent `drive` is refused.
- Cancellation works: the client can cancel and the child dies.
- Read-only live gate green on example.com/IANA plus one form flow.
- Docs the owner can hold: README + SPEC + architecture ≤ 300 lines + CHANGELOG
  + known-issues.

Out of scope (state it in the release notes):

- Parallel drives, worker pools, multi-drive sessions.
- Headless browsers, the desktop preview, OpenCode/TypeScript adapters.
- Local decision backends, UPLOAD_FILE, candidate paging.
- New stop reasons, new tool arguments, new guards.

## 4. Control moves, in order

### M1 — Pick the base and stop the line (half a day)

Merge **PR #29 commit-by-commit** (18 coherent commits), not as one 123-file
diff; then land the 3 commits on `feat/slim-continue` as a second PR. Reasons:
both trees are green, the branch is net-deleting, and its commits are already
the review units. Tag `v1.2.0-controlled-base`. Freeze: no new tool argument,
stop reason, doc, or test file until 1.2.0 ships; every fix must delete
something or extend an existing test file.

If commit-by-commit review is too slow, the fallback is to keep `main` and
cherry-pick only the structural commits (docs synthesis, harness split,
`instances`, file splits) and defer the stop-policy rewrite. Do not merge the
stop-policy change without the live gate in M5.

### M2 — Make the package the product (1–2 days, release-critical)

The wheel proof above is the whole reason "any agent" does not work today.

- Move `scripts/mcp.py` into `jev_driver/mcp.py`; add `wwwdrive-mcp =
  jev_driver.mcp:main` to `[project.scripts]`.
- Run the driver in-process, not by re-executing `scripts/drive.py`: add
  `jev_driver/__main__.py`, and have `plugin/handler.py` spawn
  `[sys.executable, "-m", "jev_driver.cli", ...]` with no `driver_home` lookup.
  Keep a checkout override only for development.
- Single version source: `pyproject.toml` + `importlib.metadata.version`;
  delete the four hard-coded copies.
- Exclude `plugin/.crush/**`, `.cursor/**`, logs, and the demo asset from the
  sdist/wheel; add a `.gitignore` for agent state.
- CI step: build the wheel, install into a clean venv, run the MCP handshake,
  `tools/list`, `status`, and a no-key `drive {}`. A wheel that cannot answer
  those is not releasable.

Acceptance: `uvx --from <wheel> wwwdrive-mcp` serves the three tools with
`ready` reporting only environment facts, and `run_drive` no longer can return
`drive.py missing`.

### M3 — Collapse the decision policy (2 days)

The degraded functionality lives here, not in the adapters.

- One `verdict(Evidence) -> Verdict` owns accept/stop. Delete
  `_reject_weak_done`, `_moved_on` bypass, the confidence fallback, and
  `already_followed` as separate decisions; express each as a clause in one
  table with named constants (`DONE_MIN`, `ZERO_ACTION_DONE_MIN`,
  `ACTED_DONE_EVIDENCE_MAX`).
- Public stop reasons become a small enum (≈6): `done`, `model_blocked`,
  `no_page`, `max_steps`, `time_budget`, `stale_page`. Internal causes
  (`scroll_only`, `toggle_undo`, `already_followed`) move to `why`, not
  `reason`.
- Reduce three budget knobs to two: keep `max_steps` and `timeout_s`; derive
  the inner deadline from the outer one.
- Test strategy: one table-driven matrix (decision × page × evidence →
  verdict) replaces per-incident tests. Keep a handful of live-derived
  regression rows as data, not as prose.

Acceptance: every accepted DONE is traceable to one line of the table; a
mutation that removes `ZERO_ACTION_DONE_MIN` fails exactly the matrix rows.

### M4 — Split product from lab (1 day)

- Move `scripts/live`, `fixtures`, `bench_nway.py`, and `docs/live-baseline`
  to `lab/` (or a separate repo). Remove `scripts/live` from `testpaths`;
  add `pytest -m lab`.
- Move `jev_driver/processes.py` (pid/zombie/orphan/impl-hash) and the
  metrics aggregate out of the product path into `jev_driver/diagnostics/`
  (or delete). The MCP contract does not need them; `status` needs only
  preflight.
- Delete the second implementation: `preflight` imports `plugin/core/env.py`
  instead of restating it (the pinned duplication is a liability, not a
  guarantee).
- Sweep: delete the 38 branches with no unique commits.

### M5 — Re-establish the live gate (1 day, read-only)

Three runs on example.com/IANA with the goal wording from the ledger, plus one
form flow, recorded in the ledger with commit, ticks, stop reason, and DONE
probability. No mutations. This is the only evidence that M3 did not regress
the product; a green unit suite is not.

### M6 — Encode the budgets in CI (half a day)

Add `scripts/audit.py` (mirrors the repo's existing audit habit) that fails on:

- product kernel > 6,000 lines, or any product module > 600 lines;
- `drive` > 6 parameters, `read` > 5;
- more than 8 public stop reasons, or more than one `DONE_*` bypass;
- docs > 6 files or > 1,200 lines total;
- tests > 1.5× product lines;
- any hard-coded version outside `pyproject.toml`.

Make it part of `pnpm`-equivalent `uv run pytest` gate, so the next agent
cannot grow the surface without deleting something else.

## 5. Sequence

| Week | Work | Gate |
|---|---|---|
| 1 | M1 (merge base), M2 (packaging/MCP entry) | wheel smoke + suite + ruff |
| 2 | M3 (policy), M4 (lab split) | verdict matrix + suite |
| 3 | M5 (live gate), M6 (audit), docs reset | live ledger rows + audit green |
| 4 | Release notes, tag `v1.2.0`, delete branches | install on a clean machine |

## 6. The one rule that keeps it controlled

**A new guard must delete a guard, a test, or a doc paragraph.** The repo's
failure mode was never a bad individual change; it was that every change was
additive and nothing owned the whole.
