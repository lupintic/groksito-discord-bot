"""
Gamemeca (gamemeca.com) Weekly Popularity Ranking integration.

Provides the top 50 from the main "인기 게임 순위" (weekly) using the site's
server-rendered HTML table at https://www.gamemeca.com/ranking.php

Used exclusively by the /korea50 slash command.

No public JSON API like TheLog was found (confirmed via probes); we parse
the authoritative weekly table. Added 2026-06-22 per project-orchestrator plan.
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

GAMEMECA_RANK_URL = "https://www.gamemeca.com/ranking.php"
CACHE_TTL_SECONDS = 300  # 5 minutes (data is weekly, but we stay fresh)
_game_rank_cache: dict[str, Any] = {"data": None, "ts": 0.0, "week": None}

DATA_FILE: Path = Path("data") / "gamemeca_ranking.json"


def _load_from_json() -> tuple[str | None, list[dict[str, Any]]]:
    """Load previously saved ranking from disk. Returns (week, ranking_list)."""
    if not DATA_FILE.exists():
        return None, []
    try:
        with open(DATA_FILE, encoding="utf-8") as f:
            payload = json.load(f)
        return payload.get("week"), payload.get("ranking", [])
    except Exception as exc:
        logger.warning("[Gamemeca] failed to load %s: %s", DATA_FILE, exc)
        return None, []


def _save_to_json(week: str | None, ranking: list[dict[str, Any]]) -> None:
    """Persist ranking to JSON so /korea50 can avoid live scraping."""
    try:
        DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "week": week,
            "updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "ranking": ranking,
        }
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        logger.info("[Gamemeca] saved ranking for week %s (%d items)", week, len(ranking))
    except Exception as exc:
        logger.warning("[Gamemeca] failed to save %s: %s", DATA_FILE, exc)


async def refresh_ranking() -> list[dict[str, Any]]:
    """Force a live scrape + parse + save to JSON. Intended for weekly background job."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml",
        "Referer": "https://www.gamemeca.com/",
    }

    try:
        async with httpx.AsyncClient(timeout=12.0, follow_redirects=True, headers=headers) as client:
            resp = await client.get(GAMEMECA_RANK_URL)
            if resp.status_code != 200:
                logger.warning("[Gamemeca] refresh bad status %s", resp.status_code)
                return []
            html = resp.text
    except Exception as exc:
        logger.warning("[Gamemeca] refresh fetch failed: %s", exc)
        return []

    week, raw_ranking = _parse_ranking(html)
    if not raw_ranking:
        return []

    normalized: list[dict[str, Any]] = []
    for item in raw_ranking:
        try:
            kname = item.get("name", "").strip()
            kpub = item.get("publisher", "").strip()
            normalized.append({
                "rank": int(item.get("rank", 0)),
                "name": kname,
                "english_name": get_english_name(kname),
                "publisher": kpub,
                "english_publisher": get_english_publisher(kpub),
                "genre": item.get("genre", ""),
                "model": item.get("model", ""),
                "change": int(item.get("change", 0)),
                "raw": item.get("raw", item),
            })
        except Exception:
            continue

    _save_to_json(week, normalized)
    _game_rank_cache["data"] = normalized
    _game_rank_cache["ts"] = time.time()
    _game_rank_cache["week"] = week
    return normalized


