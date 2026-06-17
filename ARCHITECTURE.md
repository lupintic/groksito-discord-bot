# Architecture

**Groksito Discord Bot** — Standalone conversational Discord bot powered directly by Grok (xAI).

This document describes the actual architecture as of the current codebase (June 2026). It is a self-contained bot extracted to run independently. It uses the xAI Responses API (OpenAI-compatible), direct HTTP endpoints for image/video/TTS, and a small set of custom Discord tools.

## Core Principles

- **Maximum nativeness**: Minimal custom memory or proactive context injection. Trust Grok's long context, native vision, web_search, x_search, and reasoning. On-demand tools (e.g. `get_recent_context`) only when the model explicitly needs them.
- **Strict activation & safety**: Guild whitelists, per-user rate limits (enforced before any LLM work), conservative reply-to-other-user policy, and clear separation between conversational owner process and the independent web dashboard.
- **Decoupled operations**: The Discord bot process and the FastAPI web dashboard are separate runtimes that communicate indirectly via shared `data/` volumes (heartbeats, stats, context snapshots) and the `.env` file.

## High-Level Components

```
Discord (Gateway + REST)
        │
        ▼
┌─────────────────────────────────────────────────────────────┐
│  Groksito Bot Process (groksito / python -m groksito_discord) │
│  ┌──────────────┐   ┌──────────────────┐   ┌─────────────┐ │
│  │ client.py    │──▶│ conversation.py  │──▶│ llm/client  │ │
│  │ (on_message, │   │ (activation,     │   │ (Responses  │ │
│  │  slashes,    │   │  vision harvest, │   │  + tools)   │ │
│  │  heartbeats) │   │  ref context)    │   └──────┬──────┘ │
│  └──────────────┘   └──────────────────┘          │        │
│                                                    ▼        │
│  ┌──────────────┐   ┌───────────────────────────────┐      │
│  │ tools.py     │◀──│ hybrid execution + tiered     │      │
│  │ (Discord +   │   │ selection (light / minimal)   │      │
│  │  media)      │   └───────────────────────────────┘      │
│  └──────────────┘                                          │
│                                                            │
│  Steam (discord/integrations/steam.py)  •  Grok OAuth (grok_oauth) │
└─────────────────────────────────────────────────────────────┘
        │ (shared volumes: data/, oauth/, .env)
        ▼
┌─────────────────────────────────────────────────────────────┐
│  Independent Web Dashboard (web/main.py — FastAPI)          │
│  • Status / health / guilds                                 │
│  • Safe .env config editor (backups + protected keys)       │
│  • Usage / quotas snapshots                                 │
│  Runs on its own process / container (port 8010)            │
└─────────────────────────────────────────────────────────────┘
```

## Detailed Layers

### 1. Discord Client & Activation (`client.py`, `bot.py`)
- Owns the persistent Gateway connection (the "conversational owner").
- Guild whitelist enforcement on every message and slash command.
- Thin `on_message` orchestrator: correlation ID, context update (short-term per-channel), emoji tracking, rate limit check, activation decision delegated to `conversation.py`.
- Registers slash commands: `/mislimites`, `/stmchr`, `/steamchart`, `/topgames`, `/audio` + context menu for TTS.
- Background heartbeat task (~35s) writes liveness + guild snapshots so the separate web dashboard can show accurate "Connected" status even across restarts.
- Lifecycle events (`on_ready`, `on_disconnect`, `on_resumed`) also feed health data.

### 2. Conversation & Context (`conversation.py`, `context/`)
- Strict activation policy (documented in code):
  - Direct @mention or reply to bot → activate.
  - Reply to another human → only on strong directed signals (visual intent keywords + `STRONG_DIRECTED_KEYWORDS`).
- Vision harvesting: attachments, embed images, text-extracted image URLs, recent referent images (fresh only).
- Referenced message + chain context building for replies.
- Always updates short-term per-channel history (`data/pantsu_context.json` — legacy filename kept for data compatibility).
- No automatic long-term memory injection. `get_recent_context` tool (and optional summarizer) is offered on-demand only.

### 3. LLM Orchestration & Tool System (`llm/client.py`, `llm_input.py`, `tools.py`, `media_tools.py`)
- Uses `openai.AsyncOpenAI` against `https://api.x.ai/v1` (Responses API) with the unified bearer from `get_grok_bearer()`.
- Tiered custom tool selection:
  - Normal addressed turns: lightweight delivery tools + `get_recent_context` / `respond_directly`.
  - Continuation rounds: ultra-minimal set (aggressive token saving).
  - Pure image gen intent: tiny schema.
