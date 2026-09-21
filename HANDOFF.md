# HANDOFF — jev-terminal-browser-driver (session continuation, 2026-09-21)

Read this first in a new session. Everything below is verified state + remaining work.

## What the project is
- Repo: `/Users/marius/Projects/jev-terminal-browser-driver` (branch `feature/hermes-plugin`, main has the published state)
- Public repo: https://github.com/nedzen/jev-terminal-browser-driver (main pushed)
- Port of browser-use/jev-ultrafast agent loop driving a real browser with Jev
  (typed-decision model) via OpenRouter `https://openrouter.ai/api/alpha/decisions`
  model `typesafe/jev-1.13`. NO TypeSafe key, NO Laya (explicitly out of scope).
- Reads: snapshot.js element table + ≤6K text, decisions = one OpenRouter call
  (operation + per-op target heads, independent, strict probability validation),
  acts via raw CDP. Zero screenshots, ~200 bytes JSON per tick to agent context.
- Also ships: SKILL.md, README.md (with demo video), bench (20/20 @ N≈20, 8/8 @
  N=120), LICENSE + attribution (MIT, verbatim files from jev-ultrafast).
- Upstream repo (read-only reference): /Users/marius/Projects/jev-ultrafast

## Branch state (feature/hermes-plugin, all committed)
- e3bd2bf CDP discovery ladder
- 7093341 Hermes jev_drive plugin
- a314746 cold-start probe race + deferred tool schema
- 29bd660/c93ae07 AGENT_BROWSER_ENGINE env fixes (see Pitfall below)
- Working tree may contain HANDOFF.md + research docs (untracked) — commit or
  ignore as desired.
- 47/47 pytest green, ruff clean. NOT merged to main, NOT pushed (branch).

## Plugin (working, verified live)
- `plugin/` (plugin.yaml, __init__.py, handler.py) — native `jev_drive` tool,
  toolset `jev`, subprocess to `scripts/drive.py --json`, strict schema
  (goal/url/target/max_steps≤30/cdp_url/timeout_s≤900), check_fn gating.
- Discovery ladder (jev_driver/discover.py): explicit --cdp/JEV_CDP_URL →
  terminal-browser ls → agent-browser daemon (~/.agent-browser, session
  `jev-driver`) → loopback 9222-9330 → headless auto-provision.
- Installed: symlink `~/.hermes/plugins/jev-driver` + profiles intern/memory
  (per-profile install REQUIRED — no inheritance; use scripts/install_plugin.sh).
  Enabled in configs. Live-verified: `hermes chat -q` deferred search → load →
  call → `success done` (click fixture + Google Flights ZRH→LHR full form flow).
- Desktop-parity test (fresh session via test-jev pane): full ZRH→LHR flow ran
  autonomously, reached real results URL (tfs= blob), flights visible
  (easyJet/SWISS nonstops in snapshot). Status "blocked" only because tick
  budget 14 exhausted post-submit — consider default max_steps 14→16 or
  counting differently. Headless browser is invisible by design (agent-browser
  engine); to watch, run a terminal-browser instance first (ladder picks it).

## Key pitfalls discovered (already fixed in code, know they exist)
1. `~/.hermes/.env` had `AGENT_BROWSER_ENGINE=f0246…` (engine HASH). Hermes
   injects .env into session env; agent-browser rejects non-name engines →
   every in-Hermes auto-launch died while terminal tests passed. Fix:
   discover.py `_child_env()` keeps AGENT_BROWSER_ENGINE only if
   chrome|lightpanda, else pops it. If weird "Unknown engine" errors return,
   check .env and `~/.agent-browser/jev-driver.*` (corrupt session state —
   `agent-browser close --session jev-driver` + delete files fixes).
2. browser.py mutable-global import (`from .discover import LAST` binds None) —
   now uses `_discover.LAST`. Never reintroduce.
3. `Target.createTarget` unsupported on terminal-browser's Electron; and NEVER
   `Target.closeTarget` (TUI PageHost crash "Object has been destroyed"). Close
   = detach only. Never `terminal-browser shutdown`.
4. Plugin tools are DEFERRED: model must tool_search → load → call.
5. Cold-start: first `get cdp-url` auto-launches Chrome (~10-15s) — probe
   timeout is 20s + retry loop; don't shorten.
6. Tests: 47 offline, `uv run pytest` (no network, no live browser needed).

## Remaining TODO (in priority order)
1. **Merge** `feature/hermes-plugin` → main after user is satisfied; push.
   Round 1 (desktop preview.open emit + page_text) and Round 2 (TUI watch,
   session continuity, overlay wait, WatchAgent takeover) are COMMITTED on the
   branch (0d8f914, 9e1d561) — live-verify watch+continuity once in the TUI
   and desktop, then merge.
2. **Separate plugin repo** (user decision): extract `plugin/` + install script
   into standalone repo for publishing. Plugin is stdlib-only by design.
3. **Desktop zap button** (user wants; zap icon saved at
   `~/Desktop/codicon-zap.svg`, 16x16, matches toolbar grid). Research done:
   `docs/research/DESKTOP_BUTTON_RESEARCH.md`. Verdict: preview toolbar is NOT
   pluggable; ship v1 as desktop `plugin.js` (statusBar.right chip + popover,
   `onEvent('tool.started'/'tool.completed')` watching `jev_drive`,
   `defaultEnabled: true` via standalone `~/.hermes/desktop-plugins/` door —
   the unified door forces defaultEnabled:false). Upstream preview-bar slot PR
   (~60-100 lines) deferred until second consumer (Hermes AGENTS.md forbids
   single-consumer seams). Also design decision: toggle gate backend-
   authoritative via plugin_api.py + ctx.rest.
4. **Cleanup:** ~9 leftover fixture tabs in terminal-browser TUI (close by
   hand; CDP close crashes TUI). Unexplained: user's X tab disappeared from
   browser list mid-session (grok denies touching it; unresolved).
5. Optional polish: `--json` final envelope `{event:"finished"}`, /drive slash
   command (deferred by design), cold-start note in NOTES.

## People / panes (may be stale in new session — re-check `herdr agent list`)
- HQ hermes: was wE:p1D (tab wE:tD). grok: wE:p1F (worker, always-approve,
  plan-mode via shift+tab; cwd = repo). research: wE:p1G. test-jev: wE:p1H
  (fresh default-profile session used for the desktop-parity live test).
- Workers know report-back via `herdr agent prompt wE:p1D "<msg>"` (update pane
  id to the new HQ pane in the new session).

## Env facts
- Auth: OPENROUTER_API_KEY in ~/.hermes/.env (carried a poisoned
  AGENT_BROWSER_ENGINE hash — harmless now, code scrubs it; user may clean it).
- uv on PATH; repo venv via `uv sync`.
- Local test fixtures: repo `fixtures/{click,search,nway}.html`.
- Bash history of the whole build lives in this session; all durable knowledge
  is in repo docs: README.md (plugin-centric), SKILL.md, HANDOFF.md,
  docs/README.md (index), docs/architecture.md, docs/research/* (3 research
  docs), docs/archive/iteration-1-cli/* (pre-plugin notes + bench).
