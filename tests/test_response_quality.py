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
        assert "product/tool options" in web["description"]