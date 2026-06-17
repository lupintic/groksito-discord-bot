"""Tests for is_pure_video_generation_request — ultra-light T2V path parity with image."""

import pytest

from groksito_discord.core.intent import is_pure_video_generation_request


class TestIsPureVideoGenerationRequest:
    @pytest.mark.parametrize(
        "text, expected",
        [
            ("genera un video de un gato", True),
            ("haz un video de un perro corriendo", True),
            ("make a video of a sunset", True),
            ("ahora me podrias generar un video de esa imagen porfis", False),
            ("genera una imagen de un gato", False),
            ("qué ves en este video", False),
        ],
    )
    def test_pure_video_detector(self, text, expected):
        assert is_pure_video_generation_request(text) is expected