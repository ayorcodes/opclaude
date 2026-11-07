import os
import re

import litellm
from litellm.integrations.custom_logger import CustomLogger

import oc_router

# `oc` launches auto-routed cheap sessions with this sentinel model name.
# When the proxy sees it, the per-turn router below rewrites it to the right
# opencode-Zen model for that specific turn. Any other model name (e.g. a
# plain `opclaude --model claude-kimi-2.7` session) is left untouched, so
# opclaude's explicit-model behavior is unaffected.
AUTO_MODEL_SENTINEL = "claude-auto"

_ROUTER_CONFIG_CACHE = {"mtime": None, "config": None}


def _router_config():
    """Load router.yaml, re-reading only when the file changes."""
    path = os.path.expanduser("~/.config/opclaude/router.yaml")
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = None
    if _ROUTER_CONFIG_CACHE["mtime"] != mtime:
        _ROUTER_CONFIG_CACHE["config"] = oc_router.load_router_config(path)
        _ROUTER_CONFIG_CACHE["mtime"] = mtime
    return _ROUTER_CONFIG_CACHE["config"]


def _route_turn(messages):
    """Pick the cheap model for the latest user turn (heuristics only)."""
    config = _router_config()
    if not config.get("per_turn", {}).get("enabled", True):
        return config.get("models", {}).get("moderate", {}).get(
            "default", "claude-deepseek-v4-pro"
        )
    task = oc_router.last_user_text(messages)
    tier, _ = oc_router.heuristic_tier(task)
    if tier is None:
        tier = "moderate"  # ambiguous -> safe middle (no per-turn LLM call)
    task_type = oc_router.detect_task_type(task)
    return oc_router.pick_cheap_model(tier, task_type, config)


def _strip_thinking(messages):
    cleaned = []
    for msg in messages:
        if not isinstance(msg, dict):
            cleaned.append(msg)
            continue

        msg.pop("thinking_blocks", None)
        msg.pop("redacted_thinking", None)

        content = msg.get("content")
        if isinstance(content, list):
            msg["content"] = [
                c for c in content
                if not (isinstance(c, dict) and c.get("type") in ("thinking", "redacted_thinking"))
            ]
            for c in msg["content"]:
                if isinstance(c, dict):
                    c.pop("thinking_blocks", None)

        cleaned.append(msg)
    return cleaned


# Alibaba/dashscope (qwen) rejects max_tokens outside [1, 65536]. Claude Code
# can request much larger values (its defaults run into the hundreds of
# thousands for extended-thinking-capable models), so clamp for qwen models.
QWEN_MAX_TOKENS = 8192

# Per-model output token caps — first matching entry wins (substring match on
# lowercased model name). Applied before the context-window budget check below.
# Covers both cloud and local (Ollama) variants of the same model family.
MODEL_MAX_TOKENS = {
    "deepseek-v4-flash": 65536,   # opencode + Ollama limit
}

# Ollama-routed models use the claude-ol- prefix. Any Ollama model not
# matched by MODEL_MAX_TOKENS above gets this conservative default cap, since
# Ollama models commonly reject max_tokens above ~32k.
LOCAL_MODEL_PREFIX = "claude-ol-"
LOCAL_DEFAULT_MAX_TOKENS = 32768

# Claude Code's extended thinking sends `thinking: {type: "enabled",
# budget_tokens: N}`. Ollama's /v1/chat/completions accepts `reasoning_effort`
# instead. We map budget_tokens to effort level and drop the Anthropic-specific
# `thinking` key so it doesn't cause a 400 from the Ollama endpoint.
#
# Only models confirmed to support Ollama thinking (via `/set think` test) get
# reasoning_effort forwarded. Others just have the Anthropic keys stripped.
# Verified 2026-06-29: deepseek-v4-flash, deepseek-v4-pro, glm-5.2 → yes
#                      qwen3-coder → warns "does not support thinking output"
LOCAL_REASONING_MODEL_SUBSTRINGS = ("deepseek-v4", "glm-5", "minimax-m2", "gpt-oss", "kimi")

_REASONING_EFFORT_TIERS = [
    (20_000, "max"),
    (8_000,  "high"),
    (3_000,  "medium"),
    (1,      "low"),
]

# Total context window (input + output tokens) for models whose upstream
# provider enforces it strictly. Claude Code requests a fixed large
# max_tokens (e.g. 120000) irrespective of how much input it's also sending,
# which overflows these models' actual window once tool/text input is large.
CONTEXT_LIMITS = {
    "minimax-m2.5": 204800,
    "minimax-m3": 204800,
}
CONTEXT_LIMIT_MARGIN = 2000

