# litellm proxy fixes

Two separate bugs were causing model requests (GLM-5.2, minimax-m3, etc.) to fail or silently stall when routed through this proxy's `use_chat_completions_url_for_anthropic_messages: true` path.

## 1. GLM-5.2 `400 Extra inputs are not permitted: messages[2].thinking_blocks`

**Cause:** Claude Code sends prior-turn extended-thinking content back as conversation history. litellm's Anthropic→OpenAI translation attaches `thinking_blocks`/`redacted_thinking` onto the resulting OpenAI-format messages. Models without reasoning support (`model_info.supports_reasoning: false`, e.g. GLM-5.2) hit a strict OpenAI-compatible backend that rejects the unrecognized field. `drop_params: true` doesn't help — it only strips unsupported top-level request params, not fields litellm itself injects into nested messages.

**Fix:**
- `litellm_hooks.py` — rewritten as a `CustomLogger` subclass (`ClampClaudeCodeRequest`) with `async_pre_call_hook` that strips `thinking_blocks` / `redacted_thinking` from every message before the request leaves the proxy.
- `config.yaml` — replaced the dead `request_transformer` / `modify_params` / `post_call_rules` keys (none of these are real litellm config options, which is why they never fired) with the correct registration:
  ```yaml
  litellm_settings:
    callbacks:
      - litellm_hooks.proxy_handler_instance
  ```

Applies to all models behind the proxy, not just GLM-5.2.

## 2. Streams silently stopping / `IndexError: list index out of range` / `API Error: Content block not found`

**Cause:** `litellm/llms/anthropic/experimental_pass_through/adapters/handler.py` unconditionally set `stream_options.include_usage: true` on every streamed request through this adapter. That makes OpenAI-compatible backends emit a trailing usage-only chunk with an **empty `choices` list**. The adapter's streaming code assumed every chunk has at least one choice:

- `streaming_iterator.py::_should_start_new_content_block` and two `is_final_chunk` checks indexed `chunk.choices[0]` unguarded → `IndexError`, which silently killed the SSE stream mid-response (looked like the model "just stopped").
- After guarding those, `transformation.py::translate_streaming_openai_response_to_anthropic` had the same unguarded `response.choices[0]` access one frame deeper — once that crash was also guarded, the usage-only chunk still got converted into a spurious second `message_delta` event, desyncing the client's content-block indices and producing `API Error: Content block not found`.

**Fix (in `/Users/ayorcodes/.local/share/uv/tools/litellm/lib/python3.12/site-packages/litellm/`):**

1. `llms/anthropic/experimental_pass_through/adapters/streaming_iterator.py`
   - `_should_start_new_content_block`: return `False` early when `chunk.choices` is empty.
   - Both `is_final_chunk = chunk.choices[0].finish_reason is not None` sites: guarded as `bool(chunk.choices) and chunk.choices[0].finish_reason is not None`.

2. `llms/anthropic/experimental_pass_through/adapters/transformation.py`
   - `translate_streaming_openai_response_to_anthropic`: treat an empty-`choices` chunk as the same "final/usage" branch (`stop_reason=None` when there are no choices) instead of indexing into an empty list.

3. `llms/anthropic/experimental_pass_through/adapters/handler.py`
   - At the time, removed the forced `stream_options = {"include_usage": True}` on every stream request, since the downstream chunk handling appeared not to support the empty-`choices` usage trailer it produces. **Superseded by fix #5 below** — turns out the installed litellm version already has a correct hold-and-merge mechanism for this trailer (see #5), so the real problem was elsewhere and removing `include_usage` was an overcorrection that broke Claude Code's context-usage UI / auto-compact (no usage ever came back). `include_usage` has been restored.

Fixes in `streaming_iterator.py` / `transformation.py` above are kept as defense-in-depth in case some upstream provider sends an empty-`choices` chunk unprompted.

**Caveat:** these are edits to installed third-party package files under `.local/share/uv/tools/litellm/...`, not to this repo. They will be **lost on the next `litellm` upgrade/reinstall** — worth upstreaming as a bug report/PR to litellm, or re-applying after any litellm version bump.

