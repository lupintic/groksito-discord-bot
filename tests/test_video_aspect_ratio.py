"""Tests for I2V aspect-ratio inference (prevents stretched output)."""

from groksito_discord.media.video_handler import (
    _dimensions_to_aspect_ratio,
    _sniff_image_dimensions,
)


def test_dimensions_to_aspect_ratio_labels():
    assert _dimensions_to_aspect_ratio(1920, 1080) == "16:9"
    assert _dimensions_to_aspect_ratio(1080, 1920) == "9:16"
    assert _dimensions_to_aspect_ratio(1024, 1024) == "1:1"
    assert _dimensions_to_aspect_ratio(1200, 1000) == "1:1"


def test_sniff_png_dimensions():
    # Minimal PNG IHDR: 800x600
    header = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
        + (800).to_bytes(4, "big")
        + (600).to_bytes(4, "big")
        + b"\x00" * 100
    )
    assert _sniff_image_dimensions(header) == (800, 600)