# litellm.token_counter doesn't recognize these custom-routed model names, so
# it falls back to a generic tokenizer to estimate input tokens. That
# estimate has been observed to undercount the provider's actual count by
# ~10-15% on requests with heavy tool/JSON content (e.g. estimated ~106k
# against an actual 120752, letting an unclamped 96928-token max_tokens
# request through and overflowing the real 204800 limit). Inflate the
# estimate before computing the budget to compensate.
INPUT_TOKEN_SAFETY_FACTOR = 1.2

# Models whose backend compiles tool schemas into a constrained grammar
# (structural_tag). With many tools active at once (Claude Code regularly
# sends 40+, including several MCP servers), GLM-5.2's grammar compiler
# fails to resolve $ref against $defs across the combined tool set --
# "Cannot find field $defs in #/$defs/<Name>" -- even when each tool's
# schema is independently flat and valid (observed with multiple Stitch
# tools that each declare their own $defs.SelectedScreenInstance). Rather
# than depend on however that compiler scopes/merges $defs across tools,
# inline every $ref and drop $defs entirely so there is nothing left for
# it to resolve.
INLINE_REFS_MODEL_SUBSTRINGS = ("glm",)


def _inline_refs(schema):
    if not isinstance(schema, dict):
        return schema

    defs = schema.get("$defs")
    if not isinstance(defs, dict):
        return schema

    def resolve(node, stack):
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/$defs/"):
                name = ref.rsplit("/", 1)[-1]
                target = defs.get(name)
                if target is not None and name not in stack:
                    inlined = resolve(target, stack | {name})
                    if isinstance(inlined, dict):
                        merged = dict(inlined)
                        merged.update({k: v for k, v in node.items() if k != "$ref"})
                        return merged
                return node
            return {
                key: resolve(value, stack)
                for key, value in node.items()
                if key != "$defs"
            }
        if isinstance(node, list):
            return [resolve(item, stack) for item in node]
        return node

    resolved = resolve(schema, frozenset())
    schema.clear()
    schema.update(resolved)
    return schema


def _inline_tool_schemas(tools):
    for tool in tools:
        if not isinstance(tool, dict):
            continue

        # Anthropic Messages format (what reaches this proxy's pre-call
        # hook, since the Anthropic->OpenAI tool translation happens later
        # inside litellm's own handler code).
        input_schema = tool.get("input_schema")
        if isinstance(input_schema, dict):
            _inline_refs(input_schema)

        # OpenAI chat-completions format, in case this hook ever runs after
        # translation or against a request built directly in that shape.
        function = tool.get("function")
        if isinstance(function, dict):
            parameters = function.get("parameters")
            if isinstance(parameters, dict):
                _inline_refs(parameters)


def _drop_untyped_properties(schema):
    """Apple Foundation Models rejects the whole request (400 "data couldn't
    be read") when any property schema lacks a `type` (e.g. Workflow's `args`).
    Drop such properties, recursively."""
    if isinstance(schema, list):
        for item in schema:
            _drop_untyped_properties(item)
        return
    if not isinstance(schema, dict):
        return
    props = schema.get("properties")
    if isinstance(props, dict):
        for name in [
            n for n, v in props.items()
            if isinstance(v, dict) and not any(k in v for k in ("type", "anyOf", "oneOf", "allOf", "enum", "$ref"))
        ]:
            del props[name]
            if isinstance(schema.get("required"), list) and name in schema["required"]:
                schema["required"].remove(name)
    for value in schema.values():
        _drop_untyped_properties(value)


def _sanitize_tools_for_apple(tools):
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        _drop_untyped_properties(tool.get("input_schema"))
        function = tool.get("function")
        if isinstance(function, dict):
            _drop_untyped_properties(function.get("parameters"))


# ---------------------------------------------------------------------------
# Response-side: strip visible <think> blocks from text content
# ---------------------------------------------------------------------------
# Some OpenAI-compatible providers (minimax via opencode-zen, Meta AI) return
# the model's reasoning/thinking as <think>...</think> in the regular
# content field rather than as structured reasoning_content in the
# streaming delta. litellm's OpenAI->Anthropic response converter passes this
# through as a text_delta, so Claude Code renders the thinking as visible
# text instead of hiding it inside a thinking content block.
#
# The async_post_call_streaming_iterator_hook below intercepts streaming
# chunks in Anthropic SSE dict format and strips these markers from text
# content before they reach the client.

# Models whose reasoning output is embedded as <think> tags in text content.
THINK_IN_TEXT_MODELS = ("minimax", "meta")


_MAX_TAG_LEN = len("</think>")  # longest marker we look for


