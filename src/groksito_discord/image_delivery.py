"""
Lightweight in-memory request tracking system for image and video generation.

Purpose:
- Prevent response mixing when multiple users request image gen/edit/video concurrently.
- Tie each media operation to its originating Discord message so results are
  delivered to the correct user.

Design:
- Simple dict + asyncio.Lock (no external dependencies).
- Automatic expiration of stale requests (>90 seconds).
- Used by media_tools (generate_image, edit_image, generate_video).

Used by media_tools (generate_image, edit_image, generate_video, generate_audio) via the
DIRECT_DELIVERY_PERFORMED sentinel pattern.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any, Optional, TypedDict

from .correlation import cid_prefix


class PendingImageRequest(TypedDict):
    """Structural documentation for entries in the in-memory pending media request map.

    Used only for type clarity / IDE support. No runtime behavior change.
    """
    user_id: int
    channel_id: int
    message_id: int
    operation_type: str
    timestamp: float
    original_message: Any

logger = logging.getLogger("groksito.image_delivery")


# =============================================================================
# Direct Delivery Sentinel
# =============================================================================

# Special sentinel object returned by call_grok_for_groksito (llm layer) when
# a media tool (generate_image / edit_image / generate_video / generate_audio) or reply_to_user
# has already performed a direct Discord reply with the result.
# conversation.py checks identity against this to completely suppress any
# secondary text reply and prevent duplicate messages to the user.
DIRECT_DELIVERY_PERFORMED = object()


# =============================================================================
# Internal State
# =============================================================================

_pending_image_requests: dict[str, PendingImageRequest] = {}
_image_request_lock = asyncio.Lock()
_IMAGE_REQUEST_TTL = 90  # seconds


# =============================================================================
# Public API
# =============================================================================

async def register_image_request(
    *,
    user_id: int,
    channel_id: int,
    message_id: int,
    operation_type: str,  # "generate", "edit", or "video"
    original_message: Any,
) -> str:
    """
    Register a pending media operation and return a unique request_id.

    The request_id is later passed to the media tool (image/edit/video/audio).
    When the tool completes, it calls consume_image_request() to retrieve the original
    message and deliver the result directly.
    """
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
        logger.info(f"{cid_prefix()}[ImageDelivery] Registered {operation_type} request {request_id} for user {user_id} msg {message_id}")
        return request_id


async def consume_image_request(request_id: str) -> Optional[dict[str, Any]]:
    """
    Retrieve and remove a pending request.

    Returns None if the request does not exist or has expired.
    """
    async with _image_request_lock:
        await _cleanup_expired_image_requests()
        info = _pending_image_requests.pop(request_id, None)
        if info:
            logger.debug(f"{cid_prefix()}[ImageDelivery] Consumed request {request_id}")
        return info


# =============================================================================
# Internal Helpers
# =============================================================================

async def _cleanup_expired_image_requests() -> None:
    """Remove expired entries (must be called under lock)."""
    now = time.time()
    expired = [
        rid for rid, info in _pending_image_requests.items()
        if now - info.get("timestamp", 0) > _IMAGE_REQUEST_TTL
    ]
    for rid in expired:
        _pending_image_requests.pop(rid, None)
        logger.debug(f"{cid_prefix()}[ImageDelivery] Cleaned expired request {rid}")
