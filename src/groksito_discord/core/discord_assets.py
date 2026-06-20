"""
Lightweight Discord asset resolvers for on-demand LLM tools.

Fetches public CDN URLs for user avatars and top-used server custom emojis.
Resolved URLs can be appended to the turn's vision reference list so
edit_image / generate_video can use them as I2V or edit references.
"""

from __future__ import annotations

import logging
from typing import Any

from ..utils import emoji_registry
from ..utils.correlation import cid_prefix

logger = logging.getLogger("groksito.discord_assets")

_MAX_REFERENCE_URLS = 5


def build_avatar_url(user: Any, *, size: int = 512) -> str:
    """Return a sized Discord CDN URL for a user's display avatar."""
    avatar = getattr(user, "display_avatar", None)
    if avatar is None:
        return ""
    try:
        return str(avatar.replace(size=size).url)
    except Exception:
        return str(getattr(avatar, "url", "") or "")


def pick_user_from_message(
    message: Any,
    *,
    user_id: str | int | None = None,
    username: str | None = None,
) -> Any | None:
    """Best-effort sync resolution from mentions, guild lookup, or message author."""
    if message is None:
        return None

    mentions = list(getattr(message, "mentions", []) or [])
    guild = getattr(message, "guild", None)
    author = getattr(message, "author", None)

    if user_id is not None and str(user_id).strip():
        try:
            uid = int(user_id)
        except (TypeError, ValueError):
            return None
        for member in mentions:
            if getattr(member, "id", None) == uid:
                return member
        if author and getattr(author, "id", None) == uid:
            return author
        if guild is not None:
            member = guild.get_member(uid)
            if member is not None:
                return member
        return None

    if username and str(username).strip():
        uname = str(username).lstrip("@").strip()
        lowered = uname.lower()
        for member in mentions:
            names = {
                str(getattr(member, "name", "")).lower(),
                str(getattr(member, "display_name", "")).lower(),
            }
            if lowered in names:
                return member
        if guild is not None:
            try:
                member = guild.get_member_named(uname)
                if member is not None:
                    return member
            except Exception:
                pass
        return None

    if len(mentions) == 1:
        return mentions[0]
    if mentions:
        return mentions[0]

    return author


def append_reference_url(image_urls: list[str] | None, url: str) -> None:
    """Append a resolved asset URL to the mutable turn reference list (deduped, capped)."""
    if image_urls is None or not url or url in image_urls:
        return
    if len(image_urls) >= _MAX_REFERENCE_URLS:
        logger.debug(f"{cid_prefix()}[DiscordAssets] reference list full ({_MAX_REFERENCE_URLS}); skip append")
        return
    image_urls.append(url)


def get_top_server_emoji_record(
    guild_id: int | str | None,
    *,
    guild_obj: Any = None,
    rank: int = 1,
) -> dict[str, Any]:
    """Return metadata for the Nth most-used custom emoji in this guild (1 = top)."""
    if not guild_id:
        return {"ok": False, "error": "No guild context for server emoji lookup."}

    emojis = dict(emoji_registry.get_emojis_for_guild(guild_id))

    if guild_obj is not None:
        for em in getattr(guild_obj, "emojis", []) or []:
            eid = str(getattr(em, "id", ""))
            if not eid or eid in emojis:
                continue
            emojis[eid] = {
                "id": eid,
                "name": getattr(em, "name", ""),
                "url": str(getattr(em, "url", "") or ""),
                "usage_count": 0,
            }

    if not emojis:
        return {"ok": False, "error": "This server has no known custom emojis yet."}

    sorted_recs = sorted(
        emojis.values(),
        key=lambda r: (-int(r.get("usage_count", 0)), str(r.get("name", ""))),
    )

    idx = max(1, int(rank or 1)) - 1
    if idx >= len(sorted_recs):
        return {
            "ok": False,
            "error": f"Only {len(sorted_recs)} custom emoji(s) tracked; rank {rank} is out of range.",
        }

    rec = sorted_recs[idx]
    url = str(rec.get("url") or "").strip()
    name = str(rec.get("name") or "unknown")
    if not url:
        return {"ok": False, "error": f"Emoji :{name}: has no CDN URL."}

    return {
        "ok": True,
        "name": name,
        "url": url,
        "usage_count": int(rec.get("usage_count", 0)),
        "rank": idx + 1,
        "animated": bool(rec.get("animated")),
    }


async def _fetch_user_by_id(message: Any, user_id: int) -> Any | None:
    picked = pick_user_from_message(message, user_id=user_id)
    if picked is not None:
        return picked

    client = getattr(getattr(message, "_state", None), "client", None)
    if client is None:
        return None
    try:
        return await client.fetch_user(user_id)
    except Exception as fetch_err:
        logger.debug(f"{cid_prefix()}[DiscordAssets] fetch_user({user_id}) failed: {fetch_err}")
        return None


async def resolve_user_avatar(
    message: Any,
    *,
    user_id: str | int | None = None,
    username: str | None = None,
    image_urls: list[str] | None = None,
    size: int = 512,
) -> dict[str, Any]:
    """Resolve a Discord user's profile avatar CDN URL for media reference use."""
    if message is None:
        return {"ok": False, "error": "No message context for avatar lookup."}

    user = pick_user_from_message(message, user_id=user_id, username=username)

    if user is None and user_id is not None and str(user_id).strip():
        try:
            user = await _fetch_user_by_id(message, int(user_id))
        except (TypeError, ValueError):
            pass

    if user is None:
        hint = (
            "Could not resolve user. Pass user_id or username, @mention them in the message, "
            "or ask about your own avatar (defaults to message author)."
        )
        return {"ok": False, "error": hint}

    avatar_url = build_avatar_url(user, size=size)
    if not avatar_url:
        return {"ok": False, "error": "User found but avatar URL could not be built."}

    append_reference_url(image_urls, avatar_url)

    display = (
        getattr(user, "display_name", None)
        or getattr(user, "global_name", None)
        or getattr(user, "name", None)
        or "user"
    )

    logger.info(
        f"{cid_prefix()}[DiscordAssets] Resolved avatar for {display} "
        f"(id={getattr(user, 'id', '?')})"
    )

    return {
        "ok": True,
        "avatar_url": avatar_url,
        "display_name": str(display),
        "user_id": int(getattr(user, "id", 0) or 0),
        "username": str(getattr(user, "name", "") or ""),
    }


async def resolve_top_server_emoji(
    message: Any,
    *,
    rank: int = 1,
    image_urls: list[str] | None = None,
) -> dict[str, Any]:
    """Resolve this guild's most-used custom emoji CDN URL."""
    if message is None:
        return {"ok": False, "error": "No message context for emoji lookup."}

    guild = getattr(message, "guild", None)
    guild_id = getattr(guild, "id", None)
    result = get_top_server_emoji_record(guild_id, guild_obj=guild, rank=rank)

    if not result.get("ok"):
        return result

    append_reference_url(image_urls, result["url"])
    logger.info(
        f"{cid_prefix()}[DiscordAssets] Resolved top emoji :{result['name']}: "
        f"(rank={result['rank']}, usage={result['usage_count']})"
    )
    return result