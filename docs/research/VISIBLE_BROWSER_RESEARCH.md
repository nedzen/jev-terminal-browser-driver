# Making jev_drive visible & user-takeover-able: Desktop mirror / Desktop true-drive / TUI watch

Researched 2026-09-21. Builds on `DESKTOP_PLUGIN_RESEARCH.md` (backend/plugin
loading parity) and `DESKTOP_BUTTON_RESEARCH.md` (desktop plugin surface:
areas, SDK doors, tool-event tap) — that context is not repeated here. Paths
relative to `~/.hermes/hermes-agent/` unless noted.

## TL;DR verdicts

| Path | Verdict | One line |
|---|---|---|
| Q1 Desktop **mirror** | ✅ Feasible **today, plugin-only** — but via a plugin-owned pane with an iframe, NOT by navigating the core preview pane | `openPreview` is not exposed to plugins; `host.revealPane` + a plugin pane + iframe is |
| Q2 Desktop **true-drive** (CDP into the preview webview) | ❌ Not feasible via plugin; upstream-only, and it fights two written security gates | No CDP door; packaged app never opens a debugger port; capability bridge is deliberately narrow |
| Q3 TUI **watch** | ✅ Feasible — option (a), herdr split + `terminal-browser open --split` | terminal-browser's CLI natively speaks herdr (`HERDR_*` env), and hermes-under-herdr inherits it |

---

## Q1. Desktop mirror: can a plugin point the preview pane at a URL?

**How the preview pane gets its URL.** `src/app/chat/right-rail/preview-pane.tsx`
takes `target.url` as a prop (line 146); the target comes from `$previewTabs`
(`src/store/preview.ts:130`, persistentAtom) through the `$previewTarget`
computed (`preview.ts:170`). Navigation is the pane-internal `navigateTo`
callback (`preview-pane.tsx:693-703`). Crucially, the store's own docstring:
"`openPreview` is the **only entry point**, so a tool result, a file-browser
click, and an artifact card all travel the same road"
(`src/store/preview.ts:13-15`); `openPreview(target)` at `preview.ts:385`,
`openBrowserTab()` at `preview.ts:402`.

**Can plugin.js reach it? No.** The renderer-side surface a plugin gets is
`@hermes/plugin-sdk` only — the runtime loader rewrites bare specifiers to
SDK/react shims (`src/contrib/runtime-loader.ts:6-30`; import map
`src/sdk/runtime.ts`), so a plugin cannot `import '@/store/preview'`. What
the SDK exposes:

- `host.state.*` — **READONLY** curated atoms (`src/sdk/index.ts:13-15`,
  definition at `640-696`: activeSessionId, busy, cwd, gateway, model,
  profile, viewport, focusedUsage…). **No preview atoms** — no
  `$previewTabs`, no way to even *read* the open preview URL.
- `host.navigate(path)` — hash **app-router** navigation only
  (`src/sdk/index.ts:720-733`); it cannot open a preview tab.
- Export tail (`src/sdk/index.ts:1833-1925`) has no `openPreview` /
  `openBrowserTab` re-export.
- `host.request` is gateway JSON-RPC — the backend has no "open desktop
  preview" verb (it's a renderer store action, not a gateway method).

**What the plugin CAN do (the feasible mirror).** Plugins register `panes`
contributions (established in DESKTOP_BUTTON_RESEARCH §1) and get
pane-management verbs on the SDK: `host.paneVisibility(paneId)`
(`src/sdk/index.ts:1334-1342`), `host.undismissPane(paneId)` (1344-1355),
**`host.revealPane(paneId)` — "Reveal a contributed pane and its zone from an
explicit user action" (1357-1363)**. So the mirror is:

1. Plugin registers a `panes` contribution rendering its own React surface
   with an `<iframe>` (full renderer authority; error-isolated).
