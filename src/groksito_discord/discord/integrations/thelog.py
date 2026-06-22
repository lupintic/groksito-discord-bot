"""
TheLog (thelog.co.kr) Korea PC bang game ranking integration.

Provides the top games from 게임순위 (전체) using the site's own JSON endpoint.
Used exclusively by the /topkorea slash command.

Added 2026-06-22 per design docs/superpowers/specs/2026-06-22-topkorea-korea-game-ranking-design.md
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

THELOG_RANK_URL = "https://www.thelog.co.kr/api/common/getCommonState.do"
CACHE_TTL_SECONDS = 90
_game_rank_cache: dict[str, Any] = {"data": None, "ts": 0.0}


async def get_top_korea_rank(limit: int = 10) -> list[dict[str, Any]]:
    """Fetch top N from TheLog 게임순위 (전체) using gameDataType=S (actual data).

    Returns list of dicts with both original Korean + English display fields:
      {"rank": int, "name": str, "english_name": str,
       "shares": float, "publisher": str, "english_publisher": str,
       "change": int, "raw": dict}

    English translation:
    - Known games → shown in English (e.g. League of Legends)
    - New / unknown games that appear in the future → fallback to original Korean name
      (we log at debug level so we can easily add the translation later)
    """
    if limit < 1:
        limit = 10
    limit = min(limit, 20)

    now = time.time()
    if _game_rank_cache["data"] and (now - _game_rank_cache["ts"] < CACHE_TTL_SECONDS):
        cached = _game_rank_cache["data"]
        return cached[:limit]

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Referer": "https://www.thelog.co.kr/index.do",
    }

    try:
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=True, headers=headers) as client:
            resp = await client.get(THELOG_RANK_URL, params={"gameDataType": "S"})
            if resp.status_code != 200:
                logger.warning("[TheLog] bad status %s", resp.status_code)
                return []
            payload = resp.json()
    except Exception as exc:
        logger.warning("[TheLog] fetch failed: %s", exc)
        return []

    if payload.get("returnCode") != "SUCCESS":
        return []

    raw_list = payload.get("gameRank") or []
    normalized: list[dict[str, Any]] = []
    for item in raw_list:
        try:
            korean_name = str(item.get("gameName") or "").strip()
            korean_publisher = str(item.get("publisher") or item.get("devCom") or "").strip()

            normalized.append({
                "rank": int(item.get("gameRank") or 0),
                "name": korean_name,                       # original Korean
                "english_name": get_english_name(korean_name),
                "shares": float(item.get("gameShares") or 0.0),
                "publisher": korean_publisher,             # original
                "english_publisher": get_english_publisher(korean_publisher),
                "change": int(item.get("gameRankUpDown") or 0),
                "raw": item,
            })
        except Exception:
            continue

    # Defensive sort
    normalized.sort(key=lambda x: x["rank"])

    _game_rank_cache["data"] = normalized
    _game_rank_cache["ts"] = now
    return normalized[:limit]


def format_rank_change(delta: int) -> str:
    """Return colored indicator for rank change.

    Green for up (🟢▲), red for down (🔴▼), white for no change.
    Used so the arrows + numbers stand out with color in Discord.
    """
    if delta > 0:
        return f"🟢▲{delta}"
    if delta < 0:
        return f"🔴▼{abs(delta)}"
    return "⚪="


# ------------------------------------------------------------------
# English translations for display in /topkorea embed
# (User requested the command info shown entirely in English)
#
# IMPORTANT BEHAVIOR:
# - Games and publishers that exist in these dictionaries will be shown in English.
# - If a new game enters the top 10 that we haven't added yet, it will fall back
#   to the original Korean name (this is intentional and accurate).
# - To support new games in the future, just add them to the dictionaries below.
# ------------------------------------------------------------------

GAME_NAME_TRANSLATIONS: dict[str, str] = {
    # Current top / very frequent
    "리그 오브 레전드": "League of Legends",
    "메이플스토리": "MapleStory",
    "배틀그라운드": "PUBG: BATTLEGROUNDS",
    "발로란트": "VALORANT",
    "FC 온라인": "FC Online",
    "리니지 클래식": "Lineage Classic",
    "오버워치": "Overwatch",
    "서든어택": "Sudden Attack",
    "로블록스": "Roblox",
    "메이플스토리 월드": "MapleStory Worlds",

    # Other very common Korean PC Bang games (to handle future rotations)
    "던전앤파이터": "Dungeon Fighter Online",
    "던전 앤 파이터": "Dungeon Fighter Online",
    "로스트아크": "Lost Ark",
    "리니지": "Lineage",
    "리니지2": "Lineage 2",
    "아이온": "Aion",
    "검은사막": "Black Desert",
    "검은 사막": "Black Desert",
    "카트라이더": "KartRider",
    "크레이지 아케이드": "Crazy Arcade",
    "사이퍼즈": "Cyphers",
    "테라": "TERA",
    "아키에이지": "ArcheAge",
    "트라하": "Truah",
    "에어": "A.V.A",  # Alliance of Valiant Arms variant sometimes listed differently
}

PUBLISHER_TRANSLATIONS: dict[str, str] = {
    "라이엇게임즈": "Riot Games",
    "넥슨": "Nexon",
    "엔씨소프트": "NCSoft",
    "블리자드": "Blizzard",
    "로블록스": "Roblox",
    "펍지 주식회사": "PUBG Corporation",
    "카카오게임즈": "Kakao Games",
    "카카오 / 스팀": "Kakao / Steam",
    "카카오/스팀": "Kakao / Steam",
    "스마일게이트": "Smilegate",
    "크래프톤": "Krafton",
    "스팀": "Steam",
}


def get_english_name(korean_name: str) -> str:
    """Return common English name for the game.
    
    Falls back to the original Korean name if we don't have a translation yet.
    This happens when a brand new game enters the ranking.
    """
    name = korean_name.strip()
    if name in GAME_NAME_TRANSLATIONS:
        return GAME_NAME_TRANSLATIONS[name]
    # Log once so we know when we need to add a new translation
    logger.debug("[TheLog] Unknown game name, using original Korean: %s", name)
    return name


def get_english_publisher(korean_publisher: str) -> str:
    """Return common English publisher name.
    
    Falls back to original if unknown.
    """
    pub = korean_publisher.strip()
    if pub in PUBLISHER_TRANSLATIONS:
        return PUBLISHER_TRANSLATIONS[pub]
    logger.debug("[TheLog] Unknown publisher, using original: %s", pub)
    return pub
