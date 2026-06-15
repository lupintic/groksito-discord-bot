"""Tests for image edit intent detection and edit payload shaping."""

import pytest

from groksito_discord.core.intent import is_image_edit_request, _detect_image_creation_intent
from groksito_discord.media.image_handler import (
    _build_edit_payload,
    _needs_base64_for_edit_api,
    _resolve_edit_reference_url,
)


class TestImageEditIntent:
    @pytest.mark.parametrize(
        "text",
        [
            "le pones pelo corto de mujer rosado un poco de pecas y rubor rosado en los cachetes",
            "ponle pecas y rubor en los cachetes",
            "hazle el pelo rosado corto",
            "edita esta imagen con estilo anime",
            "change this image to gothic style",
        ],
    )
    def test_edit_intent_with_reference_image(self, text):
        assert is_image_edit_request(text, has_reference_image=True) is True
        assert _detect_image_creation_intent(text, has_reference_image=True) is True

    @pytest.mark.parametrize(
        "text",
        [
            "qué ves en esta imagen",
            "describe la foto",
            "quién es esta persona",
            "hola groksito",
        ],
    )
    def test_not_edit_intent(self, text):
        assert is_image_edit_request(text, has_reference_image=True) is False

    def test_imperative_without_reference_is_not_edit(self):
        text = "le pones pelo corto rosado"
        assert is_image_edit_request(text, has_reference_image=False) is False


def test_build_edit_payload_single_uses_image_key():
    payload = _build_edit_payload("make hair pink", ["https://example.com/a.png"], None)
    assert "image" in payload
    assert payload["image"]["url"] == "https://example.com/a.png"
    assert "images" not in payload


def test_build_edit_payload_multi_uses_images_key():
    urls = ["https://example.com/a.png", "https://example.com/b.png"]
    payload = _build_edit_payload("blend styles", urls, "1:1")
    assert "images" in payload
    assert len(payload["images"]) == 2
    assert payload["aspect_ratio"] == "1:1"


def test_needs_base64_for_discord_cdn():
    assert _needs_base64_for_edit_api("https://cdn.discordapp.com/attachments/1/2/photo.png") is True
    assert _needs_base64_for_edit_api("https://example.com/photo.png") is False


@pytest.mark.asyncio
async def test_resolve_edit_reference_url_keeps_public_urls():
    url = "https://example.com/photo.png"
    assert await _resolve_edit_reference_url(url) == url


@pytest.mark.asyncio
async def test_resolve_edit_reference_url_base64_for_discord(monkeypatch):
    png_header = b"\x89PNG\r\n\x1a\n" + b"\x00" * 20

    async def fake_download(_url):
        return png_header

    monkeypatch.setattr(
        "groksito_discord.media.image_handler._download_url",
        fake_download,
    )
    url = "https://cdn.discordapp.com/attachments/1/2/photo.png"
    resolved = await _resolve_edit_reference_url(url)
    assert resolved.startswith("data:image/png;base64,")