async def get_korea_weekly_ranking(limit: int = 50) -> list[dict[str, Any]]:
    """Fetch top N from Gamemeca 주간 인기순위 (weekly popularity ranking).

    Returns list of dicts:
      {"rank": int, "name": str, "english_name": str,
       "publisher": str, "english_publisher": str,
       "genre": str, "model": str,
       "change": int, "raw": dict}

    English translation:
    - Known games → shown in English (reuses/extends TheLog mappings where possible)
    - Unknown → fall back to original Korean name
    """
    if limit < 1:
        limit = 50
    limit = min(limit, 50)

    # Prefer persisted JSON to avoid live scraping on every /korea50 call (rate limit / block protection)
    week_json, data_json = _load_from_json()
    if data_json:
        normalized: list[dict[str, Any]] = []
        for item in data_json[:limit]:
            try:
                kname = item.get("name", "").strip()
                kpub = item.get("publisher", "").strip()
                normalized.append({
                    "rank": int(item.get("rank", 0)),
                    "name": kname,
                    "english_name": get_english_name(kname),
                    "publisher": kpub,
                    "english_publisher": get_english_publisher(kpub),
                    "genre": item.get("genre", ""),
                    "model": item.get("model", ""),
                    "change": int(item.get("change", 0)),
                    "raw": item.get("raw", item),
                })
            except Exception:
                continue
        return normalized

    # Fallback to live (will also persist)
    now = time.time()
    if (
        _game_rank_cache["data"]
        and (now - _game_rank_cache["ts"] < CACHE_TTL_SECONDS)
    ):
        cached = _game_rank_cache["data"]
        return cached[:limit]

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml",
        "Referer": "https://www.gamemeca.com/",
    }

    try:
        async with httpx.AsyncClient(timeout=12.0, follow_redirects=True, headers=headers) as client:
            resp = await client.get(GAMEMECA_RANK_URL)
            if resp.status_code != 200:
                logger.warning("[Gamemeca] bad status %s", resp.status_code)
                return []
            html = resp.text
    except Exception as exc:
        logger.warning("[Gamemeca] fetch failed: %s", exc)
        return []

    week, ranking = _parse_ranking(html)
    if not ranking:
        return []

    normalized: list[dict[str, Any]] = []
    for item in ranking[:limit]:
        try:
            korean_name = item.get("name", "").strip()
            korean_pub = item.get("publisher", "").strip()
            normalized.append({
                "rank": int(item.get("rank", 0)),
                "name": korean_name,
                "english_name": get_english_name(korean_name),
                "publisher": korean_pub,
                "english_publisher": get_english_publisher(korean_pub),
                "genre": item.get("genre", ""),
                "model": item.get("model", ""),
                "change": int(item.get("change", 0)),
                "raw": item.get("raw", item),
            })
        except Exception:
            continue

    _game_rank_cache["data"] = normalized
    _game_rank_cache["ts"] = now
    _game_rank_cache["week"] = week

    # Persist for future command calls
    _save_to_json(week, normalized)
    return normalized[:limit]


def _parse_ranking(html: str) -> tuple[str | None, list[dict[str, Any]]]:
    """Parse the actual <table class="ranking-table"> rows.

    This captures the real up/down change data that is missing from JSON-LD.
    Structure observed:
      <span class="rank">CURRENT</span>
      <span class="rankChange"><span class="ranking-static-img ranking-static-up"></span>DELTA</span>

    Also extracts name, pub, genre, model.
    """
    week = None
    wm = re.search(r"(\d{4}\.\d{2}\.\d{2}~\d{2}\.\d{2})", html)
    if wm:
        week = wm.group(1)

    items = []

    # Find all table rows
    rows = re.findall(r'<tr class="ranking-table-rows".*?</tr>', html, re.DOTALL | re.I)

    for row in rows:
        # Current rank
        rank_m = re.search(r'<span class="rank[^"]*">(\d+)</span>', row)
        if not rank_m:
            continue
        rank = int(rank_m.group(1))

        # Change delta (the number after the arrow span)
        change = 0
        ch_m = re.search(
            r'class="rankChange"[^>]*>\s*<span class="[^"]*ranking-static-(up|down)[^"]*"></span>\s*(\d+)',
            row, re.DOTALL | re.I
        )
        if ch_m:
            direction = ch_m.group(1).lower()
            delta = int(ch_m.group(2))
            change = delta if direction == 'up' else -delta
        else:
            # Sometimes the number is directly in rankChange without inner span
            ch2 = re.search(r'class="rankChange"[^>]*>.*?(\d+)', row, re.DOTALL)
            if ch2:
                # Can't determine direction without class, leave as positive or check class separately
                pass

        # Name
        name_m = re.search(r'<a href="/game.php\?rts=gmview[^"]*">([^<]+)</a>', row)
        name = name_m.group(1).strip() if name_m else ""

        # Publisher
        pub_m = re.search(r'<a href="/game.php\?rts=company[^"]*">([^<]+)</a>', row)
        pub = pub_m.group(1).strip() if pub_m else ""

        # Genre and model - they appear as last <span> elements in the game-info div
        gms = re.findall(r'<span>([^<]+)</span>', row)
        genre = ""
        model = ""
        if len(gms) >= 2:
            genre = gms[-2].strip()
            model = gms[-1].strip()

        if not name:
            continue

        items.append({
            "rank": rank,
            "name": name,
            "publisher": pub,
            "genre": genre,
            "model": model,
            "change": change,
            "raw": {"name": name, "pub": pub, "genre": genre, "model": model},
        })

    if len(items) < 40:
        logger.warning("[Gamemeca] table parser only found %d items", len(items))
        return week, []

    return week, items


def format_rank_change(delta: int) -> str:
    """Return indicator for rank change (same style as TheLog)."""
    if delta > 0:
        return f"🟢▲{delta}"
    if delta < 0:
        return f"🔴▼{abs(delta)}"
    return "⚪="


# ------------------------------------------------------------------
# English translations (extended from thelog.py where games overlap)
# ------------------------------------------------------------------

