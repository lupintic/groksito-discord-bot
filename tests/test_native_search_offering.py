"""Tests for prompt-driven native search tool offering (#48)."""

import pytest

from groksito_discord.llm.llm_utils import _build_native_search_tools


def test_normal_turn_offers_both_search_tools():
    tools = _build_native_search_tools(
        query_text="qué pasó hoy en Argentina",
        context_need="normal",
        has_visual_intent=False,
        has_attached_images=False,
    )
    types = {t["type"] for t in tools}
    assert types == {"web_search", "x_search"}


def test_image_gen_turn_offers_no_search():
    tools = _build_native_search_tools(
        query_text="genera una imagen de un gato",
        context_need="image_gen",
        has_visual_intent=False,
        has_attached_images=False,
    )
    assert tools == []


def test_visual_query_enables_image_flags():
    tools = _build_native_search_tools(
        query_text="muéstrame fotos del evento",
        context_need="normal",
        has_visual_intent=True,
        has_attached_images=False,
    )
    web = next(t for t in tools if t["type"] == "web_search")
    assert web.get("enable_image_search") is True
    assert web.get("enable_image_understanding") is True