"""
Groksito Web Dashboard (independent FastAPI app).

Run separately from the Discord bot:
- Locally: uvicorn web.main:app --reload
- Docker: docker compose up web

Provides:
- Dashboard overview
- Config viewer/editor (safe settings only; edits .env)
- Usage / Quotas (from shared data/)

Uses Tailwind via CDN for zero-build modern UI.
Jinja2 templates.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles

# -----------------------------------------------------------------------------
# Paths (works in container at /app and locally)
# -----------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
DATA_DIR = Path(os.getenv("DATA_DIR", BASE_DIR.parent / "data"))
CONTEXT_FILE = DATA_DIR / "pantsu_context.json"
ENV_FILE = BASE_DIR.parent / ".env"   # mounted in docker

# Make bot modules importable (web is independent but reuses skill_registry for persistence)
import sys
BOT_SRC = BASE_DIR.parent / "src"
if str(BOT_SRC) not in sys.path:
    sys.path.insert(0, str(BOT_SRC))

# Unified safe .env handling (single source of truth shared with setup.py)
from groksito_discord.utils.env_utils import (
    safe_write_env,
    parse_env_file,
    parse_env_lines,
    backup_env as backup_env_file,  # alias for local code that used the old name
    deduplicate_env_file,
    _format_env_value,
    _get_ci,
    CRITICAL_KEYS as CRITICAL_ENV_KEYS,
    PROTECTED_KEYS,
)

from groksito_discord.skills.skill_registry import SkillRegistry, Skill

# Ensure templates dir exists (for dev)
TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Groksito Dashboard", description="Independent web config UI")

# Jinja2
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Add a simple date filter for timestamps (used in skill views)
def _format_timestamp(ts: float | None) -> str:
    if not ts:
        return "—"
    try:
        from datetime import datetime
        return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(ts)[:16]

templates.env.filters["format_ts"] = _format_timestamp

# Mount static if we add any later (currently using CDN)
static_dir = BASE_DIR / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


# -----------------------------------------------------------------------------
# Protected keys (canonical definition lives in env_utils).
# The local name is re-exported from the top-level import for the rest of the file.
# The web saver ALWAYS passes protected_keys=PROTECTED_KEYS to the unified writer.
# -----------------------------------------------------------------------------
# (already imported at the top of the file)

# -----------------------------------------------------------------------------
# Safe editable config keys (whitelist of NON-PROTECTED settings only)
# Only these appear in the form and can be changed via the web UI.
# -----------------------------------------------------------------------------
EDITABLE_KEYS: dict[str, str] = {
    # Feature flags (bool)
    "enable_video_generation": "bool",
    "enable_skill_auto_creation": "bool",
    "enable_skill_decision_layer": "bool",
    "enable_recent_context_summary": "bool",
    "summarization_enabled": "bool",
    "context_smart_mode": "bool",
    "aggressive_continuation_tool_minimization": "bool",
    "log_tool_selection": "bool",
    "log_cache_metrics": "bool",

    # Limits / thresholds (int)
    "recent_context_message_limit": "int",
    "recent_context_max_tokens": "int",
    "summarization_threshold_tokens": "int",
    "api_max_retries": "int",
    "skill_proposal_min_occurrences": "int",
    "skill_auto_create_min_occurrences": "int",
    "skill_auto_create_window_hours": "int",

    # Strings (safe, non-auth)
    "grok_model": "str",
    "log_level": "str",

    # Audio / TTS (web-configurable defaults for audio_handler + xAI /v1/tts)
    "tts_default_voice": "str",
    "tts_default_language": "str",

    # API resilience (safe operational tuning)
    "api_retry_base_delay_seconds": "float",
    "api_timeout_seconds": "float",

    # Skills (legacy proposal flag is safe to expose)
    "enable_skill_proposals": "bool",
}

# Default values for display when not in .env
DEFAULTS = {
    "enable_video_generation": "true",
    "enable_skill_auto_creation": "true",
    "enable_skill_decision_layer": "true",
    "enable_recent_context_summary": "true",
    "context_smart_mode": "true",
    "aggressive_continuation_tool_minimization": "true",
    "log_tool_selection": "true",
    "log_cache_metrics": "true",
    "recent_context_message_limit": "20",
    "recent_context_max_tokens": "400",
    "summarization_threshold_tokens": "6000",
    "api_max_retries": "3",
    "api_retry_base_delay_seconds": "0.5",
    "api_timeout_seconds": "60.0",
    "grok_model": "grok-4.3",
    "log_level": "INFO",
    "tts_default_voice": "eve",
    "tts_default_language": "es",
    "enable_skill_proposals": "true",
}

# Metadata for UI: display names, subtitles, help, advanced flag
SETTINGS_METADATA = {
    # Feature Flags
    "enable_video_generation": {
        "display_name": "Video Generation",
        "subtitle": "T2V + I2V (master switch)",
        "help": "Master switch for the generate_video tool (T2V + I2V).",
        "advanced": False
    },
    "enable_skill_auto_creation": {
        "display_name": "Auto Skill Creation",
        "subtitle": "Automatically create and approve skills for recurring patterns",
        "help": "Auto-create and approve skills for strong recurring patterns.",
        "advanced": False
    },
    "enable_skill_decision_layer": {
        "display_name": "Skill Decision Layer",
        "subtitle": "Use approved skills automatically in chat",
        "help": "Let the decision layer use or propose approved skills.",
        "advanced": False
    },
    "enable_skill_proposals": {
        "display_name": "Skill Proposals (legacy)",
        "subtitle": "Allow proposing new skills",
        "help": "Legacy path for proposing new skills (still supported).",
        "advanced": True
    },
    "enable_recent_context_summary": {
        "display_name": "Recent Context Summaries",
        "subtitle": "On-demand summaries of recent messages",
        "help": "Generate short on-demand summaries of recent channel messages.",
        "advanced": False
    },
    "summarization_enabled": {
        "display_name": "Proactive Summarization",
        "subtitle": "Summarize old conversation history",
        "help": "Proactive summarization of older conversation history.",
        "advanced": False
    },
    "context_smart_mode": {
        "display_name": "Smart Context Mode",
        "subtitle": "Dynamically choose light or rich context",
        "help": "Dynamically use lighter or richer context per query.",
        "advanced": False
    },
    "aggressive_continuation_tool_minimization": {
        "display_name": "Aggressive Tool Minimization",
        "subtitle": "Minimal tools on continuation rounds",
        "help": "Send the smallest possible custom tool list on continuations.",
        "advanced": False
    },
    "log_tool_selection": {
        "display_name": "Log Tool Selection",
        "subtitle": "Detailed tool schema decisions",
        "help": "Log detailed tool schema selection decisions.",
        "advanced": True
    },
    "log_cache_metrics": {
        "display_name": "Log Cache Metrics",
        "subtitle": "Prompt cache hit rates and effectiveness",
        "help": "Log prompt cache effectiveness (hit rate, tokens).",
        "advanced": True
    },

    # Limits & Thresholds
    "recent_context_message_limit": {
        "display_name": "Recent Context Messages",
        "subtitle": "Max messages kept for summaries",
        "help": "Max recent messages considered for context summaries.",
        "advanced": False
    },
    "recent_context_max_tokens": {
        "display_name": "Recent Context Tokens",
        "subtitle": "Target token budget for summaries",
        "help": "Target token budget for recent context summaries.",
        "advanced": False
    },
    "summarization_threshold_tokens": {
        "display_name": "Summarization Threshold",
        "subtitle": "Token count to trigger proactive summary",
        "help": "Approx. token count that triggers proactive summarization.",
        "advanced": False
    },
    "api_max_retries": {
        "display_name": "API Max Retries",
        "subtitle": "Retries for transient errors",
        "help": "Maximum retries for transient Grok API errors.",
        "advanced": False
    },
    "api_retry_base_delay_seconds": {
        "display_name": "API Retry Base Delay",
        "subtitle": "Seconds (exponential backoff)",
        "help": "Base delay (seconds) for exponential backoff on retries.",
        "advanced": True
    },
    "api_timeout_seconds": {
        "display_name": "API Timeout",
        "subtitle": "Total seconds for Grok API calls",
        "help": "Total timeout (seconds) for Grok API calls (Responses + media).",
        "advanced": True
    },
    "skill_proposal_min_occurrences": {
        "display_name": "Skill Proposal Min Occur.",
        "subtitle": "Min requests before legacy proposal",
        "help": "Min similar requests before considering a skill proposal (legacy).",
        "advanced": True
    },
    "skill_auto_create_min_occurrences": {
        "display_name": "Auto-Create Min Occur.",
        "subtitle": "Min occurrences before auto skill",
        "help": "Min occurrences in window before auto-creating an approved skill.",
        "advanced": False
    },
    "skill_auto_create_window_hours": {
        "display_name": "Auto-Create Window",
        "subtitle": "Hours for occurrence counting",
        "help": "Time window (hours) for counting occurrences for auto skill creation.",
        "advanced": False
    },

    # Model & Behavior
    "grok_model": {
        "display_name": "Grok Model",
        "subtitle": "Responses API model name",
        "help": "Grok model name used for the Responses API.",
        "advanced": False
    },

    # TTS & Voice
    "tts_default_voice": {
        "display_name": "Default TTS Voice",
        "subtitle": "eve / ara / rex / sal / leo",
        "help": "Default voice for TTS (eve, ara, rex, sal, leo).",
        "advanced": False
    },
    "tts_default_language": {
        "display_name": "Default TTS Language",
        "subtitle": "BCP-47 code (es, en, auto...)",
        "help": "Default language code for TTS (BCP-47, e.g. es, en, auto).",
        "advanced": False
    },

    # Logging
    "log_level": {
        "display_name": "Log Level",
        "subtitle": "DEBUG / INFO / WARNING / ERROR",
        "help": "Logging level (DEBUG, INFO, WARNING, ERROR).",
        "advanced": False
    },
}

# -----------------------------------------------------------------------------
# Critical / protected keys (never to be accidentally removed or overwritten by UI)
# These are used for status display (masked) + post-save verification + recovery.
# Note: actual secret *values* are never exposed in the web UI.
# -----------------------------------------------------------------------------
CRITICAL_ENV_KEYS: list[str] = [
    "DISCORD_BOT_TOKEN",
    "XAI_API_KEY",
]

OAUTH_RELATED_KEYS: list[str] = [
    "GROK_AUTH_MODE",
    "GROK_OAUTH_PORT",
    "GROK_OAUTH_TOKEN_FILE",
]

# The regex and low-level parsers now live in groksito_discord.env_utils (single source of truth).
# We keep a tiny local reference only for any code that directly referenced ENV_LINE_RE.
# All new .env writes MUST go through safe_write_env (imported above).
try:
    from groksito_discord.utils.env_utils import ENV_LINE_RE  # re-export for any local direct use
except Exception:
    ENV_LINE_RE = re.compile(
        r'^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?:"([^"]*)"|\'([^\']*)\'|([^\s#]*))?\s*(?:#\s*(.*))?$',
        re.IGNORECASE,
    )


def get_auth_status(env_path: Path) -> dict[str, Any]:
    """Return masked/presence info for critical auth + OAuth state.
    Never returns raw secret values.
    """
    env_values = parse_env_file(env_path)
    status: dict[str, Any] = {}

    # XAI key
    xai = _get_ci(env_values, "XAI_API_KEY")
    status["xai_api_key_present"] = bool(xai and len(xai.strip()) > 8)
    if xai and len(xai) > 12:
        status["xai_api_key_masked"] = f"{xai[:6]}...{xai[-4:]}"
    elif xai:
        status["xai_api_key_masked"] = "set (short)"
    else:
        status["xai_api_key_masked"] = "missing"

    # Discord
    disc = _get_ci(env_values, "DISCORD_BOT_TOKEN")
    status["discord_token_present"] = bool(disc and len(disc.strip()) > 10)

    # Auth mode (editable but important)
    mode = _get_ci(env_values, "GROK_AUTH_MODE") or "api_key"
    status["grok_auth_mode"] = mode.lower()

    # OAuth token file (tokens themselves live in JSON, not .env)
    tok_file = _get_ci(env_values, "GROK_OAUTH_TOKEN_FILE") or ""
    if not tok_file:
        # match the default used by the bot
        tok_file = str((Path.cwd() / "oauth" / "xai_oauth_tokens.json").resolve())
    status["oauth_token_file"] = tok_file
    try:
        status["oauth_token_file_exists"] = Path(tok_file).exists()
    except Exception:
        status["oauth_token_file_exists"] = False

    # Effective credential hint (same logic spirit as bot)
    prefers_oauth = mode in ("oauth", "auto") or status["oauth_token_file_exists"]
    if prefers_oauth and status["oauth_token_file_exists"]:
        status["effective_credential"] = "OAuth token (preferred)"
    elif xai:
        status["effective_credential"] = "XAI_API_KEY"
    else:
        status["effective_credential"] = "None (bot will likely fail to start)"

    status["has_any_credential"] = status["xai_api_key_present"] or status["oauth_token_file_exists"]
    return status


# backup_env_file, parse_*, _format, _get_ci etc. are now provided by the import of env_utils
# (with backup_env aliased as backup_env_file for compatibility).
# The old local implementations have been removed to eliminate duplication and drift.


def save_env_updates(path: Path, updates: dict[str, Any]) -> tuple[bool, str]:
    """
    Web-facing safe saver (thin wrapper).

    Delegates to the single unified safe_write_env in env_utils.
    Hard-enforces PROTECTED_KEYS so the web UI can never modify or duplicate
    secrets (DISCORD_BOT_TOKEN, XAI_API_KEY) or auth controls (GROK_AUTH_MODE, etc.).
    """
    if not updates:
        return True, "no changes"

    safe_updates = {
        k: v for k, v in updates.items()
        if k.lower() not in {pk.lower() for pk in PROTECTED_KEYS}
    }
    if not safe_updates and updates:
        return True, "no (safe) changes — all submitted keys were protected and were ignored"

    ok, msg, _bak = safe_write_env(
        path, safe_updates, force_backup=True, protected_keys=PROTECTED_KEYS
    )
    return (True, "") if ok else (False, msg or "save failed")


def get_quotas() -> dict[str, Any]:
    """Read quotas from shared pantsu_context.json (video only for now)."""
    quotas: dict[str, Any] = {
        "video": {},
        "images": "Not quota-tracked (per-user daily video is the only hard limit)",
        "audio": "Not quota-tracked",
        "total_video_today": 0,
    }
    if not CONTEXT_FILE.exists():
        return quotas

    try:
        data = json.loads(CONTEXT_FILE.read_text(encoding="utf-8"))
        video_quotas = data.get("video_quotas", {})
        today = date.today().isoformat()

        total = 0
        per_user = {}
        for uid_str, qdata in video_quotas.items():
            used = qdata.get(today, 0)
            per_user[uid_str] = used
            total += used

        quotas["video"] = per_user
        quotas["total_video_today"] = total
    except Exception:
        pass

    return quotas


def get_config_for_display() -> dict[str, Any]:
    """Load current config values (from .env + defaults) for the UI.
    Attaches help text, advanced flag, and default for better UX.
    """
    env_values = parse_env_file(ENV_FILE)
    display = {}

    for key, typ in EDITABLE_KEYS.items():
        raw = env_values.get(key, DEFAULTS.get(key, ""))
        meta = SETTINGS_METADATA.get(key, {})

        if typ == "bool":
            val = raw.lower() in ("true", "1", "yes", "on") if raw else DEFAULTS.get(key, "true").lower() in ("true", "1")
            display[key] = {
                "value": val,
                "type": "bool",
                "raw": raw,
                "display_name": meta.get("display_name", key.replace("_", " ").title()),
                "subtitle": meta.get("subtitle", ""),
                "help": meta.get("help", ""),
                "advanced": meta.get("advanced", False),
                "default": DEFAULTS.get(key, ""),
            }
        elif typ == "int":
            try:
                display[key] = {
                    "value": int(raw),
                    "type": "int",
                    "raw": raw,
                    "display_name": meta.get("display_name", key.replace("_", " ").title()),
                    "subtitle": meta.get("subtitle", ""),
                    "help": meta.get("help", ""),
                    "advanced": meta.get("advanced", False),
                    "default": DEFAULTS.get(key, ""),
                }
            except Exception:
                display[key] = {
                    "value": int(DEFAULTS.get(key, 0)),
                    "type": "int",
                    "raw": raw,
                    "display_name": meta.get("display_name", key.replace("_", " ").title()),
                    "subtitle": meta.get("subtitle", ""),
                    "help": meta.get("help", ""),
                    "advanced": meta.get("advanced", False),
                    "default": DEFAULTS.get(key, ""),
                }
        elif typ == "float":
            try:
                display[key] = {
                    "value": float(raw),
                    "type": "float",
                    "raw": raw,
                    "display_name": meta.get("display_name", key.replace("_", " ").title()),
                    "subtitle": meta.get("subtitle", ""),
                    "help": meta.get("help", ""),
                    "advanced": meta.get("advanced", False),
                    "default": DEFAULTS.get(key, ""),
                }
            except Exception:
                display[key] = {
                    "value": float(DEFAULTS.get(key, 0)),
                    "type": "float",
                    "raw": raw,
                    "display_name": meta.get("display_name", key.replace("_", " ").title()),
                    "subtitle": meta.get("subtitle", ""),
                    "help": meta.get("help", ""),
                    "advanced": meta.get("advanced", False),
                    "default": DEFAULTS.get(key, ""),
                }
        else:
            display[key] = {
                "value": raw or DEFAULTS.get(key, ""),
                "type": "str",
                "raw": raw,
                "display_name": meta.get("display_name", key.replace("_", " ").title()),
                "subtitle": meta.get("subtitle", ""),
                "help": meta.get("help", ""),
                "advanced": meta.get("advanced", False),
                "default": DEFAULTS.get(key, ""),
            }

    return display


def get_flash_messages(request: Request) -> list[dict[str, str]]:
    """Simple flash messages via query params (no sessions for lightness).
    Extended for config save with restart reminders and detailed errors.
    """
    msgs: list[dict[str, str]] = []
    success = request.query_params.get("success")
    error = request.query_params.get("error")
    saved = request.query_params.get("saved")
    detail = request.query_params.get("detail", "")

    if success == "approved":
        msgs.append({"type": "success", "text": "Skill approved successfully."})
    elif success == "revoked":
        msgs.append({"type": "success", "text": "Skill disabled (approval revoked)."})
    elif success == "deleted":
        msgs.append({"type": "success", "text": "Skill deleted."})

    if saved == "whitelist":
        msgs.append({
            "type": "success",
            "text": "✅ Guild whitelist updated in .env. IMPORTANT: Restart the Discord bot (or container) for changes to take effect. A backup was created automatically."
        })
    elif saved == "dedup":
        msgs.append({
            "type": "success",
            "text": "✅ .env duplicate keys cleaned (most recent value kept for each key). A backup was created."
        })
    elif saved:
        msgs.append({
            "type": "success",
            "text": "✅ Configuration saved to .env. IMPORTANT: Restart the Discord bot (or container) for changes to take effect."
        })

    if success == "dedup_noop":
        msgs.append({"type": "success", "text": "No duplicate keys found in .env."})

    if error == "notfound":
        msgs.append({"type": "error", "text": "Skill not found."})
    elif error == "save_failed":
        base = "❌ Failed to save configuration."
        if detail:
            base += f" Detail: {detail}"
        else:
            base += " Check permissions on .env and that the web process can write the file (and its parent dir)."
        msgs.append({"type": "error", "text": base})
    elif error == "invalid_guild_list":
        msgs.append({"type": "error", "text": "Invalid guild ID list submitted."})
    elif error == "whitelist_save":
        base = "❌ Failed to update guild whitelist."
        if detail:
            base += f" Detail: {detail}"
        msgs.append({"type": "error", "text": base})
    elif error:
        msgs.append({"type": "error", "text": f"Action failed: {error}."})

    return msgs


# =============================================================================
# Live bot status via heartbeat file (shared data volume with the bot)
# =============================================================================

BOT_HEARTBEAT_FILE = DATA_DIR / "bot_heartbeat.json"
BOT_HEARTBEAT_MAX_AGE = 90  # seconds — if no update in this window we consider the bot down

# New lightweight snapshots (written by the bot, read-only here)
BOT_GUILDS_FILE = DATA_DIR / "bot_guilds.json"
BOT_STATS_FILE = DATA_DIR / "bot_stats.json"
BOT_HEALTH_FILE = DATA_DIR / "bot_health.json"


def get_bot_live_status() -> dict[str, Any]:
    """
    Read the bot's heartbeat file (written periodically by the Discord client).

    Returns a small dict the dashboard can use for color + message.
    The bot process writes this via health.write_bot_heartbeat().
    """
    if not BOT_HEARTBEAT_FILE.exists():
        return {
            "online": False,
            "status_text": "Not running (no heartbeat — start the bot)",
            "color": "rose",
            "last_seen": None,
        }

    try:
        data = json.loads(BOT_HEARTBEAT_FILE.read_text(encoding="utf-8"))
        last = float(data.get("last_seen", 0) or 0)
        age = time.time() - last if last else 999999
        explicitly_connected = data.get("connected", True)

        if age > BOT_HEARTBEAT_MAX_AGE:
            return {
                "online": False,
                "status_text": f"Down or stale (last seen ~{int(age)}s ago)",
                "color": "amber",
                "last_seen": last,
                **{k: v for k, v in data.items() if k != "last_seen"},
            }

        if not explicitly_connected:
            user = data.get("user") or ""
            if "start" in str(user).lower():
                status_text = "Starting up..."
            else:
                status_text = "Disconnected (reconnecting...)"
            return {
                "online": False,
                "status_text": status_text,
                "color": "amber",
                "last_seen": last,
                **{k: v for k, v in data.items() if k != "last_seen"},
            }

        # Bot looks alive
        user = data.get("user") or "bot"
        guilds = data.get("guilds")
        latency = data.get("latency")

        extra = ""
        if guilds is not None:
            extra += f" • {guilds} guild(s)"
        if latency is not None and latency > 0:
            extra += f" • {latency * 1000:.0f}ms"

        return {
            "online": True,
            "status_text": f"Connected as {user}{extra}",
            "color": "emerald",
            "last_seen": last,
            **{k: v for k, v in data.items() if k != "last_seen"},
        }
    except Exception:
        return {
            "online": False,
            "status_text": "Heartbeat file unreadable",
            "color": "amber",
            "last_seen": None,
        }


def get_bot_guilds() -> dict[str, Any]:
    """Read the guilds snapshot written by the bot (read-only for web)."""
    if not BOT_GUILDS_FILE.exists():
        return {"count": 0, "guilds": [], "last_seen": None}
    try:
        data = json.loads(BOT_GUILDS_FILE.read_text(encoding="utf-8"))
        return {
            "count": int(data.get("count", 0) or 0),
            "guilds": data.get("guilds", []) or [],
            "last_seen": data.get("last_seen"),
        }
    except Exception:
        return {"count": 0, "guilds": [], "last_seen": None}


def get_bot_stats() -> dict[str, Any]:
    """Read token/session stats snapshot (read-only)."""
    if not BOT_STATS_FILE.exists():
        return {"stats": None, "recent_calls": [], "last_seen": None}
    try:
        data = json.loads(BOT_STATS_FILE.read_text(encoding="utf-8"))
        return {
            "stats": data.get("stats"),
            "recent_calls": data.get("recent_calls", []) or [],
            "last_seen": data.get("last_seen"),
        }
    except Exception:
        return {"stats": None, "recent_calls": [], "last_seen": None}


def get_bot_health_snapshot() -> dict[str, Any]:
    """Read the slim health snapshot for richer status display."""
    if not BOT_HEALTH_FILE.exists():
        return {}
    try:
        return json.loads(BOT_HEALTH_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def get_current_allowed_guild_ids() -> list[int]:
    """Read the current ALLOWED_GUILD_IDS value from .env (supports JSON array or comma list)."""
    if not ENV_FILE.exists():
        return []
    try:
        env = parse_env_file(ENV_FILE)
        raw = _get_ci(env, "allowed_guild_ids") or _get_ci(env, "ALLOWED_GUILD_IDS") or ""
        raw = raw.strip()
        if not raw:
            return []
        if raw.startswith("[") and raw.endswith("]"):
            lst = json.loads(raw)
            return [int(x) for x in lst if str(x).strip()]
        # fallback comma separated
        return [int(x.strip()) for x in raw.split(",") if x.strip().isdigit() or x.strip().lstrip("-").isdigit()]
    except Exception:
        return []


def get_capabilities(health: dict[str, Any] | None = None, config_display: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Build a curated list of bot capabilities with live-ish status (read-only).
    Statuses are derived only from existing health snapshot and safe config values.
    """
    h = health or {}
    cfg = config_display or {}

    def flag(key: str, default: bool = True) -> bool:
        if key in cfg:
            val = cfg[key]
            if isinstance(val, dict):
                return bool(val.get("value", default))
            return bool(val)
        return default

    video_enabled = bool(h.get("video_generation_enabled", True))

    return [
        {
            "name": "Vision",
            "desc": "Image understanding from attachments and referenced messages",
            "status": "active",
            "badge": "native",
        },
        {
            "name": "Image Generation & Editing",
            "desc": "Text-to-image and image-to-image via xAI Imagine",
            "status": "active",
            "badge": "native",
        },
        {
            "name": "Video Generation",
            "desc": "Text-to-Video and Image-to-Video (explicit intent)",
            "status": "active" if video_enabled else "disabled",
            "badge": "feature flag",
            "detail": "5 videos / user / day" if video_enabled else "disabled in config",
        },
        {
            "name": "Web + X Search",
            "desc": "Native Grok web_search and x_search tools",
            "status": "active",
            "badge": "native",
        },
        {
            "name": "Hybrid Tools",
            "desc": "Custom Discord tools + native tool loop with direct delivery",
            "status": "active",
            "badge": "core",
        },
        {
            "name": "Skills System",
            "desc": "Lightweight approved instructions + restricted tools for recurring tasks",
            "status": "active" if flag("enable_skill_decision_layer", True) else "disabled",
            "badge": "opt-in",
        },
        {
            "name": "Recent Context Summarization",
            "desc": "On-demand high-signal summaries of recent channel messages",
            "status": "active" if flag("enable_recent_context_summary", True) else "disabled",
            "badge": "opt-in",
        },
        {
            "name": "Direct Media Delivery",
            "desc": "Media delivered directly to Discord (no duplicate replies)",
            "status": "active",
            "badge": "core",
        },
        {
            "name": "Rate Limiting & Guild Security",
            "desc": "Per-user 6 req/min + ALLOWED_GUILD_IDS whitelist",
            "status": "active",
            "badge": "security",
        },
    ]


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    """Main dashboard (enhanced with richer status from snapshots)."""
    quotas = get_quotas()
    bot_live = get_bot_live_status()
    guilds_snap = get_bot_guilds()
    stats_snap = get_bot_stats()
    health_snap = get_bot_health_snapshot()

    # Status for the dashboard card.
    # bot_online + bot_color are used by the template for green/amber/rose indicators.
    status = {
        "web_status": "Running",
        "bot_status": bot_live["status_text"],
        "bot_online": bot_live["online"],
        "bot_color": bot_live.get("color", "amber"),
        "data_dir": str(DATA_DIR),
        "env_file": str(ENV_FILE),
        # Richer live details from heartbeat if present
        "latency": bot_live.get("latency"),
        "guild_count": guilds_snap.get("count") or bot_live.get("guilds"),
        "user": bot_live.get("user"),
    }

    # Provide a clean quotas dict for the dashboard template.
    clean_quotas = {
        "total_video_today": quotas.get("total_video_today", 0) if isinstance(quotas, dict) else 0,
    }

    # Capabilities for compact dashboard block (live status where possible)
    try:
        config_for_caps = get_config_for_display()
    except Exception:
        config_for_caps = {}
    cap_list = get_capabilities(health_snap, config_for_caps)

    context = {
        "status": status,
        "quotas": clean_quotas,
        "guilds": guilds_snap,
        "stats": stats_snap,
        "health": health_snap,
        "capabilities": cap_list[:6],  # compact summary on dashboard
        "active": "dashboard",
    }

    return templates.TemplateResponse(request, "dashboard.html", context)


