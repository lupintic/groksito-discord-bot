"""Regression tests for #112 — no legacy per-user memory in context or LLM input."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from groksito_discord.context import core as ctx_core
from groksito_discord.llm.llm_input import build_responses_input


@pytest.fixture
def isolated_context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Fresh in-memory context with persistence pointed at a temp file."""
    monkeypatch.setattr(ctx_core, "_channel_histories", ctx_core.defaultdict(ctx_core.deque))
    monkeypatch.setattr(ctx_core, "_channel_summaries", ctx_core.defaultdict(dict))
    monkeypatch.setattr(ctx_core, "PERSISTENCE_ENABLED", True)

    context_file = tmp_path / "pantsu_context.json"
    monkeypatch.setattr(
        ctx_core,
        "_get_context_file_path",
        lambda: context_file,
    )
    return context_file


class TestContextPersistenceNoUserProfiles:
    def test_save_context_omits_profiles_key(self, isolated_context: Path):
        ctx_core.update_from_message(
            channel_id=100,
            user_id=42,
            author_name="Alice",
            content="SECRET_USER_MEMORY_MARKER_112",
        )
        assert ctx_core.save_context() is True

        data = json.loads(isolated_context.read_text(encoding="utf-8"))
        assert "profiles" not in data
        assert "channels" in data

    def test_load_ignores_legacy_profiles_section(self, isolated_context: Path, monkeypatch):
        legacy_payload = {
            "version": 1,
            "saved_at": 0,
            "channels": {
                "100": [
                    {
                        "ts": 1.0,
                        "author_id": 42,
                        "author": "Alice",
                        "content": "channel-visible",
                        "is_bot": False,
                        "image_urls": [],
                        "links": [],
                    }
                ]
            },
            "profiles": {
                "42": {
                    "display_name": "Alice",
                    "last_seen": 1.0,
                    "recent_messages": [
                        {"ts": 1.0, "channel_id": 100, "content": "LEGACY_PROFILE_LEAK_112"}
                    ],
                }
            },
            "channel_summaries": {},
        }
        isolated_context.write_text(json.dumps(legacy_payload), encoding="utf-8")

        monkeypatch.setattr(ctx_core, "_channel_histories", ctx_core.defaultdict(ctx_core.deque))
        monkeypatch.setattr(ctx_core, "_channel_summaries", ctx_core.defaultdict(dict))
        ctx_core._load_context()

        msgs = ctx_core.get_recent_channel_messages(100, limit=5)
        assert len(msgs) == 1
        assert msgs[0]["content"] == "channel-visible"
        assert not hasattr(ctx_core, "_user_profiles")


class TestBuildResponsesInputNoUserMemory:
    @pytest.mark.asyncio
    async def test_user_profile_data_not_in_built_input(self, isolated_context: Path):
        secret_marker = "SECRET_USER_PROFILE_BUFFER_112"
        ctx_core.update_from_message(
            channel_id=200,
            user_id=99,
            author_name="Bob",
            content=secret_marker,
        )

        fake_message = SimpleNamespace(author=SimpleNamespace(id=99))
        with patch(
            "groksito_discord.llm.llm_input.log_context_injection",
        ) as mock_log:
            result = await build_responses_input(
                user_message="hello grok",
                channel_id=200,
                original_message=fake_message,
                image_urls=None,
                referenced_context=None,
                reply_chain_contexts=None,
                is_reply_continuation=False,
                has_x_link_intent=False,
                is_reply_to_bot=True,
                is_mentioned=False,
            )

        mock_log.assert_called_once()
        assert mock_log.call_args.kwargs.get("has_memory") is False
        assert mock_log.call_args.kwargs.get("has_summary") is False

        serialized = json.dumps(result["initial_input"])
        assert secret_marker not in serialized
        assert "SECRET_USER_PROFILE" not in serialized


# =============================================================================
# TDD for Task 3: attachments block injected into build_responses_input
# Tests written first (will fail until _build_attachments_block + injection + sig update).
# =============================================================================

class TestBuildResponsesInputAttachments:
    @pytest.mark.asyncio
    async def test_attachments_block_injected_in_built_input(self):
        """TDD: attachments (mix image+text+other, with text_content) produce the block in final user content."""
        atts = [
            {"filename": "funny.gif", "content_type": "image/gif", "size": 2400000},
            {"filename": "main.py", "content_type": "text/x-python", "size": 4200, "text_content": "def foo():\n    pass\n"},
            {"filename": "report.pdf", "content_type": "application/pdf", "size": 1470000},
        ]
        fake_message = SimpleNamespace(author=SimpleNamespace(id=123))
        result = await build_responses_input(
            user_message="check these files",
            channel_id=1,
            original_message=fake_message,
            image_urls=None,
            referenced_context=None,
            reply_chain_contexts=None,
            is_reply_continuation=False,
            has_x_link_intent=False,
            is_reply_to_bot=True,
            is_mentioned=False,
            attachments=atts,
        )

        serialized = json.dumps(result["initial_input"])
        assert "[Attachments sent with this message:" in serialized
        assert "funny.gif (image/gif, 2.3MB)" in serialized
        assert "main.py (text/x-python, 4.1KB)" in serialized
        assert "```python" in serialized
        assert "def foo():" in serialized
        assert "report.pdf (application/pdf, 1.4MB)" in serialized
        assert serialized.count("]") >= 1  # block closer
        assert "check these files" in serialized

    @pytest.mark.asyncio
    async def test_attachments_with_vision_images_still_produces_correct_input_image_count(self):
        """TDD: attachments block present, but only supported vision images become input_image (gif meta only)."""
        atts = [
            {"filename": "photo.jpg", "content_type": "image/jpeg", "size": 12345},
            {"filename": "anim.gif", "content_type": "image/gif", "size": 9999},
            {"filename": "notes.txt", "content_type": "text/plain", "size": 100, "text_content": "hello world from txt"},
        ]
        # only supported vision url passed separately (as harvest does)
        image_urls = ["https://cdn.example.com/photo.jpg"]
        fake_message = SimpleNamespace(author=SimpleNamespace(id=42))
        result = await build_responses_input(
            user_message="what is in the pic and txt",
            channel_id=1,
            original_message=fake_message,
            image_urls=image_urls,
            referenced_context=None,
            reply_chain_contexts=None,
            is_reply_continuation=False,
            has_x_link_intent=False,
            is_reply_to_bot=True,
            is_mentioned=False,
            attachments=atts,
        )

        initial_input = result["initial_input"]
        user_turn = initial_input[-1]
        content = user_turn.get("content")
        assert isinstance(content, list), "multimodal expected with images"

        img_count = sum(1 for c in content if isinstance(c, dict) and c.get("type") == "input_image")
        assert img_count == 1, "exactly the supported vision images, not gifs"

        # the first (or only) input_text contains the prepended attachments block + note
        text_blobs = " ".join(
            (c.get("text") or "") for c in content if isinstance(c, dict) and c.get("type") == "input_text"
        )
        assert "[Attachments sent with this message:" in text_blobs
        assert "photo.jpg" in text_blobs
        assert "anim.gif" in text_blobs  # listed in meta
        assert "notes.txt" in text_blobs
        assert "hello world from txt" in text_blobs
        # no vision block for gif
        assert not any(
            isinstance(c, dict) and c.get("type") == "input_image" and "gif" in str(c).lower()
            for c in content
        )
