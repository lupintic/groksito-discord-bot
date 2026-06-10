"""
Steam Charts integration for Groksito.

Provides player count data (current + peaks) and thumbnail resolution for
the /stmchr and /steamchart slash commands.

This module was extracted during Phase 1 of the professional refactoring
("Extract Steam Integration + Client Hygiene").

All logic, scraping strategies, headers, regexes, fallback behavior, and
data structures are preserved **exactly** as they were in client.py.
No behavioral changes of any kind.

The Discord-specific command registration and presentation (embeds, messages,
guild checks, defer/followup) remain in client.py. This module only owns
the external data fetching and game resolution concerns.
"""

from __future__ import annotations

import difflib
import re

import httpx


# =============================================================================
# Steam Charts helpers (simple, self-contained, no extra deps)
# =============================================================================

# Known games for /stmchr (fixed list) and /steamchart lookup.
# Keys are lowercased for matching; values are (display_name, app_id)
# Expanded with popular Steam games so that /steamchart "dota 2", "cs2", etc. work
# without requiring exact names. Many aliases included for forgiveness.
STEAM_GAMES: dict[str, tuple[str, int]] = {
    # === Core curated list (for /stmchr and common /steamchart) ===
    # Black Desert
    "black desert": ("Black Desert", 582660),
    "black desert online": ("Black Desert", 582660),
    "bdo": ("Black Desert", 582660),
    # Where Winds Meet
    "where winds meet": ("Where Winds Meet", 3564740),
    "winds meet": ("Where Winds Meet", 3564740),
    "wwm": ("Where Winds Meet", 3564740),
    # Lost Ark
    "lost ark": ("Lost Ark", 1599340),
    # Path of Exile 2
    "path of exile 2": ("Path of Exile 2", 2694490),
    "path of exile2": ("Path of Exile 2", 2694490),
    "poe 2": ("Path of Exile 2", 2694490),
    "poe2": ("Path of Exile 2", 2694490),
    # Guild Wars 2
    "guild wars 2": ("Guild Wars 2", 1284210),
    "guild wars2": ("Guild Wars 2", 1284210),
    "gw2": ("Guild Wars 2", 1284210),
    # Crimson Desert
    "crimson desert": ("Crimson Desert", 3321460),
    # Throne and Liberty
    "throne and liberty": ("Throne and Liberty", 2429640),
    "throne liberty": ("Throne and Liberty", 2429640),
    "tal": ("Throne and Liberty", 2429640),
    # TBH: Task Bar Hero
    "task bar hero": ("TBH: Task Bar Hero", 3678970),
    "taskbar hero": ("TBH: Task Bar Hero", 3678970),
    "tbh": ("TBH: Task Bar Hero", 3678970),
    "tbh: task bar hero": ("TBH: Task Bar Hero", 3678970),

    # === Popular games frequently requested on Steam Charts ===
    # Counter-Strike 2 (very common request)
    "counter strike 2": ("Counter-Strike 2", 730),
    "counter-strike 2": ("Counter-Strike 2", 730),
    "counterstrike 2": ("Counter-Strike 2", 730),
    "cs2": ("Counter-Strike 2", 730),
    "cs 2": ("Counter-Strike 2", 730),
    "counter strike": ("Counter-Strike 2", 730),
    "cs:go": ("Counter-Strike 2", 730),
    "csgo": ("Counter-Strike 2", 730),
    "cs": ("Counter-Strike 2", 730),
    # Dota 2 (very common request, exact name on steamcharts.com)
    "dota 2": ("Dota 2", 570),
    "dota2": ("Dota 2", 570),
    "dota": ("Dota 2", 570),
    # PUBG: BATTLEGROUNDS
    "pubg": ("PUBG: BATTLEGROUNDS", 578080),
    "pubg: battlegrounds": ("PUBG: BATTLEGROUNDS", 578080),
    "pubg battlegrounds": ("PUBG: BATTLEGROUNDS", 578080),
    "playerunknown": ("PUBG: BATTLEGROUNDS", 578080),
    "playerunknown's battlegrounds": ("PUBG: BATTLEGROUNDS", 578080),
    # Apex Legends
    "apex legends": ("Apex Legends", 1172470),
    "apex": ("Apex Legends", 1172470),
    # Grand Theft Auto V
    "grand theft auto v": ("Grand Theft Auto V", 271590),
    "gta v": ("Grand Theft Auto V", 271590),
    "gta5": ("Grand Theft Auto V", 271590),
    "gtav": ("Grand Theft Auto V", 271590),
    "gta": ("Grand Theft Auto V", 271590),
    # Rust
    "rust": ("Rust", 252490),
    # Other frequently seen top games
    "team fortress 2": ("Team Fortress 2", 440),
    "tf2": ("Team Fortress 2", 440),
    "warframe": ("Warframe", 230410),
    "destiny 2": ("Destiny 2", 1085660),
    "helldivers 2": ("Helldivers 2", 553850),
    "elden ring": ("Elden Ring", 1245620),
    "cyberpunk 2077": ("Cyberpunk 2077", 1091500),
    "baldur's gate 3": ("Baldur's Gate 3", 1086940),
    "baldurs gate 3": ("Baldur's Gate 3", 1086940),
    "bg3": ("Baldur's Gate 3", 1086940),
    "war thunder": ("War Thunder", 236390),
    "new world": ("New World", 1063730),
}

