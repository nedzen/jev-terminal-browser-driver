# Desktop 'Jev driving' preview-toolbar button: research

Researched 2026-09-21 from `~/.hermes/hermes-agent/apps/desktop` (local source,
authoritative) on the `preview-browser-bar` question. Paths relative to
`apps/desktop/` unless noted. `preview-browser-bar.tsx` is confirmed at
`src/app/chat/right-rail/preview-browser-bar.tsx` (279 lines); the annotate
pattern lives in `preview-annotate-card.tsx` / `preview-annotate-host.ts`.

## TL;DR

- The Desktop plugin surface is real and reasonably rich (panes, palette
  commands, routes, layouts, status-bar chips, gateway events, REST/socket to a
  plugin backend, OS door) — but the preview browser toolbar is **not** one of
  its areas. It renders hardcoded props (`PaneStripGlyph`s), consults the
  contribution registry nowhere. A plugin **cannot** put a button there today.
- The "Jev is driving" signal is easy: the gateway broadcasts
  `tool.started`/`tool.completed` events with the tool name, and every inbound
  gateway event is fanned to plugins before app dispatch. A desktop plugin
  watches for `jev_drive` — no new channel needed.
- Recommended path: ship the indicator as a desktop plugin **now** using an
  existing surface (status-bar chip or preview-adjacent pane), and only file
  the upstream preview-bar slot PR when a second real consumer exists — the
  desktop AGENTS.md explicitly forbids extension seams for a single consumer.

---

## 1. What a user-level desktop plugin can do today

Delivery doors (`src/contrib/plugins.ts:15-18`, `src/plugins/README.md`):

- Bundled: `src/plugins/<name>/plugin.{js,ts,tsx}` (vite glob, in-repo only).
- Runtime disk door: `<hermes home>/desktop-plugins/<name>/plugin.js`.
- Unified package half: `~/.hermes/plugins/<name>/desktop/plugin.js` —
  **materialized** by Electron into the app-level root
  (`electron/desktop-plugins-root.ts:6-25` documents the copy + marker
  semantics; `materializeDesktopHalf` at `desktop-plugins-root.ts:116+`).

Loading pipeline (`src/contrib/runtime-loader.ts:6-30`): plain-ESM
`plugin.js` → integrity check → bare-specifier rewrite to SDK/react shim blobs
→ blob `import()` → validate default `HermesPlugin` → `register(ctx)`. Hot
reload on file change. Security posture: **not a sandbox** — full renderer
authority, error isolation only (`runtime-loader.ts:20-28`).

⚠️ Default-on nuance: the unified `~/.hermes/plugins/<name>/desktop/` door
sets a root-level `defaultEnabled: false` CAP — installed-but-inert until the
user toggles it in Capabilities ▸ Plugins (GHSA-mcfc-hp25-cjv7;
`runtime-loader.ts:46-49`). The standalone `~/.hermes/desktop-plugins/` door
honors the plugin's own `defaultEnabled: true`. For "on by default when
installed," ship as a **standalone desktop-plugin** or accept the one-time
opt-in toggle.

Extension surface — a plugin receives a scoped `PluginContext`
(`src/contrib/plugin.ts:76-107`) and registers `Contribution`s
(`src/contrib/types.ts:29-63`: `{id, area, render|data, when, order, enabled}`)
into a shared registry (`src/contrib/registry.ts`). Areas actually consumed
by the app today:

| Area | Consumer | Evidence |
|---|---|---|
| `panes` | pane tree (PaneHost) | `src/components/pane-shell/tree/store.ts:695,735`; `controller.tsx:700` |
| `layouts` | layout picker presets | `src/components/pane-shell/tree/renderer/layout-picker.tsx:113` |
| `routes` | app routes | `src/app/routes.ts:100-106` |
| `palette` | command palette | `src/app/command-palette/contrib.ts:10` (`PALETTE_AREA = 'palette'`) |
| `statusBar.left` / `statusBar.right` | status bar (data/render) | `src/app/contrib/controller.tsx:106,824-835`; live example `src/plugins/hello-runtime/plugin.runtime.js` (`area: 'statusBar.right'`) |
| `workspace.pageHeader` | workspace title | `controller.tsx:482,500` |

