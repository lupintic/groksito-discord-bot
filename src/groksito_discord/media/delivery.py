"""
Centralized Discord media delivery for Groksito.

Downloads generated/edited media from transient xAI URLs and delivers them as
real Discord file attachments instead of expiring links in message text.

Used by image_handler, video_handler, and (optionally) audio_handler for a
unified direct-delivery UX with the DIRECT_DELIVERY_PERFORMED sentinel pattern.
"""

from __future__ import annotations

import asyncio
import logging
import random
from io import BytesIO
from typing import Any
from urllib.parse import urlparse

import discord
import httpx

from ..correlation import cid_prefix
from ..config import settings

logger = logging.getLogger("groksito.media.delivery")


def build_image_caption(prompt: str | None = None) -> str:
    """Short, natural caption for image delivery (no URLs)."""
    short_desc = (prompt or "").strip()
    for prefix in ("un ", "una ", "el ", "la ", "los ", "las "):
        if short_desc.lower().startswith(prefix):
            short_desc = short_desc[len(prefix) :].strip()
            break
    if len(short_desc) > 70:
        short_desc = short_desc[:67].rstrip() + "…"

    options = [
        f"Acá tenés {short_desc}." if short_desc else "Acá tenés la imagen.",
        "Ahí va.",
        f"Generé {short_desc}." if short_desc else "Listo.",
        "Listo.",
        "Acá tenés la imagen que pediste.",
        f"{short_desc}." if short_desc else "Acá está.",
    ]
    return random.choice(options)


def build_edit_caption() -> str:
    return "Acá tenés la versión editada."


def build_video_caption(*, from_image: bool, duration: int, daily_used: int, daily_remaining: int) -> str:
    mode_label = "a partir de la imagen de referencia" if from_image else "generado"
    return (
        f"Acá tenés el video {mode_label}. (480p, {duration}s)\n\n"
        f"Video {daily_used}/5 del día — te quedan {daily_remaining}."
    )


def _guess_filename(url: str, kind: str, index: int = 0) -> str:
    path = urlparse(url).path
    ext = ""
    if "." in path:
        ext = path.rsplit(".", 1)[-1].lower()
        if ext not in ("png", "jpg", "jpeg", "webp", "gif", "mp4", "webm", "mov"):
            ext = ""
    if not ext:
        ext = {"image": "png", "video": "mp4"}.get(kind, "bin")
    suffix = f"_{index + 1}" if index else ""
    return f"groksito_{kind}{suffix}.{ext}"


async def _download_url(url: str) -> bytes | None:
    try:
        async with httpx.AsyncClient(timeout=settings.api_timeout_seconds) as client:
            resp = await client.get(url)
            if resp.status_code == 200 and resp.content:
                return resp.content
    except Exception as err:
        logger.warning(f"{cid_prefix()}[MediaDelivery] Download failed for {url[:80]}: {err}")
    return None


async def _download_urls(urls: list[str]) -> list[tuple[bytes, str]]:
    """Download URLs in parallel; return (bytes, filename) pairs for successful downloads."""
    if not urls:
        return []

    async def _one(url: str, idx: int) -> tuple[bytes, str] | None:
        data = await _download_url(url)
        if not data:
            return None
        kind = "video" if any(h in url.lower() for h in (".mp4", ".webm", "/video")) else "image"
        return data, _guess_filename(url, kind, idx)

    tasks = [_one(url, i) for i, url in enumerate(urls[:4])]
    downloaded = await asyncio.gather(*tasks)
    return [item for item in downloaded if item is not None]


async def deliver_media_to_message(
    orig_msg: Any,
    *,
    caption: str,
    urls: list[str] | None = None,
    files: list[discord.File] | None = None,
    kind: str = "image",
) -> bool:
    """
    Deliver media as Discord attachments on a reply to orig_msg.

    Prefers pre-built discord.File objects; otherwise downloads from urls.
    Returns True on successful delivery.
    """
    if not orig_msg:
        return False

    attachments: list[discord.File] = list(files or [])

    if not attachments and urls:
        downloaded = await _download_urls(urls)
        for data, filename in downloaded:
            attachments.append(discord.File(BytesIO(data), filename=filename))

    if not attachments:
        logger.warning(f"{cid_prefix()}[MediaDelivery] No attachments to deliver ({kind})")
        return False

    try:
        await orig_msg.reply(caption, files=attachments[:10], mention_author=False)
    except Exception as send_err:
        logger.error(f"{cid_prefix()}[MediaDelivery] Discord reply failed ({kind}): {send_err}")
        return False

    try:
        ch = getattr(orig_msg, "channel", None)
        ch_id = getattr(ch, "id", None) if ch else None
        if ch_id:
            from .. import context as ctx

            ctx.update_from_message(
                channel_id=ch_id,
                user_id=0,
                author_name="Groksito",
                content=caption,
                is_bot=True,
            )
    except Exception:
        pass

    logger.info(
        f"{cid_prefix()}[MediaDelivery] Delivered {len(attachments)} {kind} attachment(s) "
        f"to msg {getattr(orig_msg, 'id', '?')}"
    )
    return True


async def deliver_from_request(
    request_id: str | None,
    *,
    caption: str,
    urls: list[str] | None = None,
    files: list[discord.File] | None = None,
    kind: str = "image",
) -> bool:
    """Consume a pending request and deliver media to its original message."""
    if not request_id:
        return False

    from ..image_delivery import consume_image_request

    info = await consume_image_request(request_id)
    if not info:
        return False

    orig_msg = info.get("original_message")
    return await deliver_media_to_message(
        orig_msg,
        caption=caption,
        urls=urls,
        files=files,
        kind=kind,
    )