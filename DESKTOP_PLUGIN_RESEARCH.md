# Jev driver as a Hermes plugin: Desktop (Electron) vs TUI research

Researched 2026-09-21 from the local Hermes source tree at `~/.hermes/hermes-agent/`
(mirrors https://github.com/NousResearch/hermes-agent) plus live probes on this
machine. All paths are relative to `~/.hermes/hermes-agent/` unless absolute.

## TL;DR

The Desktop app is **not** a sandboxed runtime. It is an Electron shell that
spawns the same Python Hermes agent locally via `hermes serve` on
`127.0.0.1`. Tool execution (terminal/subprocess, plugins, browser) happens in
that local Python backend using the identical `~/.hermes` tree and plugin
loader as the TUI. A plugin that shells out to `drive.py` and talks CDP to a
loopback port will behave identically in TUI and Desktop — provided the
browser itself runs headless (which was verified experimentally, §4).

---

## 1. Desktop tool/terminal execution: local, same machine

- `apps/desktop/electron/backend-command.ts:15-22` — `serveBackendArgs(profile)`
  builds the argv the desktop launches: `hermes serve --host 127.0.0.1 --port 0`
  (legacy runtimes fall back to `dashboard --no-open`, same headless backend,
  see `apps/desktop/electron/backend-serve-support.ts:1-23`).
- `apps/desktop/electron/local-backend-lifecycle.ts:94` — the backend child is
  spawned locally (Electron `child_process.spawn`, process-group isolation,
  `apps/desktop/electron/backend-child.ts:6-13` notes it's the same Python
  backend that "spawned ... a pty terminal").
- Remote is opt-in, not default: `apps/desktop/electron/connection-registry.ts`
  ("The registry ALWAYS contains exactly one `local` connection"), with
  remote/SSH/Cloud entries only when the user adds them
  (`apps/desktop/electron/remote-lifecycle.ts`, `ssh-connection.ts`). When a
  remote connection IS active, tools execute on that host — same rule as the
  TUI's remote terminal backends (`agent/prompt_builder.py`:
  `_REMOTE_TERMINAL_BACKENDS` = docker/ssh/modal/...; local backend = host).
- So: on the default local connection, a plugin's `register_tool` handler runs
  inside the local Python backend and can `subprocess`-spawn anything
  (drive.py, uv, Chromium) exactly as in a TUI session. There is no extra
  sandbox layer for tools in desktop mode; approvals (`approvals.mode`) apply
  the same as in the TUI.

## 2. Plugins: same loader, same HERMES_HOME

- `apps/desktop/electron/main.ts:813-867` — the desktop resolves `HERMES_HOME`
  exactly like the installer scripts (`process.env.HERMES_HOME` respected;
  default `~/.hermes`), then launches the backend under that root.
- `hermes_cli/plugins.py:1-10` — plugin discovery order: bundled
  `<repo>/plugins/<name>/`, **user `~/.hermes/plugins/<name>/`**, project
  `./.hermes/plugins/<name>/` (opt-in), pip entry points. Path comes from
  `get_hermes_home()` (`hermes_cli/plugins.py:1524-1526`), which the serve
  process resolves from its `HERMES_HOME` env — identical in TUI and desktop.
- `hermes_cli/plugins_discovery.py:158-160` — `user_dir = get_hermes_home() / "plugins"`.
- Plugin tools register via `PluginContext.register_tool()`
  (`hermes_cli/plugins.py:460`), same `tools/registry` the TUI dispatches
  through. Registration is gated by `plugins.enabled` in `config.yaml`
  (`hermes_cli/plugins_discovery.py:80-96`) — the same config file is read by
  the serve backend.
- **The one real difference is the profile.** The desktop runs a named profile
  (`serveBackendArgs(profile)`; per-profile routing in
  `apps/desktop/electron/desktop-profile.ts` / `profile-session-routing.ts`).
  Profiles are rooted at `~/.hermes/profiles/<name>/` with their own
  `plugins/`, `skills/`, `cron/` (see `agent/system_prompt.py:387-407`). So a
  plugin must be installed into the profile the desktop session uses — or the
  desktop's profile uses the default home. Check which HERMES_HOME/profile the
  desktop targets before assuming `~/.hermes/plugins/` is picked up.
- Do not confuse **agent plugins** (`~/.hermes/plugins/<name>/plugin.yaml` +
  `__init__.py::register(ctx)`) with **desktop half-packages**:
  `apps/desktop/electron/desktop-plugins-root.ts` describes
  `<HERMES_HOME>/desktop-plugins/` and `plugins/<name>/desktop/plugin.js` —
  those extend the Electron UI (panes, palette commands), not the agent's
  toolset. Our `jev_drive` tool is an *agent plugin* and needs none of that.

## 3. Hermes' built-in `browser` toolset: agent-browser, CDP-capable

- `tools/browser_tool.py:2` — "Browser automation tools driven by the
  **agent-browser CLI**". It resolves a bundled/npx agent-browser binary
  (`tools/browser_tool.py:140-166`).