@app.get("/guilds", response_class=HTMLResponse)
async def guilds_page(request: Request):
    """Guilds / servers overview + Access Control (whitelist management)."""
    guilds = get_bot_guilds()
    bot_live = get_bot_live_status()
    current_allowed = get_current_allowed_guild_ids()
    messages = get_flash_messages(request)
    return templates.TemplateResponse(
        request,
        "guilds.html",
        {
            "guilds": guilds,
            "current_allowed": current_allowed,
            "bot_live": bot_live,
            "active": "guilds",
            "messages": messages,
        },
    )


@app.post("/guilds/whitelist/update")
async def update_guild_whitelist(request: Request):
    """Update ALLOWED_GUILD_IDS via the safe .env writer. Scoped to low-risk list edits."""
    form = await request.form()
    ids_str = form.get("guild_ids", "")
    try:
        if ids_str.strip().startswith("["):
            new_ids = [int(x) for x in json.loads(ids_str) if str(x).strip()]
        else:
            new_ids = [int(x.strip()) for x in ids_str.split(",") if x.strip()]
        new_ids = sorted(set(new_ids))
    except Exception:
        return RedirectResponse("/guilds?error=invalid_guild_list", status_code=303)

    updates = {"allowed_guild_ids": new_ids}
    ok, msg = save_env_updates(ENV_FILE, updates)
    if ok:
        return RedirectResponse("/guilds?saved=whitelist", status_code=303)
    else:
        err_detail = (msg or "save failed").replace("\n", " | ")[:300]
        return RedirectResponse(f"/guilds?error=whitelist_save&detail={err_detail}", status_code=303)


