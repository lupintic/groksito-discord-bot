# LLM Orchestration and Prompt System Refactor Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce a surgical, minimal-change refactor of the LLM prompt and input/orchestration layer (prompt_builder.py, llm_input.py, llm/client.py, llm_utils.py) that strengthens Maximum Nativeness, improves token efficiency on first-turn + multi-turn (previous_response_id) paths, centralizes guidance, and keeps the bot feeling like direct native Grok while preserving all current invariants (sentinel delivery, activation policy, no auto-injection, vision safety, tests).

**Architecture:** Keep the existing clean separation (llm_input builds the one `initial_input`; client orchestrates calls + tool loop using previous_response_id; prompt_builder is the sole source for SYSTEM_PROMPT + search descriptions). Make small targeted extractions and tightenings only. Rely even more on Grok's native capabilities (reasoning, tool choice, long context + previous_response_id retention of prior tool defs). No heavy new classification or context stuffing.

**Tech Stack:** Python 3.11+, discord.py, openai (AsyncOpenAI to xAI Responses API `/v1/responses`), Pydantic settings, existing context + tools modules. No new deps.

---

## Current State Summary (from exploration)

- **Maximum Nativeness already strong** (post #19/#24 cleanups): ultra-minimal SYSTEM_PROMPT, zero automatic recent history or user memory injection, on-demand `get_recent_context` + `respond_directly` only on addressed turns via light decision tools, previous_response_id used for multi-turn state, native web_search/x_search offered (prompt-driven), vision via native input_image.
- Single authoritative `build_responses_input` in llm_input.py.
- Tiered custom tools (tools.py): ultra-lazy first-turn (0 tools for casual/normal), continuation-minimal (mostly just `reply_to_user` light), heavy media only on explicit creation intent.
- Dynamic blocks ([R:] refs + reply chains on addressed, server emojis on addressed) injected as separate system messages (caching-friendly).
- Orchestrator in client.py is long but functional; contains mixed prep + loop + retry + logging.
- Native search descriptions sourced from prompt_builder (single source of truth started).
- Key invariants to protect: DIRECT_DELIVERY_PERFORMED sentinel, no duplicate replies, previous_response_id continuity (model remembers offered tools across rounds), activation policy in conversation.py, vision URL filtering.

## Refactor Goals (precise)

1. **Native-first emphasis**: Further reduce custom scaffolding. Let SYSTEM_PROMPT + native tool schemas + Grok's reasoning do more decision making. Avoid over-explaining completeness in every turn if the base model already excels.
2. **Single source of truth hardening**: All text that reaches Grok (SYSTEM, native tool descriptions, light decision tool descriptions) must derive from prompt_builder without drift.
3. **Token efficiency & multi-turn perf**:
   - Tighten SYSTEM_PROMPT + dynamic blocks (fewer repeated phrases).
   - Make continuation native search decision cleaner (reduce or eliminate output scanning hack).
   - Keep (or improve) aggressive minimization on continuations.
4. **Surgical & minimal**: No large refactors or renames of public APIs. Extract small pure helpers only where duplication exists. Preserve call signatures (`call_grok_for_groksito`, `build_responses_input`, `get_tools_for_request`).
5. **Maintainability**: Slim client.py orchestration surface (smaller functions), better comments on previous_response_id contract, remove outdated cleanup comments where they no longer add value.
6. **Observability unchanged**: Logging, token metrics, cache metrics, addressed-turn metrics must continue to work identically.
7. **Testability**: All existing tests (esp. test_tool_selection.py, test_response_quality.py, test_native_search_offering.py) must pass without modification or with only additive assertions.

## Files That Will Be Modified (or read-only reviewed)

**Primary (must change):**
- `src/groksito_discord/llm/prompt_builder.py` — SYSTEM_PROMPT, COMPLETENESS_* constants, search description generators, comments.
- `src/groksito_discord/llm/llm_input.py` — classification shim, context assembly, vision content builder, result typing, comments. (Keep single build path.)
- `src/groksito_discord/llm/client.py` — orchestration flow, continuation logic, tool prep calls, possible extraction of 2-3 small helpers. No behavior change to public API.
- `src/groksito_discord/llm/llm_utils.py` — native search builder (if needed), any shared helpers touched.

**Secondary (review + minimal if alignment requires):**
- `src/groksito_discord/llm/tools.py` — only if a description string needs to be pulled from prompt_builder (rare).
- `src/groksito_discord/core/intent.py` — review light detectors; no heavy changes expected.
- `ARCHITECTURE.md` — update LLM section if the conceptual "prompt-driven + previous_response_id minimization" description improves.
- `tests/test_response_quality.py`, `tests/test_tool_selection.py`, `tests/test_native_search_offering.py` — add/verify assertions only if prompt text changes in observable ways.
- No changes to: media/, discord/, context/ core logic, web/, direct delivery sentinel, activation policy.

**Never touched in this plan:**
- Any secret handling, rate limits, guild whitelist, media delivery.py pattern.
- Old skill/MCP remnants (already removed).

## Proposed Order of Changes (topological, safe increments)

1. prompt_builder.py (foundational — everything downstream reads from it).
2. llm_input.py (depends on prompt_builder; classification & injection review).
3. llm_utils.py (helpers used by both input + client).
4. client.py (orchestration — largest surface, do last so inputs are stable).
5. Cross checks + tests + ARCHITECTURE.md + final verification.
6. (Optional) small alignment in tools.py/intent.py only if strings are centralized.

Each step produces a working, testable increment. Frequent small commits.

## Task Breakdown

### Task 1: Prompt Builder — Centralize and Tighten Guidance (Maximum Nativeness)

**Files:**
- Modify: `src/groksito_discord/llm/prompt_builder.py:82-99` (SYSTEM_PROMPT)
- Modify: constants at top (COMPLETENESS_*, WEB_SEARCH_*)
- Modify: `get_native_search_descriptions` and related
- Test: `tests/test_response_quality.py` (existing checks on SYSTEM_PROMPT)

- [ ] **Step 1.1: Read the full current prompt_builder.py and response_quality test to capture exact current text.**
  ```bash
  # (agent will do this)
  ```

- [ ] **Step 1.2: Introduce (or strengthen) explicit "GROK_GUIDANCE" or "NATIVE_BEHAVIOR" section in prompt_builder.**
  Extract repeated ideas (search when fresh, use vision natively, media delivery via tools, get_recent only on demand, concise vs complete judgment) into named constants that are also imported for use in tool descriptions.
  Example structure (keep total prompt length same or smaller):
  ```python
  # New/strengthened constants (single source)
  NATIVE_TOOL_JUDGMENT = "You have native tools (web_search, x_search, vision, generate_image, generate_video, ...). Use your own judgment..."
  DISCORD_DELIVERY_NOTE = "For image/video/audio generation and final replies use the provided tools (generate_* / reply_to_user). Delivery to channel happens automatically."
  ON_DEMAND_CONTEXT = "Call get_recent_context only when you need prior channel messages for coherence. Never assume history."
  ```

- [ ] **Step 1.3: Rewrite SYSTEM_PROMPT using the new constants so the f-string is 5-8 lines max.**
  Keep identity ("You are Grok (Groksito on this Discord server)"), language mix note, and completeness self-check.
  Target: shorter than current while preserving intent. Remove any "I searched" meta guidance if model already handles it.

- [ ] **Step 1.4: Update get_native_search_descriptions and the four *_DESCRIPTION constants so they are thin wrappers or direct references to the new central strings.**
  Ensure no duplication of "use focused queries; synthesize..." language.

- [ ] **Step 1.5: Run the response quality test and any prompt snapshot assertions.**
  ```bash
  pytest tests/test_response_quality.py -q --tb=line
  ```
  Expected: PASS. Note any length reduction.

- [ ] **Step 1.6: Commit**
  ```bash
  git add src/groksito_discord/llm/prompt_builder.py tests/test_response_quality.py
  git commit -m "refactor(llm): centralize Grok guidance in prompt_builder; tighten SYSTEM_PROMPT for nativeness + tokens"
  ```

### Task 2: llm_input.py — Review & Surgical Cleanup of Classification + Injection

**Files:**
- Modify: `src/groksito_discord/llm/llm_input.py`
- Read-only review: callers in client.py and conversation.py (no signature changes)

- [ ] **Step 2.1: Analyze current _classify_query_context_need and its call sites.**
  Confirm it now only special-cases pure image/video gen and defaults to "normal" for addressed turns. This is intentionally vestigial.

- [ ] **Step 2.2: Decide and implement minimal classification strategy.**
  Option chosen for plan (surgical): keep the function for backward logging/compat but rename internally to `_resolve_context_need_for_logging` or keep name, and add a clear comment:
  ```python
  # Classification is now extremely light (post-#24). "need" is primarily for:
  # - logging and metrics
  # - gating native search offering (casual/image_gen get none)
  # - pure_*_gen ultra-light paths
  # The model decides almost everything via native reasoning + SYSTEM_PROMPT + tool schemas.
  # We deliberately avoid reintroducing keyword-heavy tiers.
  ```
  No new classification code.

- [ ] **Step 2.3: Extract two tiny pure helpers (if they reduce duplication) — surgical only.**
  - `_build_dynamic_referenced_context_block(...)` (the [R:] + chain ancestor logic).
  - `_build_emoji_block_if_addressed(...)`.
  These remain private, called only from build_responses_input. Return the strings (or "") exactly as before.

- [ ] **Step 2.4: Clean up vision path inside _build_multimodal_user_content.**
  Keep the image_edit note injection. Ensure filter_unreliable_vision_urls remains the last-mile guard. Add one clarifying comment about why separate system blocks are used (prompt cache friendliness).

- [ ] **Step 2.5: Ensure ResponsesInputData TypedDict and return shape are untouched.**
  Verify stable_prefix_len still based on SYSTEM_PROMPT len.

- [ ] **Step 2.6: Execute relevant tests that touch input construction (via mocks).**
  ```bash
  pytest tests/test_error_observability.py -q -k "llm or vision" --tb=line
  pytest tests/test_native_search_offering.py -q --tb=line
  ```
  Expected: all PASS. Behavior identical.

- [ ] **Step 2.7: Commit**
  ```bash
  git add src/groksito_discord/llm/llm_input.py
  git commit -m "refactor(llm): slim classification comments + extract context block builders in llm_input (surgical)"
  ```

### Task 3: llm_utils.py — Clean Native Search Builder and Shared Helpers

**Files:**
- Modify: `src/groksito_discord/llm/llm_utils.py`
- No behavior change to token extraction or retry.

- [ ] **Step 3.1: Review _build_native_search_tools.**
  Confirm it already skips on casual/minimal/image_gen and calls get_native_search_descriptions.

- [ ] **Step 3.2: Make any description logic pull strictly from prompt_builder (if drift exists).**
  Already imports `get_native_search_descriptions`. Add a defensive comment:
  ```python
  # Descriptions come exclusively from prompt_builder.get_native_search_descriptions
  # (single source of truth with SYSTEM_PROMPT completeness guidance).
  ```

- [ ] **Step 3.3: If any other guidance strings live here, move the constant to prompt_builder and re-export.**
  (Expect none.)

- [ ] **Step 3.4: Leave retry, token extraction, and _maybe_proactive_summarize untouched** (they are orthogonal to prompt/orchestration focus).

- [ ] **Step 3.5: Run token-related and search tests.**
  ```bash
  pytest tests/test_native_search_offering.py tests/test_response_quality.py -q --tb=line
  ```
  PASS.

- [ ] **Step 3.6: Commit**
  ```bash
  git add src/groksito_discord/llm/llm_utils.py
  git commit -m "chore(llm): document single-source search descriptions in llm_utils"
  ```

### Task 4: client.py — Orchestration Slimming + Continuation Logic Improvement (Core of Plan)

**Files:**
- Modify: `src/groksito_discord/llm/client.py` (target only ~80-120 lines net change, mostly extraction + comments)
- Preserve exact public API and all side-effects (logging order, sentinel returns, error messages).

- [ ] **Step 4.1: Identify the three main phases inside call_grok_for_groksito and add section comments if missing.**
  Current high-level:
  1. Credential + pure-intent prep + build_responses_input (single call)
  2. First-turn tool selection + native search + initial responses.create
  3. Tool execution loop + continuation with previous_response_id

- [ ] **Step 4.2: Extract (surgical) three private helpers at module level (or inside the function as nested if preferred for minimal diff).**
  Keep them tiny:
  ```python
  async def _prepare_first_turn_data(...) -> dict: ...
  def _select_tools_for_first_turn(...) -> tuple[list, list]: ...
  async def _execute_tool_loop(...) -> tuple[Any, bool]: ...
  ```
  The public function body becomes a short, readable sequence that calls these + existing _finalize.

  Exact signatures decided at implementation time to minimize diff; no new public exports.

- [ ] **Step 4.3: Improve continuation native search decision (the current "scan prev_output for search_call" logic).**
  Current (around lines 626-638):
  ```python
  # scan for evidence search happened then re-send native_search_tools
  ```
  Refactor options (choose the least invasive):
  - Option A (preferred for nativeness): Document strongly that previous_response_id is expected to retain prior tool declarations. Change default to send `[]` for continuation_native_search_tools unless an explicit flag (e.g. `search_was_used_last_round`) is threaded. For now keep scanning but wrap in a well-named helper `_should_reoffer_native_search_on_continuation(prev_response)`.
  - Make the detection more robust (check for both function_call and web_search_call types that Responses actually emits).
  - Add a short circuit: if the model just called respond_directly or a delivery tool, never re-offer search on the immediate cont.

  Goal: reduce unnecessary re-sending of non-trivial search schemas.

- [ ] **Step 4.4: Centralize the "offer_light_decision_tools" decision point.**
  It already calls `should_offer_light_decision_tools`. Move the try/except + defaulting into a tiny local helper `_should_offer_light_decision(...)` so the main flow stays clean.

- [ ] **Step 4.5: Add / strengthen a module-level or function docstring block titled "previous_response_id Multi-Turn Contract".**
  Explain:
  - Custom tools are minimized on cont because model remembers.
  - Native search re-inclusion is conservative and only when prior search activity detected.
  - Vision images are sent only on the *first* turn of a logical user message (continuations are text/tool results only).
  - DIRECT_DELIVERY short-circuit must happen before sending tool outputs back.

- [ ] **Step 4.6: Ensure vision 404 retry path still rebuilds via the single build_responses_input call (it does today).** Just add a one-line comment.

- [ ] **Step 4.7: Run the full LLM/tool related test suite.**
  ```bash
  pytest tests/test_tool_selection.py tests/test_native_search_offering.py tests/test_response_quality.py tests/test_error_observability.py -q --tb=short
  ```
  Expected: all PASS. No change in observed behavior.

- [ ] **Step 4.8: Manual smoke (if env allows) or at least import + call signature check.**
  ```bash
  python -c "
  from groksito_discord.llm.client import call_grok_for_groksito
  import inspect
  sig = inspect.signature(call_grok_for_groksito)
  print('Public API preserved:', 'user_message' in str(sig))
  "
  ```

- [ ] **Step 4.9: Commit**
  ```bash
  git add src/groksito_discord/llm/client.py
  git commit -m "refactor(llm): extract thin orchestration helpers in client.py; clarify + improve continuation native search logic for previous_response_id"
  ```

### Task 5: Cross-File Alignment, Tests, Docs, and Verification

**Files:**
- Modify (minimal): `ARCHITECTURE.md` (LLM section)
- Modify (if needed): any test that hard-asserts exact prompt substring length (rare)
- Read: `src/groksito_discord/llm/tools.py` (for description centralization opportunities)
- Full test run + packaging check

- [ ] **Step 5.1: Check tools.py and media_tools for any free-floating guidance strings that should import from prompt_builder.**
  If found (unlikely after Task 1), pull them. Otherwise add a comment at top of tools.py:
  ```python
  # Tool descriptions for media/delivery are intentionally detailed here (user-visible effect).
  # High-level "when to use search / vision / recent context" guidance lives in prompt_builder.
  ```

- [ ] **Step 5.2: Update ARCHITECTURE.md LLM bullet (one paragraph) to reflect the refined philosophy.**
  Add sentence: "Tool selection and prompt content are intentionally minimal; Grok's native reasoning + previous_response_id drive most decisions and continuity."

- [ ] **Step 5.3: Full relevant test suite + existing broader checks.**
  ```bash
  pytest tests/ -q --tb=line -k "tool or llm or native or response or vision or prompt" | cat
  python -m py_compile src/groksito_discord/llm/*.py
  ```
  All must PASS.

- [ ] **Step 5.4: Run any project packaging / import test.**
  ```bash
  python -m pytest tests/test_packaging.py -q --tb=line
  ```

- [ ] **Step 5.5: If any prompt text change is user-visible in tests, update the test assertions surgically (prefer `in` checks over exact strings).**

- [ ] **Step 5.6: Final self-review checklist (per writing-plans skill)**
  - Spec coverage: prompt, input building, classification, orchestration, previous_response_id, token/multi-turn, nativeness all have tasks.
  - No placeholders left.
  - Types / public APIs unchanged.
  - No new heavy classification introduced.
  - Sentinel + direct delivery paths untouched.

- [ ] **Step 5.7: Commit docs + any final test tweaks**
  ```bash
  git add ARCHITECTURE.md docs/superpowers/plans/2026-06-17-llm-orchestration-prompt-refactor.md
  git commit -m "docs: record LLM orchestration refactor plan + minor ARCHITECTURE alignment"
  ```

## Risks and Considerations

**High priority (must not regress):**
- previous_response_id contract: changing what tools are declared on continuation can confuse the model mid-conversation or cause it to re-ask for tools it thinks it has. Mitigation: extremely conservative changes; document the contract; test with multi-round flows if possible.
- Direct delivery sentinel and short-circuit paths in the tool loop. Any reordering of `if direct_delivery_performed` breaks "exactly one user-visible reply".
- Vision 404 retry + plain-text fallback path must remain identical.
- Token metrics / cache metrics logging must keep firing with same fields.

**Medium:**
- Prompt text changes can alter response style/quality. Because we keep completeness guidance and native judgment language, risk is low. Validate via test_response_quality and manual spot checks post-implementation.
- "need" string simplification: some logs and a few tests key off "normal"/"image_gen". Plan keeps the strings flowing for compatibility.
- Emoji block and dynamic ref injection cost: already gated to addressed turns — do not move the gate.

**Low (surgical nature protects):**
- No architecture layer moves (still llm_input single source, tools tiered, etc.).
- No impact on web dashboard or Steam or Discord client activation.
- Docker / packaging unchanged.

**Rollback strategy:** Each commit is small and focused. Revert any single commit if a test or live behavior regresses.

**When NOT to expand scope during implementation:**
- Do not add new custom tools.
- Do not re-enable proactive summarization by default.
- Do not touch media sentinel or delivery.py.
- Do not introduce heavy per-turn classification tables.

**Success criteria (for exit of implementation):**
- All focused tests pass.
- `git diff --stat` shows small targeted changes only in the four llm/ files + minimal docs.
- Manual review of a few addressed-turn + continuation traces (via logs) shows expected lighter or equal token use.
- SYSTEM_PROMPT and search descriptions remain consistent (grep for duplicated phrases should find none).
- ARCHITECTURE.md still accurately describes the flow.
- "Maximum Nativeness" language in AGENTS.md still holds.

## Notes for Implementer

- Always run `grok inspect` or equivalent context load at start of each subagent/task.
- Prefer `search_replace` with unique small strings.
- After each task, run the narrow pytest command listed.
- Update the "Current Project Focus" in AGENTS.md at the very end if the plan author chooses (optional).
- This plan itself is the artifact; do not begin code edits until user approves via exit of plan mode.

---

**Plan written using the writing-plans skill.**

End of plan.
