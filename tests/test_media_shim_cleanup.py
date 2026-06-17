"""Regression tests for media shim removal (#72 / #74)."""

from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MEDIA_DIR = PROJECT_ROOT / "src" / "groksito_discord" / "media"

REMOVED_SHIMS = (
    MEDIA_DIR / "generator.py",
    MEDIA_DIR / "video_generation.py",
    MEDIA_DIR / "editor.py",
)


def test_media_compatibility_shim_files_removed():
    """Thin re-export shims must not exist; handlers are canonical."""
    for shim in REMOVED_SHIMS:
        assert not shim.exists(), f"Remove stale shim: {shim.relative_to(PROJECT_ROOT)}"


def test_canonical_media_handlers_present():
    for handler in ("image_handler.py", "video_handler.py", "audio_handler.py", "delivery.py"):
        assert (MEDIA_DIR / handler).is_file()


def test_media_tools_public_surface_importable():
    from groksito_discord.llm import media_tools

    assert hasattr(media_tools, "ENABLE_VIDEO_GENERATION")
    assert hasattr(media_tools, "has_explicit_video_intent")
    assert hasattr(media_tools, "has_explicit_audio_intent")
    assert hasattr(media_tools, "_handle_generate_image")
    assert hasattr(media_tools, "_handle_generate_video")