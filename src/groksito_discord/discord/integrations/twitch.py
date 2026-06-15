"""
Twitch Helix integration for Groksito.

Powers /versus with live viewer counts and stream stats per game category.
Requires TWITCH_CLIENT_ID + TWITCH_CLIENT_SECRET (client credentials flow).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx
from rapidfuzz import fuzz, process

from .steam import normalize_game_name_for_lookup

logger = logging.getLogger(__name__)

TWITCH_API_BASE = "https://api.twitch.tv/helix"
TWITCH_AUTH_URL = "https://id.twitch.tv/oauth2/token"
FUZZY_MATCH_THRESHOLD = 75
_MAX_CONCURRENT_TWITCH_FETCHES = 4

_token_cache: dict[str, Any] = {"access_token": None, "expires_at": 0.0}
_token_lock = asyncio.Lock()
_fetch_semaphore = asyncio.Semaphore(_MAX_CONCURRENT_TWITCH_FETCHES)


def _get_twitch_credentials() -> tuple[str | None, str | None]:
    try:
        from ...config import settings

        client_id = getattr(settings, "twitch_client_id", None)
        client_secret = getattr(settings, "twitch_client_secret", None)
        if client_id and client_secret:
            return str(client_id).strip(), str(client_secret).strip()
    except Exception:
        pass
    return None, None


def is_twitch_configured() -> bool:
    client_id, client_secret = _get_twitch_credentials()
    return bool(client_id and client_secret)


async def _get_app_access_token() -> str | None:
    """Obtain (and cache) a Twitch app access token via client credentials."""
    client_id, client_secret = _get_twitch_credentials()
    if not client_id or not client_secret:
        return None

    async with _token_lock:
        now = time.time()
        cached = _token_cache.get("access_token")
        expires_at = float(_token_cache.get("expires_at") or 0)
        if cached and now < expires_at - 60:
            return cached

        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                resp = await client.post(
                    TWITCH_AUTH_URL,
                    params={
                        "client_id": client_id,
                        "client_secret": client_secret,
                        "grant_type": "client_credentials",
                    },
                )
                if resp.status_code != 200:
                    logger.debug("[Twitch] Token request failed: %s", resp.status_code)
                    return None
                data = resp.json()
        except Exception as exc:
            logger.debug("[Twitch] Token request error: %s", exc)
            return None

        token = data.get("access_token")
        expires_in = int(data.get("expires_in") or 3600)
        if not token:
            return None

        _token_cache["access_token"] = token
        _token_cache["expires_at"] = now + expires_in
        return token


async def _twitch_get(
    path: str,
    *,
    params: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Authenticated GET against the Twitch Helix API."""
    client_id, _ = _get_twitch_credentials()
    token = await _get_app_access_token()
    if not client_id or not token:
        return None

    headers = {
        "Client-Id": client_id,
        "Authorization": f"Bearer {token}",
    }
    url = f"{TWITCH_API_BASE}{path}"

    async with _fetch_semaphore:
        try:
            async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
                resp = await client.get(url, headers=headers, params=params or {})
                if resp.status_code == 401:
                    _token_cache["access_token"] = None
                    _token_cache["expires_at"] = 0.0
                    return None
                if resp.status_code != 200:
                    logger.debug("[Twitch] %s returned %s", path, resp.status_code)
                    return None
                return resp.json()
        except Exception as exc:
            logger.debug("[Twitch] Request failed for %s: %s", path, exc)
            return None


def _pick_best_category_match(query: str, categories: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not categories:
        return None
    names = [(c.get("name") or "").strip() for c in categories]
    names = [n for n in names if n]
    if not names:
        return None

    lookup_name = normalize_game_name_for_lookup(query)
    for cat in categories:
        if (cat.get("name") or "").strip().lower() == lookup_name.lower():
            return cat

    result = process.extractOne(
        lookup_name,
        names,
        scorer=fuzz.token_set_ratio,
        score_cutoff=FUZZY_MATCH_THRESHOLD,
    )
    if not result:
        return None
    matched_name, _score, idx = result
    return categories[idx]


async def _resolve_twitch_category(term: str) -> dict[str, Any] | None:
    """Resolve a game name to a Twitch category (id, name, box_art_url)."""
    lookup = normalize_game_name_for_lookup(term)
    if not lookup:
        return None

    data = await _twitch_get("/games", params={"name": lookup})
    games = (data or {}).get("data") or []
    if games:
        game = games[0]
        return {
            "id": game.get("id"),
            "name": game.get("name") or lookup,
            "box_art_url": game.get("box_art_url"),
        }

    search = await _twitch_get("/search/categories", params={"query": lookup, "first": 10})
    categories = (search or {}).get("data") or []
    picked = _pick_best_category_match(lookup, categories)
    if picked:
        return {
            "id": picked.get("id"),
            "name": picked.get("name") or lookup,
            "box_art_url": picked.get("box_art_url"),
        }
    return None


async def _fetch_stream_stats(game_id: str) -> dict[str, int]:
    """Aggregate live stream viewer counts for a Twitch game category."""
    total_viewers = 0
    live_streams = 0
    cursor: str | None = None

    while True:
        params: dict[str, Any] = {"game_id": game_id, "first": 100}
        if cursor:
            params["after"] = cursor

        data = await _twitch_get("/streams", params=params)
        if not data:
            break

        streams = data.get("data") or []
        for stream in streams:
            viewers = stream.get("viewer_count")
            if isinstance(viewers, int) and viewers >= 0:
                total_viewers += viewers
            live_streams += 1

        cursor = (data.get("pagination") or {}).get("cursor")
        if not cursor or not streams:
            break

    return {"viewer_count": total_viewers, "live_streams": live_streams}


def _format_box_art_url(url: str | None) -> str | None:
    if not url:
        return None
    return url.replace("{width}", "285").replace("{height}", "380")


async def get_twitch_game_data(game_name: str) -> dict[str, Any]:
    """Resolve a game and return Twitch live stats.

    Result keys:
      - original_name
      - matched_name (None if not found)
      - game_id
      - viewer_count
      - live_streams
      - image_url (box art)
      - configured (whether Twitch credentials are set)
      - found (whether the category was resolved)
    """
    original = (game_name or "").strip()
    base = {
        "original_name": original,
        "matched_name": None,
        "game_id": None,
        "viewer_count": None,
        "live_streams": None,
        "image_url": None,
        "configured": is_twitch_configured(),
        "found": False,
    }
    if not original:
        return base

    if not base["configured"]:
        return base

    category = await _resolve_twitch_category(original)
    if not category or not category.get("id"):
        return base

    stats = await _fetch_stream_stats(str(category["id"]))
    return {
        "original_name": original,
        "matched_name": category.get("name"),
        "game_id": category.get("id"),
        "viewer_count": stats.get("viewer_count", 0),
        "live_streams": stats.get("live_streams", 0),
        "image_url": _format_box_art_url(category.get("box_art_url")),
        "configured": True,
        "found": True,
    }


async def get_twitch_game_data_batch(game_names: list[str]) -> list[dict[str, Any]]:
    """Fetch Twitch stats for multiple games concurrently."""
    if not game_names:
        return []
    tasks = [get_twitch_game_data(name) for name in game_names]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    out: list[dict[str, Any]] = []
    for item in results:
        if isinstance(item, dict):
            out.append(item)
    return out