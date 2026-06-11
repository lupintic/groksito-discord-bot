# Groksito Discord Bot — Architecture

This document describes the current architecture of the standalone Groksito Discord bot, plus the target future (agentic) architecture that serves as the north star for the ongoing migration (see the Target section).

## Overview and Design Philosophy

Groksito is a standalone Discord bot that owns the persistent Discord Gateway connection and delivers a rich conversational experience powered by Grok models (via the xAI Responses API and Imagine/Video endpoints).

**Core philosophy**: "Let Grok be Grok." The bot minimizes custom memory injection and heavy agentic scaffolding. It relies primarily on:
- Grok's native personality, reasoning, and long context.
- High-quality referenced/reply context (especially strong on direct replies to the bot).
- Native tools (`web_search`, `x_search`) plus a small set of custom Discord tools.
- Explicit user intent for expensive operations (e.g., video generation).

A deliberately **lightweight, non-agentic skills system** provides optional consistency for recurring fresh-data patterns (user-approved instructions + restricted tool subsets). It is gated, auditable via the web UI, and reuses the normal tool-calling loop rather than introducing new agent runtimes.

The system is designed for token efficiency (tiered tools, prompt caching, optional summarization, direct media delivery) and operational safety (guild allow-lists, response length guards, health checks, sandboxed privileged tools).

## High-Level Components

The application lives under the `src/groksito_discord/` package (src layout).

### Discord Integration Layer
- `client.py`: Sole owner of the Discord Gateway WebSocket. Handles guild whitelist (`ALLOWED_GUILD_IDS`), rate limiting, slash commands, and routes messages into the conversational stack.
- `bot.py` / `__main__.py`: CLI entry points, async runtime bootstrap, `--status` / `--check` diagnostics, and OAuth command wiring.
- `conversation.py`: Activation detection (mentions, replies to bot, explicit visual/X-link intent), vision harvesting (attachments + referenced messages + reply chains), referenced context building, and orchestration of the LLM call.
- `intents.py`: Discord intent configuration.

### LLM Interaction Layer
- `llm.py`: Primary orchestrator (`call_grok_for_groksito`). Manages Responses API calls, the multi-round tool execution loop (using `previous_response_id` for continuations), tiered/hybrid tool selection, proactive summarization trigger, and detection of direct-delivery sentinel.
- `llm_input.py`: Single source of truth for input construction. Builds multimodal content, applies smart context tiering (minimal/normal/rich), injects high-priority referenced messages (with X-link enrichment), and constructs the stable initial input using the minimal system prompt.
- `llm_utils.py`: Helpers for token logging, cache metrics, native search tool builders, visual intent detection, final text extraction, and stub responses.
- `prompt.py`: Ultra-minimal `SYSTEM_PROMPT` and the optional `SUMMARIZATION_PROMPT`.
- `tools.py`: Hybrid custom tool schemas and dispatcher (media tools, reply, skill decision tools, legacy context tools, and sandboxed power tools). Implements aggressive tool minimization on continuation rounds.
- `skill_tools.py`: Skill-related tool schemas and handlers.

### Context Management
- `context/core.py`: Short-term per-channel history buffers (always updated on incoming messages). Used for optional summarization and certain tools.
- `context/context_summarizer.py`: Proactive summarization logic (when enabled via `SUMMARIZATION_ENABLED` and threshold).
- Recent context and "smart mode" features are controlled by environment flags (`ENABLE_RECENT_CONTEXT*`, `CONTEXT_SMART_MODE`).

There is **no automatic long-term per-user memory injection**. The model relies on native conversation history plus the lightweight injected blocks described above. Skills can provide scoped prescriptive instructions when activated.

### Skills System (Lightweight, Non-Agentic)
Located in `skills/`:
- `skill_registry.py`: Storage and lookup of approved skills (persisted in `data/skills.json`).
- `decision.py`: Pre-filter + heuristic that decides whether to offer or invoke a skill on addressed turns (alongside direct reply / search / recent context).
- `skill_proposer.py`: Conservative auto-creation of skills on strong recurring patterns (e.g., specific Steam games) within a time window, subject to semantic blockers.
- `skill_executor.py`: Execution support and post-tool filtering to enforce a skill's declared `allowed_tools`.

