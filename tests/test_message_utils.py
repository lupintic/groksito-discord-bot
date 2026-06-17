"""Tests for Discord message splitting utilities."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from groksito_discord.utils.message_utils import (
    DISCORD_SAFE_LENGTH,
    send_split_messages,
    split_message_text,
)


def test_short_text_returns_single_chunk():
    text = "Hello, world!"
    assert split_message_text(text) == [text]


def test_empty_text_returns_empty_list():
    assert split_message_text("") == []
    assert split_message_text("   ") == []


def test_exact_limit_returns_single_chunk():
    text = "x" * DISCORD_SAFE_LENGTH
    assert split_message_text(text) == [text]


def test_prefers_paragraph_breaks():
    para_a = "A" * 800
    para_b = "B" * 800
    text = f"{para_a}\n\n{para_b}"
    chunks = split_message_text(text, max_length=900)
    assert len(chunks) == 2
    assert chunks[0] == para_a
    assert chunks[1] == para_b


def test_prefers_line_breaks_over_hard_cut():
    line_a = "A" * 500
    line_b = "B" * 500
    text = f"{line_a}\n{line_b}"
    chunks = split_message_text(text, max_length=550)
    assert len(chunks) == 2
    assert chunks[0] == line_a
    assert chunks[1] == line_b


def test_keeps_fenced_code_block_intact_when_fits():
    code = "```python\nprint('hi')\n```"
    prefix = "word " * 80
    text = f"{prefix}\n\n{code}"
    chunks = split_message_text(text, max_length=len(prefix) + 20)
    assert len(chunks) == 2
    assert code in chunks[1]
    for chunk in chunks:
        assert chunk.count("```") % 2 == 0


def test_splits_oversized_code_block_with_balanced_fences():
    lines = "\n".join(f"line {i}: {'x' * 20}" for i in range(80))
    code = f"```\n{lines}\n```"
    chunks = split_message_text(code, max_length=300)
    assert len(chunks) > 1
    for chunk in chunks:
        assert chunk.count("```") % 2 == 0
    joined = "\n".join(chunks)
    assert "line 0:" in joined
    assert "line 79:" in joined


def test_avoids_splitting_inside_inline_code_when_possible():
    before = "A" * 400
    inline = "`" + "x" * 100 + "`"
    after = "B" * 400
    text = f"{before} {inline} {after}"
    chunks = split_message_text(text, max_length=450)
    assert len(chunks) >= 2
    for chunk in chunks:
        backticks = chunk.count("`")
        assert backticks % 2 == 0


@pytest.mark.asyncio
async def test_single_chunk_replies_once_to_user():
    user_msg = MagicMock()
    user_msg.reply = AsyncMock(return_value=MagicMock(id=1))
    user_msg.channel = MagicMock(id=99)

    await send_split_messages(user_msg, "short", mention_author=False)

    user_msg.reply.assert_awaited_once_with("short", mention_author=False)


@pytest.mark.asyncio
async def test_multi_chunk_sends_followups_to_channel():
    user_msg = MagicMock()
    channel = MagicMock(id=99)
    user_msg.reply = AsyncMock(return_value=MagicMock(id=10))
    user_msg.channel = channel
    channel.send = AsyncMock(return_value=MagicMock(id=11))

    text = "A" * 1000 + "\n\n" + "B" * 1000
    await send_split_messages(user_msg, text, max_length=1100)

    user_msg.reply.assert_awaited_once()
    channel.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_first_chunk_respects_mention_author():
    user_msg = MagicMock()
    user_msg.reply = AsyncMock(return_value=MagicMock(id=1))
    user_msg.channel = MagicMock(id=99)

    await send_split_messages(user_msg, "hello", mention_author=True)

    user_msg.reply.assert_awaited_once_with("hello", mention_author=True)