# Groksito Discord Bot — Architecture

This document describes the current architecture of the standalone Groksito Discord bot.

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