A skill consists of natural-language instructions plus an explicit `allowed_tools` subset. Approved skills (or auto-created ones) can inject a high-priority system block and restrict/augment the tools offered for that turn only. Advanced tools (`code_execution`, `playwright_browser`) are **only** available when an approved skill explicitly lists them and the runtime supports the sandbox.

Management UI is provided by the web dashboard. Feature flags: `ENABLE_SKILL_DECISION_LAYER`, `ENABLE_SKILL_AUTO_CREATION`, `ENABLE_SKILL_PROPOSALS`.

### Media Generation, Handling, and Delivery
- `image_generation.py`, `image_editing.py`, `video_generation.py`: Direct calls to xAI Imagine and Video APIs. Respect the `ENABLE_VIDEO_GENERATION` flag and explicit video intent.
- `media_tools.py`: High-level media tool implementations and policy/quota handling.
- `media/`: Specialized handlers (`audio_handler.py`, `image_handler.py`, `video_handler.py`).
- `image_delivery.py`: In-memory tracker + `DIRECT_DELIVERY_PERFORMED` sentinel. Media tools (and `reply_to_user`) deliver results directly to Discord; the conversation layer detects the sentinel and suppresses a duplicate text reply.
- Per-user daily video quota tracking and early rejection (optimistic) lives in context + media layers.

### Sandbox
- `sandbox.py`: Implements safe execution environments (Docker-based) for privileged tools such as code execution and browser automation. These tools are **never** offered in normal chat — they are only injected when an approved skill's `allowed_tools` explicitly requests them.

### Web Dashboard
- `web/main.py`: Lightweight web application exposing observability and management endpoints.
- Templates (`dashboard.html`, `stats.html`, `usage.html`, `skills.html`, `skill_detail.html`, `guilds.html`, `config.html`, `capabilities.html`, `base.html`): UI for real-time stats, token/health/usage metrics, guild information, skill listing/approval/revocation, and safe (non-secret) configuration viewing.

The dashboard is intended for operators. It does not expose or allow editing of secrets (tokens live only in `.env` / `oauth/`).

### Authentication & Configuration
- `config.py`: Pydantic-based centralized settings (tokens, feature flags, thresholds, model selection, data directory, etc.).
- `env_utils.py`: Environment loading helpers.
- `grok_oauth.py`: Full OAuth 2.0 + PKCE client for xAI accounts. Supports `api_key`, `oauth`, and `auto` modes (`GROK_AUTH_MODE`). Handles proactive refresh (~15 min before expiry), reactive refresh, token storage in `oauth/xai_oauth_tokens.json`, and loopback or manual-paste flows. The same bearer is used for chat, image, and video endpoints.

See [GROK_OAUTH.md](./GROK_OAUTH.md) for operational details.

### Observability, Safety, and Health
- `health.py`: `--status` and `--check` diagnostics (credential presence, module loading, feature flags, video quota state).
- `token_usage.py`: Structured logging of prompt/completion/cached tokens and prompt-cache effectiveness metrics (`LOG_CACHE_METRICS`).
- `response_safety.py`: Smart truncation to respect Discord's message length limits.
- `emoji_registry.py`: Emoji knowledge and usage support.
- `correlation.py`: Request correlation / tracing helpers.

### Persistence
Runtime state is stored as JSON files under `DATA_DIR` (default `./data`):
- Guild tracking, bot health/heartbeat/stats
- Skills (`skills.json`)
- Emoji knowledge
- Short-term channel context / pantsu context
- Other operational data

The directory is tracked via `.gitkeep`; actual data files are ignored by `.gitignore`. On first run the bot creates the structure as needed. Docker volumes ensure persistence.

### Integrations
- `integrations/steam.py`: Steam Web API integration (player counts, etc.). Used by skills for fresh data lookups.

Additional integrations can be added in the same package.

## Core Request Flow (High Level)