@app.get("/stats", response_class=HTMLResponse)
async def stats_page(request: Request):
    """Statistics & recent activity (read-only). Token stats + context-derived activity."""
    stats = get_bot_stats()
    quotas = get_quotas()
    health = get_bot_health_snapshot()
    activity = get_recent_activity(limit=25)  # defined below
    messages = get_flash_messages(request)
    return templates.TemplateResponse(
        request,
        "stats.html",
        {
            "stats": stats,
            "quotas": quotas,
            "health": health,
            "activity": activity,
            "active": "stats",
            "messages": messages,
        },
    )


@app.get("/capabilities", response_class=HTMLResponse)
async def capabilities_page(request: Request):
    """Full Capabilities view with live status where available (read-only)."""
    health = get_bot_health_snapshot()
    try:
        cfg = get_config_for_display()
    except Exception:
        cfg = {}
    caps = get_capabilities(health, cfg)
    bot_live = get_bot_live_status()
    messages = get_flash_messages(request)
    return templates.TemplateResponse(
        request,
        "capabilities.html",
        {
            "capabilities": caps,
            "health": health,
            "bot_live": bot_live,
            "active": "capabilities",
            "messages": messages,
        },
    )


def get_recent_activity(limit: int = 25) -> list[dict[str, Any]]:
    """Lightweight recent activity derived from the shared pantsu_context (short-term channel history).
    Strictly read-only view of what the bot has observed. No core data mutation.
    """
    if not CONTEXT_FILE.exists():
        return []
    try:
        data = json.loads(CONTEXT_FILE.read_text(encoding="utf-8"))
        channels = data.get("channels", {}) or {}
        all_msgs: list[dict[str, Any]] = []
        for ch_id, msgs in channels.items():
            if not isinstance(msgs, list):
                continue
            for m in msgs:
                try:
                    all_msgs.append({
                        "ts": float(m.get("ts", 0)),
                        "channel_id": str(ch_id),
                        "author": m.get("author") or "unknown",
                        "author_id": m.get("author_id"),
                        "content": (m.get("content") or "")[:280],
                        "is_bot": bool(m.get("is_bot", False)),
                    })
                except Exception:
                    continue
        # Sort newest first, cap
        all_msgs.sort(key=lambda x: x["ts"], reverse=True)
        return all_msgs[: max(1, int(limit))]
    except Exception:
        return []


