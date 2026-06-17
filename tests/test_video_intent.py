"""
Tests for has_explicit_video_intent() — the critical guard that prevents
generate_video from being offered on generic image questions.

This is high-value because the guard is the only thing standing between
"describe this image" and accidentally burning video quota / calling the
expensive video endpoint.
"""

import pytest

from groksito_discord.llm.media_tools import has_explicit_video_intent


class TestHasExplicitVideoIntent:
    """Core positive / negative / edge cases for the video intent detector."""

    @pytest.mark.parametrize(
        "text, expected, reason",
        [
            # Spanish explicit positives (the bulk of the keyword list)
            ("haz un video de un gato", True, "core 'haz un video' phrase"),
            ("Hacé un video de esta", True, "accented 'hacé' + 'esta'"),
            ("Genera un video por favor", True, "capitalized + polite"),
            ("crea un video animado", True, "'crea un video'"),
            ("quiero un video de la foto", True, "'quiero un video'"),
            ("necesito un video ya", True, "'necesito un video'"),
            ("hazme un video de esto", True, "'hazme un video'"),
            ("video de esta imagen", True, "short 'video de esta'"),
            ("un video de la foto que subí", True, "'un video de'"),
            ("anima esta", True, "short 'anima esta'"),
            ("convierte esta en video", True, "'convierte ... en video'"),
            ("haz video de la que respondí", True, "short 'haz video'"),
            # English positives
            ("make a video of the cat", True, "english 'make a video'"),
            ("generate a video please", True, "english 'generate a video'"),
            ("animate this picture", True, "'animate this'"),
            ("turn this into a video", True, "full english phrase"),
            # Mixed / embedded in longer text (still explicit intent)
            ("por favor haz un video de un zorro corriendo", True, "intent inside sentence"),
            ("puedes hacer un video de esta imagen??", True, "question + intent"),
            (
                "ahora me podrias generar un video de esa imagen porfis",
                True,
                "polite reply-to-bot I2V phrasing",
            ),
            ("generar un video de la foto", True, "infinitive 'generar un video'"),
        ],
    )
    def test_positive_cases(self, text, expected, reason):
        assert has_explicit_video_intent(text) is expected, f"Failed for: {reason}"

    @pytest.mark.parametrize(
        "text, reason",
        [
            # Generic image questions (must NOT trigger video)
            ("qué ves en esta imagen", "pure vision / description"),
            ("descríbeme esta foto", "analysis, no creation intent"),
            ("para qué sirve esta imagen", "question about image, not video"),
            ("genera una imagen de un gato", "image gen, not video"),
            ("edita esta foto en blanco y negro", "edit intent, not video"),
            ("qué es esto?", "generic question"),
            # No video keywords at all
            ("hola", "pure greeting"),
            ("jajaja eso estuvo bueno", "laughter"),
            ("cuéntame un chiste", "normal request"),
            # "video" mentioned but not as creation command
            ("me gusta el video que hiciste ayer", "past reference, not request"),
            ("el video de ayer estaba bueno", "reference, not 'haz un video'"),
            # Partial matches that should not count (the list uses whole phrases)
            ("video", "single word too vague"),
            ("quiero ver un video", "watching, not generating"),
        ],
    )
    def test_negative_cases(self, text, reason):
        assert has_explicit_video_intent(text) is False, f"Should be False for {reason}"

    def test_edge_cases(self):
        assert has_explicit_video_intent(None) is False
        assert has_explicit_video_intent("") is False
        assert has_explicit_video_intent("   ") is False
        assert has_explicit_video_intent("\n\t") is False

        # Case / accent / punctuation robustness (lowercasing + substring)
        assert has_explicit_video_intent("HAZ UN VIDEO DE ESTO!") is True
        assert has_explicit_video_intent("Generá un Video de la imagen") is True
        assert has_explicit_video_intent("  anima esta foto porfa  ") is True

        # Very long text containing exact phrase
        long = "blah blah " * 20 + "haz un video de un perro" + " blah" * 10
        assert has_explicit_video_intent(long) is True

    def test_no_false_positive_on_similar_words(self):
        # Make sure we don't accidentally match "videos" in other contexts that aren't commands
        assert has_explicit_video_intent("quiero ver videos de gatos") is False
        assert has_explicit_video_intent("el mejor video del año") is False