# Canonical list for /stmchr (one embed per game). We sort output by current players.
_STMCHR_GAMES: list[tuple[str, int]] = [
    ("Black Desert", 582660),
    ("Where Winds Meet", 3564740),
    ("Lost Ark", 1599340),
    ("Path of Exile 2", 2694490),
    ("Guild Wars 2", 1284210),
    ("Crimson Desert", 3321460),
    ("Throne and Liberty", 2429640),
    ("TBH: Task Bar Hero", 3678970),
]

# Theme colors for /stmchr embeds (makes the list much nicer to scan)
STMCHR_COLORS: dict[str, int] = {
    "Black Desert": 0xA93226,        # dark red / leather
    "Where Winds Meet": 0x16A085,    # teal / jade
    "Lost Ark": 0xE67E22,            # golden orange
    "Path of Exile 2": 0x922B21,     # blood red
    "Guild Wars 2": 0x27AE60,        # vibrant green
    "Crimson Desert": 0xC0392B,      # crimson
    "Throne and Liberty": 0x7D3C98,  # regal purple
    "TBH: Task Bar Hero": 0x00ACC1,  # bright cyan (fun)
    # Common variants that appear in live top lists
    "Apex Legends™": 0xE67E22,
    "Apex Legends": 0xE67E22,
}


def get_game_color(name: str) -> int | None:
    """Return the themed embed color for a game name if known.

    Tries exact match, then strips common trademark symbols for robustness
    (helps with live top lists that sometimes include ™ / ®).
    """
    if not name:
        return None
    if name in STMCHR_COLORS:
        return STMCHR_COLORS[name]
    # Strip common symbols and retry
    cleaned = name.replace("™", "").replace("®", "").strip()
    if cleaned in STMCHR_COLORS:
        return STMCHR_COLORS[cleaned]
    return None


async def _get_current_players_from_steam_api(app_id: int) -> int | None:
    """Fetch current players using Steam's public, unauthenticated API endpoint.

    This endpoint is reliable, returns clean JSON, and is not blocked like
    steamdb.info scraping (which now returns 403 for most bots/scrapers).
    Only provides "right now" count (no peaks), which is sufficient for /stmchr.
    """
    url = f"https://api.steampowered.com/ISteamUserStats/GetNumberOfCurrentPlayers/v1/?appid={app_id}"
    try:
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
            resp = await client.get(url)
            if resp.status_code == 200:
                data = resp.json()
                resp_obj = data.get("response", {})
                if resp_obj.get("result") == 1:
                    pc = resp_obj.get("player_count")
                    if isinstance(pc, int) and pc >= 0:
                        return pc
    except Exception:
        # Non-fatal; caller treats None as "no data for this title"
        pass
    return None