@app.get("/config", response_class=HTMLResponse)
async def config_page(request: Request):
    """Config editor page with grouped safe settings + prominent protected Auth/OAuth status."""
    config = get_config_for_display()

    # Sections for logical grouping (protected/auth keys are deliberately excluded from the editable form)
    sections = [
        ("Feature Flags", [
            "enable_video_generation", "enable_skill_auto_creation", "enable_skill_decision_layer",
            "enable_skill_proposals", "enable_recent_context_summary", "summarization_enabled",
            "context_smart_mode", "aggressive_continuation_tool_minimization",
            "log_tool_selection", "log_cache_metrics",
        ]),
        ("Limits & Thresholds", [
            "recent_context_message_limit", "recent_context_max_tokens", "summarization_threshold_tokens",
            "api_max_retries", "api_retry_base_delay_seconds", "api_timeout_seconds",
            "skill_proposal_min_occurrences", "skill_auto_create_min_occurrences",
            "skill_auto_create_window_hours",
        ]),
        ("Model & Behavior", ["grok_model"]),
        ("TTS & Voice", ["tts_default_voice", "tts_default_language"]),
        ("Logging & Debugging", ["log_level"]),
    ]

    # Only include sections that have at least one key present in the config dict
    # (avoids empty sections and Jinja scoping issues with has_any inside loops)
    sections = [
        (title, [k for k in keys if k in config])
        for title, keys in sections
        if any(k in config for k in keys)
    ]

    # Auth / OAuth status (read-only, masked, always shown for visibility & protection)
    auth_status = get_auth_status(ENV_FILE)

    # Collect any warnings for criticals
    warnings: list[str] = []
    if not auth_status.get("has_any_credential"):
        warnings.append("No XAI_API_KEY and no OAuth token file detected. The bot will not be able to call the xAI API.")
    if not auth_status.get("discord_token_present"):
        warnings.append("DISCORD_BOT_TOKEN appears to be missing. The bot cannot connect to Discord.")

    # Backups info (for user awareness / future "restore" UI)
    backup_info = {
        "latest_backup": str(ENV_FILE.with_name(ENV_FILE.name + ".backup")) if ENV_FILE.exists() else None,
        "has_timestamped_backups": any(
            p.name.startswith(ENV_FILE.name + ".backup-") for p in ENV_FILE.parent.glob(ENV_FILE.name + ".backup-*")
        ) if ENV_FILE.parent.exists() else False,
    }

    messages = get_flash_messages(request)
    return templates.TemplateResponse(
        request,
        "config.html",
        {
            "config": config,
            "sections": sections,
            "auth_status": auth_status,
            "warnings": warnings,
            "backup_info": backup_info,
            "messages": messages,
            "active": "config",
        },
    )