## 3. Qwen `400 InternalError.Algo.InvalidParameter: Range of max_tokens should be [1, 65536]`

**Cause:** `config.yaml` had a `request_transform: - max_tokens: 8192` block under `claude-qwen-3.5-plus`, but `request_transform` is not a real litellm config key (same class of dead key as the earlier `request_transformer`/`modify_params`/`post_call_rules`) — it was silently ignored. Claude Code can request `max_tokens` far larger than Alibaba/dashscope's qwen endpoint accepts, and nothing was clamping it.

**Fix:**
- `litellm_hooks.py` — `async_pre_call_hook` now also clamps `max_tokens` to `8192` for any model whose name contains `qwen`, whenever the client-requested value is missing or outside `[1, 65536]`.
- `config.yaml` — removed the dead `request_transform` block; clamping now happens for real in the hook.

## 4. minimax `400 This endpoint's maximum context length is 204800 tokens`

**Cause:** Claude Code requests a fixed large `max_tokens` (e.g. 120000) for output regardless of how much input/tool content it's also sending. minimax-m2.5/m3's actual context window (204800 tokens) covers input + output combined, so a large input plus the fixed output request overflows it even by a small margin (204812 vs 204800 in the reported case).

**Fix:**
- `litellm_hooks.py` — `async_pre_call_hook` now counts actual input tokens via `litellm.token_counter` for models matching `CONTEXT_LIMITS` (`minimax-m2.5`, `minimax-m3`, limit `204800`) and clamps `max_tokens` down to `context_limit - input_tokens - 2000` (margin) whenever the requested value would overflow. Add more `model_substring: limit` entries to `CONTEXT_LIMITS` if other models hit the same error.
- **Follow-up:** the first version of this fix only passed `messages` to `litellm.token_counter`, not `tools` — so the ~31k tokens of tool schemas Claude Code sends with every request weren't counted, and the clamp still let requests overflow by a small margin (204814 vs 204800). Now passes `tools=data.get("tools")` too.

## 5. Claude Code context-usage UI / auto-compact not working

**Cause:** Fix #2/step 3 (above) removed `stream_options.include_usage` entirely to stop a duplicate-`message_delta`/"Content block not found" bug. That also meant upstream providers never sent usage data in streaming responses at all anymore, so Claude Code had no token counts to show its context-usage UI or to trigger auto-compact with.

On closer inspection, the installed litellm version already has a purpose-built hold-and-merge mechanism in `streaming_iterator.py` (`holding_stop_reason_chunk`, `queued_usage_chunk`, `_merge_usage_into_held_stop_reason_chunk`): it holds the real `finish_reason` chunk, waits for the next chunk, and if that next chunk is the empty-`choices` usage trailer, merges its usage into the held chunk and emits one combined `message_delta` — exactly the case that previously broke. This logic likely wasn't reachable/correct yet when fix #2 was first applied (the `IndexError`/`response.choices[0]` crashes happened *before* reaching it), and once those were guarded, the mechanism above was never re-tested before `include_usage` got removed instead.

**Fix:**
- `llms/anthropic/experimental_pass_through/adapters/handler.py` — restored `completion_kwargs["stream_options"] = {"include_usage": True}` for streamed requests, now that the hold-and-merge path in `streaming_iterator.py` is confirmed to handle it correctly.

If "Content block not found" or duplicate `message_delta` events resurface after this, the bug is in the hold-and-merge logic itself (`_merge_usage_into_held_stop_reason_chunk` / the `will_merge_into_held` branch in `__next__`/`__anext__`), not a reason to remove `include_usage` again.

## 6. `general_settings.master_key` auth: unauthenticated/wrong-key requests return `500` instead of `401`