async def _get_steam_player_counts(app_id: int) -> dict[str, int | None]:
    """Internal helper: current players from Steam public API (reliable, primary source)
    + best-effort peaks from SteamDB.info (frequently returns 403; peaks optional).

    Returns: {"current": int|None, "peak24": int|None, "alltime": int|None}
    Uses only httpx + re. Multiple regex strategies for robustness on peaks.
    """
    url = f"https://steamdb.info/app/{app_id}/"

    # Realistic browser headers + retry. SteamDB is aggressive against scrapers
    # (Cloudflare + behavioral detection). We only use it for peaks now; current
    # always comes from the official keyless Steam API (see below).
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Referer": "https://steamdb.info/",
    }

    # Single attempt for peaks (SteamDB is now frequently blocked with 403).
    # We do not rely on it for "current" counts anymore. Keep timeout short to fail fast.
    html = None
    try:
        async with httpx.AsyncClient(timeout=5.0, follow_redirects=True) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 200:
                html = resp.text
    except Exception:
        pass

    current = None
    peak24 = None
    alltime = None

    if html:
        # === Primary patterns from SteamDB "Steam charts" section (publicly visible) ===
        # Examples seen in page:
        #   **18,056** players right now
        #   **20,688** 24-hour peak
        #   **60,395** all-time peak ...
        m = re.search(r'(\d[\d,]*)\s*players right now', html, re.IGNORECASE)
        if m:
            try:
                current = int(m.group(1).replace(",", ""))
            except ValueError:
                pass

        m = re.search(r'(\d[\d,]*)\s*24-hour peak', html, re.IGNORECASE)
        if m:
            try:
                peak24 = int(m.group(1).replace(",", ""))
            except ValueError:
                pass

        m = re.search(r'(\d[\d,]*)\s*all-time peak', html, re.IGNORECASE)
        if m:
            try:
                alltime = int(m.group(1).replace(",", ""))
            except ValueError:
                pass

        # === Alternative phrasing + label-anchored fallback ===
        if current is None:
            m = re.search(r'currently\s*(\d[\d,]*)\s*players live playing', html, re.IGNORECASE)
            if m:
                try:
                    current = int(m.group(1).replace(",", ""))
                except ValueError:
                    pass

        # Walk backwards from known labels (more resilient)
        for label, target in [
            ("players right now", "current"),
            ("24-hour peak", "peak24"),
            ("all-time peak", "alltime"),
        ]:
            idx = html.lower().find(label.lower())
            if idx > 0:
                window = html[max(0, idx - 450):idx]
                matches = re.findall(r'(\d[\d,]{2,})', window)
                if matches:
                    try:
                        val = int(matches[-1].replace(",", ""))
                        if target == "current" and current is None:
                            current = val
                        elif target == "peak24" and peak24 is None:
                            peak24 = val
                        elif target == "alltime" and alltime is None:
                            alltime = val
                    except ValueError:
                        pass

    # Secondary best-effort peaks via steamcharts.com (more permissive than steamdb.info at time of writing).
    # Only used to enrich 24h/all-time peaks for the text /steamchart command; /stmchr only needs current.
    if peak24 is None or alltime is None:
        try:
            sch_url = f"https://steamcharts.com/app/{app_id}"
            # Reuse similar realistic headers
            async with httpx.AsyncClient(timeout=6.0, follow_redirects=True) as client:
                r = await client.get(sch_url, headers=headers)
                if r.status_code == 200:
                    sch_html = r.text
                    if peak24 is None:
                        m = re.search(
                            r'<span[^>]*class=["\']num["\'][^>]*>([\d,]+)</span>\s*<br>\s*24-hour peak',
                            sch_html,
                            re.IGNORECASE,
                        )
                        if m:
                            try:
                                peak24 = int(m.group(1).replace(",", ""))
                            except ValueError:
                                pass
                    if alltime is None:
                        m = re.search(
                            r'<span[^>]*class=["\']num["\'][^>]*>([\d,]+)</span>\s*<br>\s*all-time peak',
                            sch_html,
                            re.IGNORECASE,
                        )
                        if m:
                            try:
                                alltime = int(m.group(1).replace(",", ""))
                            except ValueError:
                                pass
        except Exception:
            pass

    # PRIMARY SOURCE for "current" (right-now concurrent players):
    # Steam's official public API. Never blocked for this, returns exact number quickly.
    # We always prefer/override with it when available. Peaks (if any) come from the scrape(s) above.
    api_current = await _get_current_players_from_steam_api(app_id)
    if api_current is not None:
        current = api_current

    return {"current": current, "peak24": peak24, "alltime": alltime}


