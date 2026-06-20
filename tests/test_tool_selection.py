"""
High-value tests for tool selection logic (tools.py + interaction with media flags).

These protect the "extreme laziness" + "only offer heavy tools on explicit visual"
philosophy that keeps token usage low and the experience feeling native.

We call the public entry points (get_tools_for_request, get_continuation_tools)
with the same arguments the LLM layer would pass.
"""

import pytest

import groksito_discord.llm.tools as tools_mod
from groksito_discord.llm.tools import (
    get_tools_for_request,
    get_continuation_tools,
)


def _tool_names(tools: list[dict]) -> set[str]:
    return {t.get("name") for t in tools if isinstance(t, dict)}


class TestFirstTurnLaziness:
    """Casual / minimal / normal first turns without explicit visual creation intent → zero custom tools."""

    @pytest.mark.parametrize("query_need", ["casual", "minimal", "normal"])
    def test_no_custom_tools_on_ordinary_first_turns(self, query_need, patch_video_enabled):
        patch_video_enabled(True)  # even with video on, no visual intent → no media tools
        tools = get_tools_for_request(
            query_need=query_need,
            has_visual_intent=False,
            has_explicit_video_intent=False,
            is_tool_continuation=False,
            pure_image_gen=False,
        )
        # Simplified model: no internal search/history tools offered on first turn (only media on explicit visual).
        # casual/minimal/normal/rich all get zero custom (unless visual).
        assert tools == [], f"Expected zero custom tools for {query_need} first-turn without visual intent"

    def test_image_gen_pure_mode_only_tiny_generate_image(self, patch_video_enabled):
        patch_video_enabled(True)
        tools = get_tools_for_request(
            query_need="image_gen",
            has_visual_intent=False,  # pure_image_gen path takes precedence
            has_explicit_video_intent=False,
            is_tool_continuation=False,
            pure_image_gen=True,
        )
        names = _tool_names(tools)
        assert names == {"generate_image"}, "pure_image_gen should offer only the tiny generate_image schema"
        # No edit, no video unless explicit video + flag
        assert "edit_image" not in names
        assert "generate_video" not in names

    def test_pure_video_gen_mode_only_tiny_generate_video(self, patch_video_enabled):
        patch_video_enabled(True)
        tools = get_tools_for_request(
            query_need="image_gen",
            has_visual_intent=False,
            is_tool_continuation=False,
            pure_video_gen=True,
        )
        names = _tool_names(tools)
        assert names == {"generate_video"}, "pure_video_gen should offer only the tiny generate_video schema"
        assert "generate_image" not in names
        assert "edit_image" not in names


class TestVisualIntentTriggersMedia:
    """has_visual_intent=True (strict creation/edit/transform signals) → offers heavy media tools."""

    def test_visual_intent_offers_generate_and_edit(self, patch_video_enabled):
        patch_video_enabled(True)
        tools = get_tools_for_request(
            query_need="normal",
            has_visual_intent=True,
            has_explicit_video_intent=False,
            is_tool_continuation=False,
        )
        names = _tool_names(tools)
        assert "generate_image" in names
        assert "edit_image" in names
        assert "generate_video" in names  # video offered with visual creation path when enabled

    def test_visual_intent_offers_video_when_enabled(self, patch_video_enabled):
        patch_video_enabled(True)
        tools = get_tools_for_request(
            query_need="rich",
            has_visual_intent=True,
            is_tool_continuation=False,
        )
        names = _tool_names(tools)
        assert {"generate_image", "edit_image", "generate_video"} <= names

    def test_video_not_offered_when_flag_disabled_even_with_intent(self, patch_video_enabled):
        patch_video_enabled(False)
        tools = get_tools_for_request(
            query_need="normal",
            has_visual_intent=True,
            is_tool_continuation=False,
        )
        names = _tool_names(tools)
        assert "generate_video" not in names
        # gen/edit still offered (they are not behind the video flag)
        assert "generate_image" in names
        assert "edit_image" in names


