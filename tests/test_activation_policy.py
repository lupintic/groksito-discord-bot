"""Strict activation policy: only @mention or reply-to-bot."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from groksito_discord.core.conversation import _resolve_referenced_and_activation


def _message(*, content="", mentions=None, reference=None, channel=None):
    return SimpleNamespace(
        content=content,
        mentions=mentions or [],
        reference=reference,
        channel=channel or MagicMock(),
    )


def _referenced(*, author_id=999, attachments=None):
    author = SimpleNamespace(id=author_id)
    return SimpleNamespace(
        author=author,
        attachments=attachments or [],
        content="referenced content",
    )


@pytest.mark.asyncio
async def test_user_to_user_reply_with_image_does_not_set_visual_intent():
    """Reply to another user's image without @mention must not set visual intent."""
    bot = SimpleNamespace(id=42)
    other_user_msg = _referenced(
        attachments=[SimpleNamespace(content_type="image/png", filename="pic.png")],
    )
    channel = MagicMock()
    channel.fetch_message = AsyncMock(return_value=other_user_msg)
    msg = _message(
        content="linda foto",
        reference=SimpleNamespace(message_id=100),
        channel=channel,
    )

    _ref, is_reply_to_bot, explicit_visual, is_reply_cont, *_ = await _resolve_referenced_and_activation(
        message=msg,
        client_user=bot,
        author_display="alice",
    )

    assert is_reply_cont is True
    assert is_reply_to_bot is False
    assert explicit_visual is False


@pytest.mark.asyncio
async def test_mention_reply_to_other_users_image_sets_visual_intent():
    bot = SimpleNamespace(id=42)
    other_user_msg = _referenced(
        attachments=[SimpleNamespace(content_type="video/mp4", filename="clip.mp4")],
    )
    channel = MagicMock()
    channel.fetch_message = AsyncMock(return_value=other_user_msg)
    msg = _message(
        content="mejorá este video",
        mentions=[bot],
        reference=SimpleNamespace(message_id=100),
        channel=channel,
    )

    _ref, is_reply_to_bot, explicit_visual, is_reply_cont, *_ = await _resolve_referenced_and_activation(
        message=msg,
        client_user=bot,
        author_display="alice",
    )

    assert is_reply_to_bot is False
    assert explicit_visual is True


@pytest.mark.asyncio
async def test_reply_to_bot_sets_reply_to_bot():
    bot = SimpleNamespace(id=42)
    bot_msg = _referenced(author_id=42)
    channel = MagicMock()
    channel.fetch_message = AsyncMock(return_value=bot_msg)
    msg = _message(
        content="seguí",
        reference=SimpleNamespace(message_id=200),
        channel=channel,
    )

    _ref, is_reply_to_bot, *_ = await _resolve_referenced_and_activation(
        message=msg,
        client_user=bot,
        author_display="bob",
    )

    assert is_reply_to_bot is True