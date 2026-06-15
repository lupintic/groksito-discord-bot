"""Tests for dynamic Steam game name resolution (/steamchart lookup)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from groksito_discord.discord.integrations import steam


@pytest.fixture(autouse=True)
def clear_dynamic_cache():
    steam._DYNAMIC_RESOLVE_CACHE.clear()
    yield
    steam._DYNAMIC_RESOLVE_CACHE.clear()


def test_score_name_match_exact_and_demo_skip_logic():
    assert steam._score_name_match("dota 2", "Dota 2") == 1.0
    assert steam._score_name_match("embers of the uncrowned", "Embers of the Uncrowned") == 1.0
    assert steam._score_name_match("cs2", "Counter-Strike 2") < 0.45


def test_local_resolver_still_hits_curated_aliases():
    assert steam._resolve_steam_game_local("cs2") == ("Counter-Strike 2", 730)
    assert steam._resolve_steam_game_local("dota 2") == ("Dota 2", 570)


@pytest.mark.asyncio
async def test_search_store_api_picks_full_game_over_demo():
    fake_items = {
        "items": [
            {"type": "app", "name": "Embers of the Uncrowned", "id": 3767660},
            {"type": "app", "name": "Embers of the Uncrowned Demo", "id": 4350460},
        ]
    }

    class FakeResponse:
        status_code = 200

        def json(self):
            return fake_items

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.get = AsyncMock(return_value=FakeResponse())
        mock_client_cls.return_value = mock_client

        result = await steam._search_store_api("embers of the uncrowned")

    assert result == ("Embers of the Uncrowned", 3767660)


@pytest.mark.asyncio
async def test_dynamic_resolve_accepts_store_hit_without_player_api():
    """Games without concurrent stats (API result=42) must still resolve."""

    async def fake_store(term: str):
        if term == "embers of the uncrowned":
            return ("Embers of the Uncrowned", 3767660)
        return None

    with patch.object(steam, "_search_store_api", side_effect=fake_store):
        result = await steam._resolve_steam_game("embers of the uncrowned")

    assert result == ("Embers of the Uncrowned", 3767660)


@pytest.mark.asyncio
async def test_dynamic_resolve_falls_back_to_charts_with_store_validation():
    charts_html = '<a href="/app/999"><img alt="Mystery Game" src="/x.jpg"></a>'

    class ChartsResponse:
        status_code = 200
        text = charts_html

    class AppDetailsResponse:
        status_code = 200

        def json(self):
            return {"999": {"success": True, "data": {"name": "Mystery Game"}}}

    async def mock_get(url, *args, **kwargs):
        if "steamcharts.com/search" in str(url):
            return ChartsResponse()
        if "appdetails" in str(url):
            return AppDetailsResponse()
        raise AssertionError(f"unexpected url: {url}")

    with patch.object(steam, "_search_store_api", AsyncMock(return_value=None)):
        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__.return_value = mock_client
            mock_client.get = AsyncMock(side_effect=mock_get)
            mock_client_cls.return_value = mock_client

            result = await steam._resolve_steam_game("mystery game")

    assert result == ("Mystery Game", 999)