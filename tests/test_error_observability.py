"""
Tests for tightened error observability (issue #61).

Covers:
- utils/errors.py helpers
- Vision 404 retry path in the LLM client
- Tool error formatting edge cases
- env_utils parse failure logging
"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from groksito_discord.utils.errors import (
    format_tool_execution_error,
    is_image_fetch_404_error,
    log_auxiliary_failure,
    run_auxiliary,
    safe_auxiliary,
)


def test_format_tool_execution_error_includes_context():
    err = ValueError("sandbox unavailable")
    msg = format_tool_execution_error(
        "run_sandbox",
        err,
        round_num=2,
        arg_keys=["code", "language"],
    )
    assert "run_sandbox" in msg
    assert "ValueError" in msg
    assert "sandbox unavailable" in msg
    assert "round=2" in msg
    assert "arg_keys=" in msg


def test_is_image_fetch_404_error_detects_stale_urls():
    assert is_image_fetch_404_error(
        Exception("fetching image failed: 404 not found"),
        has_images=True,
    )
    assert is_image_fetch_404_error(
        Exception("unrecoverable data loss while fetching image"),
        has_images=True,
    )
    assert not is_image_fetch_404_error(
        Exception("fetching image failed: 404 not found"),
        has_images=False,
    )
    assert not is_image_fetch_404_error(
        Exception("rate limit exceeded"),
        has_images=True,
    )


def test_safe_auxiliary_logs_without_raising(caplog):
    caplog.set_level(logging.WARNING)
    test_logger = logging.getLogger("test.safe_auxiliary")

    with safe_auxiliary("test op", logger=test_logger, feature="TestFeature"):
        raise RuntimeError("boom")

    assert any("test op failed" in r.message for r in caplog.records)
    assert any("[TestFeature]" in r.message for r in caplog.records)


def test_run_auxiliary_returns_fallback():
    test_logger = logging.getLogger("test.run_auxiliary")

    result = run_auxiliary(
        lambda: (_ for _ in ()).throw(OSError("nope")),
        fallback=42,
        operation="disk read",
        logger=test_logger,
        feature="Env",
    )
    assert result == 42


def test_log_auxiliary_failure_marks_degraded(caplog):
    caplog.set_level(logging.WARNING)
    test_logger = logging.getLogger("test.degraded")

    log_auxiliary_failure(test_logger, "heartbeat write", OSError("disk full"), feature="Health")

    assert any("[degraded]" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_vision_404_retry_succeeds_without_images(monkeypatch):
    """First API call fails on stale image URL; retry without images succeeds."""
    from groksito_discord.llm.client import call_grok_for_groksito

    image_url = "https://cdn.discordapp.com/attachments/1/2/stale.png"

    async def fake_build_responses_input(**kwargs):
        has_images = bool(kwargs.get("image_urls"))
        return {
            "initial_input": [
                {
                    "role": "user",
                    "content": (
                        [{"type": "input_image", "image_url": image_url}]
                        if has_images
                        else "describe the meme"
                    ),
                }
            ],
            "stable_prefix_len": 50,
            "need": "normal",
            "user_id": "1",
            "user_message_text": "qué ves en esta imagen",
        }

    monkeypatch.setattr(
        "groksito_discord.llm.client.build_responses_input",
        fake_build_responses_input,
    )
    monkeypatch.setattr(
        "groksito_discord.llm.client._get_grok_bearer",
        lambda: "fake-test-bearer",
    )
    success_response = MagicMock()
    success_response.output_text = "Es un meme gracioso."
    success_response.output = []
    success_response.usage = MagicMock(input_tokens=30)

    call_count = 0

    async def flaky_responses_call(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("fetching image failed: 404 not found")
        return success_response

    monkeypatch.setattr(
        "groksito_discord.llm.client._call_responses_with_retry",
        flaky_responses_call,
    )

    result = await call_grok_for_groksito(
        user_message="qué ves en esta imagen",
        author_name="tester",
        channel_id=99,
        image_urls=[image_url],
        is_mentioned=True,
    )

    assert result == "Es un meme gracioso."
    assert call_count == 2


def test_normalize_bot_emoji_output_upgrades_shortcode(monkeypatch, tmp_path):
    from groksito_discord.utils import emoji_registry

    guild_id = "111"
    emoji_registry._EMOJI_KNOWLEDGE = {
        "version": 1,
        "guilds": {
            guild_id: {
                "emojis": {
                    "999": {
                        "id": "999",
                        "name": "jaja",
                        "url": "https://cdn.discordapp.com/emojis/999.webp",
                        "animated": False,
                        "usage_count": 1,
                    }
                }
            }
        },
    }
    emoji_registry._LOADED = True

    result = emoji_registry.normalize_bot_emoji_output(":jaja: nice", guild_id)
    assert "<:jaja:999>" in result


def test_env_utils_parse_failure_logs_warning(tmp_path, caplog):
    caplog.set_level(logging.WARNING)

    from groksito_discord.utils.env_utils import parse_env_file

    bad_env = tmp_path / ".env"
    bad_env.write_bytes(b"\xff\xfe invalid bytes")

    values = parse_env_file(bad_env)
    assert values == {}
    assert any("Failed to parse .env" in r.message for r in caplog.records)