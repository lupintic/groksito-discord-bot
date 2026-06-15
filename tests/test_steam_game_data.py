"""Tests for the shared get_steam_game_data() helper."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from groksito_discord.discord.integrations import steam


@pytest.fixture(autouse=True)
def reset_steam_state():
    steam._DYNAMIC_RESOLVE_CACHE.clear()
    steam._APP_LIST = [
        {"appid": 730, "name": "Counter-Strike 2"},
        {"appid": 570, "name": "Dota 2"},
        {"appid": 30, "name": "Day of Defeat"},
        {"appid": 3767660, "name": "Embers of the Uncrowned"},
        {"appid": 4350460, "name": "Embers of the Uncrowned Demo"},
    ]
    steam._index_app_list(steam._APP_LIST)
    steam._APP_LIST_LOADED = True
    yield
    steam._DYNAMIC_RESOLVE_CACHE.clear()
    steam._APP_LIST_LOADED = False


def test_fuzzy_match_prefers_full_game_over_demo():
    result = steam._fuzzy_match_from_app_list("embers of the uncrowned")
    assert result == ("Embers of the Uncrowned", 3767660)


def test_fuzzy_match_does_not_hit_day_of_defeat_for_embers_demo():
    result = steam._fuzzy_match_from_app_list("embers of the uncrowned demo")
    assert result == ("Embers of the Uncrowned Demo", 4350460)


@pytest.mark.asyncio
async def test_resolve_term_prefers_store_for_embers_demo():
    with patch.object(
        steam,
        "_search_store_api",
        AsyncMock(return_value=("Embers of the Uncrowned Demo", 4350460)),
    ):
        result = await steam._resolve_term_to_app("embers of the uncrowned demo", None)
    assert result == ("Embers of the Uncrowned Demo", 4350460)


@pytest.mark.asyncio
async def test_resolve_term_uses_curated_alias_before_fuzzy():
    result = await steam._resolve_term_to_app("cs2", None)
    assert result == ("Counter-Strike 2", 730)


@pytest.mark.asyncio
async def test_get_steam_game_data_returns_sorted_results():
    async def fake_player(appid: int):
        return {730: 500_000, 570: 400_000}.get(appid)

    async def fake_image(appid: int):
        return f"https://cdn.example/{appid}.jpg"

    with patch.object(steam, "_fetch_player_count_for_app", side_effect=fake_player):
        with patch.object(steam, "_fetch_image_from_appdetails", side_effect=fake_image):
            with patch.object(steam, "ensure_steam_app_list_loaded", AsyncMock()):
                results = await steam.get_steam_game_data("dota 2, cs2")

    assert len(results) == 2
    assert results[0]["matched_name"] == "Counter-Strike 2"
    assert results[0]["player_count"] == 500_000
    assert results[1]["matched_name"] == "Dota 2"
    assert results[0]["image_url"].endswith("730.jpg")


@pytest.mark.asyncio
async def test_get_steam_game_data_uses_preresolved_appids():
    async def fake_player(appid: int):
        return 12_345

    async def fake_image(appid: int):
        return "https://cdn.example/header.jpg"

    preresolved = {"Black Desert": 582660}

    with patch.object(steam, "_fetch_player_count_for_app", side_effect=fake_player):
        with patch.object(steam, "_fetch_image_from_appdetails", side_effect=fake_image):
            with patch.object(steam, "ensure_steam_app_list_loaded", AsyncMock()):
                results = await steam.get_steam_game_data(
                    "Black Desert",
                    preresolved=preresolved,
                )

    assert len(results) == 1
    assert results[0]["original_name"] == "Black Desert"
    assert results[0]["appid"] == 582660
    assert results[0]["player_count"] == 12_345


@pytest.mark.asyncio
async def test_get_steam_game_data_falls_back_to_demo_player_stats():
    async def fake_player(appid: int):
        return 5937 if appid == 4350460 else None

    async def fake_image(appid: int):
        return f"https://cdn.example/{appid}.jpg"

    with patch.object(steam, "_fetch_player_count_for_app", side_effect=fake_player):
        with patch.object(steam, "_fetch_image_from_appdetails", side_effect=fake_image):
            with patch.object(
                steam,
                "_search_store_api",
                AsyncMock(
                    side_effect=lambda term: (
                        ("Embers of the Uncrowned", 3767660)
                        if term == "Embers of the Uncrowned"
                        else ("Embers of the Uncrowned Demo", 4350460)
                    )
                ),
            ):
                with patch.object(steam, "ensure_steam_app_list_loaded", AsyncMock()):
                    results = await steam.get_steam_game_data("Embers of the Uncrowned")

    assert len(results) == 1
    assert results[0]["matched_name"] == "Embers of the Uncrowned"
    assert results[0]["player_count"] == 5937
    assert results[0]["player_count_source"] == "demo"