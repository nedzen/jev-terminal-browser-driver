# wwwdrive

https://github.com/user-attachments/assets/f2273688-e330-4390-8287-baf96705ad40

*Real run: Google Flights, Zürich to London, one-way, Oct 15 2026. 14
autonomous ticks, 8.4 s, about $0.0025 in Jev spend, zero screenshots.*

**wwwdrive drives a real, visible browser tab for any agent that speaks MCP**
— Hermes, OpenCode, omp, Grok, Claude Code/Crush, anything with an MCP client.
One stdio server, zero per-host code: register one command and the tools are
there.

The browser is [terminal-browser](https://terminal-browser.dev) (Chromium in
your terminal). Decisions come from
[Jev](https://docs.typesafe.ai/introduction) (TypeSafe): cheap typed choices
instead of screenshots or huge a11y dumps in the agent context.

## Tools (`wwwdrive` MCP server)

- **`drive`** — click, type, select, scroll until a goal is done. Returns
  `status`, `actions`, `why`, `reason`, `page_text`.
- **`read`** — same tab, no clicks. Outline or JSON via optional script;
  `scrolls` for long pages.
- **`status`** — can this machine drive? No browser, no spend.

Schemas: [`plugin/__init__.py`](plugin/__init__.py).

## Install

Needs: macOS/Linux, Python 3.12+, [uv](https://astral.sh/uv/),
`terminal-browser` on PATH, a kitty-graphics terminal (kitty, ghostty, wezterm,
tmux, cmux; not iTerm2/Terminal.app), `TYPESAFE_API_KEY`. OpenRouter key
optional (typing only).

```bash
git clone https://github.com/nedzen/jev-terminal-browser-driver
cd jev-terminal-browser-driver
uv sync
```

Hermes:

```bash
hermes mcp add wwwdrive --command uv --args run --directory <repo> python scripts/mcp.py
```

Other hosts — same command/args in MCP config. Set `TYPESAFE_API_KEY` in the
host environment. `WWWDRIVE_DEBUG=0` turns the overlay off.

Optional Hermes native plugin:

```bash
./scripts/install_plugin.sh
hermes plugins enable wwwdrive
```

Then **Plugins → wwwdrive**: TypeSafe key (required), OpenRouter key (typing),
debug overlay (default on).

## CLI

```bash
export TYPESAFE_API_KEY=...
uv run python scripts/drive.py --json --debug \
  --url 'https://en.wikipedia.org/wiki/Main_Page' \
  --goal "Search for 'string trimmer' and open that article. Done when it is showing."

uv run python scripts/read.py --json --script "document.title"
```

Model DONE is a claim — check `final_url` / `page_text`.

## Logs

`~/.cache/wwwdrive/drive.log` and `drive.jsonl`. Tail the log while a run is
live. Pre-1.0 `~/.cache/jev-driver/` is orphaned.

## Docs

| Doc | Role |
|---|---|
| [docs/architecture.md](docs/architecture.md) | Layout, tick loop, gates |
| [docs/decisions.md](docs/decisions.md) | Why the gates and boundaries look like this |
| [docs/live-testing.md](docs/live-testing.md) | Offline + live S-batch, scoreboard lessons |
| [docs/known-issues.md](docs/known-issues.md) | Open faults and gotchas |
| [docs/roadmap.md](docs/roadmap.md) | Open work |
| [CHANGELOG.md](CHANGELOG.md) | User-facing versions |
| [AGENTS.md](AGENTS.md) | Contributor agreement |

```bash
uv run pytest
uv run ruff check .
```

## Safety

Shares your browser profile and cookies. Drives only its own tab. Skips
`chrome://`, `devtools://`, extensions, workers. Visible credentials are
redacted before they leave the machine.

## Credits / license

Built on [browser-use/jev-ultrafast](https://github.com/browser-use/jev-ultrafast)
(© 2026 Browser Use, MIT). See [NOTICE](NOTICE). MIT — [LICENSE](LICENSE).