Plus non-UI doors on the context: `onEvent(type|'*')` (every gateway event,
pre-dispatch fan-out — `src/contrib/events.ts:38-46`), `rest()` to a
`plugin_api.py` backend (`/api/plugins/<id>...`, mounted at
`~/.hermes/hermes-agent/hermes_cli/web_server.py:596-608,995`), `socket()`
live WebSocket to the same namespace, `os` (notify/openExternal/reveal/
pickers/clipboard), namespaced `storage`, `i18n`.

**Bottom line for Q1:** panes yes, palette commands yes, status-bar chips yes,
DOM injection no (and not needed — render is React via the registry). A
**preview-browser-bar button: no.** `preview-browser-bar.tsx` takes
`PreviewBrowserBarProps` and renders fixed `PaneStripGlyph`s
(`preview-browser-bar.tsx:100-279`); there is no `useContributions` /
registry call anywhere in the right-rail preview code, and no preview-bar area
exists (full area audit above).

## 2. Existing in-repo examples

- `src/plugins/hello-runtime/plugin.runtime.js` — the canonical runtime-door
  example: plain ESM, `jsx()` calls, registers a `statusBar.right` chip that
  subscribes to app state via `@hermes/plugin-sdk` (`host.state`, `Tip`,
  `useValue`). Shipped as raw text through the real runtime pipeline.
- `src/plugins/radio/`, `src/plugins/hermes-bots/` — bundled examples; the
  README points heavier samples to the companion
  `hermes-example-plugins` repo.
- No example adds toolbar chrome to a core bar — the areas are panes, status
  bar, palette, routes, layouts. The capability bridge is
  `createPluginContext` (`src/contrib/plugin.ts:217-244`) + the SDK import map
  (`src/sdk/runtime.ts`); session-sourced gating for agent-callable renderer
  capabilities is described in `AGENTS.md:216-219` ("source: 'desktop' on
  `session.create`").

## 3. Minimal upstream patch if the slot must exist

Given `preview-browser-bar.tsx` is a pure-props component, the smallest
honest change:

1. Define one new area id, e.g. `previewBrowserBar` (constant beside
   `PALETTE_AREA` in `src/app/command-palette/contrib.ts` pattern, or a small
   `src/app/chat/right-rail/preview-areas.ts`).
2. In `preview-browser-bar.tsx`, after the annotate/comment group (~line 232),
   render plugin bar-items:
   `const extras = useContributions('previewBrowserBar')` (hook exists at
   `src/contrib/react/use-contributions.ts:10-11`) → map to
   `PaneStripGlyph`-shaped items (icon/label/active/onSelect) — i.e. constrain
   contributions to the same visual contract, not arbitrary DOM.
3. Capability gating per AGENTS.md:216: only render when the session's source
   is `'desktop'` — availability keyed off the session client, not the backend
   process env.
4. Test: register/unregister updates the bar; non-desktop session hides the
   area; plugin crash isolation (ContribBoundary already covers render
   errors).

Size estimate: **~60–100 lines + one test file**. That is genuinely small —
but AGENTS.md:206-213 says the internal registries "are composition seams,
not a public plugin ABI; do not build a universal extension system ... for a
single consumer. Design a shared contract only once more than one real
consumer proves its shape." Today `previewBrowserBar` has exactly one
consumer (jev). Expect pushback on an upstream PR now; expect acceptance once
a second preview-bar action exists.

## 4. State ownership and the "Jev is driving" channel

Per `AGENTS.md:22-37` (authority rule):

- **Toggle (Jev driving enabled/disabled)** — this is a user preference about
  whether the agent may use the tool. Two layers:
  - Whether the *desktop UI* shows/reacts: renderer/plugin-scoped storage is
    correct (`ctx.storage`, keys under `hermes.plugin.<id>.`,
    `plugin.ts:110-135`) — it is this window's presentation.
  - Whether the *agent* may drive: backend-authoritative. The unified package
    can ship a `plugin_api.py` (`ctx.rest`) holding the flag, and the agent
    half's `jev_drive` tool consults it (or the tool is enabled/disabled via
    the standard plugin config). The desktop toggle then does
    `ctx.rest('/toggle', {on})` — optimistic UI, backend refresh wins.
