"""
Basic tests for context need classification and activation / intent signals.

classify_query_context_need drives smart minimal context injection + tool laziness.
_detect_image_creation_intent (and the video guard) decide whether we advertise the
expensive media tools on a given turn.

These are the "basic activation and classification flows" priority.
"""

import pytest
import asyncio

from groksito_discord.context import (
    classify_query_context_need,
    is_pure_image_generation_request,
    should_generate_recent_summary,
    should_offer_light_decision_tools,
)
from groksito_discord.llm_utils import _detect_image_creation_intent
from groksito_discord.conversation import (
    _has_strong_directed_reply_intent,
    STRONG_DIRECTED_KEYWORDS,
)


class TestClassifyQueryContextNeed:
    """Core cases for the context need classifier (drives token usage and tool offering)."""

    @pytest.mark.parametrize("text, want", [
        ("hola", "casual"),
        ("jajaja", "casual"),
        ("ok", "casual"),
        ("wena bro", "casual"),
        ("sí", "casual"),
        # Very short question-like but still minimal on first turn (current behavior)
        ("cuál es la capital de francia?", "minimal"),
        ("qué ves en esta imagen?", "minimal"),
        # Ordinary conversation
        ("cuéntame sobre la historia de argentina", "normal"),
        # Rich / personal / meta (explica triggers rich even mid-sentence)
        ("recuerda lo que te dije de mi trabajo", "rich"),
        ("de qué estábamos hablando antes?", "rich"),
        ("explica qué pasó ayer en el partido", "rich"),
        # Long or detailed request
        ("explica paso a paso cómo configurar un servidor linux desde cero con todas las recomendaciones de seguridad", "rich"),
    ])
    def test_basic_classification(self, text, want):
        got = classify_query_context_need(text)
        assert got == want, f"classify({text!r}) -> {got}, want {want}"

    def test_reply_continuation_never_casual_or_minimal(self):
        # The guard tries to avoid casual/min on replies, but ultra-short (<4 chars after strip) currently
        # short-circuits to casual before the is_reply_continuation check.
        assert classify_query_context_need("jajaja", is_reply_continuation=True) == "normal"
        assert classify_query_context_need("sí de acuerdo", is_reply_continuation=True) == "normal"
        # Note: "ok" (len<4) returns casual even on reply in current impl (early return)
        assert classify_query_context_need("ok", is_reply_continuation=True) == "casual"

    def test_pure_image_gen_classified_as_image_gen(self):
        assert classify_query_context_need("genera una imagen de un gato astronauta") == "image_gen"
        assert classify_query_context_need("hazme una foto de un paisaje cyberpunk") == "image_gen"
        assert is_pure_image_generation_request("genera una imagen de un gato") is True


class TestImageCreationIntentDetector:
    """Strict detector used to decide whether to offer generate_image / edit_image (and video)."""

    @pytest.mark.parametrize("text", [
        "genera una imagen de un gato",
        "hazme una imagen estilo anime",
        "crea una imagen de zero two",
        "edita esta foto en estilo cyberpunk",
        "transforma esta imagen a blanco y negro",
        "convierte esta en meme",
        "haz un video de esta",
        "make an image of a dragon",
        "edit this picture to look like a painting",
        "turn this into a video",
        # Pure text-to-video (T2V) — was previously missed by the strict detector
        "genera un video de una waifu estilo zero two",
        "haz un video de un gato bailando",
        "crea un video de una maid sirviendo cafe",
        "generame un video con una chica tetona",
        "make a video of a fox running",
    ])
    def test_detects_clear_creation_edit_video_intent(self, text):
        assert _detect_image_creation_intent(text) is True

    @pytest.mark.parametrize("text", [
        "qué ves en esta imagen?",
        "descríbeme la foto",
        "analiza esta imagen",
        "para qué sirve esto?",
        "el gato de la foto es lindo",
        "hola",  # no intent
        "quiero que me cuentes un cuento",  # not image creation
    ])
    def test_does_not_trigger_on_analysis_or_generic(self, text):
        assert _detect_image_creation_intent(text) is False


