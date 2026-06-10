# Groksito Web Dashboard

Independent FastAPI + Jinja2 + Tailwind dashboard for Groksito.

## Running

### Locally (after installing web deps)
```bash
pip install -r requirements.txt   # or the web extras
uvicorn web.main:app --reload --port 8000
```

### With Docker Compose (recommended)
```bash
# Only the web UI (bot stays stopped)
docker compose up web

# Both (if you want)
docker compose up
```

The web service is configured to **not** start automatically when you just run `docker compose up` without arguments in some setups, but explicitly starting with `web` is the cleanest.

Access at http://localhost:8000

## Features (MVP)
- **Dashboard**: Quick status + stats cards.
- **Configuration**: Grouped editor for safe settings (Auth mode, TTS voice/language, features, limits). Extremely defensive .env writer with automatic backups + critical value recovery.
- **Usage & Quotas**: Live video quota from shared `data/pantsu_context.json`. Placeholders for images/audio.

## Configuration editing
- Only whitelisted safe keys are shown/editable (see `EDITABLE_KEYS` in `main.py`).
- Secrets (`XAI_API_KEY`, `DISCORD_BOT_TOKEN`, OAuth-related values, and the separate `oauth/xai_oauth_tokens.json` file) are **never loaded into the form** and **can never be deleted or overwritten** by the web UI.
- **Safety guarantees**:
  - Full original `.env` (including comments, blank lines, custom keys, and all secret lines) is read.
  - Only the exact keys being edited have their *value* portion updated (original key casing + inline comments on that line are preserved when possible).
  - Automatic backup: on every save a timestamped `.env.backup-YYYYMMDD-HHMMSS` + rolling `.env.backup` are created next to the real `.env`.
  - Atomic write + post-save verification: if a critical auth key that existed before the save is missing afterward, the file is automatically restored from the backup and the operation fails with a loud message.
- After saving `.env`, **you must restart** the bot:
  ```bash
  docker compose restart groksito-discord-bot
  ```
- The `/config` page now shows a prominent **Authentication & OAuth Status** panel (masked presence only) so you always know whether the bot has the credentials it needs.

## Architecture notes
- Completely separate from `src/groksito_discord/`.
- Reuses the same data volume for quotas/context.
- Uses the shared `.env` for config editing.
- Tailwind via CDN (no build step required).
- Easy to extend: add new routers/ pages under `/templates` and routes in `main.py`.

## Future ideas
- Skills management UI
- Live logs viewer
- Bot health / restart button (careful)
- Multi-user auth (overkill for now)
