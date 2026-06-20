"""Tests for Discord asset resolution (avatars + top server emojis)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from groksito_discord.core import discord_assets


class _FakeAvatar:
    def __init__(self, url: str):
        self._url = url

    def replace(self, *, size: int = 128):
        base = self._url.split("?")[0]
        return SimpleNamespace(url=f"{base}?size={size}")

    @property
    def url(self):
        return self._url


def _fake_user(*, uid: int, name: str, avatar_url: str, display_name: str | None = None):
    return SimpleNamespace(
        id=uid,
        name=name,
        display_name=display_name or name,
        display_avatar=_FakeAvatar(avatar_url),
    )


class TestBuildAvatarUrl:
    def test_builds_sized_cdn_url(self):
        user = _fake_user(uid=1, name="alice", avatar_url="https://cdn.discordapp.com/avatars/1/x.png")
        url = discord_assets.build_avatar_url(user, size=512)
        assert url == "https://cdn.discordapp.com/avatars/1/x.png?size=512"

    def test_returns_empty_when_no_avatar(self):
        user = SimpleNamespace(display_avatar=None)
        assert discord_assets.build_avatar_url(user) == ""


class TestPickMentionedUser:
    def test_picks_single_mention_by_default(self):
        mentioned = _fake_user(uid=99, name="bob", avatar_url="https://cdn/x.png")
        message = SimpleNamespace(mentions=[mentioned], author=_fake_user(uid=1, name="me", avatar_url="https://cdn/me.png"))
        assert discord_assets.pick_user_from_message(message) is mentioned

    def test_resolves_by_user_id(self):
        target = _fake_user(uid=42, name="target", avatar_url="https://cdn/t.png")
        message = SimpleNamespace(
            mentions=[target],
            author=_fake_user(uid=1, name="me", avatar_url="https://cdn/me.png"),
            guild=None,
        )
        picked = discord_assets.pick_user_from_message(message, user_id="42")
        assert picked.id == 42

    def test_resolves_by_username_via_guild(self):
        target = _fake_user(uid=7, name="DrSonic", avatar_url="https://cdn/d.png")
        guild = MagicMock()
        guild.get_member_named.return_value = target
        message = SimpleNamespace(mentions=[], author=_fake_user(uid=1, name="me", avatar_url="https://cdn/me.png"), guild=guild)
        picked = discord_assets.pick_user_from_message(message, username="DrSonic")
        assert picked is target
        guild.get_member_named.assert_called_once_with("DrSonic")

    def test_falls_back_to_author_when_no_mentions(self):
        author = _fake_user(uid=5, name="self", avatar_url="https://cdn/self.png")
        message = SimpleNamespace(mentions=[], author=author, guild=None)
        assert discord_assets.pick_user_from_message(message) is author


class TestGetTopServerEmoji:
    def test_returns_highest_usage_emoji(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "groksito_discord.utils.emoji_registry._KNOWLEDGE_FILE",
            tmp_path / "emoji_knowledge.json",
        )
        monkeypatch.setattr("groksito_discord.utils.emoji_registry._LOADED", False)
        monkeypatch.setattr(
            "groksito_discord.utils.emoji_registry._EMOJI_KNOWLEDGE",
            {
                "version": 1,
                "guilds": {
                    "111": {
                        "last_scanned": 0,
                        "emojis": {
                            "1": {"id": "1", "name": "chill", "url": "https://cdn/e1.webp", "usage_count": 3},
                            "2": {"id": "2", "name": "jaja", "url": "https://cdn/e2.webp", "usage_count": 10},
                        },
                    }
                },
            },
        )
        monkeypatch.setattr("groksito_discord.utils.emoji_registry._LOADED", True)

        result = discord_assets.get_top_server_emoji_record("111", rank=1)
        assert result["ok"] is True
        assert result["name"] == "jaja"
        assert result["url"] == "https://cdn/e2.webp"
        assert result["usage_count"] == 10

    def test_second_rank_returns_runner_up(self, monkeypatch):
        monkeypatch.setattr("groksito_discord.utils.emoji_registry._LOADED", True)
        monkeypatch.setattr(
            "groksito_discord.utils.emoji_registry._EMOJI_KNOWLEDGE",
            {
                "version": 1,
                "guilds": {
                    "111": {
                        "emojis": {
                            "1": {"id": "1", "name": "a", "url": "https://cdn/a.webp", "usage_count": 1},
                            "2": {"id": "2", "name": "b", "url": "https://cdn/b.webp", "usage_count": 5},
                        }
                    }
                },
            },
        )
        result = discord_assets.get_top_server_emoji_record("111", rank=2)
        assert result["ok"] is True
        assert result["name"] == "a"


class TestResolveUserAvatar:
    @pytest.mark.asyncio
    async def test_resolve_returns_avatar_and_appends_reference(self):
        user = _fake_user(uid=9, name="waifu", avatar_url="https://cdn.discordapp.com/avatars/9/a.png")
        message = SimpleNamespace(mentions=[user], author=user, guild=None, _state=SimpleNamespace(client=AsyncMock()))
        refs: list[str] = []

        result = await discord_assets.resolve_user_avatar(message, image_urls=refs)

        assert result["ok"] is True
        assert "avatars/9" in result["avatar_url"]
        assert refs == [result["avatar_url"]]

    @pytest.mark.asyncio
    async def test_resolve_fetches_user_when_not_in_guild(self):
        fetched = _fake_user(uid=88, name="remote", avatar_url="https://cdn.discordapp.com/avatars/88/r.png")
        client = AsyncMock()
        client.fetch_user.return_value = fetched
        message = SimpleNamespace(
            mentions=[],
            author=_fake_user(uid=1, name="me", avatar_url="https://cdn/me.png"),
            guild=SimpleNamespace(get_member=MagicMock(return_value=None)),
            _state=SimpleNamespace(client=client),
        )

        result = await discord_assets.resolve_user_avatar(message, user_id="88")

        assert result["ok"] is True
        assert result["user_id"] == 88
        client.fetch_user.assert_awaited_once_with(88)


class TestToolExecution:
    @pytest.mark.asyncio
    async def test_execute_get_user_avatar_tool(self, monkeypatch):
        from groksito_discord.llm.tools import execute_hybrid_tool

        user = _fake_user(uid=3, name="hero", avatar_url="https://cdn.discordapp.com/avatars/3/h.png")
        message = SimpleNamespace(
            mentions=[user],
            author=user,
            guild=SimpleNamespace(id=999),
            _state=SimpleNamespace(client=AsyncMock()),
        )
        refs: list[str] = []

        out = await execute_hybrid_tool("get_user_avatar", {}, original_message=message, image_urls=refs)

        assert "avatar_url" in out.lower() or "https://" in out
        assert len(refs) == 1

    @pytest.mark.asyncio
    async def test_execute_get_top_server_emoji_tool(self, monkeypatch):
        from groksito_discord.llm.tools import execute_hybrid_tool

        monkeypatch.setattr(
            "groksito_discord.core.discord_assets.get_top_server_emoji_record",
            lambda gid, guild_obj=None, rank=1: {
                "ok": True,
                "name": "groksito",
                "url": "https://cdn.discordapp.com/emojis/1.webp",
                "usage_count": 42,
                "rank": 1,
            },
        )
        message = SimpleNamespace(guild=SimpleNamespace(id=123))
        refs: list[str] = []

        out = await execute_hybrid_tool("get_top_server_emoji", {}, original_message=message, image_urls=refs)

        assert "groksito" in out
        assert refs == ["https://cdn.discordapp.com/emojis/1.webp"]