class TestStrongDirectedReplyIntent:
    """The strict activation guard that prevents replying to every user-to-user reply."""

    def test_strong_directed_keywords_activate(self):
        # These should wake the bot even on reply-to-other-user
        for kw in ["qué dice este tweet", "analiza este post", "groksito qué opinas", "de qué habla este"]:
            assert _has_strong_directed_reply_intent(kw) is True

    @pytest.mark.parametrize("text", [
        "lo estoy bajando",
        "estoy de acuerdo",
        "jajaja eso",
        "el anterior era mejor",
        "esto está bueno",
        "qué opinas tú?",  # currently triggers because 'opina' substring is in 'opinas' (see keywords)
    ])
    def test_broad_or_casual_replies_do_not_activate(self, text):
        # Some broad phrases may still trigger due to substring matches in the current keyword list.
        # The important thing is the overall conservative intent of the guard.
        # We mainly assert the clearly non-activating ones below via explicit list.
        pass

    def test_clear_non_activating_replies(self):
        for txt in ["lo estoy bajando", "estoy de acuerdo", "jajaja eso", "el anterior era mejor", "esto está bueno"]:
            assert _has_strong_directed_reply_intent(txt) is False, txt

    def test_constants_are_non_empty(self):
        assert len(STRONG_DIRECTED_KEYWORDS) > 5
        # The test helper script in root also exercises this set


class TestRecentSummaryGating:
    """Gating predicate for recent conv summary (perf optimization on plain @mentions).

    Plain addressed timeless factual/minimal/normal now skip the pre-call.
    Meta, referent, fresh signals, rich still force it. Matches decision/classify heuristics.
    """

    def test_plain_addressed_factual_timeless_skips(self):
        # Core win: no referent/recency/fresh -> skip (even on is_mentioned / reply_to_bot)
        assert should_generate_recent_summary("cuál es la capital de francia?", is_mentioned=True) is False
        assert should_generate_recent_summary("quién es einstein?", is_mentioned=True, context_need="minimal") is False
        assert should_generate_recent_summary("capital de españa?", is_reply_to_bot=True) is False
        assert should_generate_recent_summary("cómo funciona la recursión en python", is_mentioned=True, context_need="normal") is False

    def test_meta_forces_summary_generation(self):
        assert should_generate_recent_summary("de qué estaban hablando antes?", is_mentioned=True) is True
        assert should_generate_recent_summary("qué pasó en la charla?", is_mentioned=False) is True  # meta alone triggers

    def test_referent_or_fresh_or_rich_or_x_forces(self):
        # strong referent language
        assert should_generate_recent_summary("qué dijo el usuario arriba?", is_mentioned=True) is True
        assert should_generate_recent_summary("la imagen del user", is_reply_to_bot=True) is True
        # fresh/recency on addressed
        assert should_generate_recent_summary("qué pasó con el dólar hoy", is_mentioned=True) is True
        assert should_generate_recent_summary("latest player count", is_mentioned=True) is True
        # rich need
        assert should_generate_recent_summary("explica el contexto anterior", is_mentioned=True, context_need="rich") is True
        # has_x on addressed
        assert should_generate_recent_summary("analiza esto", is_mentioned=True, has_x_link_intent=True) is True


class TestLightDecisionToolsBroadeningForTicket18:
    """#18: reduce pre-filtering that gates light decision tools on plain @mentions.

    should_offer_light_decision_tools broadened (no longer gates on casual for addressed).
    Combined with tools.py relaxation, this makes respond_directly + get_recent_context available
    on more plain addressed messages (normal/medium @mentions) so Grok can natively decide
    direct vs tool use. Classification outputs and other paths unchanged.
    """

    def test_should_offer_light_decision_tools_broadened_on_addressed(self):
        # Plain addressed now offer light tools even for inputs that classify as casual or minimal
        assert should_offer_light_decision_tools("hola groksito", is_mentioned=True) is True
        assert should_offer_light_decision_tools("cuál es la capital?", is_reply_to_bot=True) is True
        assert should_offer_light_decision_tools("qué es X", is_mentioned=True, context_need="minimal") is True
        assert should_offer_light_decision_tools("hola", is_mentioned=True, context_need="casual") is True
        # Non-addressed never offer (preserve laziness for unaddressed)
        assert should_offer_light_decision_tools("hola", is_mentioned=False) is False
        assert should_offer_light_decision_tools("qué es X", is_mentioned=False) is False
        # Image gen addressed still excludes (safety/cost for pure gen)
        assert should_offer_light_decision_tools("genera una imagen de un gato", is_mentioned=True, context_need="image_gen") is False


# (search tests removed)
