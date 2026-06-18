"""Tests for systemic response quality / completeness mechanisms (#63)."""

import pytest

from groksito_discord.core.intent import needs_breadth_grounding
from groksito_discord.llm.llm_utils import _build_native_search_tools
from groksito_discord.llm.prompt_builder import SYSTEM_PROMPT


class TestBreadthGroundingDetector:
    """Query-type breadth signals — not topic-specific."""

    @pytest.mark.parametrize(
        "query",
        [
            "qué alternativas hay a Chromecast para Stremio",
            "best apps for streaming to TV",
            "recomendame herramientas para editar video",
            "compare Plex vs Jellyfin vs Emby",
            "how to watch movies on TV without cable",
            "what are the top options for note taking",
            "apps para castear desde el celular",
            # Casual discovery phrasing (no topic hardcoding; query-shape only)
            "como castear stremio a la tv",
            "cast stremio to tv",
            "que puedo usar para castear",
            "stremio en la tele",
        ],
    )
    def test_detects_breadth_queries(self, query):
        assert needs_breadth_grounding(query) is True

    @pytest.mark.parametrize(
        "query",
        [
            "hola",
            "qué hora es",
            "gracias",
            "2+2",
            "quién inventó la bombilla",
            "explica qué es la fotosíntesis",
        ],
    )
    def test_skips_non_breadth_queries(self, query):
        assert needs_breadth_grounding(query) is False


class TestSystemPromptCompleteness:
    def test_prompt_includes_completeness_self_check(self):
        lowered = SYSTEM_PROMPT.lower()
        assert "self-check" in lowered
        assert "web-grok-level completeness" in lowered or "web-grok" in lowered
        assert "multiple focused searches" in lowered
        assert "balance completeness with accuracy" in lowered
        assert "1-2 key facts" not in lowered

    def test_prompt_includes_grok_voice_guidance(self):
        """SYSTEM_PROMPT must enforce neutral Grok voice, not regional dialect (#110)."""
        lowered = SYSTEM_PROMPT.lower()
        assert "xai" in lowered
        assert "neutral" in lowered
        assert "regional dialect" in lowered or "regional" in lowered


class TestPromptSingleSourceOfTruth:
    def test_native_search_descriptions_imported_from_prompt_builder(self):
        from groksito_discord.llm import prompt_builder

        assert hasattr(prompt_builder, "get_native_search_descriptions")
        # Descriptions are now intentionally stable (single pair) for prompt cache prefix
        # effectiveness. Both calls return identical comprehensive text.
        web_b, x_b = prompt_builder.get_native_search_descriptions(
            "qué alternativas hay para castear"
        )
        web_n, x_n = prompt_builder.get_native_search_descriptions(
            "cuál es la capital de Francia"
        )
        assert web_b == web_n
        assert "multiple focused searches" in web_b
        assert "well-known options" in web_b or "comprehensive" in web_b.lower()


class TestNativeSearchBreadthDescriptions:
    def test_breadth_query_gets_multi_search_description(self):
        tools = _build_native_search_tools(
            query_text="qué alternativas hay para castear Stremio",
            context_need="normal",
            has_visual_intent=False,
            has_attached_images=False,
        )
        web = next(t for t in tools if t["type"] == "web_search")
        assert "multiple focused searches" in web["description"]
        assert "well-known options" in web["description"] or "comprehensive" in web["description"].lower()

    def test_normal_query_avoids_one_to_two_fact_cap(self):
        tools = _build_native_search_tools(
            query_text="cuál es la capital de Francia",
            context_need="normal",
            has_visual_intent=False,
            has_attached_images=False,
        )
        web = next(t for t in tools if t["type"] == "web_search")
        assert "1-2" not in web["description"]
        # After cache stabilization we always emit the comprehensive description
        # (product/tool options phrase lives in the old STANDARD variant; the stable
        # version emphasizes comprehensive options + synthesis instead).
        assert "comprehensive" in web["description"].lower() or "well-known options" in web["description"]
class TestProactiveSearchGuidance:
    """Additive coverage for the proactive freshness / recency bias (2026-06 feature)."""

    def test_system_prompt_and_descriptions_include_freshness_proactive_cues(self):
        from groksito_discord.llm.prompt_builder import SYSTEM_PROMPT, get_native_search_descriptions
        lowered = SYSTEM_PROMPT.lower()
        assert "proactively" in lowered
        assert "up-to-date" in lowered or "fresh" in lowered or "freshness" in lowered

        web, x = get_native_search_descriptions("qué pasó hoy")
        combined = (web + " " + x).lower()
        assert "proactively" in combined
        assert "up-to-date" in combined or "fresh" in combined

    def test_time_sensitive_query_still_offers_search(self):
        from groksito_discord.llm.llm_utils import _build_native_search_tools
        tools = _build_native_search_tools(
            query_text="score del partido de hoy",
            context_need="normal",
            has_visual_intent=False,
            has_attached_images=False,
        )
        types = {t["type"] for t in tools}
        assert types == {"web_search", "x_search"}


class TestGrokVoiceGuidance:
    """Grok identity + neutral voice anchoring (#111)."""

    def test_system_prompt_anchors_grok_identity_and_neutral_voice(self):
        lowered = SYSTEM_PROMPT.lower()
        assert lowered.startswith("you are grok (groksito on this discord server).")
        assert "truth-seeking" in lowered or "truth seeking" in lowered
        assert "helpful" in lowered
        assert "curious" in lowered
        assert "neutral" in lowered or "dialect" in lowered
        assert "mirror" in lowered or "match" in lowered
        assert "vos" in lowered or "tenés" in lowered or "regional slang" in lowered
        assert "get_recent_context only when" in lowered

    def test_system_prompt_avoids_default_regional_spanish_markers(self):
        """Prompt should discourage default Rioplatense flavor, not embed it."""
        lowered = SYSTEM_PROMPT.lower()
        assert "acá tenés" not in lowered
        assert "friendly and natural (spanish + english/mixes)" not in lowered

    def test_system_prompt_stays_cache_friendly_length(self):
        """New voice block must not bloat the stable system prefix."""
        assert len(SYSTEM_PROMPT) <= 3200

    def test_get_recent_context_tool_description_from_prompt_builder(self):
        from groksito_discord.llm import prompt_builder
        from groksito_discord.llm.tools import _get_recent_context_schema

        assert hasattr(prompt_builder, "GET_RECENT_CONTEXT_TOOL_DESCRIPTION")
        schema = _get_recent_context_schema()
        assert schema["description"] == prompt_builder.GET_RECENT_CONTEXT_TOOL_DESCRIPTION
        assert "get_recent_context only when" in schema["description"].lower()

    def test_native_search_descriptions_derive_from_shared_guidance(self):
        from groksito_discord.llm import prompt_builder

        web, x = prompt_builder.get_native_search_descriptions("hoy")
        assert prompt_builder.FRESHNESS_GUIDANCE.split()[0].lower() in web.lower()
        assert prompt_builder.SEARCH_FOCUSED_SYNTHESIS.rstrip(".") in web
        assert prompt_builder.X_SEARCH_SYNTHESIS in x

