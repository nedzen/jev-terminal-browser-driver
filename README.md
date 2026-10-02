# wwwdrive

https://github.com/user-attachments/assets/f2273688-e330-4390-8287-baf96705ad40

*Real run: Google Flights, Zürich to London, one-way, Oct 15 2026. 14
autonomous ticks, 8.4 s, about $0.0025 in Jev spend, zero screenshots.*

**wwwdrive drives a real, visible browser tab for any agent that speaks MCP**
— Hermes, OpenCode, omp, Grok, Claude Code/Crush, anything with an MCP client.
One stdlib-only stdio server, zero per-host code: register one command and the
tools are there.

The browser is [terminal-browser](https://terminal-browser.dev), a Chromium
drawn in your terminal. You watch every click as it happens, in your own
logged-in session. Each decision is made by
[Jev](https://docs.typesafe.ai/introduction), a typed-decision model from
TypeSafe: a fraction of a cent and about half a second, instead of a
screenshot, an accessibility dump, or a 50K-token snapshot in your agent's
context.

## What the agent gets

Three tools, served under the `wwwdrive` MCP server:

- **`drive`** clicks, types, selects, and scrolls until a goal is done: open a
  menu, fill a form, search, follow a link. It returns `status` (`done` or
  `blocked`), the `actions` it took, `why`, a `reason` when it stopped early,
  and the visible `page_text`.
- **`read`** reads the same tab without clicking. With no script it returns an
  outline of articles, headings, times, and links. With a script (an
  expression, a function, or a body with `return`) it returns JSON. `scrolls`
  covers a long feed in one call.
- **`status`** reports whether this machine can drive a browser at all. No
  arguments, no browser, no spend: every field is `ok` or `missing`.

The agent never sees element tables. The contract it reads is the tool
descriptions in [`plugin/__init__.py`](plugin/__init__.py), which both
adapters serve verbatim.

## Install (any MCP client)

Requirements: macOS or Linux, Python 3.12+, [uv](https://docs.astral.sh/uv/),
the [terminal-browser](https://terminal-browser.dev) program on `PATH`, a
kitty-graphics terminal (kitty, ghostty, wezterm, tmux, cmux; not iTerm2 or
Terminal.app), and a TypeSafe API key. An OpenRouter key is optional and only
needed for typing into fields.

```bash
git clone https://github.com/nedzen/jev-terminal-browser-driver
cd jev-terminal-browser-driver
uv sync
```

Register the server with your host. In Hermes:

```bash
hermes mcp add wwwdrive --command uv --args run --directory <repo> python scripts/mcp.py
```

Any other host takes the same command and args in its MCP config:

```json
{"command": "uv", "args": ["run", "--directory", "<repo>", "python", "scripts/mcp.py"]}
```

Then set `TYPESAFE_API_KEY` in the host process environment and restart the
session. Add `WWWDRIVE_DEBUG=0` to the command's environment to turn the debug
overlay off (`JEV_DEBUG=0` still works as the pre-1.0 name). API keys are
never passed through tool arguments.

### Bare tool names

The tools are deliberately bare — `drive`, `read`, `status` — because the
server name carries the namespace: a host that namespaces MCP tools shows
`mcp__wwwdrive__drive`, `mcp__wwwdrive__read`, `mcp__wwwdrive__status`, which
covers every client this project is used with. A host that has a native `read`
of its own *and* does not namespace MCP tools can collide; there, register the
server under a prefixed name or use a client that namespaces.

## Hermes plugin (optional)

Hermes can also load the same schemas as native tools, which is useful when you
want the browser gated behind the plugin's own availability check:

```bash
./scripts/install_plugin.sh            # links into ~/.hermes/plugins/wwwdrive
./scripts/install_plugin.sh work       # also a named Hermes profile
hermes plugins enable wwwdrive
```

Then open **Plugins → wwwdrive** in Hermes and set:

| Setting | What it does |
|---|---|
| TypeSafe API key | Required. Jev decisions. Stored in `~/.hermes/.env` as `TYPESAFE_API_KEY`, never in `config.yaml`. |
| OpenRouter API key (typing) | Optional. Writes the text for form fields (`inception/mercury-2.5`). Without it the driver clicks but does not type. |
| Debug overlay | Shows the Jev panel and target outlines in the page. On by default. The model cannot turn it on or off. |

Restart the Hermes session after changing plugin settings.

## Debug overlay

With the setting (or `WWWDRIVE_DEBUG`) on, the driven tab gets a frosted panel
in the bottom-right corner:

- Header: a status dot (blue running, green done, amber blocked), the step
  count, the next operation, and its confidence. Click it to expand or
  collapse. The choice is remembered across pages and runs.
- Goal, why the run stopped, a timeline of every step (with typed text and
  whether the page changed), the candidate targets, the operation mix, and
  token spend.
- On the page: a green ring and pill on the chosen target, dashed rings with
  a rank badge on up to three runner-ups. Rings follow the page as it
  scrolls.

The overlay is its own layer. It never restyles page elements and is hidden
from the driver's own snapshot.

## Logs

Every run, read, action, recovery, and stop is appended to:

- `~/.cache/wwwdrive/drive.log`, one readable line per event with the ranked
  operations and targets
- `~/.cache/wwwdrive/drive.jsonl`, the same events as JSON

```bash
tail -f ~/.cache/wwwdrive/drive.log
```

Lines to look for: `run` (goal, url, tab continuity), `act`, `stale` (the page
moved before input), `retry_click`, `look_further` (scrolled or waited after
BLOCKED), `blocked`/`done` with a `reason`, `continuity` (why a remembered tab
was not reused), and `handler` (timeouts and crashes).

Anything already in `~/.cache/jev-driver/` from a pre-1.0 run is orphaned and
is never read again: delete it when you no longer need it. The same applies to
`last-page.json`, the remembered tab.

## How a run works

1. The tool handler runs `uv run python scripts/drive.py --json` in this repo.
   No host ever imports the driver.
2. The driver attaches to the driver's own tab in the visible pane, or opens
   one. Without `url` it stays on the current page.
3. Each tick reads the viewport in-page (`snapshot.js`: interactive elements
   plus up to 6K of visible text), asks Jev for one operation and one target,
   and sends real CDP mouse and keyboard input to that element.
4. A small text model writes the value for `TYPE_TEXT` only.
5. The loop stops on DONE, BLOCKED, or a budget.

Guards that are code, not prompt:

- A click is sent only if its target is unchanged. Ticking like counts and
  relative times do not count as changes.
- A covered target is scrolled into view once before giving up.
- BLOCKED on a page that is still loading waits for it. BLOCKED elsewhere
  scrolls down up to three times to look for the target.
- A toggle it already clicked (Like to Liked) is never clicked back.
- The same link is not followed twice, except pagination.
- A low-confidence DONE on a loading page is rejected.

Full detail: [docs/architecture.md](docs/architecture.md).

## Command line

The driver also runs without any agent host:

```bash
export TYPESAFE_API_KEY=...
uv run python scripts/drive.py --json --debug \
  --url 'https://en.wikipedia.org/wiki/Main_Page' \
  --goal "Search for 'string trimmer' and open that article. Done when it is showing."

uv run python scripts/read.py --json --script "document.title"
```

Model DONE is not proof. Check `final_url` or `page_text`.

## Upgrading from 0.x

1.0.0 renamed the product, the tools, and the paths. What changed:

| | 0.x | 1.0.0 |
|---|---|---|
| MCP server name | `jev-driver` | `wwwdrive` |
| Tools | `jev_drive`, `jev_read`, `jev_status` | `drive`, `read`, `status` |
| Hermes plugin dir | `~/.hermes/plugins/jev-driver` | `~/.hermes/plugins/wwwdrive` |
| Checkout override | `JEV_DRIVER_HOME` | `WWWDRIVE_HOME` (old name still read) |
| Debug opt-out | `JEV_DEBUG=0` | `WWWDRIVE_DEBUG=0` (old name still read) |
| Cache and logs | `~/.cache/jev-driver` | `~/.cache/wwwdrive` |

The rename is a clean break for tool names and the plugin directory: there is
no compatibility alias. Re-register the MCP server under `wwwdrive`, delete the
old `jev-driver` plugin link, and re-enter the settings under **Plugins →
wwwdrive**. The two environment variables and both cache files keep working as
fallbacks where noted. The GitHub repository name is unchanged in 1.0.0.

## Tests

```bash
uv run pytest     # offline: no network, no browser, never touches ~/.cache
uv run ruff check .
```

The live release checklist is [docs/test-plan.md](docs/test-plan.md): ten
prompts to paste into a host with the server registered, with pass criteria.
[docs/mcp-agent-test-guide.md](docs/mcp-agent-test-guide.md) is the paste-ready
version for an agent driving the test itself.

## Safety

The driver shares your browser profile and cookies. It only drives its own
tab, never closes a TUI tab, never steals focus, and skips `chrome://`,
`devtools://`, extension, and worker targets. Do not point it at sessions you
do not want automated. Details: [docs/architecture.md](docs/architecture.md)
§ Safety model.

## Credits

Built on [browser-use/jev-ultrafast](https://github.com/browser-use/jev-ultrafast)
(© 2026 Browser Use, MIT). `snapshot.js` and `questions.py` are verbatim;
`agent.py` has one noted change. See [NOTICE](NOTICE).

## License

MIT. See [LICENSE](LICENSE).
