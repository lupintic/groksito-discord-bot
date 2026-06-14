"""
Tests for media/delivery.py — Discord attachment delivery (not expiring URLs).
"""

from io import BytesIO
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from groksito_discord.media.delivery import (
    build_edit_caption,
    build_image_caption,
    build_video_caption,
    deliver_from_request,
    deliver_media_to_message,
)


@pytest.mark.asyncio
async def test_deliver_media_uses_discord_file_not_urls():
    orig_msg = MagicMock()
    orig_msg.reply = AsyncMock()
    orig_msg.id = 123
    orig_msg.channel = MagicMock(id=456)

    data = b"fake-png-bytes"
    files = [discord.File(BytesIO(data), filename="groksito_image.png")]

    ok = await deliver_media_to_message(
        orig_msg, caption="Acá tenés.", files=files, kind="image"
    )

    assert ok is True
    orig_msg.reply.assert_awaited_once()
    _args, kwargs = orig_msg.reply.await_args
    assert "http" not in (_args[0] if _args else "")
    assert kwargs.get("files") is not None
    assert len(kwargs["files"]) == 1
    assert kwargs["files"][0].filename == "groksito_image.png"


@pytest.mark.asyncio
async def test_deliver_from_request_downloads_urls():
    orig_msg = MagicMock()
    orig_msg.reply = AsyncMock()
    orig_msg.id = 1
    orig_msg.channel = MagicMock(id=2)

    with patch(
        "groksito_discord.image_delivery.consume_image_request",
        new_callable=AsyncMock,
        return_value={"original_message": orig_msg},
    ), patch(
        "groksito_discord.media.delivery._download_url",
        new_callable=AsyncMock,
        return_value=b"video-bytes",
    ), patch("groksito_discord.media.delivery.ctx", create=True):
        ok = await deliver_from_request(
            "req_1",
            caption="Video listo.",
            urls=["https://cdn.x.ai/video.mp4"],
            kind="video",
        )

    assert ok is True
    call_args = orig_msg.reply.await_args
    assert call_args.kwargs["files"]
    assert "cdn.x.ai" not in str(call_args.args)


def test_caption_builders_contain_no_urls():
    assert "http" not in build_image_caption("un gato con botas")
    assert "http" not in build_edit_caption()
    cap = build_video_caption(from_image=False, duration=5, daily_used=1, daily_remaining=4)
    assert "http" not in cap
    assert "1/5" in cap