@app.post("/config/update")
async def update_config(
    request: Request,
    # We receive form data dynamically
):
    form = await request.form()
    updates: dict[str, Any] = {}

    current = get_config_for_display()

    for key, meta in current.items():
        if key not in EDITABLE_KEYS:
            continue
        # Extra guard: never allow protected keys even if they somehow appear in the current display dict
        if key.lower() in {p.lower() for p in PROTECTED_KEYS}:
            continue

        form_val = form.get(key)
        if form_val is None:
            continue

        typ = EDITABLE_KEYS[key]
        if typ == "bool":
            # Checkbox style: presence means true, or value 'on'
            val = "true" if form_val in ("on", "true", "1") else "false"
        else:
            val = str(form_val).strip()

        updates[key] = val

    if updates:
        # save_env_updates now does backup, safe write, critical recovery, and returns (ok, msg)
        ok, msg = save_env_updates(ENV_FILE, updates)
        if ok:
            # Success + strong restart reminder
            return RedirectResponse(url="/config?saved=1", status_code=303)
        else:
            # Rich error (urlencoded-ish via query; the template will show the detail when present)
            err_detail = msg.replace("\n", " | ")[:300]
            return RedirectResponse(url=f"/config?error=save_failed&detail={err_detail}", status_code=303)

    return RedirectResponse(url="/config", status_code=303)


