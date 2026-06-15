"""Tests for referenced_has_media_attachments helper (detection only, not activation)."""

from types import SimpleNamespace

from groksito_discord.core.intent import referenced_has_media_attachments


def _msg(*attachments):
    return SimpleNamespace(attachments=attachments)


def _att(*, content_type: str = "", filename: str = ""):
    return SimpleNamespace(content_type=content_type, filename=filename)


def test_detects_image_attachment():
    assert referenced_has_media_attachments(_msg(_att(content_type="image/png"))) is True


def test_detects_video_attachment_by_content_type():
    assert referenced_has_media_attachments(_msg(_att(content_type="video/mp4"))) is True


def test_detects_video_attachment_by_filename():
    assert referenced_has_media_attachments(_msg(_att(content_type="application/octet-stream", filename="clip.mp4"))) is True


def test_ignores_non_media_attachments():
    assert referenced_has_media_attachments(_msg(_att(content_type="application/pdf", filename="doc.pdf"))) is False


def test_none_or_empty_message():
    assert referenced_has_media_attachments(None) is False
    assert referenced_has_media_attachments(_msg()) is False