def _strip_think_blocks(text: str, in_think: bool = False, pending: str = ""):
    """Remove <think>...</think> blocks from text, handling chunk boundaries.

    `pending` is leftover text from a previous call that ended with a prefix
    which *might* be the start of a marker (e.g. "<th" at the end of a
    chunk) — it is un-emitted text, not a confirmed tag, so it must be
    re-examined once more text arrives rather than assumed to be one.

    Returns (cleaned_text, still_in_think, new_pending). `new_pending` must
    be passed back in on the next call (and, if the stream ends while it's
    non-empty, flushed to the caller as literal text — it was never a real
    tag).
    """
    text = pending + (text or "")
    if not text:
        return "", in_think, ""

    if in_think:
        idx = text.find("</think>")
        if idx >= 0:
            text = text[idx + 8:]
            in_think = False
        else:
            # Keep only a possible partial "</think>" suffix as pending;
            # everything else is consumed (it's inside the think block).
            for tag_len in range(_MAX_TAG_LEN - 1, 0, -1):
                if text[-tag_len:] == "</think>"[:tag_len]:
                    return "", True, text[-tag_len:]
            return "", True, ""

    # Strip complete <think>...</think> blocks within this chunk
    result_parts = []
    while True:
        start = text.find("<think>")
        if start < 0:
            # No opening tag — check for a partial one at the end (cross-chunk).
            # Only held back as `pending`, never assumed to be a real tag.
            for tag_len in range(len("<think>") - 1, 0, -1):
                suffix = text[-tag_len:]
                if "<think>"[:tag_len] == suffix:
                    result_parts.append(text[:-tag_len])
                    return "".join(result_parts), False, text[-tag_len:]
            result_parts.append(text)
            return "".join(result_parts), False, ""

        result_parts.append(text[:start])
        text_after_open = text[start + 7:]

        close = text_after_open.find("</think>")
        if close < 0:
            # No closing tag yet — check for a partial "</think>" suffix to
            # hold as pending; the rest is consumed (inside the think block).
            for tag_len in range(_MAX_TAG_LEN - 1, 0, -1):
                if text_after_open[-tag_len:] == "</think>"[:tag_len]:
                    return "".join(result_parts), True, text_after_open[-tag_len:]
            return "".join(result_parts), True, ""

        text = text_after_open[close + 8:]
        # loop for multiple think blocks in one chunk


# OpenCode Go rejects requests without a stable per-conversation session id
# (400 MissingSessionID). Claude Code sends x-claude-code-session-id; LiteLLM
# does not forward client headers upstream, so copy it into x-opencode-session
# for deployments served by opencode.ai.
OPENCODE_HOSTS = ("opencode.ai",)
CLIENT_SESSION_HEADER = "x-claude-code-session-id"


def _is_opencode_model(model_name):
    try:
        from litellm.proxy.proxy_server import llm_router
        deployments = llm_router.get_model_list(model_name=model_name) or []
    except Exception:
        return False
    return any(
        any(h in str((d.get("litellm_params") or {}).get("api_base") or "") for h in OPENCODE_HOSTS)
        for d in deployments
    )


def _client_session_id(data):
    headers = (data.get("proxy_server_request") or {}).get("headers") or {}
    for k, v in headers.items():
        if isinstance(k, str) and k.lower() == CLIENT_SESSION_HEADER and v:
            return v
    return data.get("litellm_session_id")