@app.post("/config/dedup")
async def dedup_env(request: Request):
    """Run the safe deduplicate_env_file maintenance operation (low risk, creates backup)."""
    try:
        changed = deduplicate_env_file(ENV_FILE, keep="last", make_backup=True)
        if changed:
            return RedirectResponse("/config?saved=dedup", status_code=303)
        else:
            return RedirectResponse("/config?success=dedup_noop", status_code=303)
    except Exception as e:
        return RedirectResponse(f"/config?error=dedup_failed&detail={str(e)[:100]}", status_code=303)


@app.get("/usage", response_class=HTMLResponse)
async def usage_page(request: Request):
    """Quotas / usage page."""
    quotas = get_quotas()
    return templates.TemplateResponse(request, "usage.html", {"quotas": quotas, "active": "usage"})


# =============================================================================
# Skills Management (new)
# =============================================================================

@app.get("/skills", response_class=HTMLResponse)
async def skills_list(request: Request):
    """List all skills with approve/disable/delete actions."""
    reg = SkillRegistry(data_dir=DATA_DIR)  # fresh load each request
    all_skills: list[Skill] = reg.list_all()
    # Sort: approved first, then by name
    all_skills.sort(key=lambda s: (not s.approved, s.name.lower()))

    messages = get_flash_messages(request)
    return templates.TemplateResponse(
        request, "skills.html", {"skills": all_skills, "active": "skills", "messages": messages}
    )