**Cause:** opclaude (unlike the original claude-kimi setup) enables `general_settings.master_key` so the proxy isn't wide open to anything that can reach `127.0.0.1:4000`. litellm's auth-failure path (`user_api_key_auth.py::_user_api_key_auth_builder` → `auth_exception_handler.py::_handle_authentication_error` → `db/exception_handler.py::is_database_service_unavailable_error`) unconditionally does `import prisma` while classifying the error, even though this proxy has no database configured at all. `prisma` lives under litellm's `extra-proxy` PyPI extra, not the `proxy` extra, so a plain `uv tool install litellm --with 'litellm[proxy]'` doesn't pull it in, the import crashes, and the client gets a generic `500 Internal server error` instead of a clean `401`.

**Fix:** install with both extras: `uv tool install litellm==<version> --force --with 'litellm[proxy,extra-proxy]'` (this is what `install.sh` does). No database connection is required — `prisma` just needs to be importable for this code path to classify the error correctly and return `401`/`400` instead of `500`. The auth boundary itself (reject without/with-wrong key, accept with the right key) holds correctly either way; this only affects the HTTP status code/error clarity on rejection.

## 7. GLM-5.2 `400 ... Failed to compile structural_tag grammar: Cannot find field $defs in #/$defs/<Name>`

**Cause:** GLM's backend compiles the tool schemas for the *entire* active tool set into one constrained grammar (`structural_tag`) for forced/structured tool calling. Claude Code regularly sends 40+ tools at once (several MCP servers). With that many tools active, GLM-5.2's grammar compiler fails to resolve `$ref`/`$defs` — even though each individual tool's `input_schema` is independently flat and perfectly valid JSON Schema (confirmed by logging the exact schemas sent: Stitch's `apply_design_system` and `create_design_system_from_design_md` both declare their own top-level `$defs.SelectedScreenInstance` with a plain, non-nested `$ref`). The first hypothesis — nested `$defs` — was wrong; debug logging showed the schemas reaching the proxy were already flat. The actual failure is in however the compiler scopes/merges `$defs` once multiple tools each define a same-named definition across a 40+ tool grammar; not worth reverse-engineering further.

**Fix:**
- `litellm_hooks.py` — replaced the (ineffective) `$defs`-hoisting attempt with `_inline_refs`/`_inline_tool_schemas`, called from `async_pre_call_hook` for any model matching `INLINE_REFS_MODEL_SUBSTRINGS` (currently `glm`). Fully inlines every `$ref` in a tool's `input_schema` (Anthropic Messages format — the shape tools are in when this hook fires, before litellm's later Anthropic→OpenAI translation; also handled for OpenAI's `function.parameters` shape in case the hook ever runs after that translation) by substituting the referenced definition body in place (sibling keys like a local `description` override take precedence over the definition's own), then drops `$defs` entirely. Leaves nothing for GLM's compiler to resolve, regardless of how it scopes `$defs` across tools.
- Verified directly against the real tool schemas captured via temporary debug logging in the hook (now removed) before restarting the proxy.

## 8. minimax `400 ... maximum context length is 204800 tokens` still happening after fix #4's clamp

**Cause:** fix #4's clamp relies on `litellm.token_counter(model=model, ...)` to estimate input tokens, where `model` is the proxy's alias (`claude-minimax-m2.5`), a name litellm doesn't recognize. For unrecognized models, `token_counter` falls back to a generic tokenizer, which undercounts real input tokens for this provider badly enough on tool/JSON-heavy requests that the clamp computed a budget larger than actually safe and let an unclamped `max_tokens` (96928) through — observed actual breakdown was 89400 text + 31352 tool = 120752 input tokens, but the hook's estimate was apparently low enough to not trigger clamping at all.

**Fix:**
- `litellm_hooks.py` — added `INPUT_TOKEN_SAFETY_FACTOR = 1.2`, applied to the `token_counter` estimate before computing the budget (`input_tokens = int(input_tokens * INPUT_TOKEN_SAFETY_FACTOR)`), to compensate for the fallback tokenizer's undercount on these models.
- If `400` context-length errors recur for minimax, increase `INPUT_TOKEN_SAFETY_FACTOR` further — the underlying issue is a tokenizer mismatch, not an exact formula, so this is an empirical safety margin rather than a precise fix.