- **"Jev is currently driving" (live)** — backend-authoritative by definition;
  the renderer's copy is a cache. **No new channel is needed**: the gateway
  already emits `tool.started` / `tool.completed` with the tool name
  (`gateway/run_turn_runner.py:133-158` — event_type strings emitted
  verbatim), and `src/contrib/events.ts:38-46` fans every inbound gateway
  event to plugins before app dispatch (the desktop's own tool-call rendering
  uses the same stream — `use-message-stream/gateway-event/` + `upsertToolCall`).
  A plugin does `ctx.onEvent('tool.started', e => e.name === 'jev_drive' && ...)`
  in a nanostore atom; the chip/popover subscribes. Polling is unnecessary;
  the annotate host isn't the model to copy here (it's in-page overlay I/O,
  `preview-annotate-host.ts:56-78`), the tool-event tap is.
- Crash/edge: `tool.completed` with `error` should clear the driving state;
  missed events on reconnect are self-healing on the next `tool.started`.

## 5. Recommendation

**Pick: (i) pure desktop `plugin.js` now, using existing surfaces; defer
(ii)'s upstream toolbar PR until a second consumer exists; keep (iii) out.**

Concretely, the smallest correct path that delivers the *intent* (see Jev,
turn it off) without widening a seam for one consumer:

1. `~/.hermes/desktop-plugins/jev-driver/plugin.js` (standalone door → honors
   `defaultEnabled: true`, so it's on when installed).
2. Registers:
   - a `statusBar.right` chip with a zap Codicon + active state, exactly like
     `hello-runtime`'s chip — click opens a small popover (render is free-form
     React): "Jev is driving <url> · turn off";
   - `onEvent('tool.started' | 'tool.completed')` watcher on
     `tool_name === 'jev_drive'` driving a tiny nanostore → chip lights up
     while driving (backend-authoritative state, renderer cache).
3. Toggle semantics: `ctx.storage` for the window preference +
   `ctx.rest` to a tiny `plugin_api.py` in the same package for the
   backend-authoritative gate that `jev_drive` checks. (Interim: toggle
   disables via `hermes plugins` config; note the desktop-half CAP means
   unified `~/.hermes/plugins/<name>/desktop/` installs start opt-in.)
4. If the chip is judged insufficient, ALSO open the upstream PR from §3 —
   it is small (~60-100 lines) and gated on session source `'desktop'` — but
   frame it honestly: one consumer today; the second (any future preview-bar
   action, e.g. a "zoom"/"reader mode") should land with it or shortly after.

Why not (ii) alone: the button would exist but the PR contradicts the repo's
written rule for single-consumer seams; a rejected/mothballed PR leaves the
feature with no surface at all. Why not (iii): the request is exactly about
desktop visibility, and everything short of the toolbar slot is already
achievable in (i).

### Evidence index

- Doors: `src/contrib/plugins.ts:15-18`; `src/plugins/README.md`;
  `electron/desktop-plugins-root.ts:6-25,116+`
- Loader + security + defaultEnabled CAP: `src/contrib/runtime-loader.ts:6-49`
- Plugin context surface: `src/contrib/plugin.ts:76-107,110-135,217-244`
- Contribution shape: `src/contrib/types.ts:29-63`; registry:
  `src/contrib/registry.ts:53,86`
- Areas: `controller.tsx:700,743,482`; `tree/store.ts:695`;
  `layout-picker.tsx:113`; `app/routes.ts:100-106`;
  `command-palette/contrib.ts:10`; `hello-runtime/plugin.runtime.js`
- Preview bar hardcoded: `src/app/chat/right-rail/preview-browser-bar.tsx:100-279`
- Tool events: `gateway/run_turn_runner.py:133-158`; plugin tap:
  `src/contrib/events.ts:38-46`
- Plugin REST door: `hermes_cli/web_server.py:596-608,995`
- Extension-system rule + session gating: `AGENTS.md:206-219`