class TestContinuationToolMinimization:
    """Continuation rounds are extremely minimal by default (only reply_to_user light)."""

    def test_continuation_minimal_by_default(self):
        tools = get_continuation_tools(
            has_visual_intent=False,
            has_explicit_video_intent=False,
        )
        names = _tool_names(tools)
        # The critical one for direct delivery UX must be present
        assert "reply_to_user" in names
        # No heavy media on non-visual continuation
        assert "generate_image" not in names
        assert "edit_image" not in names
        assert "generate_video" not in names

    def test_continuation_reoffers_media_only_on_visual_carryover(self, patch_video_enabled):
        patch_video_enabled(True)
        tools = get_continuation_tools(
            has_visual_intent=True,
            has_explicit_video_intent=True,
        )
        names = _tool_names(tools)
        assert "reply_to_user" in names
        assert "generate_image" in names
        assert "edit_image" in names
        assert "generate_video" in names

    def test_continuation_offers_video_on_explicit_intent_without_visual(self, patch_video_enabled):
        patch_video_enabled(True)
        tools = get_continuation_tools(
            has_visual_intent=False,
            has_explicit_video_intent=True,
        )
        names = _tool_names(tools)
        assert "generate_video" in names
        assert "generate_image" not in names

    def test_continuation_reoffers_media_after_asset_resolver(self, patch_video_enabled):
        """Avatar/emoji tools populate image_urls — continuation must re-offer generate_video."""
        patch_video_enabled(True)
        tools = get_continuation_tools(
            has_visual_intent=False,
            has_explicit_video_intent=False,
            has_resolved_asset_references=True,
        )
        names = _tool_names(tools)
        assert "generate_video" in names
        assert "edit_image" in names
        assert "reply_to_user" in names

    def test_pending_media_after_asset_is_media_only_continuation(self, patch_video_enabled):
        """Force generate_video call — no reply_to_user to role-play 'generating...'."""
        patch_video_enabled(True)
        tools = get_continuation_tools(
            pending_media_after_asset=True,
            has_explicit_video_intent=True,
        )
        names = _tool_names(tools)
        assert "generate_video" in names
        assert "reply_to_user" not in names

    def test_visual_media_consolidated_on_continuation(self, patch_video_enabled):
        """Visual media on continuations uses the same offering path as first-turn visual."""
        patch_video_enabled(True)
        tools = get_continuation_tools(
            has_visual_intent=True,
            has_explicit_video_intent=False,
        )
        names = _tool_names(tools)
        assert "generate_image" in names
        assert "edit_image" in names
        assert "generate_video" in names


class TestLegacyToolRemoval:
    """Removed duplicate helpers and legacy get_channel_context offering (#75)."""

    def test_get_heavy_tools_public_api_removed(self):
        assert not hasattr(tools_mod, "get_heavy_tools")

    @pytest.mark.parametrize(
        "factory,kwargs",
        [
            (
                get_tools_for_request,
                {
                    "query_need": "normal",
                    "has_visual_intent": True,
                    "is_tool_continuation": False,
                },
            ),
            (
                get_tools_for_request,
                {
                    "query_need": "normal",
                    "offer_light_decision_tools": True,
                    "is_tool_continuation": False,
                },
            ),
            (
                get_continuation_tools,
                {"has_visual_intent": False},
            ),
            (
                get_continuation_tools,
                {"has_visual_intent": True, "has_explicit_video_intent": True},
            ),
        ],
    )
    def test_get_channel_context_never_offered(self, factory, kwargs, patch_video_enabled, monkeypatch):
        patch_video_enabled(True)
        monkeypatch.setattr(
            "groksito_discord.config.settings.aggressive_continuation_tool_minimization",
            False,
        )
        names = _tool_names(factory(**kwargs))
        assert "get_channel_context" not in names


class TestLightDecisionOffer:
    """Light decision tools (core Discord delivery actions + respond/get_recent) on plain addressed normal/minimal.
    Core delivery tools (reply_to_user, react_to_message, create_thread) are offered here to let the model choose
    *how* to interact (implements #21). No bloat on plain addressed turns without the offer flag.
    """

    def test_light_decision_on_normal_offers_delivery_actions_and_signals(self, patch_video_enabled):
        patch_video_enabled(True)
        tools = get_tools_for_request(
            query_need="normal",
            has_visual_intent=False,
            offer_light_decision_tools=True,
        )
        names = _tool_names(tools)
        # Core Discord actions for agency (reply, react, thread) + decision signals
        assert "reply_to_user" in names
        assert "react_to_message" in names
        assert "create_thread" in names
        assert "respond_directly" in names
        assert "get_recent_context" in names
        # Image gen/edit now offered on light addressed turns so Grok can natively reason to
        # invoke Grok Imagine (generate_image) on any phrasing without client heuristics as gate.
        # (Tiny schema; full heavy only on explicit has_visual pre-detect or pure mode.)
        assert "generate_image" in names
        assert "edit_image" in names
        # no skill creation bloat
        assert "create_skill" not in names
        assert "edit_skill" not in names
        assert "use_skill" not in names
        # 5 delivery/decision + 2 image tools + video (native parity with Grok web)
        assert "generate_video" in names
        assert "get_user_avatar" in names
        assert "get_top_server_emoji" in names
        assert len(names) <= 10

    def test_light_decision_offers_video_without_keyword_gate(self, patch_video_enabled):
        """Addressed turns offer generate_video natively (like generate_image), no keyword gate."""
        patch_video_enabled(True)
        tools = get_tools_for_request(
            query_need="normal",
            has_visual_intent=False,
            has_explicit_video_intent=False,
            offer_light_decision_tools=True,
        )
        names = _tool_names(tools)
        assert "generate_video" in names

    def test_light_on_minimal_addressed_sim(self, patch_video_enabled):
        patch_video_enabled(True)
        tools = get_tools_for_request(
            query_need="minimal",
            has_visual_intent=False,
            offer_light_decision_tools=True,
        )
        names = _tool_names(tools)
        assert "respond_directly" in names
        assert "get_recent_context" in names
        assert "create_skill" not in names

    def test_no_light_when_not_offered(self, patch_video_enabled):
        patch_video_enabled(True)
        tools = get_tools_for_request(
            query_need="normal",
            has_visual_intent=False,
            offer_light_decision_tools=False,
        )
        names = _tool_names(tools)
        assert "respond_directly" not in names
        assert "get_recent_context" not in names
