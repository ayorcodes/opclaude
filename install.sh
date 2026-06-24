#!/usr/bin/env bash
# Sets up opclaude: a local litellm proxy that lets Claude Code talk to
# opencode Zen models (Kimi, Qwen, DeepSeek, GLM, Minimax, gpt-5.3-codex)
# instead of Anthropic's API. Requires an opencode Go subscription + API key.
#
# Usage: ./install.sh
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_DIR="$HOME/.config/opclaude"
ENV_FILE="$STATE_DIR/.env"
BIN_DIR="$HOME/.local/bin"
LITELLM_VERSION="1.89.3"   # pinned; see FIX.md for why and how to bump safely

if [ "$(uname -s)" != "Darwin" ]; then
  echo "opclaude v1 only supports macOS. Patching contributions for Linux welcome." >&2
  exit 1
fi

echo "== opclaude install =="
mkdir -p "$STATE_DIR" "$BIN_DIR"

# --- uv -----------------------------------------------------------------
if ! command -v uv >/dev/null 2>&1; then
  echo
  echo "uv (the Python tool installer litellm runs under) is not installed."
  read -r -p "Install it now via the official installer script? [y/N] " reply
  if [[ "$reply" =~ ^[Yy]$ ]]; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
  else
    echo "Install uv yourself (https://docs.astral.sh/uv/) and re-run this script." >&2
    exit 1
  fi
fi

# --- claude code CLI ------------------------------------------------------
if ! command -v claude >/dev/null 2>&1; then
  echo
  echo "Claude Code CLI ('claude') is not on PATH."
  if command -v npm >/dev/null 2>&1; then
    read -r -p "Install it now via 'npm install -g @anthropic-ai/claude-code'? [y/N] " reply
    if [[ "$reply" =~ ^[Yy]$ ]]; then
      npm install -g @anthropic-ai/claude-code
    else
      echo "Install Claude Code yourself and re-run this script." >&2
      exit 1
    fi
  else
    echo "npm not found. Install Node.js, then Claude Code (npm install -g @anthropic-ai/claude-code), then re-run this script." >&2
    exit 1
  fi
fi

# --- litellm + our patch --------------------------------------------------
echo
echo "Installing litellm $LITELLM_VERSION via uv ..."
uv tool install "litellm==$LITELLM_VERSION" --force --with 'litellm[proxy,extra-proxy]'

echo "Applying our patch for litellm bug #2 (see FIX.md) ..."
"$REPO_DIR/patches/apply.sh"

# --- secrets ---------------------------------------------------------------
# shellcheck disable=SC1090
[ -f "$ENV_FILE" ] && source "$ENV_FILE"

if [ -z "${OPENCODE_API_KEY:-}" ]; then
  echo
  read -r -s -p "Enter your opencode API key (OPENCODE_API_KEY): " OPENCODE_API_KEY
  echo
  if [ -z "$OPENCODE_API_KEY" ]; then
    echo "An opencode API key is required (https://opencode.ai — needs a Go subscription)." >&2
    exit 1
  fi
fi

if [ -z "${LITELLM_MASTER_KEY:-}" ]; then
  LITELLM_MASTER_KEY="sk-$(openssl rand -hex 32)"
fi

umask 077
cat > "$ENV_FILE" <<EOF
OPENCODE_API_KEY=$OPENCODE_API_KEY
LITELLM_MASTER_KEY=$LITELLM_MASTER_KEY
EOF
echo "Saved secrets to $ENV_FILE (mode 600)."

# --- bin scripts on PATH ---------------------------------------------------
ln -sf "$REPO_DIR/bin/opclaude" "$BIN_DIR/opclaude"
ln -sf "$REPO_DIR/bin/opclaude-proxy" "$BIN_DIR/opclaude-proxy"
echo "Linked opclaude and opclaude-proxy into $BIN_DIR"

case ":$PATH:" in
  *":$BIN_DIR:"*) : ;;
  *)
    echo
    echo "NOTE: $BIN_DIR is not on your PATH. Add this to your shell rc file:"
    echo "  export PATH=\"$BIN_DIR:\$PATH\""
    ;;
esac

echo
echo "Done. Run 'opclaude' to start Claude Code routed through opencode models."
echo "Manage the background proxy with: opclaude-proxy start|stop|restart|status"
