"""Tests for Twitch integration and game name normalization."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from groksito_discord.discord.integrations import steam, twitch


def test_normalize_game_name_strips_demo_suffixes():
    assert steam.normalize_game_name_for_lookup("Embers of the Uncrowned Demo") == (
        "Embers of the Uncrowned"
    )
    assert steam.normalize_game_name_for_lookup("Some Game - Demo") == "Some Game"
    assert steam.normalize_game_name_for_lookup("Another (Playtest)") == "Another"
    assert steam.normalize_game_name_for_lookup("dota 2") == "dota 2"


def test_pick_best_category_match_prefers_exact():
    categories = [
        {"id": "1", "name": "Embers of the Uncrowned", "box_art_url": "https://x/{width}x{height}.jpg"},
        {"id": "2", "name": "Something Else", "box_art_url": "https://y/{width}x{height}.jpg"},
    ]
    picked = twitch._pick_best_category_match("embers of the uncrowned demo", categories)
    assert picked is not None
    assert picked["name"] == "Embers of the Uncrowned"


@pytest.mark.asyncio
async def test_get_twitch_game_data_not_configured():
    with patch.object(twitch, "is_twitch_configured", return_value=False):
        result = await twitch.get_twitch_game_data("dota 2")
    assert result["configured"] is False
    assert result["found"] is False
    assert result["original_name"] == "dota 2"


@pytest.mark.asyncio
async def test_get_twitch_game_data_resolves_and_aggregates_streams():
    category = {
        "id": "12345",
        "name": "Dota 2",
        "box_art_url": "https://static-cdn.jtvnw.net/{width}x{height}.jpg",
    }

    async def fake_resolve(term: str):
        return category

    async def fake_streams(game_id: str):
        assert game_id == "12345"
        return {"viewer_count": 42_000, "live_streams": 3}

    with patch.object(twitch, "is_twitch_configured", return_value=True):
        with patch.object(twitch, "_resolve_twitch_category", side_effect=fake_resolve):
            with patch.object(twitch, "_fetch_stream_stats", side_effect=fake_streams):
                result = await twitch.get_twitch_game_data("dota 2")

    assert result["found"] is True
    assert result["matched_name"] == "Dota 2"
    assert result["viewer_count"] == 42_000
    assert result["live_streams"] == 3
    assert result["image_url"] is not None


@pytest.mark.asyncio
async def test_resolve_twitch_category_uses_normalized_name_for_games_endpoint():
    captured: dict[str, str] = {}

    async def fake_twitch_get(path: str, *, params=None):
        if path == "/games" and params:
            captured["name"] = params.get("name", "")
            return {"data": [{"id": "99", "name": "Embers of the Uncrowned", "box_art_url": ""}]}
        return {"data": []}

    with patch.object(twitch, "_twitch_get", side_effect=fake_twitch_get):
        result = await twitch._resolve_twitch_category("Embers of the Uncrowned Demo")

    assert captured["name"] == "Embers of the Uncrowned"
    assert result is not None
    assert result["name"] == "Embers of the Uncrowned"