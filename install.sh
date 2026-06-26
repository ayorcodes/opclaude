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
PYTHON_VERSION="3.11"      # litellm 1.x is not compatible with Python 3.14+; pin to 3.11

case "$(uname -s)" in
  Darwin) : ;;
  MINGW*|MSYS*|CYGWIN*)
    echo "On Windows, use install.ps1 instead:" >&2
    echo "  powershell -ExecutionPolicy Bypass -File install.ps1" >&2
    exit 1
    ;;
  *)
    echo "This script only supports macOS. Linux contributions welcome." >&2
    exit 1
    ;;
esac

echo "== opclaude install =="
mkdir -p "$STATE_DIR" "$BIN_DIR"

# --- uv -----------------------------------------------------------------
if ! command -v uv >/dev/null 2>&1; then
  echo
  echo "uv (the Python tool installer litellm runs under) is not installed."
  read -r -p "Install it now via the official installer script? [Y/n] " reply
  if [[ "$reply" =~ ^[Nn]$ ]]; then
    echo "Install uv yourself (https://docs.astral.sh/uv/) and re-run this script." >&2
    exit 1
  fi
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
  if ! command -v uv >/dev/null 2>&1; then
    echo "uv installed but not found on PATH. You may need to open a new terminal and re-run this script." >&2
    exit 1
  fi
fi

# --- claude code CLI ------------------------------------------------------
if ! command -v claude >/dev/null 2>&1; then
  echo
  echo "Claude Code CLI ('claude') is not on PATH."
  if command -v npm >/dev/null 2>&1; then
    read -r -p "Install it now via 'npm install -g @anthropic-ai/claude-code'? [Y/n] " reply
    if [[ "$reply" =~ ^[Nn]$ ]]; then
      echo "Install Claude Code yourself and re-run this script." >&2
      exit 1
    fi
    npm install -g @anthropic-ai/claude-code
    if ! command -v claude >/dev/null 2>&1; then
      echo "claude was installed but is not on PATH yet. You may need to open a new terminal and re-run this script." >&2
      exit 1
    fi
  else
    echo "npm not found. Install Node.js (https://nodejs.org), then re-run this script." >&2
    echo "  brew install node   # if you have Homebrew" >&2
    exit 1
  fi
fi

# --- litellm + our patch --------------------------------------------------
echo
echo "Installing litellm $LITELLM_VERSION via uv ..."
uv tool install "litellm==$LITELLM_VERSION" --python "$PYTHON_VERSION" --force --with 'litellm[proxy,extra-proxy]'

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
ln -sf "$REPO_DIR/bin/oc" "$BIN_DIR/oc"
ln -sf "$REPO_DIR/bin/oc-classify" "$BIN_DIR/oc-classify"
echo "Linked opclaude, opclaude-proxy, and oc into $BIN_DIR"

# --- router config (for `oc`) ----------------------------------------------
ROUTER_CONFIG="$STATE_DIR/router.yaml"
if [ ! -f "$ROUTER_CONFIG" ]; then
  cp "$REPO_DIR/router.yaml.example" "$ROUTER_CONFIG"
  echo "Seeded $ROUTER_CONFIG from router.yaml.example."
fi

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