2. `ctx.onEvent('tool.started')` filter `tool_name === 'jev_drive'` (tap at
   `src/contrib/events.ts:38-46`; events carry the tool name and args) — on
   first sighting, set a local atom with the driven URL and call
   `host.revealPane('jev-driver:mirror')` (+ `undismissPane` first if the
   user closed it, per the docstring's remembered-Close semantics).
3. Later `tool.started` events with a different `url` update the atom → the
   iframe's `src` follows.

**If the pane is closed:** `revealPane` re-fronts it; `undismissPane`
un-remembers a user Close so adoption restores it. Both feature-detectable on
older desktops (`typeof host.revealPane === 'function'`).

**Security note:** the iframe shows pages the agent was already driving in a
real browser; the AGENTS.md guest-content rules
(`apps/desktop/AGENTS.md:163-186`, sandboxed artifact iframes +
`persist:hermes-preview` webview, `setWindowOpenHandler` denies everything) are
about *guest pages opening things*. A plugin-pane iframe doesn't grant popups
and the core `window-open-policy.ts` still applies to the window; but mirror
it as plain display, and prefer `sandbox`-less-but-blocked-popup defaults over
re-implementing any guest bridge. No conflict found, but don't add
`allowpopups`-style affordances from a plugin.

**Honest limitation:** this is a *second* view of the driven page, not the
user's existing preview pane, and headless Chrome won't render visuals the
iframe can reproduce unless the driven page is also reachable by URL — it is
(jev only ever navigates real URLs). Auth/session mismatch (cookies live in
the agent-browser profile, not the desktop webview partition) is the real
gap: pages behind logins will show their logged-out state in the mirror.

## Q2. Desktop true-drive: attach CDP to the preview `<webview>`

**What exists:**
- The app enables `--remote-debugging-port` ONLY in dev:
  `electron/main.ts:575` + `electron/dev-cdp.ts:1-45` — "packaged build →
  always closed, whatever the env says" (`dev-cdp.ts:26-29`). That port is for
  the **renderer page** (dev tooling), not the preview webview, and it is
  deliberately not configurable to off-host.
- The renderer can get the guest's `webContentsId` (`webview.getWebContentsId()`,
  used at `preview-pane.tsx:415,1216` for the annotate capture through
  `window.hermesDesktop.capturePreview`) — an IPC **capability**, not raw CDP.
