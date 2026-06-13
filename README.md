# Groksito Discord Bot

![Python](https://img.shields.io/badge/Python-3.11%2B-blue.svg)
![Discord](https://img.shields.io/badge/Discord-Bot-7289da.svg)
![xAI](https://img.shields.io/badge/xAI-Grok-ff6b6b.svg)
![License](https://img.shields.io/badge/License-MIT-green.svg)

**Groksito** is a standalone Discord bot that brings Grok (xAI) natively into Discord servers. It is a fully conversational experience powered directly by Grok models, with vision, tool use, direct image/video/audio generation, and a lightweight skills system for recurring tasks.

The bot is designed around "maximum nativeness": minimal custom memory or context injection, trusting Grok's long context window, native web_search / x_search, vision, and reasoning. It adds just enough Discord integration and optional reusable skills to be useful in real servers.

## ✨ Features

- **Conversational Grok in Discord**
  - Activates on direct mentions, replies to the bot, or strong directed signals in reply chains.
  - Native vision: processes images from attachments, embeds, and recent referenced messages/URLs.
  - On-demand recent conversation summaries via tool (no automatic heavy context stuffing).

- **Direct Media Generation (Grok-native)**
  - Image generation (`generate_image`) with Grok Imagine — supports stylized and suggestive content per Grok's model policy.
  - Image editing (`edit_image`).
  - Video generation (`generate_video`): text-to-video and image-to-video (toggleable).
  - TTS audio (`generate_audio`): multiple voices (eve, ara, rex, sal, leo) with language control. Dedicated `/audio` slash command and context menu "🔊 Leer en voz alta".

- **Discord Interaction Tools**
  - The model controls response style via tools: `reply_to_user`, `react_to_message`, `create_thread`.
  - Full support for referenced messages, reply chains, and image harvesting.

- **Steam Integration**
  - Slash commands: `/stmchr` (fixed popular list), `/steamchart` (custom games), `/topgames` (live top from Steam Charts).
  - Rich embeds with current players, game-themed colors, thumbnails (robust CDN + fallback resolution), and direct links to Steam store.

- **Lightweight Skills System**
  - Reusable, user-approved "skills": detailed natural-language instructions + explicitly allowed tools (web_search, code_execution, playwright_browser, etc.).
  - Conservative auto-creation for strong recurring patterns (e.g. Steam player counts).
  - Model-driven creation/editing via `create_skill` / `edit_skill` / `use_skill` tools (with automatic pre-save testing harness).
  - Skills inject focused instructions + restricted tool schemas only when selected — keeps normal chat lightweight.
  - Full web UI for browsing, approving, and managing skills.

- **Sandbox Power Tools (Skill-only)**
  - `code_execution` and `playwright_browser` available exclusively to approved skills that declare them.
  - Execute in isolated Docker containers (host Docker socket mount optional). Graceful simulation fallback when unavailable.

- **xAI Authentication Options**
  - Classic `XAI_API_KEY` (stable default).
  - Experimental browser OAuth for SuperGrok / X Premium+ users (`--login-oauth`).
  - `auto` mode prefers fresh OAuth tokens with seamless fallback to API key.
  - Same bearer token used for Responses API + all image/video/TTS endpoints.
  - Docker-friendly flows (`--no-browser`, `--print-url-only` + SSH tunnel).

- **Independent Web Dashboard**
  - Separate FastAPI + Jinja2 application (run via `docker compose up web` or standalone uvicorn).
  - Status & health (live heartbeats from the bot process), guilds list, usage/quotas, configuration editor (safe keys only — secrets never exposed or overwritten).
  - Skills management UI (list, details, approve/revoke).
  - Shares `data/` and `.env` via volumes in Docker. Bot and web are intentionally decoupled processes.

- **Security & Operations**
  - Guild whitelist (`ALLOWED_GUILD_IDS`) — bot ignores everything else.
  - Per-user rate limiting (6 requests / 60s sliding window) enforced before LLM calls.
  - Strict activation policy prevents replying to random user-to-user conversations.
  - All secrets via environment variables only. OAuth tokens in dedicated `./oauth/` (gitignored, Docker volume friendly).
  - Rich structured logging (cyberpunk neon banner at startup) + correlation IDs.
  - Health snapshots and heartbeats feed the dashboard even during startup/reconnects.

- **Docker & Self-hosting**
  - Multi-stage Dockerfile (full "bot" image + slim "web" dashboard image).
  - `docker-compose.yml` with separate services, recommended volume mounts for `data/` and `oauth/`.
  - `--check`, `--status`, `--auth-status`, `--test-auth` CLI commands for safe validation.

## 🚀 Installation & Running

### Prerequisites
- Python 3.11+
- Discord Bot token (https://discord.com/developers/applications)
- xAI authentication: either an `XAI_API_KEY` (console.x.ai) **or** a SuperGrok / X Premium+ account for OAuth login
- (Optional but recommended) Docker for sandboxes and easy deployment
- (For full video gen) ffmpeg is included in the Docker image

### Quick Start (Local)

```bash
# 1. Clone
git clone https://github.com/lupintic/groksito-discord-bot.git
cd groksito-discord-bot

# 2. Create .env (or use setup.py for guided setup)
cp .env.example .env
# Edit .env — at minimum: DISCORD_BOT_TOKEN and XAI_API_KEY (or plan to use --login-oauth)

# 3. (Recommended) Install + validate
python -m pip install -r requirements.txt
python -m src.groksito_discord --check

# 4. (Optional but powerful) Login with OAuth instead of / in addition to API key
python -m src.groksito_discord --login-oauth
# or for Docker/VPS: --login-oauth --print-url-only  (then SSH tunnel from laptop)

# 5. Run the bot
python -m src.groksito_discord
```

Useful CLI flags:
- `--check` / `--status` — validate config and show health without connecting
- `--auth-status`, `--test-auth` — verify xAI credentials (OAuth or key)
- `--login-oauth`, `--logout-oauth`

### Docker (Recommended for 24/7)

```bash
# Full stack (bot + web dashboard on :8010)
docker compose up -d

# Web dashboard only
docker compose up web

# Login OAuth from the container (no browser inside)
docker compose run --rm groksito-discord-bot --login-oauth --print-url-only
```

Access the dashboard at http://localhost:8010 (or the port you mapped).

See `docker-compose.yml` comments for the optional Docker socket mount (enables real `code_execution` + `playwright_browser` for skills).

## 📖 Usage

- Mention `@Groksito` or reply directly to the bot → it activates.
- Strong signals (e.g. "qué es eso de arriba", "genera una imagen de...", "lee esto en voz alta") in replies to other users can also wake it (conservative policy).
- Use `/audio` or right-click message → Apps → "🔊 Leer en voz alta" for TTS.
- Steam: `/stmchr`, `/steamchart`, `/topgames`.
- Skills surface automatically for matching recurring queries once approved. You can also ask the bot to create or edit skills.
- The web dashboard (`/skills`, `/config`, `/usage`, etc.) lets you review/approve skills and tweak safe settings without touching secrets.

Example interactions are natural Spanish/English conversation. The bot is intentionally low-ceremony.

## 🏗️ Architecture & Internals

See [ARCHITECTURE.md](./ARCHITECTURE.md) for component breakdown, data flow, the hybrid tool system, skills decision layer, media stack, OAuth handling, and extension points.

High-level pieces live under `src/groksito_discord/`:
- `bot.py` + `client.py` — Discord client, activation, slash commands, heartbeats.
- `llm.py` + `llm_input.py` — Responses API orchestration, tiered tool selection, multi-turn tool loops.
- `tools.py` + `skill_tools.py` + `media_tools.py` — custom Discord/media tools + skill meta-tools.
- `skills/` — registry, decision layer, proposer, executor.
- `integrations/steam.py` — player count fetching and embed data.
- `grok_oauth.py` — OAuth PKCE + token management.
- `web/` — independent FastAPI dashboard (reuses only skill_registry + env_utils).

## 🛠️ Development & Configuration

- All runtime configuration is in `.env` (Pydantic `GroksitoSettings`).
- Key flags: `GROK_AUTH_MODE`, `ALLOWED_GUILD_IDS`, `ENABLE_VIDEO_GENERATION`, `ENABLE_SKILL_*`, TTS voice/language, etc.
- The web `/config` page edits only whitelisted safe keys and creates timestamped backups on every save.
- Add new custom tools by extending the schemas/handlers in the tools layer and registering them (most new capabilities should go through the skills system for normal chat).
- Tests live in `tests/`. Run with `pytest`.

Never commit `.env` or `oauth/xai_oauth_tokens.json`.

## 📄 License

MIT License — see [LICENSE](./LICENSE).

## 🤝 Contributing

Contributions, bug reports, and feature ideas are welcome.

1. Fork the repo
2. Create a feature branch
3. Make your changes + add tests when reasonable
4. Open a Pull Request

Keep changes focused and respect the "maximum nativeness" philosophy.

## 🙏 Credits

- Built with heavy iteration using Grok models and tooling.
- Thanks to the xAI team for the Grok models and APIs.
- Steam data via public Steam Charts + store APIs (no affiliation).

---

**Status**: Active. Self-hostable with Docker. Focused on a clean, powerful, and natural Grok-in-Discord experience.

Made with ❤️ by [@lupintic](https://github.com/lupintic).
