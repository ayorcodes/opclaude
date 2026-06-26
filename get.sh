#!/usr/bin/env bash
# Bootstrap for `curl -fsSL .../get.sh | bash`.
#
# install.sh needs the rest of the repo alongside it (patches/, bin/,
# config.yaml), so this clones opclaude into a fixed local path and hands
# off to the real installer there.
set -euo pipefail

REPO_URL="https://github.com/ayorcodes/opclaude"
SRC_DIR="${OPCLAUDE_SRC_DIR:-$HOME/.opclaude-src}"

if ! command -v git >/dev/null 2>&1; then
  echo "git is required. Install it (e.g. via Xcode Command Line Tools) and re-run." >&2
  exit 1
fi

if [ -d "$SRC_DIR/.git" ]; then
  echo "Updating existing checkout at $SRC_DIR ..."
  git -C "$SRC_DIR" pull --ff-only
else
  echo "Cloning opclaude into $SRC_DIR ..."
  git clone "$REPO_URL" "$SRC_DIR"
fi

# Redirect stdin from the terminal so install.sh's interactive prompts work
# even when this script was piped in via `curl ... | bash` (where stdin is the
# curl pipe, not the user's terminal).
exec "$SRC_DIR/install.sh" </dev/tty