async def _resolve_steam_thumb(app_id: int) -> str | None:
    """Resolve a working thumbnail URL for a Steam app.

    Tries common static paths first (fast 404s for most). Falls back to scraping
    the store page for the current (possibly hashed) asset path used by newer titles.

    This fixes broken header.jpg for games like Where Winds Meet and TBH that
    only publish assets under content-addressed subdirectories on Fastly.
    """
    # Fast common patterns (work for the majority of games).
    # Library cover is last because we prefer landscape header/capsule visuals.
    candidates = [
        f"https://shared.fastly.steamstatic.com/store_item_assets/steam/apps/{app_id}/header.jpg",
        f"https://shared.cloudflare.steamstatic.com/store_item_assets/steam/apps/{app_id}/header.jpg",
        f"https://cdn.cloudflare.steamstatic.com/steam/apps/{app_id}/header.jpg",
        f"https://steamcdn-a.akamaihd.net/steam/apps/{app_id}/header.jpg",
        f"https://cdn.cloudflare.steamstatic.com/steam/apps/{app_id}/capsule_616x353.jpg",
        f"https://cdn.cloudflare.steamstatic.com/steam/apps/{app_id}/capsule_231x87.jpg",
        f"https://cdn.cloudflare.steamstatic.com/steam/apps/{app_id}/library_600x900.jpg",
    ]

    static_good: str | None = None
    for url in candidates:
        try:
            async with httpx.AsyncClient(timeout=3.5, follow_redirects=True) as client:
                r = await client.head(url)
                if r.status_code == 200:
                    static_good = url
                    if "library_600" not in url:  # prefer non-tall if possible
                        return url
                    # else continue to see if scrape gives us something nicer
        except Exception:
            continue

    # Slow path: fetch store page and extract a current good asset (handles hashed paths).
    # We prefer header > 616 capsule from the scrape results.
    try:
        store_url = f"https://store.steampowered.com/app/{app_id}/"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        }
        async with httpx.AsyncClient(timeout=7.0, follow_redirects=True, headers=headers) as client:
            page = await client.get(store_url)
            if page.status_code == 200:
                html = page.text
                # header variants first (best for thumbnails), then nice capsules
                patterns = [
                    rf'https?://shared\.(?:fastly|cloudflare)\.steamstatic\.com/store_item_assets/steam/apps/{app_id}/[^"\'<>\s]*header[^"\'<>\s]*\.(?:jpg|png)',
                    rf'https?://shared\.(?:fastly|cloudflare)\.steamstatic\.com/store_item_assets/steam/apps/{app_id}/[^"\'<>\s]*capsule_616[^"\'<>\s]*\.(?:jpg|png)',
                    rf'https?://shared\.(?:fastly|cloudflare)\.steamstatic\.com/store_item_assets/steam/apps/{app_id}/[^"\'<>\s]*capsule_231[^"\'<>\s]*\.(?:jpg|png)',
                ]
                for pat in patterns:
                    for m in re.finditer(pat, html, re.IGNORECASE):
                        candidate = m.group(0).split("?")[0]
                        try:
                            hr = await client.head(candidate)
                            if hr.status_code == 200:
                                return candidate  # scraped header/capsule wins
                        except Exception:
                            continue
    except Exception:
        pass

    # If we only ever found a library cover (or nothing better), return it
    return static_good


