# TUI-only discovery fix — visible panes, never herdr, never headless

Branch `fix/tui-only`, commit `f320105` (2026-09-24).
Supersedes the headless/agent-browser paths inherited from `feature/hermes-plugin`.

## The problem

Three stacked failures, each masking the next:

1. **Invisible runs.** `jev_drive` from inside a herdr pane (e.g. a Hermes
   session) fell down the old discovery ladder — terminal-browser `ls` came
   back empty → agent-browser daemon → headless Chromium auto-launch. The
   browser ran in the background; the user saw nothing.
2. **Sites rejected it.** The headless fingerprint is bot-blocked (x.com
   answered `HTTP 403 "Access to x.com was denied"` on every load and every
   Reload). Same URL from the visible terminal-browser Chromium: instant 200.
3. **When terminal-browser *was* provisioned, it nested in herdr.**
   `terminal-browser open --split right` opened the browser as a new herdr
   pane inside the agent's herdr tab — not a split in the user's real
   terminal window.

## Root causes

### 1. terminal-browser's terminal-adapter chain matches herdr first

`@zenbu-labs/pixel` (bundled in terminal-browser's CLI) detects the host
terminal by iterating an ordered adapter list:

```js
TERMINALS = [herdr, tmux, tty7, wezterm, kitty, cmux, supacode, ghostty, vscode]
detect(env): for (recognise of TERMINALS) { if (t = recognise(env, run)) return t }
```

The herdr adapter matches on **one condition**: `HERDR_PANE_ID` is set. Every
process spawned from inside a herdr pane — including every subprocess Hermes
runs — inherits herdr's env, so herdr always wins detection. Its `split()` is:

```js
herdr pane split --pane <calling-pane> --direction right --focus
herdr pane run  <new-pane-id> <command>
```

→ browser born inside a herdr pane of the agent's tab. The outer ghostty/cmux
window (which natively supports automation, v1.3.0+ required) is never asked.

### 2. `ls` is env/TTY-blind

`terminal-browser ls` lists browsers visible to the *detected* terminal. From
a no-TTY herdr subprocess it sees only herdr panes — a browser split into the
real terminal window is **invisible** to `ls` from inside herdr, forever. Any
discovery that relies on `ls` alone will conclude "no browser" and fall through.

### 3. The old ladder treated headless as a normal rung

`discover.py` (pre-fix) had four rungs ending in `_launch_headless()` via
agent-browser. There was no "visible or fail" contract, so failures were
silent and the user-visible outcome was "nothing happened" or "site blocked".

## The fix

New discovery contract (TUI-only): **explicit CDP → running terminal-browser
pane → provision a visible pane. Raise rather than degrade.**

### Files changed (5)

| File | Change |
|---|---|
| `jev_driver/discover.py` | Rewritten. Removed: `agent_browser_*`, `_run_agent_browser`, `_child_env` (agent-browser engine scrub), `_launch_headless`, `_loopback_discovery`, `LOOPBACK_PORTS`, `BUNDLED_AGENT_BROWSER`, headless `SESSION`. Added: `_provision_env()`, `_provision_terminal_browser()`, `_instance_record_port()`, `_daemon_db_discovery()`. |
| `jev_driver/browser.py` | `_open_via_chrome()`: removed the agent-browser `open` fallback; `Target.createTarget` failure now raises with a TUI-only message. |
| `plugin/handler.py` | `check_jev_drive()` gate: terminal-browser **installed** is enough (the driver provisions its own visible pane). Removed `resolve_agent_browser`, `BUNDLED_AB`; `agent_browser_daemon_present()` stubbed `False`. |
| `tests/test_discover.py` | Rewritten for the new ladder: explicit-first, running-pane-wins, provision-on-missing, never-headless raises, `HERDR_*` scrub, instance-record `cdpPort` parse, daemon-DB discovery, provision argv shape (`open <url> --split right --no-merge`, HERDR-free env). |
| `tests/test_watch.py` | Watch path rerouted through `_provision_terminal_browser` (same visible contract); stderr-surfacing test moved to the `subprocess.run` shape. |

### Mechanism details

**Provisioning (`_provision_terminal_browser`)**

```
subprocess.run([tb, "open", url, "--split", "right", "--no-merge"], env=_provision_env())
```

- `_provision_env()` strips **every `HERDR_*` variable**. With herdr out of
  the env, adapter detection falls through to the real terminal
  (ghostty/cmux here), which opens a visible split in the user's window.
- `--no-merge` prevents the instance from being absorbed as a tab into a
  neighbor terminal-browser.
- On nonzero exit the terminal's own stderr is surfaced (e.g. "unsupported
  terminal: iTerm2") — actionable, no fallback.
- Timeout is 60 s; cold-start of the split happens inside terminal-browser.

**Getting the CDP port back (two paths):**

1. **Instance record.** `terminal-browser open` prints a JSON record on
   stdout containing `cdpPort`. From a no-TTY caller this is the only
   immediate source — `_instance_record_port()` parses it. Port is verified
   against `/json/version` before use.
2. **Daemon SQLite (`_daemon_db_discovery`).** `ls` being TTY-blind also
   means it can never rediscover an existing ghostty-split browser from
   inside herdr. The daemon records every instance in
   `~/.local/share/terminal-browser-*/terminal-browser.db`:

   ```sql
   SELECT cdp_port FROM instances WHERE cdp_port IS NOT NULL ORDER BY started_at DESC
   ```

   Newest first, each port verified via `/json/version` before it is
   trusted; DB opened read-only (`mode=ro`).

**Discovery order in `discover()`:**

```
explicit --cdp / $JEV_CDP_URL / $BROWSER_CDP_URL
  → _terminal_browser_discovery()   (terminal-browser ls)
  → _daemon_db_discovery()          (daemon SQLite, TTY-blind caller)
  → _provision_terminal_browser()   (visible split right, HERDR_* scrubbed)
  → WatchUnavailable                (no headless fallback exists)
```

`auto_provision=False` stops before provisioning and raises with instructions.
`watch=True` shares the exact same path — watch and normal runs are now the
same ladder, so a watch run can never be less visible than a normal one.

### What was deliberately NOT deleted in this commit

`takeover.py` / `DriveAgent` / session continuity remain — they are runtime
features, not discovery. Their open-split path already routes through the new
provisioner. Scoping them out is a separate decision.

## Verification

- `uv run pytest -o addopts= -q` → **83 passed**, offline, no live browser.
- `uv run ruff check .` → clean.
- Live acceptance (from a herdr pane, no new browser spawned):
  - Discovery: `source=terminal-browser, visibility=terminal-browser-pane,
    auto_launched=False, origin=http://127.0.0.1:57463` (daemon-DB rung,
    found the existing visible ghostty-split browser).
  - Drive: `x.com/studiobakers` → `DONE` in 4 ticks, ~$0.0002, profile text
    (Bakers Studio, 6,89x followers) in the ≤6K page text.
  - Contrast (pre-fix, same URL): headless agent-browser → 403 on every load.

## Regression guards

- `test_provision_command_is_visible_split` pins the argv shape and the
  HERDR-free env — reintroducing an unscrubbed or headless launch fails CI.
- `test_no_browser_no_binary_raises` / `test_no_browser_no_provision_raises`
  pin the raise-don't-degrade contract.
- `test_daemon_db_discovery_*` pin the TTY-blind rediscovery path.