## 9. Windows: litellm proxy crashes immediately on startup (`merged_lifespan` infinite recursion)

**Cause:** `uv tool install litellm` on a fresh Windows machine pulls the latest available Python (3.14 as of mid-2026). litellm 1.x uses FastAPI lifespan hooks in a way that triggers an infinite mutual recursion in `fastapi/routing.py::merged_lifespan` under Python 3.13+, crashing the server before it can accept a single request.

**Fix:** pin the uv tool environment to Python 3.11, which litellm is tested against:
```
uv tool install "litellm==<version>" --python 3.11 --force --with "litellm[proxy,extra-proxy]"
```
uv downloads Python 3.11 automatically if it isn't already cached — no separate install needed. Both `install.sh` and `install.js` already pass `--python 3.11` for this reason.

## After any of these fixes

Restart the proxy:
```bash
pgrep -fl litellm   # find the PID
kill <pid>
litellm --config config.yaml --host 127.0.0.1 --port 4000   # from this directory
```

## Which of these survive a `litellm` upgrade, and which don't

- **Bugs #1, #3, #4 are proxy-level fixes** (`litellm_hooks.py`'s `async_pre_call_hook`, registered via `config.yaml`'s `callbacks: - litellm_hooks.proxy_handler_instance`). They run before the request ever reaches litellm's internal Anthropic→OpenAI translation code, so they are independent of the installed litellm version and survive upgrades automatically — nothing to reapply.
- **Bug #2 is the one exception.** Its fix lives inside litellm's own installed package files (`llms/anthropic/experimental_pass_through/adapters/streaming_iterator.py` and `transformation.py`), so a `litellm` upgrade overwrites it every time.

### Upgrading litellm without losing the bug #2 fix

`patches/` in this directory holds the bug #2 fix as portable unified diffs (`streaming_iterator.patch`, `transformation.patch`) plus `apply.sh`, which locates the active `uv tool install`'d litellm and applies them with `patch -p1` (safe to re-run — it detects already-applied patches and no-ops).

```bash
uv tool install litellm==<new-version> --force --with 'litellm[proxy]'
patches/apply.sh
pgrep -fl litellm && kill <pid>
litellm --config config.yaml --host 127.0.0.1 --port 4000
```

If `apply.sh` reports a hunk **FAILED**, upstream litellm changed the surrounding code in `streaming_iterator.py` / `transformation.py` enough that the old patch no longer lines up. Re-derive it: download the new version's wheel from PyPI (`pip download litellm==<version> --no-deps`), extract `litellm/llms/anthropic/experimental_pass_through/adapters/{streaming_iterator,transformation}.py`, re-apply the same conceptual fix from bug #2 above (guard every `chunk.choices[0]` / `response.choices[0]` access against an empty list), regenerate the `.patch` files with `diff -u a/litellm/... b/litellm/...` (the `a/`/`b/` prefixes matter — that's what makes `-p1` portable across machines/python versions), and drop them into `patches/`.

**Verified history:** confirmed against the jump from `1.89.0` (where these patches were originally written) to `1.89.3` (2026-06-24) — upstream had **not** fixed bug #2 in that range (`streaming_iterator.py`/`transformation.py` were byte-for-byte identical to the unpatched 1.89.0 baseline aside from our patch), so the same patch applied cleanly with no changes needed. Also checked whether upstream had independently fixed bug #1 (thinking_blocks) by 1.89.3: it added `strip_thinking_blocks_from_anthropic_messages_request_dict` in `litellm/llms/anthropic/common_utils.py`, but that's only wired into the native Anthropic-Messages-passthrough retry-on-error path (`base_llm/anthropic_messages/transformation.py`), not the `use_chat_completions_url_for_anthropic_messages: true` / OpenAI-chat-completions-translation path this proxy uses (`litellm_core_utils/prompt_templates/factory.py`'s `anthropic_messages_pt`, which still attaches `thinking_blocks` unconditionally) — so our hook-based fix for bug #1 is still necessary and was not made redundant by this upgrade.
