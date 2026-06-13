# Architecture

**Groksito Discord Bot** — Standalone conversational Discord bot powered directly by Grok (xAI).

This document describes the actual architecture as of the current codebase (June 2026). It is a self-contained bot extracted to run independently. It uses the xAI Responses API (OpenAI-compatible), direct HTTP endpoints for image/video/TTS, and a small set of custom Discord tools. No external MCP server, no Spotify integration, and no "connector" abstraction remain.

## Core Principles

- **Maximum nativeness**: Minimal custom memory or proactive context injection. Trust Grok's long context, native vision, web_search, x_search, and reasoning. On-demand tools (e.g. `get_recent_context`) only when the model explicitly needs them.
- **Strict activation & safety**: Guild whitelists, per-user rate limits (enforced before any LLM work), conservative reply-to-other-user policy, and clear separation between conversational owner process and the independent web dashboard.
- **Lightweight skills for recurring work**: Skills are optional, user-approved bundles of instructions + restricted tool lists. They improve consistency on repeated patterns without turning every chat into an agent loop. Auto-creation is deliberately conservative.
- **Sandbox for power**: Advanced tools (`code_execution`, `playwright_browser`) are never offered in normal chat — only to explicitly approved skills, and only inside isolated Docker containers when the host socket is mounted.
- **Decoupled operations**: The Discord bot process and the FastAPI web dashboard are separate runtimes that communicate indirectly via shared `data/` volumes (heartbeats, stats, context snapshots, skills.json) and the `.env` file.

## High-Level Components

```
Discord (Gateway + REST)
        │
        ▼
┌─────────────────────────────────────────────────────────────┐
│  Groksito Bot Process (python -m src.groksito_discord)      │
│  ┌──────────────┐   ┌──────────────────┐   ┌─────────────┐ │
│  │ client.py    │──▶│ conversation.py  │──▶│ llm.py      │ │
│  │ (on_message, │   │ (activation,     │   │ (Responses  │ │
│  │  slashes,    │   │  vision harvest, │   │  + tools)   │ │
│  │  heartbeats) │   │  ref context)    │   └──────┬──────┘ │
│  └──────────────┘   └──────────────────┘          │        │
│                                                    ▼        │
│  ┌──────────────┐   ┌───────────────────────────────┐      │
│  │ tools.py     │◀──│ hybrid execution + tiered     │      │
│  │ (Discord +   │   │ selection (light / minimal)   │      │
│  │  media)      │   └───────────────────────────────┘      │
│  └──────┬───────┘                                          │
│         │                                                  │
│         ▼ (skill-gated only)                               │
│  ┌──────────────┐   ┌──────────────────┐                   │
│  │ skill_tools  │   │ sandbox.py       │                   │
│  │ + registry   │   │ (Docker)         │                   │
│  └──────────────┘   └──────────────────┘                   │
│                                                            │
│  Steam (integrations/steam.py)  •  Grok OAuth (grok_oauth) │
└─────────────────────────────────────────────────────────────┘
        │ (shared volumes: data/, oauth/, .env)
        ▼
┌─────────────────────────────────────────────────────────────┐
│  Independent Web Dashboard (web/main.py — FastAPI)          │
│  • Status / health / guilds                                 │
│  • Safe .env config editor (backups + protected keys)       │
│  • Skills list + detail + approve/revoke                    │
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

### 3. LLM Orchestration & Tool System (`llm.py`, `llm_input.py`, `tools.py`, `media_tools.py`)
- Uses `openai.AsyncOpenAI` against `https://api.x.ai/v1` (Responses API) with the unified bearer from `get_grok_bearer()`.
- Tiered custom tool selection:
  - Normal addressed turns: lightweight set + decision tools.
  - Continuation rounds: ultra-minimal set (aggressive token saving).
  - Pure image gen intent: tiny schema.
- Custom tools include Discord delivery primitives (`reply_to_user`, `react_to_message`, `create_thread`) so the model chooses the presentation style.
- Media tools (`generate_image`, `edit_image`, `generate_video`, `generate_audio`) call the direct xAI endpoints (Imagine, edits, video polling, TTS) and cooperate with `image_delivery.py` for "direct delivery" (the generated asset is sent to the channel by the tool; the LLM receives only a sentinel and does not emit a duplicate text reply).
- Native Grok tools (`web_search`, `x_search`) are available via the Responses API when appropriate.
- Sandbox tools (`code_execution`, `playwright_browser`) are injected only when a skill that declared them is active.
- Multi-turn tool loop with proper `previous_response_id` continuation, prompt caching, and token usage logging.

### 4. Skills + Decision Layer (`skills/`)
- A **Skill** = name + description + highly prescriptive instructions (injected as system message) + explicit list of allowed tools.
- Storage: `data/skills.json` (small, simple JSON via `SkillRegistry`).
- Flows:
  - Conservative auto-creation on strong recurring fingerprints (time window + semantic filters + min occurrences).
  - Model-driven via native tool calls: `create_skill` (with built-in test harness), `edit_skill`, `use_skill`.
  - Decision layer (cheap cached call) or heuristics can select an approved skill; executor injects its block + filtered tools for that turn only.