- agent-browser is the exact engine bundled with terminal-browser
  (`~/.local/share/terminal-browser/app/agent-browser/bin/agent-browser`, v0.33.0).
  Its strings reveal: `agent_browser_get_cdp_url` ("Get the current browser CDP
  URL"), `--cdp` connect flag, `AGENT_BROWSER_CDP`, `AGENT_BROWSER_HEADED`,
  daemon mode with `AGENT_BROWSER_SOCKET_DIR` (`~/.agent-browser` socket dir),
  and `--remote-debugging-port=0` + `DevToolsActivePort` in its launch args.
- User-supplied CDP override exists too: `tools/browser_tool_cdp.py:6-47`
  (`browser.cdp_url` config resolves `ws://host:port` →
  `webSocketDebuggerUrl` via `/json/version` — the same discovery drive.py does).
- Therefore **yes**: when Hermes launches its local Chromium (headless by
  default), it exposes a loopback CDP websocket, and our driver could attach
  to that browser. Caveat: Hermes may credential-scrub/re-spawn agent-browser
  subprocesses (`tools/browser_tool.py:28-29`) and the browser lifecycle is
  tied to the browser tool session (`tools/browser_tool_lifecycle.py`) — we
  don't own it, and attaching to the same profile mid-session risks tab
  contention with the built-in tools.

## 4. terminal-browser headless: verified feasible

Two layers, only one of which matters to the driver:

- The **renderer** (Electron TUI pane, kitty graphics) needs a TTY — that part
  is irrelevant under Desktop.
- The **engine** (`agent-browser` binary inside terminal-browser) runs fully
  detached, no TTY. **Verified live on this machine**: ran
  `~/.local/share/terminal-browser/app/agent-browser/bin/agent-browser open
  https://example.com --session jev-headless-test` from a non-PTY shell — it
  spawned system Chrome with `--headless=new --remote-debugging-port=0`
  (ps output confirms), loaded the page (`✓ Example Domain`), reported daemon
  state via `session info` (daemon pid, socket dir `~/.agent-browser`), and
  closed cleanly. Also in the binary: doctor's "Headless launch + about:blank"
  probe and `AGENT_BROWSER_NO_XVFB`/`AGENT_BROWSER_HEADED` knobs.
- The daemon model means sessions persist across tool calls and the CDP port
  is discoverable (`DevToolsActivePort` file / `session cdp-url`). Note the
  terminal-browser *wrapper* (`terminal-browser ls --json`, which drive.py's
  `jev_driver/cdp.py:22-35` uses for port discovery) only lists TUI-attached
  instances — headless engine sessions are visible via agent-browser, not via
  `terminal-browser ls`.

## 5. Recommended architecture

Options considered:

- **(a) Plugin shells to drive.py with auto-discovery of ANY running loopback
  CDP browser** — ✅ **chosen**.
- (b) Plugin launches its own headless Chromium — works (verified in §4) but
  forks browser lifecycle and profiles; a second browser to babysit; we'd
  re-implement launch flags, daemon, and cleanup that agent-browser already
  does.
- (c) Attach to Hermes' built-in browser toolset instance — works (CDP is
  exposed, §3) but we don't own the lifecycle, risk tab/lease contention with
  `browser` toolset calls, and it silently changes behavior depending on
  whether the user has the browser toolset enabled.

**Recommended: option (a), with (b) as the automatic fallback launch path.**

Concretely:

1. `~/.hermes/plugins/jev-driver/plugin.yaml` + `__init__.py::register(ctx)` →
   `ctx.register_tool("jev_drive", ...)` (`hermes_cli/plugins.py:460`). The
   tool handler shells out to `uv run scripts/drive.py` — no Python API
   coupling, works from any Hermes surface that has a terminal.
2. Port discovery ladder in drive.py (replace the current
   `terminal-browser ls --json`-only path in `jev_driver/cdp.py:27-35`):
   1. explicit `--cdp` / `JEV_CDP_URL` (Hermes' own `browser.cdp_url`
      convention, `tools/browser_tool_cdp.py:6`);
   2. `terminal-browser ls --json` (existing TUI case);
   3. agent-browser daemon (`~/.agent-browser` / `agent-browser session
      cdp-url`) — covers Hermes' browser toolset AND headless sessions;
   4. loopback probe of the classic 9222+ range as last resort;
   5. fallback: launch a headless engine via the bundled agent-browser
      (`agent-browser open <url> --session jev`, daemonized) and rediscover —
      this gives Desktop parity with zero TTY required.
3. Install into the profile the desktop session actually uses (check
   `hermes profile list` / the desktop connection's profile), and enable it
   under `plugins.enabled` in that profile's `config.yaml` — the loader is
   shared, so nothing else differs between TUI and Desktop.

### Why this is the clean cut

- The desktop backend is the same Python process model as the TUI: same
  `PluginManager`, same `get_hermes_home()`, same local subprocess semantics
  (`hermes serve --host 127.0.0.1`, §1). The only environmental variable is
  the profile root and whether a *human* is attached to a TTY.
- Decoupling "renderer" from "browser" (what drive.py already does — it only
  needs a websocket) makes TUI vs Desktop a launch-target question, not a
  code-path question.

### Evidence appendix (one-line refs)

- Desktop backend spawn: `apps/desktop/electron/backend-command.ts:15-22`,
  `local-backend-lifecycle.ts:94`, `backend-serve-support.ts:1-23`
- Desktop is local-by-default: `apps/desktop/electron/connection-registry.ts`
  (LOCAL_CONNECTION_ID always present)
- HERMES_HOME resolution: `apps/desktop/electron/main.ts:813-867`
- Plugin discovery: `hermes_cli/plugins.py:1-10,1524`,
  `hermes_cli/plugins_discovery.py:80-96,158-162`
- Tool registration API: `hermes_cli/plugins.py:460` (`register_tool`)
- Desktop-plugin ≠ agent-plugin: `apps/desktop/electron/desktop-plugins-root.ts`
- Browser toolset = agent-browser: `tools/browser_tool.py:2,140-166`
- CDP override/discovery: `tools/browser_tool_cdp.py:6-47`
- Headless verified: live run 2026-09-21, `--headless=new
  --remote-debugging-port=0` Chrome spawned by
  `~/.local/share/terminal-browser/app/agent-browser/bin/agent-browser` from a
  non-TTY shell; loaded example.com; daemonized session in `~/.agent-browser`.
