# Architecture

**Groksito Discord Bot** — Standalone conversational Discord bot powered directly by Grok (xAI).

This document describes the current layout (June 2026). See [README.md](./README.md) for setup and usage.

## Core Principles

- **Maximum nativeness**: Minimal custom memory or proactive context injection. Trust Grok's long context, native vision, `web_search`, `x_search`, and reasoning. On-demand tools (e.g. `get_recent_context`) only when the model explicitly needs them.
- **Strict activation & safety**: Guild whitelists, per-user rate limits (before any LLM work), conservative reply-to-other-user policy, and a decoupled web dashboard.
- **Direct delivery**: Media tools send assets to the channel; the LLM path uses a sentinel so exactly one user-visible reply is produced.

## Package Layout

```
src/groksito_discord/
├── main.py                 # CLI entry (groksito console script)
├── discord/
│   ├── client.py           # Gateway, on_message, slash commands, heartbeats
│   └── integrations/
│       ├── steam.py        # Steam Charts / store data for slash commands
│       └── twitch.py       # Twitch helpers (where used)
├── core/
│   ├── conversation.py     # Activation, vision harvest, ref context
│   ├── intent.py           # Visual/audio keyword signals
│   ├── grok_oauth.py       # OAuth PKCE + bearer resolution
│   ├── health.py           # Health snapshots for dashboard
│   └── safety.py           # Safe reply helpers
├── llm/
│   ├── client.py           # Responses API + tool loop
│   ├── llm_input.py        # Prompt/input assembly
│   ├── llm_utils.py        # Native search tool builders
│   ├── tools.py            # Custom tool schemas + dispatch
│   └── media_tools.py      # Media intent gates + handler exports
├── media/
│   ├── image_handler.py    # Text-to-image + image edit
│   ├── video_handler.py    # Text/image-to-video
│   ├── audio_handler.py    # TTS
│   └── delivery.py         # Direct delivery + request tracking
├── context/                # Short-term channel history + optional summarizer
├── config/settings.py      # Pydantic settings from .env
└── utils/                  # env_utils, text, token_usage, emoji_registry, …

web/                        # Independent FastAPI dashboard (port 8010)
setup.py                    # Interactive .env setup at repo root
```

**Data compat note:** Short-term context persists to `data/pantsu_context.json`. The filename is legacy from the pre-standalone extraction; it is intentionally unchanged so existing deployments keep working.

## High-Level Components

```
Discord (Gateway + REST)
        │
        ▼
┌──────────────────────────────────────────────────────────────┐
│  Groksito Bot (groksito / python -m groksito_discord)         │
│  discord/client.py ──▶ core/conversation.py ──▶ llm/client.py │
│         │                      │                      │         │
│         │                      │                      ▼         │
│         │                      │              llm/tools.py      │
│         │                      │              llm/media_tools.py │
│         ▼                      ▼                      │         │
│  discord/integrations/steam   context/          media/*_handler │
│  core/grok_oauth.py                                   delivery  │
└──────────────────────────────────────────────────────────────┘
        │ (shared: data/, oauth/, .env)
        ▼
┌──────────────────────────────────────────────────────────────┐
│  Web Dashboard (web/main.py — FastAPI, own container)       │
│  Status · safe config editor · usage · guilds                 │
└──────────────────────────────────────────────────────────────┘
```

## Layer Summary

### Discord (`discord/client.py`)
- Owns the persistent Gateway connection.
- Guild whitelist and per-user rate limiting (6 req / 60s) before LLM work.
- Slash commands: `/mislimites`, `/stmchr`, `/steamchart`, `/topgames`, `/audio` + TTS context menu.
- Heartbeats (~35s) for dashboard liveness.

### Conversation (`core/conversation.py`, `context/`)
- Activation: @mention or reply-to-bot; reply-to-human only on strong directed signals.
- Vision: attachments, embeds, text URLs, fresh referent images.
- Channel history updated every message; no automatic long-term memory injection.
- `get_recent_context` tool offers on-demand summaries.

### LLM & Tools (`llm/`)
- `prompt_builder.py`: Single source for `SYSTEM_PROMPT` and native search tool descriptions (completeness + nativeness guidance).
- `llm_input.py`: Sole builder of `initial_input` — light classification for logging/gating, minimal `[R:]`/chain injection on addressed turns, separate system blocks for prompt-cache friendliness.
- `client.py`: OpenAI-compatible Responses API against `https://api.x.ai/v1`, three-phase orchestration (prep → first turn → tool loop), `previous_response_id` continuations with conservative native-search re-offer.
- `llm_utils.py`: Native search schema builder (descriptions from `prompt_builder`), token/cache logging, API retry helper.
- Tool selection and prompt content are intentionally minimal; Grok's native reasoning + `previous_response_id` drive most decisions and continuity.
- Tiered custom tools: ultra-minimal on continuations; heavy media only on explicit visual/audio intent; light decision tools on addressed turns.
- Native `web_search` / `x_search` offered on normal addressed paths; skipped on casual/minimal/image_gen.
- Media tools cooperate with `media/delivery.py` for direct delivery (sentinel pattern).

### Steam (`discord/integrations/steam.py`)
- Player counts, fuzzy name resolution, thumbnail URLs. Used by slash commands in `discord/client.py`.

### Auth (`core/grok_oauth.py`, `config/settings.py`)
- Modes: `api_key`, `oauth`, `auto`. Tokens in `./oauth/xai_oauth_tokens.json`.
- `get_grok_bearer()` unified across Responses + raw media HTTP endpoints.

### Web (`web/`)
- Separate FastAPI process. Imports `groksito_discord.utils.env_utils` and config only.
- Safe `.env` editor with backups; never touches secret keys.

### Media (`media/`)
- Centralized handlers for image, video, and audio generation.
- Direct delivery: register request → generate → send attachment → LLM receives sentinel only.
- **Video (Grok web parity):** `generate_video` is offered on light-decision addressed turns (like `generate_image`), with quota enforced only by xAI/SuperGrok at the API. I2V reads reference-image dimensions and maps to `16:9` / `9:16` / `1:1` so model-supplied widescreen ratios do not stretch portrait or square art. Video delivery requests use a 360s TTL (vs 90s for images) because generation polling can run ~300s.

## Typical Turn Flow

1. Message → guild + rate limit gates in `discord/client.py`.
2. Context updated; activation resolved in `core/conversation.py`.
3. `llm/llm_input.py` builds minimal input; `llm/client.py` selects tools and orchestrates the Responses API loop.
4. Tool loop until final reply or `respond_directly`.
5. Media: register → generate → deliver publicly → sentinel suppresses duplicate text.
6. Heartbeats/stats written for dashboard.

## Packaging & Deployment

- `pyproject.toml` with console script `groksito` (`python -m groksito_discord` also works).
- `scripts/configure_env.py` for interactive `.env` setup.
- Multi-stage Dockerfile: `bot` (ffmpeg) and `web` (slim dashboard).
- `docker-compose.yml`: independent bot and web services with recommended `data/` and `oauth/` volumes.

## Extension Points

- New chat tools → `llm/tools.py` + handler in `media/` or `core/`.
- New slash command → `register_slash_commands` in `discord/client.py` + data in `discord/integrations/`.
- Dashboard route → `web/main.py` + template.

## Security

- Secrets and OAuth tokens only via env / `oauth/` (gitignored).
- Guild whitelist and rate limits are first-class gates.
- Web config editor whitelists safe keys only.

---

Update this file when structure or behavior changes materially.