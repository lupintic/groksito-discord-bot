"""
Media delivery for Groksito.

Combines:
1. In-memory request tracking (register/consume) for concurrent media operations
2. Discord attachment delivery — downloads transient xAI URLs and sends discord.File
   attachments instead of expiring links in message text

Used by image_handler, video_handler, audio_handler, and the DIRECT_DELIVERY_PERFORMED
sentinel pattern in llm/client.py and core/conversation.py.

Pending-request TTL: 90s for images/audio; 360s for video (xAI polling can take up to ~300s).
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
import uuid
from io import BytesIO
from typing import Any, Optional, TypedDict
from urllib.parse import urlparse

import discord
import httpx

from ..config import settings
from ..utils.correlation import cid_prefix

logger = logging.getLogger("groksito.media.delivery")


class PendingImageRequest(TypedDict):
    """Structural documentation for entries in the in-memory pending media request map."""
    user_id: int
    channel_id: int
    message_id: int
    operation_type: str
    timestamp: float
    original_message: Any


# =============================================================================
# Direct Delivery Sentinel
# =============================================================================

DIRECT_DELIVERY_PERFORMED = object()


# =============================================================================
# Request tracking (register / consume)
# =============================================================================

_pending_image_requests: dict[str, PendingImageRequest] = {}
_image_request_lock = asyncio.Lock()
_IMAGE_REQUEST_TTL = 90  # seconds (images / audio)
_VIDEO_REQUEST_TTL = 360  # seconds — video polling can run up to ~300s


def _request_ttl_for_operation(operation_type: str) -> int:
    if operation_type == "video":
        return _VIDEO_REQUEST_TTL
    return _IMAGE_REQUEST_TTL


async def register_image_request(
    *,
    user_id: int,
    channel_id: int,
    message_id: int,
    operation_type: str,
    original_message: Any,
) -> str:
    """Register a pending media operation and return a unique request_id."""
    async with _image_request_lock:
        await _cleanup_expired_image_requests()
        request_id = f"media_{uuid.uuid4().hex[:10]}"
        _pending_image_requests[request_id] = {
            "user_id": user_id,
            "channel_id": channel_id,
            "message_id": message_id,
            "operation_type": operation_type,
            "timestamp": time.time(),
            "original_message": original_message,
        }
        logger.info(
            f"{cid_prefix()}[MediaDelivery] Registered {operation_type} request "
            f"{request_id} for user {user_id} msg {message_id}"
        )
        return request_id


async def consume_image_request(request_id: str) -> Optional[dict[str, Any]]:
    """Retrieve and remove a pending request. Returns None if missing or expired."""
    async with _image_request_lock:
        await _cleanup_expired_image_requests()
        info = _pending_image_requests.pop(request_id, None)
        if info:
            logger.debug(f"{cid_prefix()}[MediaDelivery] Consumed request {request_id}")
        return info


async def _cleanup_expired_image_requests() -> None:
    now = time.time()
    expired = [
        rid for rid, info in _pending_image_requests.items()
        if now - info.get("timestamp", 0) > _request_ttl_for_operation(info.get("operation_type", ""))
    ]
    for rid in expired:
        _pending_image_requests.pop(rid, None)
        logger.debug(f"{cid_prefix()}[MediaDelivery] Cleaned expired request {rid}")


# =============================================================================
# Discord attachment delivery
# =============================================================================

def build_image_caption(prompt: str | None = None, *, user_original: str | None = None) -> str:
    """Short, natural caption for image delivery (no URLs).

    Prefers user_original (the actual text the human typed) for the visible caption
    so language/tone stays consistent with the user even if the model passed a
    slightly adapted prompt to the generation API. This helps cross-server
    consistency (no more English-primed tool prompts leaking into captions).
    Falls back to the tool prompt if no user_original.
    """
    source = (user_original or prompt or "").strip()
    short_desc = source
    for prefix in ("un ", "una ", "el ", "la ", "los ", "las "):
        if short_desc.lower().startswith(prefix):
            short_desc = short_desc[len(prefix) :].strip()
            break
    if len(short_desc) > 70:
        short_desc = short_desc[:67].rstrip() + "…"

    options = [
        f"Aquí tienes {short_desc}." if short_desc else "Aquí tienes la imagen.",
        f"Generé {short_desc}." if short_desc else "Imagen generada.",
        "Listo.",
        "Aquí tienes la imagen que pediste.",
        f"{short_desc}." if short_desc else "Aquí está.",
    ]
    return random.choice(options)


def build_edit_caption() -> str:
    return "Aquí tienes la versión editada."


def build_video_caption(*, from_image: bool, duration: int) -> str:
    mode_label = "a partir de la imagen de referencia" if from_image else "generado"
    return f"Aquí tienes el video {mode_label}. (480p, {duration}s)"


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
    """Download a transient xAI media URL with simple retry + backoff.

    xAI CDN URLs are short-lived; a single flaky fetch would otherwise force a
    URL-in-text fallback instead of a Discord attachment (PR #49 review).
    """
    # At least 2 attempts; cap to avoid long stalls on permanently dead URLs.
    max_attempts = max(2, min(getattr(settings, "api_max_retries", 3), 4))
    last_err: Exception | None = None

    for attempt in range(max_attempts):
        try:
            async with httpx.AsyncClient(timeout=settings.api_timeout_seconds) as client:
                resp = await client.get(url)
                if resp.status_code == 200 and resp.content:
                    return resp.content
                if resp.status_code in (429, 500, 502, 503, 504) and attempt < max_attempts - 1:
                    delay = 0.4 * (2 ** attempt)
                    logger.info(
                        f"{cid_prefix()}[MediaDelivery] HTTP {resp.status_code} on download "
                        f"(attempt {attempt + 1}/{max_attempts}), retry in {delay:.1f}s"
                    )
                    await asyncio.sleep(delay)
                    continue
                logger.warning(
                    f"{cid_prefix()}[MediaDelivery] Download HTTP {resp.status_code} "
                    f"for {url[:80]} (attempt {attempt + 1}/{max_attempts})"
                )
        except (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError) as err:
            last_err = err
            if attempt < max_attempts - 1:
                delay = 0.4 * (2 ** attempt)
                logger.info(
                    f"{cid_prefix()}[MediaDelivery] Transient download error "
                    f"{type(err).__name__} (attempt {attempt + 1}/{max_attempts}), retry in {delay:.1f}s"
                )
                await asyncio.sleep(delay)
                continue
        except Exception as err:
            last_err = err
            logger.warning(f"{cid_prefix()}[MediaDelivery] Download failed for {url[:80]}: {err}")
            break

    if last_err is not None:
        logger.warning(
            f"{cid_prefix()}[MediaDelivery] Download exhausted retries for {url[:80]}: {last_err}"
        )
    return None


async def _download_urls(urls: list[str]) -> list[tuple[bytes, str]]:
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
    """Deliver media as Discord attachments on a reply to orig_msg."""
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
        # This will cause sentinel not to be returned; model will likely produce a text reply (may be in English on some servers).
        try:
            gid = getattr(getattr(orig_msg, 'guild', None), 'id', None)
            logger.info(f"{cid_prefix()}[MediaDelivery] Delivery fallback will let model speak (guild={gid}, kind={kind})")
        except Exception:
            pass
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

    info = await consume_image_request(request_id)
    if not info:
        return False

    orig_msg = info.get("original_message")
    delivered = await deliver_media_to_message(
        orig_msg,
        caption=caption,
        urls=urls,
        files=files,
        kind=kind,
    )
    if not delivered:
        try:
            ch_id = getattr(getattr(orig_msg, "channel", None), "id", None)
            gid = getattr(getattr(orig_msg, "guild", None), "id", None)
            logger.warning(f"{cid_prefix()}[MediaDelivery] deliver_from_request failed to deliver (guild={gid} ch={ch_id}) — model will likely emit text reply")
        except Exception:
            pass
    return delivered