async def _fetch_steam_chart(app_id: int, display_name: str) -> str:
    """Fetch current/peak players for a given appid.
    Current is from Steam public API (reliable); peaks best-effort from SteamDB.
    Returns a formatted string (used by /steamchart). Uses _get_steam_player_counts internally.
    """
    data = await _get_steam_player_counts(app_id)
    current = data.get("current")
    if current is None:
        return f"**{display_name}** — conteo de jugadores no encontrado en la página"

    peak24 = data.get("peak24")
    alltime = data.get("alltime")

    line = f"**{display_name}**: {current:,} ahora"
    if peak24:
        line += f" · pico 24h: {peak24:,}"
    if alltime:
        line += f" · all-time: {alltime:,}"
    return line


# In-memory cache for dynamic (search-based) resolutions so repeated queries
# for the same game don't hit the Steam store API every time.
_DYNAMIC_RESOLVE_CACHE: dict[str, tuple[str, int] | None] = {}


def _normalize_name(name: str) -> str:
    """Normalize a game name for robust matching (removes punctuation, collapses spaces)."""
    if not name:
        return ""
    s = name.lower().strip()
    # Replace common separators and punctuation with space
    s = re.sub(r"[:/\\\-–—_.,!?()[\]{}'\"]+", " ", s)
    # Collapse whitespace
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _is_whole_word(needle: str, haystack: str) -> bool:
    """Return True if needle appears as a whole word/token in haystack (not inside another word)."""
    if not needle or not haystack:
        return False
    # Use word boundaries: not preceded or followed by alphanum
    pattern = r'(?i)(?<![a-z0-9])' + re.escape(needle) + r'(?![a-z0-9])'
    return bool(re.search(pattern, haystack))


def _resolve_steam_game_local(term: str) -> tuple[str, int] | None:
    """Fast local resolver using the STEAM_GAMES map + fuzzy matching.

    This is the first stage. If it fails we fall back to dynamic Steam search.
    """
    if not term:
        return None

    raw = term.strip()
    if not raw:
        return None

    key = raw.lower()

    # 1. Direct hit (very fast path, including many aliases we pre-seeded)
    if key in STEAM_GAMES:
        return STEAM_GAMES[key]

    norm = _normalize_name(raw)
    if norm in STEAM_GAMES:
        return STEAM_GAMES[norm]

    # 2. Improved loose contains (both directions).
    # Short aliases (tal, bdo, cs, gw2, poe2, cs2...) are only allowed when they appear
    # as whole words, not as substrings inside English words like 'totally' or 'focus'.
    for k, (disp, aid) in STEAM_GAMES.items():
        # 'user query is inside a known key' is generally safe (e.g. 'poe 2' inside 'path of exile 2')
        if norm and norm in k:
            return (disp, aid)
        # 'known key/alias is inside user text'
        if k in norm or k in key:
            if len(k) >= 4:
                return (disp, aid)
            # Short alias: require it to be a whole word, or the query is basically just the alias
            if _is_whole_word(k, norm) or _is_whole_word(k, key):
                return (disp, aid)
            if len(norm) <= len(k) + 1 or norm.replace(' ', '') == k or key.replace(' ', '') == k:
                return (disp, aid)

    # 3. Fuzzy matching with difflib (catches typos: "dota 2", "counter stike 2", "path of exil 2", "balck desert")
    # We only apply fuzzy for inputs that look like plausible game names (avoid garbage matching random entries).
    if norm and len(norm) >= 3:
        candidates: list[str] = list(STEAM_GAMES.keys())
        disp_map: dict[str, tuple[str, int]] = {}  # normalized display -> (disp, aid)
        for disp, aid in STEAM_GAMES.values():
            n = _normalize_name(disp)
            if n and n not in candidates:
                candidates.append(n)
            if n:
                disp_map[n] = (disp, aid)

        matches = difflib.get_close_matches(norm, candidates, n=1, cutoff=0.82)
        if matches:
            matched = matches[0]
            # Strong guard against garbage: require that some token (>=3 chars) from the *input*
            # appears as a substring inside the matched name, or vice versa.
            # This kills "something totally unknown xyz123" while still allowing real typos.
            input_parts = [p for p in norm.split() if len(p) >= 3]
            matched_lower = matched.lower()
            has_overlap = any(p in matched_lower for p in input_parts) or any(
                p in norm for p in matched_lower.split() if len(p) >= 3
            )
            if not has_overlap:
                # Very short aliases (cs2, poe2, tal, etc.) are handled by the earlier contains logic.
                # If we got here via pure fuzzy with no token overlap, reject it.
                return None

            if matched in STEAM_GAMES:
                return STEAM_GAMES[matched]
            if matched in disp_map:
                return disp_map[matched]

    return None