- Custom tools include Discord delivery primitives (`reply_to_user`, `react_to_message`, `create_thread`) so the model chooses the presentation style.
- Media tools (`generate_image`, `edit_image`, `generate_video`, `generate_audio`) call the direct xAI endpoints (Imagine, edits, video polling, TTS) and cooperate with `image_delivery.py` for "direct delivery" (the generated asset is sent to the channel by the tool; the LLM receives only a sentinel and does not emit a duplicate text reply).
- Native Grok tools (`web_search`, `x_search`) are available via the Responses API when appropriate.
- Multi-turn tool loop with proper `previous_response_id` continuation, prompt caching, and token usage logging.

### 4. Steam Integration (`discord/integrations/steam.py`)
- Pure data layer: player count fetching (Steam Charts / Steam API), fuzzy name resolution, robust thumbnail URL construction (multiple CDN patterns + store scrape fallback).
- Used by the three slash commands registered in `client.py`. Embeds are built with game-specific colors and store links.
- No secrets, no rate-limit coupling with the LLM path.

### 5. Authentication (`grok_oauth.py`, config)
- Three modes (`GROK_AUTH_MODE`): `api_key` (default), `oauth`, `auto`.
- `get_grok_bearer()` always returns the best available credential (fresh OAuth preferred when present).
- PKCE loopback flow (`http://127.0.0.1:56121/callback` by default). Token file: `./oauth/xai_oauth_tokens.json` (outside data/, Docker volume mount recommended).
- Proactive refresh + 401 reactive refresh. Clear handling of `invalid_grant`.
- Same bearer works for Responses + raw `Authorization: Bearer` on image/video/TTS endpoints.
- CLI commands fully integrated in `bot.py` (including Docker detection for `--no-browser`).

### 6. Web Dashboard (`web/`)
- Completely separate FastAPI app (own uvicorn process / container target).
- Reuses only a tiny safe surface: `env_utils` + read-only health/context snapshots.
- Routes: dashboard, config (whitelisted safe keys only), stats, usage, guilds, capabilities.
- Defensive .env writer: full original file preserved, atomic writes, timestamped + rolling backups, critical key recovery on corruption.
- Status cards reflect bot heartbeats written by the Discord process.

### 7. Media Stack (`media/`, `delivery.py`, `*_handler.py`)
- Centralized handlers for generation + editing with improved prompt handling and Spanish-friendly behavior.
- Direct delivery protocol: a request is registered before calling the generator; the delivery module sends the asset publicly and marks a sentinel so the LLM path produces exactly one reply.
- Audio reuses the same delivery + bubble system for waveform-style voice messages.

### 8. Configuration & Operations (`config.py`, `health.py`, `env_utils.py`)
- Pydantic v2 + pydantic-settings. Everything loaded from `.env` (case-insensitive).
- `validate_for_run()` enforces only the minimal secrets for the chosen auth mode.
- Health: JSON snapshots for bot status, guilds, usage (video quota tracking in context file for now), and heartbeats.
- Rich logging with forced colors for Docker/CI. Cyberpunk startup banner (one-time per invocation).
- Directories (`data/`, `oauth/`) ensured at startup.

### 9. Packaging & Deployment
- `pyproject.toml` (entry point `groksito`) + `scripts/configure_env.py` (interactive .env setup).
- Multi-stage Dockerfile: `bot` (full, with ffmpeg) vs `web` (slim, ~280-400MB).
- `docker-compose.yml`: two services that can run independently. Recommended volumes for persistence.
- `requirements.txt` (bot + web) and `requirements-web.txt` (dashboard only).

## Data Flow (Typical Turn)

1. Message arrives → client enforces guild + rate limit.
2. Context updated, emojis recorded, activation resolved.
3. If activated: vision harvest + referenced context prepared.
4. `llm/client.py` builds Responses input (minimal by default) + selected custom tools.
5. Model may call decision tools or directly useful tools (including native web/x_search).
6. Tool results returned; loop continues until `respond_directly` or final message.
7. For media tools: registration → generation → direct public delivery (sentinel) → LLM gets confirmation only.
8. Heartbeats and stats snapshots are written continuously for the dashboard.

## Security Notes

- Secrets never in source or images.
- OAuth tokens protected by dedicated directory + `.gitignore`.
- Guild whitelist + rate limits are first-class gates.
- Web config editor never touches secret keys and always backs up before mutation.

## Extension Points (Keep It Small)

- New normal-chat tools → `tools.py` + media or Discord handlers.
- New slash command → `register_slash_commands` in `client.py` + data logic in `integrations/`.
- Dashboard pages → add template + route in `web/main.py` (keep it defensive).

---

This is the single source of truth for the current architecture. Update it when real structure or behavior changes.