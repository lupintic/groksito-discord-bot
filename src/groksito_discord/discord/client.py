"""
Discord Client + Connection Ownership for Groksito (Standalone Conversational Bot)

This module is the sole owner of the persistent Discord Gateway WebSocket
connection for the conversational @Groksito experience.

Key responsibilities:
- Singleton Discord client + Gateway connection
- Guild whitelist enforcement (early security gate)
- Per-user rate limiting (6 requests / 60s)
- Thin on_message orchestration (activation, context update, then delegate)
- Slash command registration
- Wiring to conversation.py + LLM stack (no custom memory; no automatic injection)
- Liveness heartbeats for the independent web dashboard

Important invariants (do not break):
- This process is the *only* owner of the Discord Gateway for conversation.
- Guild whitelist checked in both on_message and every slash command.
- Rate limit check happens *before* invoking the LLM path.
- Context (short-term channel history) is always updated for *every* message.
- Strict activation policy: only @mentions or direct replies to Groksito messages.
- Direct media delivery uses the DIRECT_DELIVERY_PERFORMED sentinel
  (cooperates with image_delivery.py + llm.py to guarantee exactly one reply).
- Background heartbeat task keeps the web UI informed of connection status.

Phase 3 (this refactoring): Slash commands were extracted to a dedicated
registration function for better structure. No behavior changed.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict, deque
from typing import Any, Deque, Optional

from ..utils.correlation import (
    cid_prefix,
    generate_correlation_id,
    set_correlation_id,
)

import discord

# Suppress voice-related warnings (voice is intentionally unsupported).
try:
    from discord.voice_client import VoiceClient as _DiscordVoiceClient
    _DiscordVoiceClient.warn_nacl = False
    _DiscordVoiceClient.warn_dave = False
except Exception:
    pass

from ..config import settings
from ..core.safety import safe_reply as _safe_reply

# Steam integration (extracted in Phase 1 for client hygiene).
# All data fetching, game resolution, and scraping logic lives in
# src/groksito_discord/discord/integrations/steam.py. Behavior is identical.
from .integrations import steam

# Centralized text utilities (Phase 2). Replaces inline URL extraction that
# duplicated logic from conversation.py.
from ..utils.text import extract_urls_from_text

# Dedicated /audio slash (reuses 100% of audio_handler.py for TTS + fancy voice delivery
# via the image_delivery direct-delivery tracker; no duplication of generation or bubble logic).
from ..media.delivery import register_image_request
from ..media.audio_handler import (
    _tool_generate_audio,
    prepare_text_from_interaction,
)

logger = logging.getLogger("groksito.client")


# =============================================================================
# Guild Whitelist Security
# =============================================================================
_ALLOWED_GUILD_IDS: set[int] = set(settings.allowed_guild_ids)


def is_guild_allowed(guild_id: int | None) -> bool:
    if not _ALLOWED_GUILD_IDS:
        return True
    if guild_id is None:
        return False
    return guild_id in _ALLOWED_GUILD_IDS


# =============================================================================
# Global State
# =============================================================================
_discord_client: "discord.Client | None" = None
_discord_ready = asyncio.Event()
_discord_task: asyncio.Task | None = None

rate_limiter: Any = None
tree: Any = None


# =============================================================================
# Rate Limiter
# =============================================================================
# Simple per-user sliding window rate limiter (6 requests per 60 seconds).
# Enforced in on_message (before LLM invocation) and in /mislimites.
# This is a basic defense against abuse; the actual heavy lifting for
# conversational rate limiting and cost control lives in the LLM/tool layer.
class RateLimiter:
    def __init__(self, max_requests: int = 6, window_seconds: int = 60):
        self.max_requests = max_requests
        self.window = window_seconds
        self.records: dict[int, Deque[float]] = defaultdict(deque)

    def check(self, user_id: int) -> tuple[bool, int]:
        now = time.time()
        user_records = self.records[user_id]
        while user_records and now - user_records[0] > self.window:
            user_records.popleft()
        used = len(user_records)
        if used >= self.max_requests:
            return False, 0
        user_records.append(now)
        return True, self.max_requests - used

    def get_remaining(self, user_id: int) -> int:
        now = time.time()
        user_records = self.records[user_id]
        while user_records and now - user_records[0] > self.window:
            user_records.popleft()
        return max(0, self.max_requests - len(user_records))


# =============================================================================
# Slash Command Registration (Phase 3)
# =============================================================================
def register_slash_commands(
    tree: "discord.app_commands.CommandTree", client: "discord.Client"
) -> None:
    """Register Groksito's slash commands on the provided CommandTree.

    This function was extracted in Phase 3 to improve organization and
    readability of client.py. The command definitions used to live inline
    inside ensure_discord_connected.

    All command names, descriptions, parameters, implementation logic,
    guild whitelist checks, rate limiting, defer/followup behavior,
    error messages, and user-visible output are **exactly** the same
    as before. No behavior was changed.

    The /steamchart and /stmchr commands delegate to the steam integration
    module (extracted in Phase 1).
    """
    # /mislimites ΓÇö shows remaining requests for the current user (rate limit info)
    @tree.command(
        name="mislimites", description="Muestra cu├íntas requests te quedan en este minuto"
    )
    async def mislimites(interaction: discord.Interaction):
        if interaction.guild and not is_guild_allowed(interaction.guild.id):
            await interaction.response.send_message(
                "Groksito no est├í disponible en este servidor.", ephemeral=True
            )
            return

        user_id = interaction.user.id
        rl = getattr(client, "rate_limiter", rate_limiter)
        remaining = rl.get_remaining(user_id)
        await interaction.response.send_message(
            f"**{interaction.user.display_name}**, te quedan **{remaining}/6** requests en este minuto.",
            ephemeral=True,
        )

    # /steamchart ΓÇö optional juegos (comma-separated). Falls back to a sensible default list.
    # Now renders exactly like /stmchr: one rich embed per game (name + current players,
    # game-themed color when known, clickable title to Steam store, and thumbnail image).
    # Thumbnails use the same robust resolver as /stmchr (multiple CDNs + store scrape fallback).
    # If no thumbnail resolves, falls back to the standard Steam header.jpg (same as /stmchr).
    # Implementation delegates to the extracted steam integration (Phase 1).
    @tree.command(
        name="steamchart",
        description="Muestra jugadores concurrentes en Steam. Ej: /steamchart black desert, path of exile 2",
    )
    async def steamchart(interaction: discord.Interaction, juegos: Optional[str] = None):
        if interaction.guild and not is_guild_allowed(interaction.guild.id):
            await interaction.response.send_message(
                "Groksito no est├í disponible en este servidor.", ephemeral=True
            )
            return

        await interaction.response.defer(thinking=True, ephemeral=False)

        if not juegos or not juegos.strip():
            # Sensible default list (popular ones users commonly ask about)
            juegos = "path of exile 2, black desert, crimson desert, lost ark"

        terms = [t.strip() for t in juegos.split(",") if t.strip()][:8]

        # Resolve provided terms (supports custom games, unlike the fixed /stmchr list).
        # Now uses improved fuzzy + dynamic Steam search so "dota 2", "cs2", typos, etc. work.
        resolved_games: list[tuple[str, int]] = []
        for term in terms:
            resolved = await steam._resolve_steam_game(term)
            if resolved:
                resolved_games.append(resolved)

        if not resolved_games:
            await interaction.followup.send(
                "No pude reconocer ning├║n juego con ese nombre. "
                "Prueba con 'dota 2', 'cs2', 'counter strike 2', 'path of exile 2' o similar. "
                "O usa /stmchr para la lista fija de siempre."
            )
            return

        # Fetch player counts + best available thumbnail in parallel (same as /stmchr)
        meta: list[tuple[str, int]] = []
        count_tasks = []
        thumb_tasks = []
        for disp, aid in resolved_games:
            meta.append((disp, aid))
            count_tasks.append(steam._get_steam_player_counts(aid))
            thumb_tasks.append(steam._resolve_steam_thumb(aid))

        counts_results, thumb_results = await asyncio.gather(
            asyncio.gather(*count_tasks, return_exceptions=True),
            asyncio.gather(*thumb_tasks, return_exceptions=True),
        )

        games_data = []
        for (disp, aid), count_res, thumb_res in zip(meta, counts_results, thumb_results):
            if isinstance(count_res, Exception):
                continue
            current = count_res.get("current") if isinstance(count_res, dict) else None
            if current is not None:
                thumb = None
                if not isinstance(thumb_res, Exception):
                    thumb = thumb_res
                games_data.append(
                    {
                        "name": disp,
                        "current": current,
                        "app_id": aid,
                        "thumb": thumb,
                    }
                )

        if not games_data:
            await interaction.followup.send("No se pudo obtener datos de Steam Charts en este momento.")
            return

        # Sort by current players, highest first (exactly like /stmchr for visual consistency)
        games_data.sort(key=lambda g: g["current"], reverse=True)

        # Build one embed per game ΓÇö identical structure to /stmchr
        embeds = []
        for g in games_data:
            color = steam.get_game_color(g["name"])  # None for games without a curated theme color
            embed = discord.Embed(
                title=g["name"],
                description=f"**{g['current']:,}** jugadores ahora",
                color=color,
                url=f"https://store.steampowered.com/app/{g['app_id']}/",
            )
            thumb_url = g.get("thumb") or f"https://cdn.cloudflare.steamstatic.com/steam/apps/{g['app_id']}/header.jpg"
            embed.set_thumbnail(url=thumb_url)
            embeds.append(embed)

        await interaction.followup.send(embeds=embeds)

    # /stmchr ΓÇö always shows the fixed list of 8 games (no parameters needed).
    # One embed per game, sorted by current players (highest first).
    # Thumbnails use a robust resolver (multiple CDN patterns + store page fallback)
    # so games with only hashed asset paths (WWM, TBH, etc.) still get images.
    # Each embed gets a game-specific color and the title links to the Steam store page.
    @tree.command(
        name="stmchr", description="Black Desert, PoE2, GW2, Lost Ark, Crimson Desert y m├ís"
    )
    async def stmchr(interaction: discord.Interaction):
        if interaction.guild and not is_guild_allowed(interaction.guild.id):
            await interaction.response.send_message(
                "Groksito no est├í disponible en este servidor.", ephemeral=True
            )
            return

        await interaction.response.defer(thinking=True, ephemeral=False)

        # Fetch player counts + best available thumbnail in parallel
        # (delegated to the extracted steam integration module)
        meta: list[tuple[str, int]] = []
        count_tasks = []
        thumb_tasks = []
        for name, aid in steam._STMCHR_GAMES:
            meta.append((name, aid))
            count_tasks.append(steam._get_steam_player_counts(aid))
            thumb_tasks.append(steam._resolve_steam_thumb(aid))

        counts_results, thumb_results = await asyncio.gather(
            asyncio.gather(*count_tasks, return_exceptions=True),
            asyncio.gather(*thumb_tasks, return_exceptions=True),
        )

        games_data = []
        for (disp, aid), count_res, thumb_res in zip(meta, counts_results, thumb_results):
            if isinstance(count_res, Exception):
                continue
            current = count_res.get("current") if isinstance(count_res, dict) else None
            if current is not None:
                thumb = None
                if not isinstance(thumb_res, Exception):
                    thumb = thumb_res
                games_data.append(
                    {
                        "name": disp,
                        "current": current,
                        "app_id": aid,
                        "thumb": thumb,
                    }
                )

        # Sort by current players, highest first
        games_data.sort(key=lambda g: g["current"], reverse=True)

        # Build one embed per game
        embeds = []
        for g in games_data:
            color = steam.get_game_color(g["name"])
            embed = discord.Embed(
                title=g["name"],
                description=f"**{g['current']:,}** jugadores ahora",
                color=color,
                url=f"https://store.steampowered.com/app/{g['app_id']}/",
            )
            thumb_url = g.get("thumb") or f"https://cdn.cloudflare.steamstatic.com/steam/apps/{g['app_id']}/header.jpg"
            embed.set_thumbnail(url=thumb_url)
            embeds.append(embed)

        if embeds:
            await interaction.followup.send(embeds=embeds)
        else:
            await interaction.followup.send("No se pudo obtener datos de Steam Charts en este momento.")

    # /topgames ΓÇö shows the real-time Top 10 (or so) games by current players
    # directly from https://steamcharts.com/top (not a fixed list like /stmchr).
    # Renders exactly like /stmchr and the resolved /steamchart: one rich embed
    # per game with current players, thumbnail (robust resolver), store link,
    # and themed color when the game is one of the known curated titles.
    # Fetches fresh current counts via the official Steam API for consistency.
    @tree.command(
        name="topgames",
        description="Top 10 juegos con m├ís jugadores actuales en Steam (de steamcharts.com/top)"
    )
    async def topgames(interaction: discord.Interaction):
        if interaction.guild and not is_guild_allowed(interaction.guild.id):
            await interaction.response.send_message(
                "Groksito no est├í disponible en este servidor.", ephemeral=True
            )
            return

        await interaction.response.defer(thinking=True, ephemeral=False)

        # Get the live top list (name + app_id) from steamcharts.com/top
        top_list = await steam.get_top_steam_games(10)
        if not top_list:
            await interaction.followup.send("No se pudo obtener la lista de top juegos en este momento.")
            return

        # Fetch player counts + best available thumbnail in parallel (same pattern as /stmchr)
        meta: list[tuple[str, int]] = []
        count_tasks = []
        thumb_tasks = []
        for name, aid in top_list:
            meta.append((name, aid))
            count_tasks.append(steam._get_steam_player_counts(aid))
            thumb_tasks.append(steam._resolve_steam_thumb(aid))

        counts_results, thumb_results = await asyncio.gather(
            asyncio.gather(*count_tasks, return_exceptions=True),
            asyncio.gather(*thumb_tasks, return_exceptions=True),
        )

        games_data = []
        for (disp, aid), count_res, thumb_res in zip(meta, counts_results, thumb_results):
            if isinstance(count_res, Exception):
                continue
            current = count_res.get("current") if isinstance(count_res, dict) else None
            if current is not None:
                thumb = None
                if not isinstance(thumb_res, Exception):
                    thumb = thumb_res
                games_data.append(
                    {
                        "name": disp,
                        "current": current,
                        "app_id": aid,
                        "thumb": thumb,
                    }
                )

        if not games_data:
            await interaction.followup.send("No se pudo obtener datos de Steam Charts en este momento.")
            return

        # Sort by current players, highest first (defensive; the top list is usually already ordered)
        games_data.sort(key=lambda g: g["current"], reverse=True)

        # Build one embed per game ΓÇö identical structure to /stmchr
        embeds = []
        for g in games_data:
            color = steam.get_game_color(g["name"])  # None for games not in the curated color list
            embed = discord.Embed(
                title=g["name"],
                description=f"**{g['current']:,}** jugadores ahora",
                color=color,
                url=f"https://store.steampowered.com/app/{g['app_id']}/",
            )
            thumb_url = g.get("thumb") or f"https://cdn.cloudflare.steamstatic.com/steam/apps/{g['app_id']}/header.jpg"
            embed.set_thumbnail(url=thumb_url)
            embeds.append(embed)

        await interaction.followup.send(embeds=embeds)

    # /audio — dedicated TTS/voice slash command.
    # - text is optional (for reply-to use case).
    # - voice uses app_commands.choices (eve/ara/rex/sal/leo) with friendly names.
    # - When invoked as reply to another message (no or with text): reads/combines using helper.
    # - Uses guild whitelist + rate limiter (counts as a user request).
    # - Defers ephemerally for "generating" UX.
    # - Registers for direct delivery + calls core _tool_generate_audio (reuses EVERYTHING:
    #   text prep, xAI call, pydub transcode, real waveform, voice flag bubble, context log).
    # - Ephemeral confirmation after; the voice bubble itself is delivered publicly in channel.
    @tree.command(
        name="audio",
        description="Genera audio hablado (TTS) usando voces de Grok. Responde a un mensaje para leerlo.",
    )
    @discord.app_commands.choices(
        voice=[
            discord.app_commands.Choice(name="Eve (energética, recomendada)", value="eve"),
            discord.app_commands.Choice(name="Ara (cálida)", value="ara"),
            discord.app_commands.Choice(name="Rex (profesional)", value="rex"),
            discord.app_commands.Choice(name="Sal (equilibrada)", value="sal"),
            discord.app_commands.Choice(name="Leo (autoritativa)", value="leo"),
        ]
    )
    async def audio_slash(
        interaction: discord.Interaction,
        text: Optional[str] = None,
        voice: Optional[discord.app_commands.Choice[str]] = None,
    ):
        # Guild whitelist (same as every other slash command)
        if interaction.guild and not is_guild_allowed(interaction.guild.id):
            await interaction.response.send_message(
                "Groksito no está disponible en este servidor.", ephemeral=True
            )
            return

        # Rate limit (audio gen consumes resources like a conversational request)
        rl = getattr(client, "rate_limiter", rate_limiter)
        can_use, _ = rl.check(interaction.user.id)
        if not can_use:
            await interaction.response.send_message(
                "Tranquilo campeón, ya usaste tus 6 requests este minuto.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        # Build text (reply support if user replied to a msg then ran /audio)
        provided = (text or "").strip()
        final_text = await prepare_text_from_interaction(interaction, provided)

        if not final_text:
            await interaction.followup.send(
                "Por favor proporciona texto o responde a un mensaje para leerlo en voz alta.",
                ephemeral=True,
            )
            return

        # Resolve voice (user choice or config default) + language
        selected_voice = voice.value if voice else getattr(settings, "tts_default_voice", "eve") or "eve"
        selected_lang = getattr(settings, "tts_default_language", "es") or "es"

        # Register using the Interaction as original_message (delivery code now supports it for public channel send)
        request_id = None
        try:
            ch = getattr(interaction, "channel", None)
            request_id = await register_image_request(
                user_id=interaction.user.id,
                channel_id=getattr(ch, "id", 0) or 0,
                message_id=getattr(interaction, "id", 0),
                operation_type="audio",
                original_message=interaction,
            )
        except Exception as reg_err:
            logger.warning(f"[AudioSlash] Failed to register audio request: {reg_err}")

        # Call core tool directly (it will consume the request and do direct delivery of voice bubble if possible)
        result = await _tool_generate_audio(
            text=final_text,
            voice=selected_voice,
            language=selected_lang,
            request_id=request_id,
        )

        # Ephemeral confirmation to the user who ran the command (the audio itself is public via direct delivery)
        if result and "SUCCESS" in result:
            await interaction.followup.send(
                f"✅ Audio generado con la voz **{selected_voice}** y enviado al canal.",
                ephemeral=True,
            )
        else:
            # Surface the (Spanish) error or note from the handler
            await interaction.followup.send(
                result or "No se pudo generar el audio.",
                ephemeral=True,
            )

    # =============================================================================
    # Message Context Menu: "🔊 Leer en voz alta" (Read Aloud)
    # =============================================================================
    # Right-click any message > Apps > "🔊 Leer en voz alta".
    # Quick way to TTS the message content using the configured default voice/language.
    # Reuses the exact same audio pipeline as /audio (text prep, xAI TTS, waveform bubble,
    # direct delivery via image_delivery sentinel, rate limiting, guild whitelist).
    # This restores the dedicated "context menu read aloud" UX (no reply + slash needed).
    @tree.context_menu(name="🔊 Leer en voz alta")
    async def read_aloud_context(
        interaction: discord.Interaction,
        message: discord.Message,
    ):
        # Guild whitelist (identical to every other command)
        if interaction.guild and not is_guild_allowed(interaction.guild.id):
            await interaction.response.send_message(
                "Groksito no está disponible en este servidor.", ephemeral=True
            )
            return

        # Rate limit (audio is a resource-consuming action, same bucket as chat requests)
        rl = getattr(client, "rate_limiter", rate_limiter)
        can_use, _ = rl.check(interaction.user.id)
        if not can_use:
            await interaction.response.send_message(
                "Tranquilo campeón, ya usaste tus 6 requests este minuto.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        # Extract text directly from the right-clicked target message.
        # (For pure image/attachment messages without text we gracefully error;
        # the conversational LLM + vision path can describe images instead.)
        final_text = (getattr(message, "content", "") or "").strip()
        if not final_text:
            await interaction.followup.send(
                "El mensaje no contiene texto para leer en voz alta. "
                "Responde al mensaje con /audio o menciona a Groksito para analizar imágenes.",
                ephemeral=True,
            )
            return

        # Always use the configured defaults (context menus don't support parameter choices
        # in the initial click; user can follow up with /audio + voice if they want a different one).
        selected_voice = getattr(settings, "tts_default_voice", "eve") or "eve"
        selected_lang = getattr(settings, "tts_default_language", "es") or "es"

        # Register for direct delivery (same as /audio slash so the voice bubble is sent
        # publicly by the tool without a duplicate reply from the command handler).
        request_id = None
        try:
            ch = getattr(interaction, "channel", None)
            request_id = await register_image_request(
                user_id=interaction.user.id,
                channel_id=getattr(ch, "id", 0) or 0,
                message_id=getattr(message, "id", 0) or getattr(interaction, "id", 0),
                operation_type="audio",
                original_message=message,  # target message gives correct channel for delivery
            )
        except Exception as reg_err:
            logger.warning(f"[ReadAloudContext] Failed to register audio request: {reg_err}")

        # Core generation + delivery (identical reuse as the slash and the generate_audio tool)
        result = await _tool_generate_audio(
            text=final_text,
            voice=selected_voice,
            language=selected_lang,
            request_id=request_id,
        )

        # Ephemeral confirmation to the invoker (the actual audio is delivered publicly
        # to the channel by the shared audio handler / direct-delivery logic).
        if result and "SUCCESS" in str(result).upper():
            await interaction.followup.send(
                f"✅ Audio generado con la voz **{selected_voice}** y enviado al canal.",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                result or "No se pudo generar el audio.",
                ephemeral=True,
            )


# =============================================================================
# Steam integration moved to src/groksito_discord/integrations/steam.py (Phase 1)
# All Steam player count fetching, scraping, game resolution, and data structures
# were extracted here. The two slash commands (registered above) delegate to it.
# No behavior was changed.
# =============================================================================

# =============================================================================
# Main Connection Function (Conversational Only)
# =============================================================================
async def ensure_discord_connected(conversational: bool = True) -> "discord.Client":
    """
    Ensures the Discord client is connected.

    In the standalone Groksito bot, we always run with conversational=True.
    This function owns the persistent Gateway WebSocket.
    """
    global _discord_client, _discord_task, rate_limiter, tree

    if _discord_client is not None:
        await _discord_ready.wait()
        return _discord_client

    if not settings.discord_bot_token:
        raise RuntimeError("DISCORD_BOT_TOKEN is not configured in .env")

    intents = discord.Intents.default()
    intents.guilds = True
    intents.members = True
    intents.message_content = True  # Required for conversational bot

    _discord_client = discord.Client(intents=intents)

    logger.info("=== GROKSITO DISCORD BOT (STANDALONE) ===")
    logger.info("CONVERSATIONAL OWNER: This process owns the persistent Gateway connection.")
    logger.info("Full @Groksito experience enabled (native vision via Responses API, channel context, tools, image/video gen).")

    rate_limiter = RateLimiter(max_requests=6, window_seconds=60)
    tree = discord.app_commands.CommandTree(_discord_client)

    _discord_client.rate_limiter = rate_limiter
    _discord_client.command_tree = tree

    # Register slash commands (factored out in Phase 3 for structure).
    # This call must happen after the client and rate_limiter are attached.
    # All three commands (/mislimites, /steamchart, /stmchr) are now defined
    # in register_slash_commands above.
    register_slash_commands(tree, _discord_client)

    # Lazy import of conversational stack (keeps things clean)
    from .. import context
    # No custom memory system at all (removed for 100% Grok nativeness)
    from ..core.conversation import (
        _resolve_referenced_and_activation,
        _build_referenced_context,
        _harvest_vision_images,
        _invoke_groksito,
    )

    # on_ready
    @_discord_client.event
    async def on_ready():
        logger.info(f"Γ£à Groksito connected as {_discord_client.user} (ID: {_discord_client.user.id})")
        logger.info(f"[Discord] discord.py version: {discord.__version__} (target: >=2.7.0,<3.0 for modern voice + features)")

        if _ALLOWED_GUILD_IDS:
            logger.info(f"[SECURITY] Guild whitelist ACTIVE ΓÇö {len(_ALLOWED_GUILD_IDS)} allowed guild(s)")
        else:
            logger.warning("[SECURITY] No ALLOWED_GUILD_IDS set ΓÇö bot will respond in ANY server.")

        try:
            await _discord_client.change_presence(activity=discord.Game(name="con Grok"))
            await tree.sync()
            logger.info("Γ£à Slash commands synchronized")
        except Exception as e:
            logger.error(f"Error syncing slash commands: {e}")

        _discord_ready.set()

        # Emoji / custom emote discovery (metadata only on startup).
        # Vision descriptions + popularity ranking are done *lazily* only for emotes that actually get used
        # in messages the bot sees. This is the efficient path for servers with 100-200+ emotes.
        # Data lives in data/emoji_knowledge.json.
        try:
            from ..utils import emoji_registry
            asyncio.create_task(emoji_registry.scan_all_accessible_emojis(_discord_client))
            logger.info("[Emoji] Background emote metadata scan launched (vision + usage ranking is lazy on real use)")
        except Exception as emoji_err:
            logger.debug(f"[Emoji] Could not start emote scan (non-fatal): {emoji_err}")

        # Write initial heartbeat + supporting snapshots so the web dashboard has good data immediately.
        try:
            from ..core.health import (
                write_bot_heartbeat,
                write_bot_guilds_snapshot,
                write_bot_stats,
                write_bot_health_snapshot,
            )
            guilds_list = getattr(_discord_client, "guilds", []) or []
            guilds = len(guilds_list)
            lat = getattr(_discord_client, "latency", None)
            write_bot_heartbeat(
                connected=True,
                user=str(_discord_client.user),
                user_id=_discord_client.user.id if _discord_client.user else None,
                guilds=guilds,
                latency=lat if (lat is not None and lat > 0) else None,
            )
            write_bot_guilds_snapshot(guilds_list)
            write_bot_stats()
            write_bot_health_snapshot()
        except Exception:
            pass

    # Extra lifecycle events for more accurate web dashboard status
    @_discord_client.event
    async def on_disconnect():
        try:
            from ..core.health import write_bot_heartbeat
            write_bot_heartbeat(connected=False)
        except Exception:
            pass

    @_discord_client.event
    async def on_resumed():
        try:
            from ..core.health import (
                write_bot_heartbeat,
                write_bot_guilds_snapshot,
                write_bot_stats,
                write_bot_health_snapshot,
            )
            guilds_list = getattr(_discord_client, "guilds", []) or []
            guilds = len(guilds_list)
            lat = getattr(_discord_client, "latency", None)
            write_bot_heartbeat(
                connected=True,
                user=str(getattr(_discord_client, "user", None)),
                user_id=getattr(getattr(_discord_client, "user", None), "id", None),
                guilds=guilds,
                latency=lat if (lat is not None and lat > 0) else None,
            )
            write_bot_guilds_snapshot(guilds_list)
            write_bot_stats()
            write_bot_health_snapshot()
        except Exception:
            pass

    # on_message - thin orchestrator (most logic lives in conversation.py)
    #
    # Invariants maintained here:
    # - Bot's own messages are ignored immediately.
    # - Guild whitelist is enforced first (after correlation).
    # - Context is *always* updated for every incoming message (for optional
    #   recent context summaries and legacy tools).
    # - Rate limit is checked *before* any expensive work or LLM call.
    # - Activation decision is delegated to conversation._resolve_referenced_and_activation
    #   (the authoritative strict policy that prevents bot replies to random
    #   user-to-user conversations).
    # - The actual Grok call + tools + vision happens in _invoke_groksito.
    @_discord_client.event
    async def on_message(message: discord.Message):
        cid_p = ""  # default if we error very early
        try:
            if message.author.id == _discord_client.user.id:
                return

            author_display = getattr(message.author, "display_name", None) or getattr(message.author, "name", "Usuario")

            # Generate correlation ID for this message (for full-trace logging of the interaction).
            # Set early so activation/resolve/vision logs are associated with it.
            cid = generate_correlation_id()
            set_correlation_id(cid)
            cid_p = cid_prefix()  # e.g. "cid=abc12345 "

            # Guild whitelist guard
            if message.guild and not is_guild_allowed(message.guild.id):
                logger.info(f"{cid_p}[SECURITY] Ignoring message from unauthorized guild {message.guild.id}")
                return

            # Learn which custom emotes are actually used in this server (efficient local tracking).
            # This lets us surface only the popular ones + do vision descriptions lazily instead of
            # processing every single one of the 100-200 emotes some servers have.
            try:
                from . import emoji_registry
                emoji_registry.record_emojis_from_message(message)
            except Exception:
                pass

            # Always track context (for get_channel_context tool, get_recent_context tool, optional old summarization, legacy)
            # Also capture images and links so the on-demand recent context summarizer (used by tool)
            # can analyze images (vision) and do surface search on links.
            image_urls: list[str] = []
            links: list[str] = []
            try:
                # Direct attachments
                for att in getattr(message, "attachments", []) or []:
                    ct = getattr(att, "content_type", "") or ""
                    if "image" in ct.lower() and getattr(att, "url", None):
                        image_urls.append(att.url)
                # Embeds (thumbnails / images)
                for emb in getattr(message, "embeds", []) or []:
                    for key in ("image", "thumbnail"):
                        obj = getattr(emb, key, None)
                        if obj and getattr(obj, "url", None):
                            image_urls.append(obj.url)
                # Links / URLs from text content
                # Now uses the centralized implementation (Phase 2) to eliminate
                # duplication with conversation.py extractors. Behavior is identical.
                if message.content:
                    for clean in extract_urls_from_text(message.content):
                        if clean and clean not in links:
                            links.append(clean)
            except Exception:
                pass

            context.update_from_message(
                channel_id=message.channel.id,
                user_id=message.author.id,
                author_name=author_display,
                content=message.content or "",
                is_bot=False,
                image_urls=image_urls,
                links=links,
            )

            # Activation decision
            # The resolve function now contains the authoritative strict logic (refined across iterations)
            # and emits clear per-decision logs. We still keep a defensive guard here.
            result = await _resolve_referenced_and_activation(
                message=message,
                client_user=_discord_client.user,
                author_display=author_display,
            )
            # result is now 6-tuple: ... , has_x_link_intent, has_image_creation_intent
            if len(result) >= 6:
                referenced, is_reply_to_bot, explicit_visual, is_reply_cont, has_x_link_intent, has_image_creation = result
            else:
                referenced, is_reply_to_bot, explicit_visual, is_reply_cont, has_x_link_intent = result if len(result) == 5 else (*result, False)
                has_image_creation = False

            # is_reply_to_bot + is_mentioned are passed down. Referenced context is injected for
            # direct replies to Groksito OR when the bot is @mentioned inside a reply to another user
            # (e.g. " @groksito describe the video in that link my friend just posted").

            is_mentioned = _discord_client.user in getattr(message, "mentions", [])

            # === STRICT ACTIVATION GUARD ===
            # Only @mention or direct reply to a Groksito message. User-to-user replies
            # (even with images, videos, or "groksito" in text) must never wake the bot.
            if not is_mentioned and not is_reply_to_bot:
                return

            # Rate limit
            rl = getattr(_discord_client, "rate_limiter", rate_limiter)
            can_use, _ = rl.check(message.author.id)
            if not can_use:
                await _safe_reply(message, "Tranquilo campe├│n, ya usaste tus 6 requests este minuto.", mention_author=False)
                return

            # Rich context + meta detection
            # Note: referenced may have been fetched in resolve; fetch again only if missing
            if message.reference and message.reference.message_id and referenced is None:
                try:
                    referenced = await message.channel.fetch_message(message.reference.message_id)
                    logger.info(f"{cid_p}[Reply] Fetched referenced message in client fallback")
                except Exception:
                    pass

            referenced_context = await _build_referenced_context(referenced) if referenced else None

            is_meta = False
            try:
                is_meta = context.is_conversation_meta_question(message.content or "")
            except Exception:
                pass

            # NOTE: No custom memory / rich channel context computation here.
            # Only referenced message is passed; classification (is_meta) still used for logging/heuristics.
            # All (minimal) injection decided inside llm_input.build_responses_input ([R:] on bot replies + mention-in-reply cases).
            # Recent conversation context: on-demand via get_recent_context tool only (no pre-injection, #19).
            # No custom memory at all (removed for maximum nativeness).

            # Invoke Groksito (native context via llm_input, vision, tools)
            # cid is already set in contextvar for all downstream logging.
            async with message.channel.typing():
                await _invoke_groksito(
                    message=message,
                    referenced=referenced,
                    referenced_context=referenced_context,
                    author_display=author_display,
                    is_meta_convo=is_meta,
                    explicit_visual_reply_intent=explicit_visual,
                    is_reply_continuation=is_reply_cont,
                    has_x_link_intent=has_x_link_intent,  # X/link intent signal (affects native x_search offering + ref enrichment)
                    is_reply_to_bot=is_reply_to_bot,
                    has_image_creation_intent=has_image_creation,
                    is_mentioned=is_mentioned,
                )

        except Exception as e:
            logger.exception(f"{cid_p}Unhandled error in on_message: {e}")

    # Start the bot
    async def _runner():
        try:
            await _discord_client.start(settings.discord_bot_token)
        except Exception as exc:
            logger.error(f"Discord connection failed: {exc}", exc_info=True)
            _discord_ready.clear()

    _discord_task = asyncio.create_task(_runner())
    logger.info("Starting Groksito Discord connection (CONVERSATIONAL OWNER)...")

    try:
        await asyncio.wait_for(_discord_ready.wait(), timeout=30.0)
    except asyncio.TimeoutError:
        raise RuntimeError("Timeout waiting for Discord connection. Check token and network.")

    # -------------------------------------------------------------------------
    # Background heartbeat task (lets the separate web dashboard know we're alive)
    # Writes every ~35s so the web can show a green "Connected" indicator + basic stats.
    # -------------------------------------------------------------------------
    async def _heartbeat_updater() -> None:
        while True:
            try:
                await asyncio.sleep(35)
                if _discord_client and getattr(_discord_client, "is_ready", lambda: False)():
                    try:
                        from ..core.health import (
                            write_bot_heartbeat,
                            write_bot_guilds_snapshot,
                            write_bot_stats,
                            write_bot_health_snapshot,
                        )
                        guilds_list = getattr(_discord_client, "guilds", []) or []
                        guilds = len(guilds_list)
                        lat = getattr(_discord_client, "latency", None)
                        write_bot_heartbeat(
                            connected=True,
                            user=str(getattr(_discord_client, "user", None)),
                            user_id=getattr(getattr(_discord_client, "user", None), "id", None),
                            guilds=guilds,
                            latency=lat if (lat is not None and lat > 0) else None,
                        )
                        write_bot_guilds_snapshot(guilds_list)
                        write_bot_stats()
                        write_bot_health_snapshot()
                    except Exception:
                        pass
            except asyncio.CancelledError:
                break
            except Exception:
                # Never let the heartbeat task kill the bot
                await asyncio.sleep(10)

    asyncio.create_task(_heartbeat_updater())

    return _discord_client


__all__ = ["ensure_discord_connected", "is_guild_allowed", "rate_limiter"]