class ClampClaudeCodeRequest(CustomLogger):
    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        messages = data.get("messages")
        if messages:
            data["messages"] = _strip_thinking(messages)

        # Per-turn auto-routing: resolve the sentinel to a real cheap model
        # based on this turn's content, before the model-specific clamps below
        # (which key off the resolved model name).
        model = data.get("model") or ""
        if model.lower() == AUTO_MODEL_SENTINEL:
            model = _route_turn(data.get("messages"))
            data["model"] = model

        model_lower = model.lower()

        if _is_opencode_model(model):
            session_id = _client_session_id(data)
            if session_id:
                extra = dict(data.get("extra_headers") or {})
                extra.setdefault("x-opencode-session", session_id)
                data["extra_headers"] = extra

        if any(s in model_lower for s in INLINE_REFS_MODEL_SUBSTRINGS):
            tools = data.get("tools")
            if isinstance(tools, list):
                _inline_tool_schemas(tools)

        if model_lower.startswith("claude-apple-"):
            tools = data.get("tools")
            if isinstance(tools, list):
                _sanitize_tools_for_apple(tools)

        if "qwen" in model_lower:
            max_tokens = data.get("max_tokens")
            if not isinstance(max_tokens, int) or max_tokens < 1 or max_tokens > 65536:
                data["max_tokens"] = QWEN_MAX_TOKENS

        # Per-model caps (deepseek-v4-flash, etc.) — first match wins.
        for key, limit in MODEL_MAX_TOKENS.items():
            if key in model_lower:
                max_tokens = data.get("max_tokens")
                if isinstance(max_tokens, int) and max_tokens > limit:
                    data["max_tokens"] = limit
                break
        else:
            # Conservative fallback for local Ollama models not listed above.
            if model_lower.startswith(LOCAL_MODEL_PREFIX):
                max_tokens = data.get("max_tokens")
                if isinstance(max_tokens, int) and max_tokens > LOCAL_DEFAULT_MAX_TOKENS:
                    data["max_tokens"] = LOCAL_DEFAULT_MAX_TOKENS

        # Map Claude Code's extended-thinking request to Ollama reasoning_effort.
        # The Anthropic `thinking` key causes a 400 on Ollama endpoints, so pop
        # it regardless. Only forward reasoning_effort to models confirmed to
        # support Ollama thinking (LOCAL_REASONING_MODEL_SUBSTRINGS).
        if model_lower.startswith(LOCAL_MODEL_PREFIX):
            thinking = data.pop("thinking", None)
            data.pop("betas", None)
            supports_reasoning = any(
                s in model_lower for s in LOCAL_REASONING_MODEL_SUBSTRINGS
            )
            if supports_reasoning and isinstance(thinking, dict) and thinking.get("type") == "enabled":
                budget = thinking.get("budget_tokens", 0)
                effort = next(
                    (lvl for threshold, lvl in _REASONING_EFFORT_TIERS if budget >= threshold),
                    None,
                )
                if effort:
                    data["reasoning_effort"] = effort

        # Meta AI API — maps Anthropic thinking to reasoning effort
        if "meta" in model_lower:
            thinking = data.pop("thinking", None)
            data.pop("betas", None)
            if isinstance(thinking, dict) and thinking.get("type") == "enabled":
                budget = thinking.get("budget_tokens", 0)
                effort = next(
                    (lvl for threshold, lvl in _REASONING_EFFORT_TIERS if budget >= threshold),
                    None,
                )
                if effort:
                    data["reasoning"] = {"effort": effort}

        for key, context_limit in CONTEXT_LIMITS.items():
            if key in model_lower:
                max_tokens = data.get("max_tokens")
                if isinstance(max_tokens, int) and messages:
                    try:
                        input_tokens = litellm.token_counter(
                            model=model,
                            messages=messages,
                            tools=data.get("tools"),
                        )
                    except Exception:
                        input_tokens = 0
                    input_tokens = int(input_tokens * INPUT_TOKEN_SAFETY_FACTOR)
                    budget = context_limit - input_tokens - CONTEXT_LIMIT_MARGIN
                    if budget < 1:
                        budget = 1
                    if max_tokens > budget:
                        data["max_tokens"] = budget
                break

        return data

    async def async_post_call_streaming_iterator_hook(
        self,
        user_api_key_dict,
        response,
        request_data,
    ):
        """Intercept streaming chunks and strip visible <think> blocks."""
        model = (request_data or {}).get("model", "")
        needs_cleanup = any(s in model.lower() for s in THINK_IN_TEXT_MODELS)

        if not needs_cleanup:
            async for item in response:
                yield item
            return

        in_think = False
        pending = ""
        async for chunk in response:
            if isinstance(chunk, dict) and chunk.get("type") == "content_block_delta":
                delta = chunk.get("delta")
                if isinstance(delta, dict) and delta.get("type") == "text_delta":
                    raw_text = delta.get("text", "")
                    if isinstance(raw_text, str) and raw_text:
                        cleaned, in_think, pending = _strip_think_blocks(
                            raw_text, in_think, pending
                        )
                        if cleaned:
                            delta["text"] = cleaned
                            yield chunk
                        continue
            elif isinstance(chunk, dict) and chunk.get("type") == "message_start":
                in_think = False
                pending = ""

            yield chunk

        if pending:
            # Held-back suffix never turned out to be a real tag (stream
            # ended before it could be confirmed either way) — it's
            # genuine text, so don't drop it silently.
            yield {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": pending},
            }

    async def async_post_call_success_hook(
        self,
        data: dict,
        user_api_key_dict,
        response,
    ) -> None:
        """Strip visible <think> blocks from non-streaming responses."""
        model = (data or {}).get("model", "")
        if not any(s in model.lower() for s in THINK_IN_TEXT_MODELS):
            return

        if not isinstance(response, dict):
            return

        content = response.get("content")
        if not isinstance(content, list):
            return

        modified = False
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                cleaned, _, pending = _strip_think_blocks(block["text"])
                cleaned += pending  # non-streaming: nothing left dangling, flush as text
                if cleaned != block["text"]:
                    block["text"] = cleaned
                    modified = True

        if modified:
            # Rough estimate for usage output_tokens: removed chars / 4.
            # Not critical — the client recalculates if needed.
            pass


proxy_handler_instance = ClampClaudeCodeRequest()
