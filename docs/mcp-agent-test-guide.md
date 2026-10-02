# Testing the wwwdrive MCP server (agent guide)

Paste-ready instructions for an agent tasked with installing and exercising
the MCP server from this branch.

## Prerequisites

- `uv` on PATH; this repo checked out (`main` or later)
- `terminal-browser` on PATH.
- A `TYPESAFE_API_KEY` available (process env or `~/.hermes/.env`).
- A terminal with kitty-graphics support (kitty, ghostty, wezterm, tmux,
  cmux — not iTerm2/Terminal.app) for visible-pane driving.

## 1. Register the server

Run exactly this (`--args` must stay last):

```bash
hermes mcp add wwwdrive --command uv --args run --directory <repo> python scripts/mcp.py
```

If it asks `Enable all 3 tools?`, answer `Y`. Confirm with
`hermes mcp list` (expect `wwwdrive` enabled). Any MCP-capable client
works too: command `uv`, args `run --directory <repo> python scripts/mcp.py`.

## 2. Load the tools

Run `/reload-mcp` in the TUI (or start a new session). Verify via tool
search: expect `mcp__wwwdrive__drive` (required param `goal`),
`mcp__wwwdrive__read`, and `mcp__wwwdrive__status`, all with
source `mcp`. The names are bare because the server name namespaces them. If
a built-in `drive` tool from another plugin is also enabled, prefer the
`mcp__` tools for the test so results aren't confounded.

## 3. Smoke test (fast, no browser needed)

Call `drive` with `{}` (no goal). Expect `success: false`,
`error: "goal is required"`, `stopped_reason: "error"`, `verified: null`.

## 4. Live test (needs the visible terminal)

- `read` with `{"url": "https://example.com"}` → expect `success: true`
  with an outline.
- `drive` with `{"goal": "Click the Learn more link; done when the
  IANA example domains page shows", "max_steps": 6}` → expect
  `status: "done"`, `final_url` on iana.org, `stopped_reason: "model_done"`,
  `verified: null`.

## 5. If visible-pane provisioning fails

Symptom: `blocked` with "unsupported terminal" (nested panes without
graphics passthrough). Workaround: from a supported terminal launch a
shared browser with `terminal-browser new-tab about:blank`, find its
`cdpPort` via `terminal-browser ls --all --json`, then pass
`background: true` + `cdp_url: "http://127.0.0.1:<port>"` on `drive`
or `read` calls (both accept them; read-only calls without them attach
via the tab lease instead).

## 6. Report back

Full result JSONs, wall-clock time per call, and anything where observed
behavior differs from the above. Do not post, like, submit, or otherwise
mutate any real site — example.com/IANA only.

## 7. Known server limits

- The MCP server is serial: one `tools/call` at a time. During a long
  drive it cannot process `notifications/cancelled`; use `timeout_s` /
  `max_steps` budgets, or SIGTERM the drive subprocess.
- MCP servers don't hot-reload: after pulling new repo code, restart the
  host session (or kill `scripts/mcp.py`) so tool schemas and handlers
  pick up the changes.
