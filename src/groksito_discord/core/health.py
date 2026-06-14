"""
Simple health / status reporting for the standalone Groksito bot.

This module provides lightweight diagnostics that can be used by
the entrypoint, monitoring, or the --status command.

Also provides a file-based heartbeat so the independent web dashboard
(groksito-config-web) can show whether the Discord bot process is
actually connected and alive (shared data/ volume).
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("groksito.health")


# =============================================================================
# Live heartbeat (for separate web UI)
# =============================================================================

HEARTBEAT_FILENAME = "bot_heartbeat.json"
HEARTBEAT_MAX_AGE_SECONDS = 90

# Additional lightweight snapshots (written by the bot, consumed read-only by web dashboard).
# All are best-effort and must never block or raise in the bot.
STATS_FILENAME = "bot_stats.json"
GUILDS_FILENAME = "bot_guilds.json"
HEALTH_SNAPSHOT_FILENAME = "bot_health.json"


def get_heartbeat_path(data_dir: Path | None = None) -> Path:
    """Return the path to the bot heartbeat file (used by web dashboard too)."""
    if data_dir is None:
        try:
            from ..config import settings
            data_dir = settings.data_dir
        except Exception:
            data_dir = Path("./data")
    return Path(data_dir) / HEARTBEAT_FILENAME


def write_bot_heartbeat(
    *,
    connected: bool = True,
    user: str | None = None,
    user_id: int | None = None,
    guilds: int | None = None,
    latency: float | None = None,
    data_dir: Path | None = None,
) -> None:
    """
    Write a small JSON heartbeat file.

    The independent web dashboard reads this (via shared data volume)
    to show a live green/amber status for the Discord bot instead of
    the old static "Independent (check logs...)" message.

    This function is intentionally fire-and-forget and must never
    raise or block the bot.
    """
    path = get_heartbeat_path(data_dir)
    payload: dict[str, Any] = {
        "last_seen": time.time(),
        "connected": bool(connected),
        "user": user,
        "user_id": user_id,
        "guilds": guilds,
        "latency": latency,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except Exception:
        # Heartbeat is best-effort only.
        pass


def _get_data_file(filename: str, data_dir: Path | None = None) -> Path:
    """Resolve a data/ file path (consistent with heartbeat)."""
    if data_dir is None:
        try:
            from ..config import settings
            data_dir = settings.data_dir
        except Exception:
            data_dir = Path("./data")
    return Path(data_dir) / filename


def write_bot_stats(*, data_dir: Path | None = None) -> None:
    """
    Write a lightweight bot_stats.json with token usage from the in-memory tracker.
    Consumed by the web dashboard for the Statistics view. Best-effort only.
    """
    path = _get_data_file(STATS_FILENAME, data_dir)
    try:
        from . import token_usage
        stats = token_usage.get_session_stats()
        recent = token_usage.get_recent_calls(6)
        payload: dict[str, Any] = {
            "last_seen": time.time(),
            "stats": stats,
            "recent_calls": recent,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except Exception:
        # Never let stats snapshot affect the bot.
        pass


def write_bot_guilds_snapshot(guilds: list | None, *, data_dir: Path | None = None) -> None:
    """
    Write a small bot_guilds.json snapshot (id, name, member_count).
    Used by web for the Guilds overview. Best-effort, minimal data.
    """
    path = _get_data_file(GUILDS_FILENAME, data_dir)
    try:
        items: list[dict[str, Any]] = []
        if guilds:
            for g in guilds:
                try:
                    gid = getattr(g, "id", None)
                    if gid is None:
                        continue
                    items.append({
                        "id": int(gid),
                        "name": str(getattr(g, "name", gid)),
                        "member_count": getattr(g, "member_count", None),
                    })
                except Exception:
                    continue
        payload: dict[str, Any] = {
            "last_seen": time.time(),
            "count": len(items),
            "guilds": items,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except Exception:
        pass


def write_bot_health_snapshot(*, data_dir: Path | None = None) -> None:
    """
    Snapshot a slim, web-safe subset of get_health_status() for richer status display.
    Includes video flag, docker, credential presence (booleans only), etc.
    """
    path = _get_data_file(HEALTH_SNAPSHOT_FILENAME, data_dir)
    try:
        health = get_health_status()
        modules = health.get("modules", {}) or {}
        slim: dict[str, Any] = {
            "last_seen": time.time(),
            "status": health.get("status"),
            "video_generation_enabled": bool(health.get("video_generation_enabled")),
            "docker_available_for_sandboxes": bool(health.get("docker_available_for_sandboxes")),
            "docker_version": health.get("docker_version"),
            "has_discord_token": bool(health.get("has_discord_token")),
            "has_xai_key": bool(health.get("has_xai_key")),
            "grok_auth_mode": health.get("grok_auth_mode"),
            "effective_bearer_source": health.get("effective_bearer_source"),
            "emoji_knowledge_count": int(health.get("emoji_knowledge_count", 0) or 0),
            "modules_loaded": sum(1 for v in modules.values() if v == "loaded"),
            "modules_total": len(modules),
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(slim, indent=2), encoding="utf-8")
    except Exception:
        pass


def get_health_status() -> dict[str, Any]:
    """
    Returns a dictionary with basic health information about the bot.
    Safe to call even before full initialization.
    """
    status: dict[str, Any] = {
        "status": "ok",
        "modules": {},
    }

    modules_to_check = [
        "discord.client",
        "core.conversation",
        "context",
        "llm.media_tools",
        "llm.tools",
        "llm",
    ]

    for name in modules_to_check:
        try:
            # When running as `python -m src.groksito_discord`, the package name inside is 'src.groksito_discord'
            # When installed normally, it is 'groksito_discord'
            try:
                __import__(f"groksito_discord.{name}")
            except ImportError:
                __import__(f"src.groksito_discord.{name}")
            status["modules"][name] = "loaded"
        except Exception as e:
            status["modules"][name] = f"error: {e}"
            status["status"] = "degraded"

    # Check for video capability
    try:
        from ..llm.media_tools import ENABLE_VIDEO_GENERATION
        status["video_generation_enabled"] = ENABLE_VIDEO_GENERATION
    except Exception:
        status["video_generation_enabled"] = False

    # Emoji / custom emote knowledge (vision-described lazily only for actually used emotes)
    try:
        from ..utils import emoji_registry
        stats = emoji_registry.get_emoji_stats()
        status["emoji_knowledge_count"] = stats.get("total_emotes", 0)
        status["emoji_knowledge_with_usage"] = stats.get("emotes_with_usage", 0)
        status["emoji_knowledge_guilds"] = stats.get("guilds_with_emotes", 0)
    except Exception:
        status["emoji_knowledge_count"] = 0
        status["emoji_knowledge_with_usage"] = 0
        status["emoji_knowledge_guilds"] = 0

    # Best-effort probe for Docker (required for skill power tools: code_execution / playwright_browser sandboxes).
    # These only activate for approved skills that explicitly list the tools in allowed_tools.
    # Default docker-compose / Dockerfile do not mount the socket; users must opt-in for full power.
    # Never blocks or fails health.
    try:
        import shutil
        import subprocess
        docker_bin = shutil.which("docker")
        if docker_bin:
            # Quick version probe (short timeout, swallow output).
            res = subprocess.run([docker_bin, "--version"], capture_output=True, text=True, timeout=3)
            status["docker_available_for_sandboxes"] = res.returncode == 0
            status["docker_version"] = (res.stdout or res.stderr or "").strip()[:80] if res.returncode == 0 else None
        else:
            status["docker_available_for_sandboxes"] = False
            status["docker_version"] = None
    except Exception:
        status["docker_available_for_sandboxes"] = False
        status["docker_version"] = None

    # Credential presence (safe — only booleans, helps during live testing setup)
    try:
        from ..config import settings
        status["has_discord_token"] = bool(settings.discord_bot_token)
        status["has_xai_key"] = bool(settings.xai_api_key)
        status["grok_auth_mode"] = settings.auth_mode
        status["using_oauth"] = settings.using_oauth
        status["auth_prefers_oauth"] = getattr(settings, "auth_prefers_oauth", False)

        has_oauth_file = False
        try:
            from .grok_oauth import load_oauth_tokens, get_grok_bearer
            tok = load_oauth_tokens()
            has_oauth_file = bool(tok and tok.access_token)
            status["has_oauth_token"] = has_oauth_file
            status["oauth_token_file"] = str(settings.oauth_token_file)
            # Effective bearer source (what will actually be used for calls)
            effective = get_grok_bearer() if get_grok_bearer else None
            status["effective_bearer_source"] = "oauth" if (effective and has_oauth_file) else ("api_key" if effective else "none")
        except Exception:
            status["has_oauth_token"] = False
            status["effective_bearer_source"] = "api_key" if settings.xai_api_key else "none"
    except Exception:
        status["has_discord_token"] = False
        status["has_xai_key"] = False
        status["grok_auth_mode"] = "unknown"
        status["using_oauth"] = False
        status["auth_prefers_oauth"] = False
        status["has_oauth_token"] = False
        status["effective_bearer_source"] = "unknown"

    return status


def print_health_status() -> None:
    """Pretty-prints the health status to the logger."""
    health = get_health_status()
    logger.info("=== Groksito Health ===")
    logger.info(f"Overall status: {health['status']}")
    logger.info(f"Video generation: {health.get('video_generation_enabled')}")
    docker_ok = health.get("docker_available_for_sandboxes")
    logger.info(f"Docker for skill sandboxes (code_execution/playwright): {'available' if docker_ok else 'NOT available (sandboxes will use simulation fallback)'}")
    if health.get("docker_version"):
        logger.info(f"  Docker: {health.get('docker_version')}")
    logger.info(f"Discord token present: {health.get('has_discord_token')}")
    logger.info(f"XAI API key present: {health.get('has_xai_key')}")
    logger.info(f"Grok auth mode: {health.get('grok_auth_mode')} (oauth_strict={health.get('using_oauth')}, prefers_oauth={health.get('auth_prefers_oauth')})")
    if health.get("has_oauth_token"):
        logger.info(f"  OAuth token present: {health.get('has_oauth_token')} (file: {health.get('oauth_token_file')})")
    logger.info(f"  Effective bearer for calls: {health.get('effective_bearer_source')}")
    if health.get("using_oauth"):
        logger.info("  NOTE: GROK_AUTH_MODE=oauth is EXPERIMENTAL (SuperGrok quota). API key mode is the stable default.")
    for name, state in health["modules"].items():
        logger.info(f"  {name}: {state}")
    logger.info("========================")