async def _search_steam_for_app(term: str) -> tuple[str, int] | None:
    """Dynamic fallback using Steam's public store search + Steam Charts search.

    This is specifically designed to handle brand new games that appear on
    steamcharts.com even if they are not yet (or not well) indexed in our local list.
    Strategy:
      1. Try the structured Steam Store search API (good names + AppIDs).
      2. If that fails, scrape Steam Charts own search (https://steamcharts.com/search)
         which is the most direct source for "what is currently tracked on steamcharts".
      3. Validate the candidate by asking the official Steam player count API.
         If we can get a current player number (even 0), the AppID is real and usable.
    """
    q = term.strip()
    if not q or len(q) < 2:
        return None

    cache_key = _normalize_name(q)
    if cache_key in _DYNAMIC_RESOLVE_CACHE:
        return _DYNAMIC_RESOLVE_CACHE[cache_key]

    # --- Strategy 1: Steam Store search API (fast, structured, usually excellent) ---
    store_result = await _search_store_api(q)
    if store_result:
        if await _validate_app_has_players(store_result[1]):
            _cache_resolution(cache_key, store_result)
            return store_result

    # --- Strategy 2: Steam Charts search page (best for games actively on steamcharts.com) ---
    charts_result = await _search_steamcharts_search_page(q)
    if charts_result:
        if await _validate_app_has_players(charts_result[1]):
            _cache_resolution(cache_key, charts_result)
            return charts_result

    # Both failed or validation failed → cache negative and give up
    _DYNAMIC_RESOLVE_CACHE[cache_key] = None
    return None


def _cache_resolution(cache_key: str, result: tuple[str, int]) -> None:
    """Store a successful resolution in cache under several normalizations."""
    _DYNAMIC_RESOLVE_CACHE[cache_key] = result
    _DYNAMIC_RESOLVE_CACHE[_normalize_name(result[0])] = result


async def _search_store_api(term: str) -> tuple[str, int] | None:
    """Steam Store public search API."""
    try:
        async with httpx.AsyncClient(timeout=6.0, follow_redirects=True) as client:
            resp = await client.get(
                "https://store.steampowered.com/api/storesearch/",
                params={"term": term, "l": "english", "cc": "US"},
            )
            if resp.status_code != 200:
                return None
            data = resp.json()
    except Exception:
        return None

    items = data.get("items") or []
    if not items:
        return None

    top = items[0]
    app_id = top.get("id")
    name = top.get("name") or term
    if isinstance(app_id, int) and app_id > 0:
        return (name, app_id)
    return None