- Power tools are deliberately unavailable outside approved skills.
- Web dashboard provides the human approval/review surface.

### 5. Steam Integration (`integrations/steam.py`)
- Pure data layer: player count fetching (Steam Charts / Steam API), fuzzy name resolution, robust thumbnail URL construction (multiple CDN patterns + store scrape fallback).
- Used by the three slash commands registered in `client.py`. Embeds are built with game-specific colors and store links.
- No secrets, no rate-limit coupling with the LLM path.

### 6. Authentication (`grok_oauth.py`, config)
- Three modes (`GROK_AUTH_MODE`): `api_key` (default), `oauth`, `auto`.
- `get_grok_bearer()` always returns the best available credential (fresh OAuth preferred when present).
- PKCE loopback flow (`http://127.0.0.1:56121/callback` by default). Token file: `./oauth/xai_oauth_tokens.json` (outside data/, Docker volume mount recommended).
- Proactive refresh + 401 reactive refresh. Clear handling of `invalid_grant`.
- Same bearer works for Responses + raw `Authorization: Bearer` on image/video/TTS endpoints.
- CLI commands fully integrated in `bot.py` (including Docker detection for `--no-browser`).

### 7. Web Dashboard (`web/`)
- Completely separate FastAPI app (own uvicorn process / container target).
- Reuses only a tiny safe surface: `env_utils` + `skills.skill_registry` + read-only health/context snapshots.
- Routes: dashboard, config (whitelisted safe keys only), skills (list/detail/approve), stats, usage, guilds, capabilities.
- Defensive .env writer: full original file preserved, atomic writes, timestamped + rolling backups, critical key recovery on corruption.
- Status cards reflect bot heartbeats written by the Discord process.

### 8. Media Stack (`media/`, `image_delivery.py`, `image_*.py`, `video_generation.py`)
- Centralized handlers for generation + editing with improved prompt handling and Spanish-friendly behavior.
- Direct delivery protocol: a request is registered before calling the generator; the delivery module sends the asset publicly and marks a sentinel so the LLM path produces exactly one reply.
- Audio reuses the same delivery + bubble system for waveform-style voice messages.

### 9. Configuration & Operations (`config.py`, `health.py`, `env_utils.py`)
- Pydantic v2 + pydantic-settings. Everything loaded from `.env` (case-insensitive).
- `validate_for_run()` enforces only the minimal secrets for the chosen auth mode.
- Health: JSON snapshots for bot status, guilds, usage (video quota tracking in context file for now), and heartbeats.
- Rich logging with forced colors for Docker/CI. Cyberpunk startup banner (one-time per invocation).
- Directories (`data/`, `oauth/`) ensured at startup.

### 10. Packaging & Deployment
- `pyproject.toml` + `setup.py` (entry point `groksito`).
- Multi-stage Dockerfile: `bot` (full, ~1GB with ffmpeg + docker-cli) vs `web` (slim, ~280-400MB).
- `docker-compose.yml`: two services that can run independently. Recommended volumes for persistence and the optional Docker socket for skill sandboxes.
- `requirements.txt` (bot + web) and `requirements-web.txt` (dashboard only).

## Data Flow (Typical Turn)

1. Message arrives → client enforces guild + rate limit.
2. Context updated, emojis recorded, activation resolved.
3. If activated: vision harvest + referenced context prepared.
4. `llm.py` builds Responses input (minimal by default) + selected custom tools.
5. Model may call decision tools or directly useful tools (including native web/x_search).
6. Tool results returned; loop continues until `respond_directly` or final message.
7. For media tools: registration → generation → direct public delivery (sentinel) → LLM gets confirmation only.
8. If a skill was selected: its instructions + filtered tools were injected for that turn.
9. Heartbeats and stats snapshots are written continuously for the dashboard.

## Security Notes

- Secrets never in source or images.
- OAuth tokens protected by dedicated directory + `.gitignore`.
- Sandbox tools require explicit skill declaration + (ideally) host Docker socket (documented risk).
- Guild whitelist + rate limits are first-class gates.
- Web config editor never touches secret keys and always backs up before mutation.

## Extension Points (Keep It Small)

- New normal-chat tools → `tools.py` + media or Discord handlers. Consider whether they belong behind the skills gate instead.
- New recurring capability → model-driven `create_skill` (preferred) or add fingerprint rule in `skill_proposer.py` for auto-creation.
- New slash command → `register_slash_commands` in `client.py` + data logic in `integrations/`.
- Dashboard pages → add template + route in `web/main.py` (keep it defensive).

---

This is the single source of truth for the current architecture. Update it when real structure or behavior changes.
