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
        attachments=[],  # extended for Task 4: attachments threaded (empty here keeps compat)
        is_mentioned=True,
    )

    assert result == "Es un meme gracioso."
    assert call_count == 2


@pytest.mark.asyncio
async def test_non_404_gif_trouble_retries_with_attachments_metadata(monkeypatch):
    """Non-404 vision/processing trouble on GIF should retry once with image_urls=[] but attachments kept, succeed using metadata."""
    from groksito_discord.llm.client import call_grok_for_groksito

    gif_url = "https://cdn.discordapp.com/attachments/1/2/funny.gif"
    gif_att = {"filename": "funny.gif", "content_type": "image/gif", "size": 234567}

    async def fake_build_responses_input(**kwargs):
        has_imgs = bool(kwargs.get("image_urls"))
        atts = kwargs.get("attachments") or []
        if has_imgs:
            content = [{"type": "input_image", "image_url": gif_url}]
        else:
            # metadata present on retry
            content = f"describe the gif\n\n[Attachments sent with this message:\n- funny.gif (image/gif, 229.1KB)\n]"
        return {
            "initial_input": [{"role": "user", "content": content}],
            "stable_prefix_len": 50,
            "need": "normal",
            "user_id": "1",
            "user_message_text": "describe the gif",
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
    success_response.output_text = "I see the attached funny.gif (image/gif). Vision is limited to JPG/PNG so I rely on metadata."
    success_response.output = []
    success_response.usage = MagicMock(input_tokens=30)

    call_count = 0

    async def flaky_responses_call(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        inp = kwargs.get("input") or (args[0] if args else None)
        images_present = False
        try:
            # Detect "when images present" per spec: look for input_image blocks in first-turn input
            for item in (inp or []):
                c = item.get("content") if isinstance(item, dict) else None
                if isinstance(c, list) and any(
                    isinstance(ii, dict) and ii.get("type") == "input_image" for ii in c
                ):
                    images_present = True
                    break
        except Exception:
            pass
        if call_count == 1 and images_present:
            # non-404 processing trouble (e.g. GIF vision rejection)
            raise RuntimeError("processing error while handling attached GIF")
        return success_response

    monkeypatch.setattr(
        "groksito_discord.llm.client._call_responses_with_retry",
        flaky_responses_call,
    )

    result = await call_grok_for_groksito(
        user_message="describe the gif",
        author_name="tester",
        channel_id=99,
        image_urls=[gif_url],
        attachments=[gif_att],
        is_mentioned=True,
    )

    assert result == "I see the attached funny.gif (image/gif). Vision is limited to JPG/PNG so I rely on metadata."
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


def test_emoji_registry_strict_per_guild_no_cross_fallback(monkeypatch, tmp_path):
    from groksito_discord.utils import emoji_registry
    emoji_registry._EMOJI_KNOWLEDGE = {
        "version": 1,
        "guilds": {
            "111": {"emojis": {"1": {"id": "1", "name": "a", "usage_count": 10}}},
            "222": {"emojis": {"2": {"id": "2", "name": "b", "usage_count": 5}}},
        },
    }
    emoji_registry._LOADED = True

    # Strict for guild 111
    g111 = emoji_registry.get_emojis_for_guild("111")
    assert "1" in g111 and "2" not in g111

    # descriptions should only see 111's (no fallback)
    block = emoji_registry.get_emoji_descriptions_for_prompt("111", max_emotes=2)
    assert ":a:" in block and ":b:" not in block

    # normalize for 111 must not know 222's ID
    out = emoji_registry.normalize_bot_emoji_output(":b: foo", "111")
    assert "<:b:" not in out   # no foreign ID


def test_get_top_used_emotes_caps_at_8_and_ranks_by_usage(monkeypatch, tmp_path):
    from groksito_discord.utils import emoji_registry
    ems = {str(i): {"id": str(i), "name": f"e{i}", "usage_count": 10-i, "description": f"desc{i}"} for i in range(1,15)}
    emoji_registry._EMOJI_KNOWLEDGE = {"version":1, "guilds": {"333": {"emojis": ems}}}
    emoji_registry._LOADED = True

    # After impl this will use usage-ranked top 8 (descriptions_for_prompt re-used/adapted for now)
    block = emoji_registry.get_emoji_descriptions_for_prompt("333", max_emotes=8)
    assert block.count(":e") == 8
    assert ":e1:" in block and ":e8:" in block  # highest usage first
    assert ":e9:" not in block  # capped


def test_normalize_prefers_live_guild_emojis(monkeypatch):
    from groksito_discord.utils import emoji_registry
    # Simulate live guild with different ID for same name
    class FakeEmoji:
        def __init__(self, i, n): self.id = i; self.name = n; self.animated = False
    class FakeGuild:
        emojis = [FakeEmoji(999, "jaja")]
    emoji_registry._EMOJI_KNOWLEDGE = {"version":1, "guilds": {"444": {"emojis": {"old": {"id":"old", "name":"jaja", "usage_count":1}}}}}
    emoji_registry._LOADED = True

    # normalize accepts guild_obj and prefers live IDs
    out = emoji_registry.normalize_bot_emoji_output(":jaja: hi", "444", guild_obj=FakeGuild())
    assert "<:jaja:999>" in out


def test_is_supported_vision_image_jpg_png_yes():
    from groksito_discord.core.intent import is_supported_vision_image
    att = type('A', (), {'content_type': 'image/jpeg', 'filename': 'x.png'})()
    assert is_supported_vision_image(att) is True

def test_is_supported_vision_image_gif_no():
    from groksito_discord.core.intent import is_supported_vision_image
    att = type('A', (), {'content_type': 'image/gif', 'filename': 'a.gif'})()
    assert is_supported_vision_image(att) is False   # we handle via metadata only

def test_is_text_attachment_detects():
    from groksito_discord.core.intent import is_text_attachment
    py_att = type('A', (), {'content_type': '', 'filename': 'foo.py'})()
    assert is_text_attachment(py_att) is True
    pdf_att = type('A', (), {'content_type': 'application/pdf', 'filename': 'r.pdf'})()
    assert is_text_attachment(pdf_att) is False


# =============================================================================
# TDD for Task 2: harvest now returns (images, attachments) and collects all + filters vision + inlines text
# =============================================================================

from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
async def test_harvest_returns_attachments_and_filters_to_supported_vision_only():
    """Failing test (TDD) for enhanced harvest: must collect ALL attachments meta for current,
    only supported images (jpg/png) go to image_urls, text attachments considered for inline (fetch may skip).
    """
    from groksito_discord.core.conversation import _harvest_vision_images

    att_jpg = SimpleNamespace(
        content_type="image/jpeg", filename="photo.jpg", url="https://cdn.example.com/photo.jpg", size=12345
    )
    att_gif = SimpleNamespace(
        content_type="image/gif", filename="anim.gif", url="https://cdn.example.com/anim.gif", size=9999
    )
    att_py = SimpleNamespace(
        content_type="text/x-python", filename="script.py", url="https://cdn.example.com/script.py", size=200
    )
    att_pdf = SimpleNamespace(
        content_type="application/pdf", filename="doc.pdf", url="https://cdn.example.com/doc.pdf", size=5000
    )

    msg = SimpleNamespace(attachments=[att_jpg, att_gif, att_py, att_pdf], content="")

    # Call with no referenced, no special intent -> should still collect current attachments
    result = await _harvest_vision_images(
        message=msg,
        referenced=None,
        explicit_visual_reply_intent=False,
        is_reply_continuation=False,
        has_x_link_intent=False,
        is_mentioned=False,
        user_text="",
    )

    # This will fail until harvest refactored to return tuple (images, attachments)
    image_urls, attachments = result

    # All 4 attachments must be present via meta (new behavior)
    assert len(attachments) == 4
    filenames = [a.get("filename") for a in attachments]
    assert "photo.jpg" in filenames
    assert "anim.gif" in filenames
    assert "script.py" in filenames
    assert "doc.pdf" in filenames

    # Only the supported vision image (jpg) should be in image_urls (gif filtered by is_supported_vision_image)
    assert len(image_urls) == 1
    assert "photo.jpg" in str(image_urls[0]) or image_urls[0].endswith("photo.jpg")  # url contains

    # For text att, if inline happened meta would have text_content; fetch uses fake url so may be absent (graceful)
    # Main point of test: attachments list is returned and populated.
    assert any(a.get("filename") == "script.py" for a in attachments)