GAME_NAME_TRANSLATIONS: dict[str, str] = {
    # Core popular titles
    "리그 오브 레전드": "League of Legends",
    "스페셜포스": "Special Force",
    "메이플스토리": "MapleStory",
    "메이플스토리 월드": "MapleStory Worlds",
    "배틀그라운드": "PUBG: BATTLEGROUNDS",
    "플레이어언노운스 배틀그라운드": "PUBG: BATTLEGROUNDS",
    "발로란트": "VALORANT",
    "FC 온라인": "FC Online",
    "리니지": "Lineage",
    "리니지 2": "Lineage 2",
    "리니지M": "Lineage M",
    "오버워치": "Overwatch",
    "오버워치(오버워치 2)": "Overwatch 2",
    "서든어택": "Sudden Attack",
    "로블록스": "Roblox",
    "던전앤파이터": "Dungeon Fighter Online",
    "로스트아크": "Lost Ark",
    "아이온": "Aion",
    "아이온2": "Aion 2",
    "검은사막": "Black Desert",
    "크레이지 아케이드": "Crazy Arcade",
    "크레이지 아케이드 비엔비": "Crazy Arcade BnB",
    "사이퍼즈": "Cyphers",
    "테일즈런너": "TalesRunner",
    "스타크래프트": "StarCraft",
    "스타크래프트 2: 자유의 날개": "StarCraft 2",
    "카운터 스트라이크 2": "Counter-Strike 2",
    "카운터 스트라이크 온라인": "Counter-Strike Online",
    "디아블로 2": "Diablo II",
    "디아블로 4": "Diablo IV",
    "월드 오브 워크래프트": "World of Warcraft",
    "파이널 판타지 14": "Final Fantasy XIV",
    "마인크래프트": "Minecraft",
    "도타 2": "Dota 2",
    "에이펙스 레전드": "Apex Legends",
    "팰월드": "Palworld",
    "원신": "Genshin Impact",
    "블레이드앤소울": "Blade & Soul",
    "뮤 온라인": "MU Online",
    "천하제일상 거상": "The First Hero: Geosang",
    "마비노기 모바일": "Mabinogi Mobile",
    "패스 오브 엑자일 2": "Path of Exile 2",
    "명조: 워더링 웨이브": "Wuthering Waves",
    "이터널 리턴": "Eternal Return",
    "솔: 인챈트": "Sol: Enchant",
    "워크래프트 3": "Warcraft 3",
    "이환": "Nexon",
    "멧챠 카멜레온": "Metcha Chameleon",
    "임진왜란: 조선의 반격": "Imjin War: Joseon's Counterattack",
    "성세천하: 여제의 탄생 2": "Seongse Cheonha",
    "델타 포스": "Delta Force",
    "아크 레이더스": "Ark Raiders",
    "붉은사막": "Red Desert",
}

PUBLISHER_TRANSLATIONS: dict[str, str] = {
    "라이엇게임즈": "Riot Games",
    "라이엇 게임즈": "Riot Games",
    "그라인딩 기어 게임즈": "Grinding Gear Games",
    "모장": "Mojang",
    "쿠로 게임즈": "Kuro Games",
    "님블뉴런": "Nimble Neuron",
    "드래곤플라이": "Dragonfly",
    "엠바크 스튜디오": "Embark Studios",
    "리스폰엔터테인먼트": "Respawn Entertainment",
    "팀 제이드": "Team Jade",
    "포켓페어": "Pocket Pair",
    "스퀘어에닉스": "Square Enix",
    "에이케이인터랙티브": "AK Interactive",
    "넥슨": "Nexon",
    "엔씨소프트": "NCSoft",
    "블리자드": "Blizzard",
    "크래프톤": "Krafton",
    "스마일게이트": "Smilegate",
    "스마일게이트 RPG": "Smilegate RPG",
    "펄어비스": "Pearl Abyss",
    "네오플": "Neople",
    "위젯스튜디오": "Wizet Studio",
    "카카오게임즈": "Kakao Games",
    "로블록스 코퍼레이션": "Roblox Corporation",
    "웹젠": "Webzen",
    "밸브": "Valve",
    "EA코리아 스튜디오": "EA Korea",
    "넥슨지티": "Nexon GT",
    "데브캣스튜디오": "DevCat Studio",
    "라온엔터테인먼트": "Raon Entertainment",
    "알트나인": "AltNine",
}


def get_english_name(korean_name: str) -> str:
    name = korean_name.strip()
    if name in GAME_NAME_TRANSLATIONS:
        return GAME_NAME_TRANSLATIONS[name]
    logger.debug("[Gamemeca] Unknown game name, using original Korean: %s", name)
    return name


def get_english_publisher(korean_publisher: str) -> str:
    pub = korean_publisher.strip()
    if pub in PUBLISHER_TRANSLATIONS:
        return PUBLISHER_TRANSLATIONS[pub]
    logger.debug("[Gamemeca] Unknown publisher, using original: %s", pub)
    return pub
