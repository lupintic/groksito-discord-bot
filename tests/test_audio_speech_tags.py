"""Tests for /audio Speech Tags help embed and TTS text preparation."""

from groksito_discord.media.audio_handler import (
    XAI_TTS_DOCS_URL,
    _clean_text_for_tts,
    _prepare_text_for_tts,
    build_audio_speech_tags_embed,
)


def test_build_audio_speech_tags_embed_has_key_sections():
    embed = build_audio_speech_tags_embed()

    assert embed.title
    assert "Speech Tags" in embed.title
    assert embed.url == XAI_TTS_DOCS_URL
    assert embed.description
    assert "Speech Tags" in embed.description

    field_names = [f.name for f in embed.fields]
    assert any("inline" in name.lower() for name in field_names)
    assert any("envolvente" in name.lower() for name in field_names)
    assert any("ejemplo" in name.lower() for name in field_names)
    assert any("consejo" in name.lower() for name in field_names)

    combined = " ".join(f.value for f in embed.fields)
    assert "[pause]" in combined
    assert "<whisper>" in combined
    assert "<slow><soft>" in combined


def test_clean_text_for_tts_preserves_speech_tags():
    raw = (
        "Entré y [pause] ahí estaba. [laugh] "
        "Tengo que contarte algo. <whisper>Es un secreto.</whisper> "
        "<slow><soft>Buenas noches.</soft></slow>"
    )
    cleaned = _clean_text_for_tts(raw)

    assert "[pause]" in cleaned
    assert "[laugh]" in cleaned
    assert "<whisper>Es un secreto.</whisper>" in cleaned
    assert "<slow><soft>Buenas noches.</soft></slow>" in cleaned


def test_prepare_text_for_tts_preserves_speech_tags():
    raw = "Hola [sigh] <whisper>muy bajito</whisper> adiós."
    prepared = _prepare_text_for_tts(raw)

    assert "[sigh]" in prepared
    assert "<whisper>muy bajito</whisper>" in prepared