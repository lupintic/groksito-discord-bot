# Contributing to groksito-discord-bot

Thank you for your interest in contributing to **Groksito**! We welcome bug reports, feature ideas, documentation improvements, and code contributions that help keep the bot feeling like talking directly to Grok.

## Code of Conduct

By participating, you are expected to uphold our [Code of Conduct](./CODE_OF_CONDUCT.md). Please report unacceptable behavior to the maintainer.

## Philosophy & Invariants (Read First)

This project follows **Maximum Nativeness** ("Let Grok be Grok"):

- Rely first on Grok’s native capabilities (long context, vision, web_search, x_search, tool calling, built-in media generation).
- Only create custom tools when truly necessary.
- The bot should feel like talking directly to Grok, not like a heavily scripted bot.
- Respect the existing architecture and separation of concerns.

**Critical invariants (never break):**
- Decoupled web dashboard (`web/`) — must **never** access or edit secrets (`DISCORD_BOT_TOKEN`, `XAI_API_KEY`, OAuth tokens).
- Media sentinel delivery pattern (`media/delivery.py`): LLM receives placeholder only; assets delivered directly to channel.
- Strict activation policy + per-user rate limiting (6 requests / 60s) + guild whitelist (`ALLOWED_GUILD_IDS`).
- OAuth tokens live only in `oauth/` (gitignored).
- No automatic long-term memory injection or heavy context stuffing.
- See [AGENTS.md](./AGENTS.md) and [ARCHITECTURE.md](./ARCHITECTURE.md) for the authoritative rules.

## Development Setup

### Prerequisites
- Python >= 3.11
- Discord Bot token
- xAI auth (API key or SuperGrok / X Premium+ via OAuth)
- Docker (recommended for full testing)

### Local Setup (Recommended Quick Path)
```bash
git clone https://github.com/lupintic/groksito-discord-bot.git
cd groksito-discord-bot

# 1. Safe interactive .env setup (idempotent, creates backups)
python scripts/configure_env.py

# 2. Install in editable mode
python -m pip install -e .

# 3. Validate
groksito --check
# or: python -m groksito_discord --check
```

### Running Tests & Verification
```bash
# Core tests
pytest -q

# Full modernization verification (includes --check, --status, optional Docker)
python scripts/check.py
python scripts/check.py --skip-docker   # faster, no image builds
```

### Docker Development
```bash
# Build & run full stack
docker compose up -d

# Web dashboard only
docker compose up web

# OAuth login from container (no browser)
docker compose run --rm groksito-discord-bot --login-oauth --print-url-only
```

See [README.md](./README.md) for complete quick start, Docker volume recommendations (`data/`, `oauth/`), and CLI flags (`--status`, `--auth-status`, etc.).

### OAuth Notes
OAuth tokens are stored in `oauth/xai_oauth_tokens.json` (never commit). Full details in [GROK_OAUTH.md](./GROK_OAUTH.md).

## What to Work On

- Bug fixes and test improvements.
- Documentation and community files (this area).
- New features that preserve "maximum nativeness" and all invariants.
- Steam / integrations only when they fit the native pattern.

Always read AGENTS.md before starting work.

## What NOT to Do

- Do not reintroduce the old MCP/skills system.
- Do not automatically inject long-term memory or conversation history.
- Do not allow the web dashboard to access or edit secrets.
- Do not break the media delivery sentinel pattern.
- Do not mix Discord bot logic with web dashboard logic.
- Do not force tool usage or override Grok's native judgment.
- Keep changes focused.

(See full list in AGENTS.md "What NOT to Do".)

## Pull Request Process

1. Fork the repository and create your feature branch from `main`.
2. Make focused changes. Add or update tests when reasonable.
3. Run the verification suite and ensure everything is green:
   - `pytest`
   - `python scripts/check.py --skip-docker`
4. Update documentation (README, ARCHITECTURE.md if architecture changes, etc.) as needed.
5. Open a Pull Request against `main`.
6. Fill out the PR template completely (checklists cover philosophy, invariants, tests, safety).
7. Be responsive to review feedback.

We use conventional-ish commits. Keep PRs small and reviewable.

## Reporting Issues

Use the GitHub issue templates (bug report / feature request). For security issues, use the private reporting flow described in [SECURITY.md](./SECURITY.md).

## License

Contributions are made under the MIT License (see [LICENSE](./LICENSE)).

## Credits

Maintained by [@lupintic](https://github.com/lupintic) (Pablo Rojas). Built with heavy iteration using Grok models.

Thank you for helping keep Groksito natural and powerful!
