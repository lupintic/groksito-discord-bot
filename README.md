# Groksito Discord Bot

A standalone, full-featured Discord bot powered by Grok (xAI). It provides rich conversational experiences, native vision, tool use, image and video generation, a lightweight skills system for recurring tasks, and an integrated web dashboard for management and observability.

The bot owns its Discord Gateway connection and emphasizes natural interactions that leverage Grok's native capabilities (personality, reasoning, vision, and built-in tools) with minimal custom injection.

## Features

- **Grok-powered conversations** with vision (image understanding), referenced message context, and native xAI tools (`web_search`, `x_search`).
- **Image generation and editing** via xAI endpoints.
- **Video generation** (Text-to-Video and explicit Image-to-Video) with feature flag control and per-user quota tracking.
- **Lightweight skills system**: user-approved or conservatively auto-proposed reusable behaviors (natural language instructions + restricted tool sets) for consistent handling of fresh-data patterns (e.g., game stats). Non-agentic, managed via web UI or tools.
- **Web dashboard**: view stats, usage, health, guilds, and manage skills (list, approve, revoke).
- **Context management**: short-term channel buffers with optional proactive summarization and recent context injection.
- **Media handling**: dedicated audio, image, and video processors plus direct delivery to avoid duplicate replies.
- **Sandbox**: safe execution environment for advanced tools (code execution, browser automation) when explicitly allowed by skills.
- **OAuth support**: browser-based xAI login (SuperGrok / X Premium+) as alternative or complement to API keys, with token persistence and refresh.
- **Observability & safety**: health checks (`--status`, `--check`), token usage and cache metrics, response length safety, guild allow-list.
- **Docker-ready**: includes `Dockerfile` and `docker-compose.yml` with volumes for persistent data and OAuth tokens.
- **Guided setup**: `python setup.py` for safe `.env` creation and repair.

## Quick Start

### 1. Configuration (Critical)

**Never commit secrets.** Real credentials belong only in `.env` (and the `oauth/` directory for tokens). Both are covered by `.gitignore`.

```bash
cp .env.example .env
# Edit .env and set at minimum:
#   DISCORD_BOT_TOKEN=your_bot_token_here
#   XAI_API_KEY=your_xai_key_here          # or use OAuth (see below)
#   ALLOWED_GUILD_IDS=123456789012345678   # strongly recommended for security
```

See `.env.example` for the complete list of variables, feature flags, and explanations (context limits, skill auto-creation thresholds, TTS defaults, summarization, etc.).

### 2. OAuth Login (Optional but Recommended for SuperGrok Users)

Instead of (or alongside) an `XAI_API_KEY`, you can authenticate using your SuperGrok / X Premium+ subscription via browser OAuth. See [GROK_OAUTH.md](./GROK_OAUTH.md) for the full guide, including headless/VPS/Docker flows.

Quick commands:
- `python -m src.groksito_discord --login-oauth`
- `python -m src.groksito_discord --auth-status`
- `python -m src.groksito_discord --test-auth`

Set `GROK_AUTH_MODE=auto` (or `oauth`) in `.env` to prefer the OAuth token when present.

### 3. Run with Docker (Recommended for Production)

```bash
# After configuring .env (and optionally logging in via OAuth)
docker compose up --build
```

The compose file mounts `./data` (runtime state) and `./oauth` (tokens) so they persist across container restarts.

### 4. Run Locally (Development / Testing)

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # Linux/macOS

pip install -r requirements.txt

python -m src.groksito_discord --status     # Validate config and modules
python -m src.groksito_discord              # Start the bot
```

Use `python setup.py` for an interactive, safe guided setup that preserves existing values and creates backups.

## Running the Bot

Common entrypoint:

```bash
python -m src.groksito_discord
```

Useful flags:
- `--status` — configuration and credential diagnostics
- `--check` — deeper health checks
- `--login-oauth`, `--auth-status`, `--test-auth`, `--logout-oauth` — OAuth flows (see GROK_OAUTH.md)

The bot supports slash commands, direct mentions, and replies. Guild access is restricted by `ALLOWED_GUILD_IDS` when set.

## Project Structure (High Level)

```
groksito-discord-bot/
├── src/groksito_discord/          # Main Python package
│   ├── bot.py, __main__.py        # Entry points and CLI
│   ├── client.py                  # Discord Gateway owner + event handling
│   ├── conversation.py            # Activation, vision harvesting, reply context
│   ├── llm.py, llm_input.py, llm_utils.py   # Grok Responses API orchestration, input building, helpers
│   ├── prompt.py                  # System prompts
│   ├── tools.py, media_tools.py, skill_tools.py   # Tool schemas, dispatch, media & skill tools
│   ├── sandbox.py                 # Sandboxed execution for privileged tools
│   ├── context/                   # Short-term buffers + context_summarizer
│   ├── skills/                    # Registry, decision layer, proposer (auto-create), executor
│   ├── media/                     # Audio, image, and video handlers
│   ├── image_generation.py, image_editing.py, image_delivery.py, video_generation.py
│   ├── grok_oauth.py              # xAI OAuth client and token management
│   ├── config.py, env_utils.py    # Settings and environment loading
│   ├── health.py, token_usage.py  # Diagnostics and observability
│   ├── response_safety.py         # Message length safety
│   ├── integrations/              # External services (e.g. Steam)
│   ├── emoji_registry.py, intents.py, utils/
│   └── ...
├── web/                           # Web dashboard (Flask/FastAPI-style)
│   ├── main.py
│   └── templates/                 # UI for dashboard, stats, usage, skills, guilds, config, capabilities
├── data/                          # Runtime persistence (bot state, skills, context, health, etc.)
│   └── .gitkeep                   # Directory is tracked; contents are gitignored
├── tests/                         # Test suite
├── docker-compose.yml
├── Dockerfile
├── setup.py                       # Interactive safe .env setup
├── requirements.txt
├── requirements-web.txt
├── pyproject.toml
├── .env.example                   # Committed template — copy to .env and fill
├── .gitignore
├── README.md
├── ARCHITECTURE.md
└── GROK_OAUTH.md
```

**Runtime-only locations** (never commit their contents):
- `data/` — JSON files for guilds, health, skills, learned patterns, short-term context, etc.
- `oauth/xai_oauth_tokens.json` — OAuth tokens and refresh tokens.
- `__pycache__/`, `.pytest_cache/`, logs, etc.

## Security and Secrets

- `.env` contains your `DISCORD_BOT_TOKEN`, `XAI_API_KEY`, and other configuration. It is ignored by git.
- OAuth tokens live in `oauth/`.
- Always use `ALLOWED_GUILD_IDS` in production to limit which servers the bot can join.
- The web dashboard only exposes safe, non-secret settings and management interfaces.
- Review `.gitignore` for the full list of ignored patterns.

## Development

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
# Configure .env, then run with --status first
python -m src.groksito_discord --status
```

Run tests with `pytest` (see `tests/` for coverage of skills, sandbox, health, config safety, etc.).

## License

See the repository for licensing information.

---

For detailed architecture, see [ARCHITECTURE.md](./ARCHITECTURE.md).  
For the xAI OAuth flow, see [GROK_OAUTH.md](./GROK_OAUTH.md).
