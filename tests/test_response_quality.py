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
        assert "balance extra completeness with accuracy" in lowered
        assert "1-2 key facts" not in lowered


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
        assert "comprehensive" in web_b.lower()


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
        assert "comprehensive" in web["description"].lower()

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

