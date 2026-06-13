# Architecture

**Groksito Discord Bot & Pantsu Connector**

*Professional tool provider bridging Grok/xAI with Discord and Spotify*

## Overview

Groksito (codenamed **Pantsu** during development) is a modular, secure and extensible Discord bot + tool server. Its primary purpose is to give Grok (and other LLMs) first-class, real-time control over Discord guilds and Spotify playback through a clean function-calling / MCP-compatible interface.

The architecture was designed with the following principles:
- **Separation of concerns** — core bot, tool implementations, embed generation and configuration are clearly separated
- **Security first** — zero secrets in source code, least-privilege Discord permissions, rate-limit handling
- **Extensibility** — adding new tools should be trivial and follow a consistent pattern
- **Developer experience** — clean structure, good documentation, easy to self-host and fork
- **Production readiness** — proper error handling, logging and validation

## High-Level Architecture

```
                    ┌─────────────────────────────┐
                    │        Grok / xAI LLM       │
                    │   (Agent with tool calling) │
                    └──────────────┬──────────────┘
                                   │ Function call (JSON)
                                   ▼
┌─────────────────────────────────────────────────────────────────┐
│                        Groksito Connector                        │
│  ┌──────────────┐   ┌─────────────────┐   ┌──────────────────┐  │
│  │   Tool       │   │   Discord       │   │    Spotify       │  │
│  │  Registry    │──▶│   Client        │──▶│    Client        │  │
│  │  + Validator │   │   (discord.py)  │   │   (spotipy)      │  │
│  └──────────────┘   └────────┬────────┘   └────────┬─────────┘  │
│                              │                     │            │
│                              ▼                     ▼            │
│                       Discord API           Spotify Web API     │
└─────────────────────────────────────────────────────────────────┘
```

## Core Components

### 1. Bot Core / Entry Point
- Main Discord client initialization with required intents
- Connection lifecycle (ready, reconnect, graceful shutdown)
- Optional HTTP/MCP server endpoint to receive tool calls from external agents
- Event loop management

### 2. Tool System (`tools/`)
- `base.py` — Abstract `BaseTool` with common interface and JSON Schema generation
- `discord_tools.py` — ~10-12 tools covering:
  - Guild & channel queries
  - Message send / edit / delete / bulk
  - Rich embed creation (supports custom themes, gaming styles like SovietNoWaifu)
  - Reactions, pins, threads
  - Member & role management
  - Image / attachment upload & retrieval
  - Permission checks
- `spotify_tools.py` — ~6-8 tools:
  - Playback (play, pause, resume, skip, seek, queue)
  - Search (track, artist, album, playlist)
  - Current playback & device control
  - Volume, shuffle, repeat
  - Library (saved tracks, playlists)
- `registry.py` — Central registry that loads, validates and dispatches tool calls

### 3. Embed Engine (`embeds/`)
- Reusable embed builders with theme support
- Gaming / aesthetic styles (SovietNoWaifu, cyberpunk, kawaii, etc.)
- Dynamic field population, image handling, timestamps
- Fallbacks for missing assets

### 4. Configuration (`config/`)
- Pydantic v2 models for strict validation
- Environment variable loading via `pydantic-settings` or `python-dotenv`
- `.env.example` provided as template
- No credentials ever committed

### 5. Utilities (`utils/`)
- Structured logging (rich / structlog)
- Discord & Spotify rate-limit handling + exponential backoff
- Error mapping to user-friendly messages
- Permission & scope validation helpers
- Image processing utilities

## Tool Invocation Flow

1. Grok (or agent) decides a tool is needed and outputs a function call
2. Groksito receives the call (MCP, HTTP POST, or internal dispatch)
3. **Validation layer** checks:
   - Tool exists in registry
   - Parameters match the defined JSON Schema
   - Caller has necessary Discord permissions / Spotify scopes
4. **Execution** — the concrete implementation in `discord_tools.py` or `spotify_tools.py` runs
5. **Side effects** executed on Discord or Spotify
6. **Result** (success data or structured error) is returned to the caller
7. Grok continues its reasoning with the tool result

## Technology Stack

- **Python** 3.11+ (asyncio-first)
- **Discord** — discord.py (or compatible modern fork) with proper intents
- **Spotify** — spotipy
- **Validation** — Pydantic v2
- **Configuration** — python-dotenv / pydantic-settings
- **Logging** — rich or structlog
- **HTTP/MCP** (planned) — FastAPI or Starlette for exposing tools
- **Testing** (planned) — pytest + pytest-asyncio + HTTP mocking

## Security & Privacy

- All secrets loaded exclusively from environment variables
- Discord bot registered with minimal required permissions and intents
- Automatic handling of rate limits and 429 responses
- No persistent storage of personal user data by default
- Input sanitization on every tool parameter
- Clear separation between bot token scopes and tool capabilities

## Extensibility Guide

Adding a new tool is intentionally simple:

1. Create a new async method in the relevant tools file
2. Document it with a clear description and full JSON Schema for parameters
3. (Optional) Add a corresponding embed builder in `embeds/`
4. Register the tool name + handler in the central registry
5. Update this ARCHITECTURE.md and README if the change is significant

This pattern was refined through many iterations with Grok Build.

## Current State (June 2026)

The project contains a working implementation of the core Discord and Spotify tool sets, embed system, and secure configuration. Test and scaffolding files from the initial Grok Build generations have been reviewed and cleaned where they no longer added value.

Future work includes full MCP server mode, Docker packaging, web dashboard, and additional platform integrations.

---

*This document is the single source of truth for the current architecture and will be kept up to date as the project evolves.*