- No `webContents.debugger.attach()` anywhere in `electron/` (grep: only
  dev-cdp's command-line switch and the updater). The capability bridge is
  "narrow, typed" by design (`AGENTS.md:20-21`: "native power arrives through
  a deliberate capability, not a general escape hatch").

**Why the plugin can't do it:** CDP attach to a webContents is a **main-process
Electron** operation (`webContents.debugger`). plugin.js runs in the renderer
realm via blob import (`runtime-loader.ts:20-28`) and has no door to it; the
bridge exposes capture/navigate-ish verbs, never a debugger. No
`browser_remote_debugging` IPC exists in `electron/fs-ipc.ts` or the preload.

**Why even an upstream patch is a hard sell:**
1. It widens the capability bridge with a debugger-equivalent power — the
   exact "general escape hatch" AGENTS.md:20-21 forbids.
2. `dev-cdp.ts:23-29` shows the repo's posture: a debugger port on a packaged
   app is the one hard gate. A permanent CDP door into guest content
   (arbitrary web pages the user browses) is a bigger version of that risk —
   any renderer compromise → page credentials/cookies via CDP.
3. The annotate feature already crosses into the guest, but via a
   **trusted, purpose-scoped** path (`executeJavaScript` from the host,
   trusted-click forwarding — `AGENTS.md:170-186`); a generic CDP channel is
   not purpose-scoped.

**What takeover could look like if pursued upstream anyway:** main-process
feature that `webContents.debugger.attach()`es only the *jev-launched*
headless target... but the preview webview is a different browser instance
from agent-browser's Chrome — you cannot attach agent-browser's daemon to the
webview's target list because they are separate Chromium processes; you'd be
driving the webview with a second CDP client (Input.dispatchMouseEvent
etc.), i.e. a new driver backend, not a reuse of drive.py. Practical verdict:
**forbidden-by-design / impractical. Recommend against; the plugin mirror (Q1)
is the visibility story on Desktop.**

## Q3. TUI watch: a visible terminal-browser pane when none is running

**Mechanics (verified on this machine):**
- `terminal-browser open --help` (v current, `~/.local/bin/terminal-browser`):
  "Opens the browser in the **current pane**. Pass `--split` to open it in a
  **new split pane** instead" — `--split right|left|down|up --size 0.2..0.95`,
  `--no-merge`. So "takes over the current pane" is solved upstream: users
  (and our handler) pass `--split`.
- The terminal-browser CLI detects and drives herdr natively: its bundled CLI
  references `HERDR_PANE_ID`, `HERDR_TAB_ID`, `HERDR_BIN_PATH`,
  `HERDR_CONFIG_PATH`, `HERDR_SOCKET_PATH` (`cli/dist/main.js` — 8 hits), and
  a herdr terminal driver exists in its pixel terminal sources (herdr.js in
  the sourcemap list). When hermes runs inside a herdr pane, its process env
  carries `HERDR_*` (verified live in this session: `HERDR_ENV=1`,
  `HERDR_PANE_ID=wE:p1G`, socket path set), and any subprocess — the plugin
  tool handler's `uv run` — inherits it. So `terminal-browser open <url>
  --split right` issued from the tool handler targets the pane hermes lives
  in without extra wiring.
- **Hermes core does not know herdr at all** (grep over `agent/`,
  `hermes_cli/`, `gateway/`, `tools/`: zero hits for herdr/HERDR_ENV) — the
  integration is purely environmental. That's fine for a plugin: no core
  change needed. Under **Desktop**, there is no herdr and no TTY, so this
  path correctly no-ops → fall back to the headless mirror of Q1.

**Option ranking for the backend plugin:**
- (a) **herdr split + `terminal-browser open --split right <url>`** — ✅
  chosen. Guard with the ladder: `HERDR_ENV` present + `terminal-browser`
  on PATH → split-open; the daemon then exposes CDP and drive.py attaches to
  the SAME browser it just opened (visible and driven are one instance — this
  is the whole point: takeover = user grabs the mouse in the pane; agent CDP
  clicks continue until the user navigates).
- (b) require a running TB and fail helpfully — keep as the fallback message
  when `terminal-browser` exists but `HERDR_ENV` is absent (e.g. tmux/wezterm
  users): error text should say "run `terminal-browser open --split right`
  (or open it in your terminal) and retry; driving attaches automatically."
- (c) agent-side instruction — reject as primary: non-deterministic, burns a
  turn, and the model may run `open` without `--split` and eat the session
  pane. Keep only as a documented hint in the tool description.

**Takeover semantics worth stating:** agent-browser keeps driving whatever
tab it holds; user takeover is natural (they navigate/click the visible
pane; drive.py should detect navigated-away/lease mismatch and yield rather
than fight — mirror the "user's mouse wins" contract; a lease re-check at the
start of each step in `drive.py` is the cheap version).

## Feasibility summary + security-rule conflicts

- **Mirror (Desktop):** no rule conflicts; uses documented SDK doors
  (`revealPane`, `undismissPane`, `onEvent`) and a plugin-owned pane. Not the
  core preview pane — that would need upstream `openPreview` exposure, which
  is the same single-consumer-seam problem documented in
  DESKTOP_BUTTON_RESEARCH §3.
- **True-drive (Desktop):** conflicts with `AGENTS.md:20-21` (narrow bridge)
  and `dev-cdp.ts:26-29` (packaged debugger gate posture). Verdict: no.
- **Watch (TUI):** no conflicts; entirely outside Hermes core (env-level
  integration), which matches the repo's seams.

### Evidence index

- Preview entry point singleton: `src/store/preview.ts:13-15,385,402`; tabs
  atom `:130`; target computed `:170`
- Preview pane prop flow: `src/app/chat/right-rail/preview-pane.tsx:146,266,693-703`
- SDK surface (readonly state, hash-only navigate, no preview export):
  `src/sdk/index.ts:13-15,640-696,720-733,1833-1925`
- Pane verbs: `src/sdk/index.ts:1334-1363` (paneVisibility / undismissPane /
  revealPane)
- Event tap: `src/contrib/events.ts:38-46`
- Dev-only CDP gate: `electron/main.ts:575`, `electron/dev-cdp.ts:1-45`
- No webContents.debugger usage: grep `electron/*.ts` (only dev-cdp +
  updater argv)
- webContentsId → capability IPC only: `preview-pane.tsx:415,1216`;
  `preview-annotate-host.ts:19-27`
- Guest-content security: `apps/desktop/AGENTS.md:163-186`; narrow bridge:
  `AGENTS.md:20-21`
- terminal-browser split: `terminal-browser open --help` (— `--split right`,
  "opens in the current pane" otherwise); herdr env integration: 8×HERDR_*
  hits in `~/.local/share/terminal-browser/app/cli/dist/main.js`; hermes core
  has zero herdr references
- Live env: `HERDR_ENV=1`, `HERDR_PANE_ID`, `HERDR_SOCKET_PATH` in this
  hermes session (inherited by tool subprocesses)
