<img src="./site/header.svg" alt="opclaude — one opencode Go subscription, every model" width="900"/>

<br/>

<p>
  <img src="https://img.shields.io/badge/kimi--2.7-ff6b57?style=flat-square" alt="kimi-2.7"/>
  <img src="https://img.shields.io/badge/qwen--3.5--plus-4d9bff?style=flat-square" alt="qwen-3.5-plus"/>
  <img src="https://img.shields.io/badge/deepseek--v4--pro-b388ff?style=flat-square" alt="deepseek-v4-pro"/>
  <img src="https://img.shields.io/badge/glm--5.2-e7d24a?style=flat-square&logoColor=000000" alt="glm-5.2"/>
  <img src="https://img.shields.io/badge/minimax--m3-ff5fa8?style=flat-square" alt="minimax-m3"/>
  &nbsp;
  <img src="https://img.shields.io/badge/macOS-only-111513?style=flat-square&logo=apple&logoColor=d9ebe2" alt="macOS only"/>
  <img src="https://img.shields.io/badge/MIT-license-46584f?style=flat-square" alt="MIT license"/>
</p>

---

Keep the Claude Code CLI you already know. **opclaude** routes it through your [opencode](https://opencode.ai) Go subscription instead of Anthropic's API — Kimi, Qwen, DeepSeek, GLM, and Minimax, all under one flat bill instead of separate per-model invoices.

> **Requires** an opencode **Go** subscription + API key → [opencode.ai](https://opencode.ai)

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/ayorcodes/opclaude/main/get.sh | bash
```

Or clone and run the installer directly:

```bash
git clone <this-repo> ~/.opclaude-src
cd ~/.opclaude-src && ./install.sh
```

The installer checks for and optionally installs `uv` and the Claude Code CLI, prompts for your `OPENCODE_API_KEY`, generates a random proxy key, and symlinks `opclaude` and `opclaude-proxy` into `~/.local/bin`.

## Use

```bash
opclaude                          # starts proxy if needed, then runs `claude`
opclaude models                   # list every available model
opclaude --model claude-kimi-2.7  # override default for one session
opclaude set-key                  # rotate OPENCODE_API_KEY, restart proxy
```

```bash
opclaude-proxy status
opclaude-proxy stop
opclaude-proxy restart
```

The proxy stays running between sessions — the next `opclaude` invocation is instant. It binds to `127.0.0.1` only and is never exposed beyond your machine.

## Models

| name | provider | notes |
|---|---|---|
| `deepseek-v4-pro` | DeepSeek | **default** — no flag needed |
| `kimi-2.7` | Moonshot AI | long-context, fast |
| `qwen-3.5-plus` | Alibaba | `max_tokens` clamped to 8 192 |
| `glm-5.2` | Zhipu AI | thinking blocks stripped |
| `minimax-m3` | Minimax | 204 800-token context, auto-clamped |

Run `opclaude models` any time for the live list.

## How it works

opclaude runs a small [litellm](https://github.com/BerriAI/litellm) proxy on `127.0.0.1` that speaks Anthropic's API to Claude Code on one side, and your opencode Go subscription on the other. Claude Code talks to it exactly like it talks to Anthropic — same CLI, same flags — while opclaude smooths over each model's quirks behind the scenes.

<details>
<summary>internals</summary>

- **`config.yaml`** — litellm proxy config: model list (each `claude-*` name maps to an opencode Zen model) plus a registered request hook.
- **`litellm_hooks.py`** — a `CustomLogger` that strips Claude Code's extended-thinking blocks for models that don't support reasoning, and clamps `max_tokens` for providers with stricter limits than Claude Code assumes.
- **`patches/`** — a file-level patch for one litellm streaming bug not fixable from config (`IndexError` on certain streaming responses). Re-apply after any litellm upgrade with `patches/apply.sh`. Full write-up in [FIX.md](FIX.md).

</details>

## Upgrading litellm

```bash
uv tool install litellm==<new-version> --force --with 'litellm[proxy,extra-proxy]'
patches/apply.sh
opclaude-proxy restart
```

See [FIX.md](FIX.md) if `patches/apply.sh` reports a failed hunk.

## Security

- Proxy binds to `127.0.0.1` only — never exposed beyond your machine.
- Requires `ANTHROPIC_AUTH_TOKEN` to match the generated `LITELLM_MASTER_KEY`; `opclaude` sets this automatically.
- `OPENCODE_API_KEY` and `LITELLM_MASTER_KEY` live only in `~/.config/opclaude/.env`, outside this repo.