async def _search_steamcharts_search_page(term: str) -> tuple[str, int] | None:
    """Scrape https://steamcharts.com/search/?q=... for the top game result.

    This is particularly good for new or trending games that are already
    appearing in Steam Charts even if the store search is slow to index them.
    """
    try:
        async with httpx.AsyncClient(timeout=6.5, follow_redirects=True) as client:
            resp = await client.get(
                "https://steamcharts.com/search/",
                params={"q": term},
            )
            if resp.status_code != 200:
                return None
            html = resp.text
    except Exception:
        return None

    # Steam Charts search results have links like <a href="/app/730">Counter-Strike 2</a>
    # We take the first /app/XXXX that looks like a real game (not dedicated server, etc.).
    # The search is relevance ordered, so the very first app link is usually correct.
    match = re.search(r'href=["\']/app/(\d+)["\']', html)
    if not match:
        return None

    app_id = int(match.group(1))

    # Try to extract a nice name near the link. Look for the text content of the first link.
    # Fallback to the original term if we can't parse a better name.
    name_match = re.search(
        r'href=["\']/app/' + str(app_id) + r'["\'][^>]*>([^<]+)</a>',
        html,
        re.IGNORECASE,
    )
    name = name_match.group(1).strip() if name_match else term

    if app_id > 0:
        return (name, app_id)
    return None


async def _validate_app_has_players(app_id: int) -> bool:
    """Quick validation: can we get a current player count for this AppID?

    Uses the official unauthenticated Steam API. For a game that is live on
    Steam Charts (even brand new), this almost always returns a number quickly.
    Returns True if we got a non-negative integer (including 0 for quiet games).
    """
    current = await _get_current_players_from_steam_api(app_id)
    return current is not None and current >= 0


async def get_top_steam_games(limit: int = 10) -> list[tuple[str, int]]:
    """Fetch the current Top N games by concurrent players from https://steamcharts.com/top.

    Returns a list of (display_name, app_id) in the order they appear on the page
    (highest current players first). This is used by the /topgames slash command.

    We only parse the name + AppID here. Current players are fetched fresh later
    using the official Steam API (same as /stmchr and /steamchart) for consistency.
    """
    if limit < 1:
        limit = 10
    limit = min(limit, 50)  # safety cap

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
    }

    try:
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
            resp = await client.get("https://steamcharts.com/top", headers=headers)
            if resp.status_code != 200:
                return []
            html = resp.text
    except Exception:
        return []

    # Steam Charts top page has rows like:
    # <a href="/app/730">Counter-Strike 2</a>
    # We capture (app_id, name) pairs. The page is ordered by current players.
    pattern = r'<a[^>]+href="/app/(\d+)"[^>]*>([^<]+)</a>'
    matches = re.findall(pattern, html, re.IGNORECASE)

    results: list[tuple[str, int]] = []
    seen_ids: set[int] = set()

    for app_id_str, name in matches:
        try:
            app_id = int(app_id_str)
        except ValueError:
            continue
        if app_id in seen_ids:
            continue
        clean_name = name.strip()
        if not clean_name or app_id <= 0:
            continue
        results.append((clean_name, app_id))
        seen_ids.add(app_id)
        if len(results) >= limit:
            break

    return results


async def _resolve_steam_game(term: str) -> tuple[str, int] | None:
    """Resolve a user-provided game term to (display_name, app_id).

    1. Fast local path: exact match + aliases + fuzzy (difflib) against our curated + popular list.
    2. Dynamic fallback (for new games, obscure titles, etc.):
       - First tries Steam Store search API.
       - Then tries Steam Charts own search page (https://steamcharts.com/search).
       - Validates the AppID by querying Steam's public player count API.

    This combination means that if a game is new and already visible on steamcharts.com,
    we have a very high chance of finding its AppID even if it is not in our static list.
    """
    # Local fast path (exact, contains, fuzzy via difflib)
    local = _resolve_steam_game_local(term)
    if local:
        return local

    # Dynamic search fallback (store + steamcharts search + validation)
    dynamic = await _search_steam_for_app(term)
    return dynamic