@app.get("/skills/{skill_id}", response_class=HTMLResponse)
async def skill_detail(request: Request, skill_id: str):
    """Show full details for one skill + actions."""
    reg = SkillRegistry(data_dir=DATA_DIR)
    skill = reg.get(skill_id)
    if not skill:
        return RedirectResponse("/skills?error=notfound", status_code=303)

    messages = get_flash_messages(request)
    return templates.TemplateResponse(
        request, "skill_detail.html", {"skill": skill, "active": "skills", "messages": messages}
    )


@app.post("/skills/{skill_id}/approve")
async def approve_skill_post(skill_id: str):
    reg = SkillRegistry(data_dir=DATA_DIR)
    sk = reg.approve(skill_id, approved_by="web-ui")
    if sk:
        return RedirectResponse(f"/skills/{skill_id}?success=approved", status_code=303)
    return RedirectResponse("/skills?error=approve", status_code=303)


@app.post("/skills/{skill_id}/revoke")
async def revoke_skill_post(skill_id: str):
    reg = SkillRegistry(data_dir=DATA_DIR)
    ok = reg.revoke(skill_id)
    if ok:
        return RedirectResponse(f"/skills/{skill_id}?success=revoked", status_code=303)
    return RedirectResponse("/skills?error=revoke", status_code=303)


@app.post("/skills/{skill_id}/delete")
async def delete_skill_post(skill_id: str):
    reg = SkillRegistry(data_dir=DATA_DIR)
    ok = reg.delete(skill_id)
    if ok:
        return RedirectResponse("/skills?success=deleted", status_code=303)
    return RedirectResponse("/skills?error=delete", status_code=303)


# -----------------------------------------------------------------------------
# Run: uvicorn web.main:app --reload
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn

    uvicorn.run("web.main:app", host="0.0.0.0", port=8000, reload=True)
