"""Tests for /versus embed builder."""

from __future__ import annotations

import discord

from groksito_discord.discord import client as discord_client


def test_build_versus_embeds_includes_steam_and_twitch_fields():
    steam_games = [
        {
            "original_name": "dota 2",
            "matched_name": "Dota 2",
            "appid": 570,
            "player_count": 400_000,
            "player_count_source": "app",
            "image_url": "https://cdn.example/dota.jpg",
        },
        {
            "original_name": "cs2",
            "matched_name": "Counter-Strike 2",
            "appid": 730,
            "player_count": 500_000,
            "player_count_source": "app",
            "image_url": "https://cdn.example/cs2.jpg",
        },
    ]
    twitch_games = [
        {
            "original_name": "dota 2",
            "matched_name": "Dota 2",
            "found": True,
            "configured": True,
            "viewer_count": 10_000,
            "live_streams": 50,
            "image_url": None,
        },
        {
            "original_name": "cs2",
            "matched_name": "Counter-Strike 2",
            "found": True,
            "configured": True,
            "viewer_count": 20_000,
            "live_streams": 80,
            "image_url": None,
        },
    ]

    embeds = discord_client._build_versus_embeds(
        "dota 2",
        "cs2",
        steam_games,
        twitch_games,
    )

    assert len(embeds) == 3
    assert isinstance(embeds[0], discord.Embed)
    assert embeds[0].title == "⚔️ Versus"

    game_embeds = embeds[1:]
    assert all(isinstance(e, discord.Embed) for e in game_embeds)
    assert "Dota 2" in game_embeds[0].title
    assert "Counter-Strike 2" in game_embeds[1].title

    steam_fields = [f for e in game_embeds for f in e.fields if f.name == "🎮 Steam"]
    twitch_fields = [f for e in game_embeds for f in e.fields if f.name == "📺 Twitch"]
    assert len(steam_fields) == 2
    assert len(twitch_fields) == 2
    assert "500,000" in steam_fields[1].value
    assert "20,000" in twitch_fields[1].value


def test_build_versus_embeds_handles_missing_game():
    embeds = discord_client._build_versus_embeds(
        "unknown game",
        "dota 2",
        [{"original_name": "dota 2", "matched_name": "Dota 2", "appid": 570, "player_count": 1}],
        [
            {"original_name": "unknown game", "found": False, "configured": True},
            {
                "original_name": "dota 2",
                "matched_name": "Dota 2",
                "found": True,
                "configured": True,
                "viewer_count": 100,
                "live_streams": 1,
            },
        ],
    )

    assert len(embeds) == 3
    assert "No se encontró" in embeds[1].description