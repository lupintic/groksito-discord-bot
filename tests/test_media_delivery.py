"""
Tests for media/delivery.py — Discord attachment delivery (not expiring URLs).
"""

from io import BytesIO
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

import httpx

from groksito_discord.media.delivery import (
    _discord_max_upload_bytes,
    _download_url,
    _is_discord_payload_too_large,
    _request_ttl_for_operation,
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
        orig_msg, caption="Aquí tienes la imagen.", files=files, kind="image"
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
        "groksito_discord.media.delivery.consume_image_request",
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


@pytest.mark.asyncio
async def test_download_url_retries_on_transient_failure():
    call_count = 0

    class FakeClient:
        async def get(self, _url):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise httpx.ConnectError("connection reset")
            resp = MagicMock()
            resp.status_code = 200
            resp.content = b"ok-bytes"
            return resp

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    with patch("groksito_discord.media.delivery.httpx.AsyncClient", return_value=FakeClient()), patch(
        "groksito_discord.media.delivery.asyncio.sleep", new_callable=AsyncMock
    ):
        data = await _download_url("https://cdn.x.ai/image.png")

    assert data == b"ok-bytes"
    assert call_count == 2


def test_discord_payload_too_large_detection():
    err = discord.HTTPException(MagicMock(), "413 Payload Too Large (error code: 40005)")
    err.status = 413
    err.code = 40005
    assert _is_discord_payload_too_large(err) is True
    assert _is_discord_payload_too_large(RuntimeError("413 Payload Too Large")) is True
    assert _is_discord_payload_too_large(RuntimeError("connection reset")) is False


@pytest.mark.asyncio
async def test_deliver_video_falls_back_to_url_when_file_too_large():
    """720p/15s videos can exceed Discord's 25MB bot upload cap."""
    orig_msg = MagicMock()
    orig_msg.reply = AsyncMock()
    orig_msg.id = 1
    orig_msg.channel = MagicMock(id=2)

    video_url = "https://cdn.x.ai/big-video.mp4"
    huge = b"x" * (_discord_max_upload_bytes() + 1)

    with patch(
        "groksito_discord.media.delivery._download_url",
        new_callable=AsyncMock,
        return_value=huge,
    ):
        ok = await deliver_media_to_message(
            orig_msg,
            caption="",
            urls=[video_url],
            kind="video",
        )

    assert ok is True
    orig_msg.reply.assert_awaited_once()
    args, kwargs = orig_msg.reply.await_args
    content = args[0] if args else kwargs.get("content")
    assert video_url in (content or "")
    assert not kwargs.get("files")


@pytest.mark.asyncio
async def test_deliver_video_falls_back_to_url_on_413():
    orig_msg = MagicMock()
    video_url = "https://cdn.x.ai/video.mp4"
    too_large = discord.HTTPException(MagicMock(), "413 Payload Too Large (error code: 40005)")
    too_large.status = 413
    too_large.code = 40005

    orig_msg.reply = AsyncMock(side_effect=[too_large, None])
    orig_msg.id = 1
    orig_msg.channel = MagicMock(id=2)

    with patch(
        "groksito_discord.media.delivery._download_url",
        new_callable=AsyncMock,
        return_value=b"small-video-bytes",
    ):
        ok = await deliver_media_to_message(
            orig_msg,
            caption="",
            urls=[video_url],
            kind="video",
        )

    assert ok is True
    assert orig_msg.reply.await_count == 2
    fallback_args, fallback_kwargs = orig_msg.reply.await_args_list[1]
    content = fallback_args[0] if fallback_args else fallback_kwargs.get("content")
    assert video_url in (content or "")
    assert not fallback_kwargs.get("files")


def test_video_request_ttl_exceeds_image_ttl():
    assert _request_ttl_for_operation("video") > _request_ttl_for_operation("image")
    assert _request_ttl_for_operation("video") >= 600


# Rioplatense markers that leak regional dialect into neutral Grok voice (#110)
_RIOPLATENSE_MARKERS = ("tenés", "Acá", "acá tenés")


def _assert_neutral_spanish_caption(caption: str) -> None:
    lowered = caption.lower()
    assert "http" not in lowered
    for marker in _RIOPLATENSE_MARKERS:
        assert marker.lower() not in lowered, f"Regional marker {marker!r} in {caption!r}"


def test_caption_builders_contain_no_urls():
    assert "http" not in build_image_caption("un gato con botas")
    assert "http" not in build_edit_caption()
    cap = build_video_caption(from_image=False, duration=5)
    assert "http" not in cap
    assert "480p" not in cap  # now clean, no tech specs in caption
    assert "/5" not in cap


def test_caption_builders_use_neutral_spanish():
    """Media delivery captions must not default to Rioplatense Spanish (#110)."""
    for _ in range(30):
        _assert_neutral_spanish_caption(build_image_caption("un gato con botas"))
        _assert_neutral_spanish_caption(build_image_caption(None))
        _assert_neutral_spanish_caption(build_image_caption(""))
    _assert_neutral_spanish_caption(build_edit_caption())
    _assert_neutral_spanish_caption(build_video_caption(from_image=False, duration=5))
    _assert_neutral_spanish_caption(build_video_caption(from_image=True, duration=6))