1. `client.py` receives a message → guild/rate-limit checks → always updates short-term context buffers → hands off to `conversation.py`.
2. `conversation.py` determines activation and harvests vision (attachments + reply chains + X links). Builds referenced context. Invokes the LLM path.
3. `llm_input.py` classifies the turn, constructs multimodal input, and builds the stable initial payload (minimal system prompt + dynamic context when needed).
4. `llm.py` performs tool selection (tiered: very small set on continuations), calls the Responses API (with prompt cache key), runs the multi-round tool loop, and detects direct-delivery sentinel.
5. Tools (`tools.py` / `media_tools.py`) execute. Media or direct-reply tools register with `image_delivery.py` and send the Discord message themselves.
6. On sentinel detection, `conversation.py` skips its normal reply path. Otherwise it sends the final text via `response_safety.safe_reply()`.
7. Skills decision (when enabled) runs as a cheap pre-pass on addressed turns and can inject instructions + filtered tools for the current request.

All paths prefer native Grok behavior and reuse the same continuation mechanism (`previous_response_id`).

**Performance notes (Ticket #5):** The dominant latency/token source on normal @mentions was the unconditional `summarize_recent_conversation` pre-call (extra Responses roundtrip) in `llm_input.py:build_responses_input` for every `is_mentioned`/`is_reply_to_bot` (even plain timeless factual that classify minimal/normal). Gated behind a cheap local `should_generate_recent_summary` predicate (centralized in `intents.py`, reusing `is_conversation_meta_question` + `_has_recent_referent_intent` + modeled kw checks from `decision._heuristic_decision` + classify). Plain addressed timeless now skip the pre-call (and the decision force); raw fallback + native context + on-signal `get_recent_context` tool preserve quality. Matches best-practice: cheap local heuristics before expensive pre-LLM work; no change to model/prompts/caching story.

**Agentic Phase 1 (Ticket #7):** Targeted incremental move toward more native tool reasoning on plain @mentions. Introduced `should_offer_light_decision_tools` (intents.py, reexported) + light vs heavy split in llm.py/tools.py: basic `respond_directly` + `get_recent_context` (small schemas) now offered on addressed non-extreme turns (normal/minimal); heavy (create/edit/use_skill + full) remain gated on strong signals only. Improved descriptions on decision tools (skill_tools.py) and native search (llm_utils.py) to better cue "prefer direct for timeless; search ONLY on clear fresh need". Reduced classification reliance for search offering: addressed "minimal" turns now surface native search schemas (model + respond_directly decide). Added lightweight [ADDRESSED] metrics in token_usage.py (latency, prompt_tokens, search_offered, chose_search/chose_direct, need) + call site in llm.py for addressed first-turn+loop (logs + small deque; no p95 yet). All changes minimal, reversible, safety-preserving; no new flags, no refactors out of scope. See tmp/grok-ticket7-impl-summary.md for measurements.

See the [Target Agentic Architecture](#target-agentic-architecture-roadmap) section below for the intended end state and north star for future phases.

## Target Agentic Architecture (Roadmap)

**Vision**: Evolve Groksito toward a more agentic architecture similar to how Grok operates on the web — with less hardcoded logic and more native reasoning by the model itself.

This is the living target document requested in #9. It defines the desired future state and serves as the reference for the migration. It is intentionally high-level on "how" (detailed tactics live in the phase tickets) and explicit about hard constraints that must never be compromised.

### Core Goals
- **Letting Grok reason more natively**: The model (not Python) decides high-level strategy on most turns — when to answer directly from knowledge, when fresh data is required, when to pull recent context, when (and how) to activate or create a skill, and which interaction primitive to use.
- **Better use of MCP + Skills**: The existing lightweight skills system (natural-language instructions + explicit `allowed_tools` surface, user-approved or conservatively auto-created, injected only when selected) becomes the primary extension point for consistent/reusable behavior. Tools and schemas are shaped like clean, self-describing MCP-style interfaces so native reasoning transfers well. Longer-term, the architecture supports surfacing additional MCP-provided capabilities (e.g., GitHub, Notion, Docker admin, custom integrations) to the model under the same approval/sandbox model used for skills.
- **Reducing manual classification and decision heuristics**: Deprecate or minimize the role of `classify_query_context_need`, the large keyword lists (`_SIMPLE_FACTUAL_HINTS`, `_FRESH_OR_TOOL_HINTS`, `STRONG_DIRECTED_*`, etc. in intents.py), and the heuristic paths in `skills/decision.py` and `llm.py` for normal decision-making and tool-offering. Retain only the minimal subset required for hard safety boundaries, spam/activation prevention, and true ultra-light extremes (pure casual greetings, pure first-turn image/video generation).
- **Keeping Discord constraints in mind**: The bot must always reply in the correct channel or thread. Safety, rate-limit respect, and operational guardrails are non-negotiable. The target must not trade these for "more agentic" behavior.

### Target End-State (High Level)
- **Thin, stable Activation + Safety Layer (outside native reasoning)**: `client.py` (sole Gateway owner), activation logic in `conversation.py`, the activation subset of intents, `response_safety`, explicit-intent detectors for expensive operations (video, image gen/edit), guild allow-lists, and sandbox gating remain and are the *only* places where conservative Python-level decisions are expected. These enforce "wake only when addressed or strongly directed", "deliver every reply to the exact right place", "never offer power tools without explicit user intent + approved skill", and cost/rate safety. Inside an activated conversational turn, Grok drives almost everything via native tool calling.
- **Inside an activated turn — native reasoning dominates**:
  - Ultra-minimal `SYSTEM_PROMPT` (philosophy preserved and possibly further tightened).
  - Native xAI tools (`web_search`, `x_search`) + a small, carefully described set of Groksito action tools are visible to the model on normal addressed turns. The model chooses via its own judgment, guided by excellent tool descriptions and the prompt rules ("use search *only* for clear fresh/time-sensitive needs; prefer direct on timeless/general knowledge").
  - Recent conversation context and channel state are primarily on-demand tools rather than pre-injected (see #11).
  - Key Discord interaction primitives are exposed as first-class, well-described native tools (generalized reply/send under the safety envelope — see #13) so the model can choose the right conversational action instead of having orchestration hardcoded in Python.
  - Skill lifecycle (create, edit, use) is fully native tool-driven and seamless. Skills remain the auditable, opt-in mechanism for user-defined reusable behaviors on recurring fresh-data patterns.
  - Classification and pre-decision heuristics are largely removed from the decision/tool-offering path for normal chat. The model sees the tools and decides. (Extreme lazy paths for pure image_gen and ultra-casual may still use minimal tiering.)
  - Continuation rounds stay aggressively minimized via `previous_response_id`.
- **Skills + MCP synergy**: Skills are "mini-programs" the model (or users via explicit requests) can author and activate. In the target they are the main way to give Grok persistent, consistent capabilities without baking logic into the core bot. All custom tools follow MCP-like conventions (clear names, rich descriptions with examples and constraints, explicit parameters, declared side-effect/privilege level). This makes native reasoning reliable. The same pattern enables future safe exposure of MCP servers to the running bot for extended operator or integration capabilities.
- **Observability for native decisions**: Lightweight structured signals (tool choices, direct vs search vs skill, latency/token impact, rationale where available) are emitted on turns so we can measure the migration, debug reasoning quality, and expose useful views in the web dashboard (#15).
- **Post-migration cleanup**: Once the target behaviors are stable and proven, the deprecated classification logic, pre-filters, old decision heuristics, and now-unused injection paths are removed (#16). The codebase becomes smaller and easier to reason about.

### Hard Constraints That Must Be Preserved
- **Correct delivery is absolute**: Every text reply or direct media delivery must land in the channel/thread the user addressed. This is enforced structurally by the Gateway owner in `client.py`, the reply/mention + referenced context paths in `conversation.py`, and the direct-delivery sentinel. The model never picks "which channel" in a way that could violate this.
- **Safety & cost controls**: Guild allow-lists (`ALLOWED_GUILD_IDS`), per-user video quotas + optimistic early rejection, explicit-intent gates for all generation/editing, sandbox isolation for `code_execution` and `playwright_browser` (never offered outside an approved skill's `allowed_tools`), `response_safety` length guards, and no unsolicited background autonomy.
- **Token and latency discipline**: Extreme laziness on first turns (most normal chat sees zero or tiny custom tool sets), continuation minimization, prompt-cache friendliness, and strong bias toward "direct on timeless" (expressed in tool descriptions + SYSTEM_PROMPT, not Python gatekeeping).
- **No full autonomous agent loops**: The design stays turn-based and conversational. Skills provide scoped, visible, reusable behavior. We do not introduce long-running ReAct-style loops that could act without user visibility or burn resources.
- **Activation heuristics for spam prevention stay (by design)**: Preventing replies to random user-to-user chatter is a core product and safety requirement. The activation subset of keyword/structure checks in `conversation.py` / intents is outside the "reduce manual heuristics" mandate.

### Realization Path (Map to Current Open Tickets)
The following open issues (labeled agentic/roadmap/technical-debt) are the concrete steps toward this target. They should be read alongside this document:

- #10 Reduce Pre-filtering on Normal @mentions (Phase 1 continuation) — broaden when light decision tools are offered, relax classification gating where it unnecessarily limits the model.
- #11 Make Recent Context Tool-Driven Instead of Pre-injected — move summarization / recent history behind an explicit `get_recent_context` tool; reduce unconditional pre-calls.
- #12 Improve Tool Descriptions for Better Native Reasoning — high-leverage refinements to native search and decision tool schemas so Grok makes higher-quality choices.
- #13 Expose Key Discord Actions as Native Tools — reply, send, and related primitives become clean tools the model can invoke naturally (while the activation/safety layer still guarantees correct delivery).
- #14 Simplify and Deprecate Heavy Classification & Decision Logic — systematically reduce the custom keyword lists, `classify_query_context_need`, and heuristic pre-decisions that currently control too much behavior.
- #15 Add Better Observability for Tool Decisions — logging/metrics so we can see what the model is choosing and why (search vs direct, skill use, etc.).
- #16 Clean Up Deprecated Code After Agentic Migration — final removal of the old classification/decision scaffolding once the native paths are proven.

Significant work in any of the above should also produce a short update to the "current" description in this document and a note of progress against the target.

### Non-Goals / Explicit Boundaries
- Do not remove or weaken the activation policy that protects against replying to non-addressed chatter.
- Do not introduce autonomous background tasks or long-running agents.
- Do not expose privileged actions (power tools, cross-guild actions, etc.) without the existing approval + sandbox + explicit-intent layers.
- Do not sacrifice Discord length, rate, or ToS compliance for "more agentic" feel.
- Keep the bot fully standalone for its core conversational role (no external monorepo dependency for the hot path).

### Maintenance
This section is living. After each phase lands, reconcile the "current architecture" description with reality and record what was achieved toward the target. Any fundamental change to the safety/activation boundary, persistence model, privileged execution, or request flow requires an update here in addition to the normal code/docs changes.

Update this document when adding or significantly refactoring major components (new top-level packages, fundamental changes to the request flow, new privileged subsystems, etc.).

## Key Design Principles

- **Modular LLM split**: `llm_input.py` is the single source of truth for input construction. This eliminated previous duplication bugs and makes prompt-caching strategy explicit.
- **Direct media delivery + sentinel**: Guarantees exactly one reply to the user even when generation tools perform the Discord post themselves.
- **Tiered tools + aggressive minimization**: Most turns see a tiny tool surface. Continuation rounds see an ultra-minimal set. The model retains definitions via conversation state.
- **Maximum nativeness**: No heavy custom memory or autonomous agent loops in the base conversation path. Skills are an opt-in, auditable extension for specific recurring needs.
- **Safety and cost control**: Feature flags, explicit-intent gates (video), guild allow-lists, sandbox gating for powerful tools, early quota rejection, and response guards.
- **Operational visibility**: Health commands, token metrics, and the web dashboard make the running bot observable without requiring log diving.
- **Persistence isolation**: All live state lives in `data/` (and `oauth/`) so the source tree and Docker image remain clean.

## Deployment Notes

- The bot is fully standalone (no dependency on external monorepo services for conversation).
- Docker compose mounts `./data` and `./oauth` for persistence.
- Use `python -m src.groksito_discord --status` (or `--check`) before first production runs.
- `setup.py` provides a safe, re-runnable, backup-creating interactive configurator.

This architecture is intended to be stable for production use while remaining easy to evolve in specific areas (new skills, additional integrations, media improvements, etc.).

Update this document when adding or significantly refactoring major components (new top-level packages, fundamental changes to the request flow, new privileged subsystems, etc.).
