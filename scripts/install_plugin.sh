#!/bin/sh
# Symlink plugin/ into Hermes plugin roots. Named profiles do not inherit ~/.hermes/plugins.
set -eu
REPO="$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)"
SRC="$REPO"
if [ ! -f "$SRC/plugin.yaml" ] && [ ! -f "$SRC/plugin/plugin.yaml" ]; then
  echo "missing $SRC/plugin.yaml" >&2
  exit 1
fi

link_into() {
  dest="$1"
  mkdir -p "$(dirname "$dest")"
  ln -sfn "$SRC" "$dest"
  echo "linked $dest -> $SRC"
}

link_into "${HERMES_HOME:-$HOME/.hermes}/plugins/jev-driver"
for profile in "$@"; do
  link_into "$HOME/.hermes/profiles/$profile/plugins/jev-driver"
done

cat <<'EOF'

Enable in that home's config (plugins are opt-in):

  hermes plugins enable jev-driver

If the CLI has no enable subcommand, add jev-driver to plugins.enabled in
that profile's config.yaml. Named profiles (~/.hermes/profiles/<name>) have
their own plugins/ and do NOT inherit ~/.hermes/plugins — pass the profile
name as an argument to this script.

Desktop uses the same Python plugin loader as the TUI (local hermes serve).
EOF
if ! command -v terminal-browser >/dev/null 2>&1 && [ ! -x "$HOME/.local/bin/terminal-browser" ]; then
  echo "Optional: install terminal-browser for watchable browsing (https://terminal-browser.dev)"
  echo "It needs an existing kitty-graphics terminal (kitty/ghostty/wezterm/tmux/vscode/cmux/supacode/herdr)."
fi
