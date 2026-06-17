"""
Emoji / Emote Knowledge Registry for Groksito (efficient version for large servers).

On startup:
- Scans allowed guilds for custom Discord emojis (just cheap metadata: name/id/url).
- NO bulk vision calls during scan.

Usage tracking (the key to "most used" without Discord providing stats):
- Every message the bot receives is lightly parsed for <:name:id> patterns.
- We maintain a local usage_count per emote (persisted).
- This gives us a very good approximation of the emotes people actually type.

Lazy vision:
- The expensive Grok vision call (to understand what the emote *looks like*)
  only happens the first time an emote is actually used in chat.
- Servers with 100-200 decorative emotes only ever pay for the ones that matter.

Prompt injection (llm_input.py):
- Only on addressed turns (to stay token-efficient).
- Sorted by real usage count (most popular first).
- Hard capped (default 40) so even big servers stay cheap.
- New/zero-usage emotes are deprioritized automatically.

Result: Groksito still gets high-quality semantic understanding of the emotes
people actually use, without ever loading or describing the full 200-emote set.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Optional

import httpx

from ..config import settings
from .correlation import cid_prefix

# Bearer resolution (same as media handlers)
try:
    from ..core.grok_oauth import get_grok_bearer
except ImportError as oauth_import_err:
    get_grok_bearer = None  # type: ignore
    logging.getLogger("groksito.emoji").debug(
        f"[Emoji] OAuth bearer resolver unavailable: {oauth_import_err}"
    )

logger = logging.getLogger("groksito.emoji")

# Regex for custom Discord emojis in message content (both static and animated)
_CUSTOM_EMOJI_RE = re.compile(r"<a?:([a-zA-Z0-9_]+):(\d+)>")

# Efficiency: only pay for vision description on emotes that are actually used.
# This prevents vision calls (and storage bloat) for the 100-200 unused decorative emotes
# that many large servers have.
MIN_USAGE_FOR_VISION = 1  # describe on first observed use
MAX_EMOTES_IN_PROMPT = 40  # hard cap even on huge servers; sorted by real usage

# ----------------------------------------------------------------------------
# Persistence
# ----------------------------------------------------------------------------

_KNOWLEDGE_FILE: Path = settings.data_dir / "emoji_knowledge.json"

# In-memory structure (loaded at import / first use):
#
# {
#   "version": 1,
#   "guilds": {
#     "123456789012345678": {
#       "last_scanned": 1720000000.0,
#       "emojis": {
#         "123456789012345678": {   # key by emoji.id (stable)
#           "id": "123456789012345678",
#           "name": "jaja",
#           "url": "https://cdn.discordapp.com/emojis/123456789012345678.webp?size=128",
#           "animated": false,
#           "description": "laughing yellow face, eyes closed, tears of joy — classic 'jajaja' meme laughter",
#           "usage": "hilarious reactions, when something is very funny in a casual Spanish way",
#           "discovered_at": 1720000000.0
#         },
#         ...
#       }
#     }
#   }
# }
#
_EMOJI_KNOWLEDGE: dict[str, Any] = {
    "version": 1,
    "guilds": {},
}
_LOADED = False
_LOCK = asyncio.Lock()


def _ensure_loaded() -> None:
    global _LOADED, _EMOJI_KNOWLEDGE
    if _LOADED:
        return
    try:
        if _KNOWLEDGE_FILE.exists():
            with open(_KNOWLEDGE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                _EMOJI_KNOWLEDGE = data
                # migrate if needed
                if "guilds" not in _EMOJI_KNOWLEDGE:
                    _EMOJI_KNOWLEDGE["guilds"] = {}

                # Backward compat: old records may lack usage_count or have description from before lazy mode.
                for gdata in _EMOJI_KNOWLEDGE.get("guilds", {}).values():
                    for rec in gdata.get("emojis", {}).values():
                        if "usage_count" not in rec:
                            # If it already had a vision description from a previous run, treat it as "used".
                            rec["usage_count"] = 1 if rec.get("description") else 0
        _LOADED = True
        total = sum(len(g.get("emojis", {})) for g in _EMOJI_KNOWLEDGE.get("guilds", {}).values())
        logger.info(f"[Emoji] Loaded knowledge for {len(_EMOJI_KNOWLEDGE.get('guilds', {}))} guild(s), {total} emotes")
    except Exception as e:
        logger.warning(f"[Emoji] Failed to load emoji knowledge (starting fresh): {e}")
        _EMOJI_KNOWLEDGE = {"version": 1, "guilds": {}}
        _LOADED = True


async def _save() -> None:
    async with _LOCK:
        try:
            settings.data_dir.mkdir(parents=True, exist_ok=True)
            tmp = _KNOWLEDGE_FILE.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(_EMOJI_KNOWLEDGE, f, indent=2, ensure_ascii=False)
            tmp.replace(_KNOWLEDGE_FILE)
            logger.debug(f"[Emoji] Saved emoji knowledge to {_KNOWLEDGE_FILE}")
        except Exception as e:
            logger.warning(f"[Emoji] Failed to save emoji knowledge: {e}")


# ----------------------------------------------------------------------------
# Vision descriptor (small, dedicated, cheap one-shot call)
# ----------------------------------------------------------------------------

async def _resolve_bearer() -> str | None:
    """Prefer fresh OAuth, fall back to XAI_API_KEY (same logic as rest of bot)."""
    if get_grok_bearer:
        try:
            tok = get_grok_bearer()
            if tok:
                return tok
        except Exception as bearer_err:
            logger.debug(f"[Emoji] get_grok_bearer failed (non-fatal): {bearer_err}")
    return (
        os.getenv("XAI_API_KEY")
        or getattr(settings, "xai_api_key", None)
    )


async def describe_emoji_with_vision(emoji_url: str, name: str) -> str:
    """
    Ask Grok vision to look at the custom emoji image and return a compact
    semantic description suitable for the LLM to decide when to use it.
    Returns a short string like:
      "laughing yellow face with closed eyes and joyful tears — 'jajaja' meme laughter, very funny reactions"
    """
    api_key = await _resolve_bearer()
    if not api_key:
        return f"custom emoji named {name} (no vision available to describe)"

    # Very focused prompt — we want short, usable-by-LLM output.
    vision_prompt = (
        f"You are examining a small custom Discord server emoji (often 128x128, may be animated).\n"
        f"Server name for this emote: :{name}:\n\n"
        "Describe exactly what the image depicts (character/expression/colors/style/meme elements). "
        "Then explain in 1 short sentence the typical emotional or meme context Spanish-speaking users "
        "would use it for in Discord chat.\n"
        "Output ONLY one line in this exact format (no extra text):\n"
        "DEPICTS: <visual> | USE: <when people use it>"
    )

    payload = {
        "model": getattr(settings, "grok_model", "grok-4.3"),
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": vision_prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": emoji_url, "detail": "low"},  # low is enough for tiny emoji art
                    },
                ],
            }
        ],
        "max_tokens": 120,
        "temperature": 0.2,
    }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    for attempt in range(2):  # quick 1-retry
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                # We use /chat/completions vision here (lightweight for meta task).
                # xAI supports the standard OpenAI vision format on this endpoint.
                resp = await client.post(
                    "https://api.x.ai/v1/chat/completions",
                    headers=headers,
                    json=payload,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    content = ""
                    try:
                        content = data["choices"][0]["message"]["content"].strip()
                    except Exception:
                        content = str(data)[:200]
                    # Clean a little
                    if content.lower().startswith("depicts:"):
                        content = content.split("depicts:", 1)[1].strip()
                    # Keep it short
                    if len(content) > 220:
                        content = content[:217] + "..."
                    logger.info(f"{cid_prefix()}[Emoji] Vision description for :{name}: got {len(content)} chars")
                    return content or f"custom emoji :{name}:"
                else:
                    # transient? retry once
                    if attempt == 0 and resp.status_code in (429, 500, 502, 503):
                        await asyncio.sleep(0.8)
                        continue
                    logger.warning(f"[Emoji] Vision call for :{name}: failed status={resp.status_code}")
                    break
        except Exception as e:
            if attempt == 0:
                await asyncio.sleep(0.5)
                continue
            logger.warning(f"[Emoji] Vision call for :{name}: exception {e}")
            break

    # Fallback so we still know the raw name
    return f"custom server emoji named :{name}:"


# ----------------------------------------------------------------------------
# Core scanning
# ----------------------------------------------------------------------------

def _get_guild_store(guild_id: int | str) -> dict[str, Any]:
    gid = str(guild_id)
    if "guilds" not in _EMOJI_KNOWLEDGE:
        _EMOJI_KNOWLEDGE["guilds"] = {}
    if gid not in _EMOJI_KNOWLEDGE["guilds"]:
        _EMOJI_KNOWLEDGE["guilds"][gid] = {
            "last_scanned": 0.0,
            "emojis": {},  # key by str(emoji.id)
        }
    return _EMOJI_KNOWLEDGE["guilds"][gid]


async def _register_emoji_metadata(
    guild_id: int | str,
    emoji_id: int | str,
    name: str,
    url: str,
    animated: bool,
) -> bool:
    """Register or update basic metadata for an emoji. Does NOT trigger vision.
    Returns True if it was newly discovered (first time we saw this id).
    """
    store = _get_guild_store(guild_id)
    eid = str(emoji_id)

    existing = store["emojis"].get(eid)
    if existing:
        changed = False
        if existing.get("name") != name:
            existing["name"] = name
            changed = True
        if existing.get("url") != url:
            existing["url"] = url
            changed = True
        if "usage_count" not in existing:
            existing["usage_count"] = 0
        if changed:
            await _save()
        return False

    # Brand new (metadata only — vision is lazy on actual use)
    store["emojis"][eid] = {
        "id": eid,
        "name": name,
        "url": url,
        "animated": bool(animated),
        "description": None,   # will be filled lazily when used
        "usage": "",          # short usage hint from vision
        "usage_count": 0,
        "discovered_at": time.time(),
    }
    store["last_scanned"] = time.time()
    await _save()
    logger.debug(f"{cid_prefix()}[Emoji] Registered metadata for new emote :{name}: (id={eid})")
    return True


async def _ensure_vision_description(guild_id: int | str, emoji_id: int | str) -> None:
    """If the emoji has no description yet, fire a vision call (background safe).
    This is the efficient lazy path — we only pay for vision on emotes that are actually used.
    """
    store = _get_guild_store(guild_id)
    eid = str(emoji_id)
    rec = store["emojis"].get(eid)
    if not rec or rec.get("description"):
        return

    name = rec.get("name", "unknown")
    url = rec.get("url", "")
    if not url:
        return

    logger.info(f"{cid_prefix()}[Emoji] First meaningful use of :{name}: — triggering lazy vision description")
    desc = await describe_emoji_with_vision(url, name)

    depicts = desc
    usage_hint = ""
    if " | USE:" in desc:
        parts = desc.split(" | USE:", 1)
        depicts = parts[0].replace("DEPICTS:", "").strip()
        usage_hint = parts[1].strip()

    rec["description"] = depicts[:180]
    rec["usage"] = usage_hint[:120]
    await _save()
    logger.info(f"{cid_prefix()}[Emoji] Lazy vision done for :{name}:")


async def scan_guild_emojis(guild: Any) -> int:
    """
    Scan a single discord.Guild for custom emojis.
    Only performs vision for emojis we have never seen before (by id).
    Returns count of newly described emojis.
    """
    _ensure_loaded()
    if guild is None:
        return 0

    guild_id = getattr(guild, "id", None)
    if not guild_id:
        return 0

    try:
        emojis = list(getattr(guild, "emojis", []) or [])
    except Exception:
        emojis = []

    if not emojis:
        return 0

    new_count = 0
    for em in emojis:
        try:
            eid = getattr(em, "id", None)
            name = getattr(em, "name", None) or f"emoji_{eid}"
            url = str(getattr(em, "url", "")) or ""
            animated = bool(getattr(em, "animated", False))

            if not eid or not url:
                continue

            # Metadata only — vision is lazy and only happens for emotes that actually get used.
            # This is the key efficiency win for servers with 100-200 emotes.
            added = await _register_emoji_metadata(guild_id, eid, name, url, animated)
            if added:
                new_count += 1
        except Exception as per_err:
            logger.debug(f"[Emoji] Skipped one emoji in guild {guild_id}: {per_err}")

    # Update last_scanned even if no new ones
    store = _get_guild_store(guild_id)
    store["last_scanned"] = time.time()
    if new_count > 0:
        await _save()

    if new_count:
        logger.info(f"{cid_prefix()}[Emoji] Guild {guild_id}: {new_count} new emote(s) discovered (vision deferred until used)")
    return new_count


async def scan_all_accessible_emojis(client: Any) -> None:
    """
    Called at startup (on_ready). Scans every guild the bot can see that is
    either whitelisted or has no whitelist.
    Non-fatal; runs to completion even if some guilds or vision calls fail.
    """
    _ensure_loaded()
    if client is None:
        return

    try:
        guilds = list(getattr(client, "guilds", []) or [])
    except Exception:
        guilds = []

    if not guilds:
        logger.info("[Emoji] No guilds visible at scan time.")
        return

    allowed = set(getattr(settings, "allowed_guild_ids", []) or [])

    total_new = 0
    for g in guilds:
        gid = getattr(g, "id", None)
        if allowed and gid not in allowed:
            continue
        try:
            new = await scan_guild_emojis(g)
            total_new += new
        except Exception as ge:
            logger.warning(f"[Emoji] Scan failed for guild {gid}: {ge}")

    total_known = sum(len(g.get("emojis", {})) for g in _EMOJI_KNOWLEDGE.get("guilds", {}).values())
    logger.info(f"[Emoji] Startup scan complete. {total_new} new this run. {total_known} emotes known across all guilds.")


# ----------------------------------------------------------------------------
# Efficient local usage tracking (the core of "most used" without Discord stats)
# ----------------------------------------------------------------------------

def record_emojis_from_message(message: Any) -> None:
    """Cheap synchronous call from on_message.
    Parses the message content for custom guild emotes and increments usage counts.
    Triggers lazy vision description (in background) the first time an emote is seen.
    This is how we learn the *actually relevant* emotes on servers with 100-200+ total emotes.
    """
    try:
        guild = getattr(message, "guild", None)
        guild_id = getattr(guild, "id", None) if guild else None
        content = getattr(message, "content", "") or ""
        if not guild_id or not content:
            return

        matches = _CUSTOM_EMOJI_RE.findall(content)
        if not matches:
            return

        store = _get_guild_store(guild_id)
        changed = False

        for name, eid_str in matches:
            eid = str(eid_str)
            rec = store["emojis"].get(eid)
            if not rec:
                # We saw an emoji id we don't have metadata for (possible race or partial cache).
                # Skip for now; next startup scan will pick it up.
                continue

            # Increment usage
            prev = rec.get("usage_count", 0)
            rec["usage_count"] = prev + 1
            changed = True

            # Lazy vision trigger: only when crossing the threshold and we don't have a description yet.
            if prev < MIN_USAGE_FOR_VISION and not rec.get("description"):
                # Fire and forget — vision is the expensive part, we don't block the message handler.
                asyncio.create_task(_ensure_vision_description(guild_id, eid))
                logger.debug(f"{cid_prefix()}[Emoji] Usage hit for :{name}: — scheduling lazy vision")

        if changed:
            # Persist (same pattern as video quotas — cheap and infrequent in practice).
            # We schedule the save so we don't await in the hot path.
            asyncio.create_task(_save())

    except Exception as e:
        logger.debug(f"[Emoji] record_emojis_from_message failed (non-fatal): {e}")


# ----------------------------------------------------------------------------
# Public query API (used by llm_input to inject knowledge)
# ----------------------------------------------------------------------------

def get_all_known_emojis() -> dict[str, dict]:
    """Flat map of emoji_id -> record across all known guilds."""
    _ensure_loaded()
    out: dict[str, dict] = {}
    for g in _EMOJI_KNOWLEDGE.get("guilds", {}).values():
        out.update(g.get("emojis", {}))
    return out


def get_emojis_for_guild(guild_id: int | str | None) -> dict[str, dict]:
    """Emojis for one specific guild (by id). Empty dict if unknown guild."""
    _ensure_loaded()
    if not guild_id:
        return {}
    store = _EMOJI_KNOWLEDGE.get("guilds", {}).get(str(guild_id), {})
    return store.get("emojis", {}) or {}


def get_emoji_descriptions_for_prompt(
    guild_id: int | str | None = None,
    max_emotes: int = None,
) -> str:
    """
    Returns the most *efficient* block possible for high-emoji servers (100-200+ emotes).

    - Sorted by real local usage count (most used first).
    - Hard capped.
    - Only used emotes get rich vision descriptions (lazy).
    - The header tells the model to use clean shortcode :name: in its text.
    - A post-send normalizer converts :name: into the proper <:name:ID> form
      so the emoji actually renders when the bot sends the message.
    """
    _ensure_loaded()
    if max_emotes is None:
        max_emotes = MAX_EMOTES_IN_PROMPT

    emojis: dict[str, dict] = {}
    if guild_id:
        emojis = get_emojis_for_guild(guild_id)

    if not emojis:
        emojis = get_all_known_emojis()

    if not emojis:
        return ""

    # Sort by actual usage (descending) — this is the efficient "most used" signal.
    sorted_recs = sorted(
        emojis.values(),
        key=lambda r: (-int(r.get("usage_count", 0)), r.get("name", "")),
    )

    lines = []
    for rec in sorted_recs:
        name = rec.get("name", "unknown")
        desc = (rec.get("description") or "").strip()
        usage = (rec.get("usage") or "").strip()

        shortcode = f":{name}:"
        if desc:
            line = f"{shortcode} — {desc}" + (f" ({usage})" if usage else "")
        else:
            line = f"{shortcode} (seen in chat — description pending or low usage)"

        if len(line) > 150:
            line = line[:147] + "..."
        lines.append(line)

    if not lines:
        return ""

    total = len(lines)
    shown = min(total, max_emotes)
    header = f"[SERVER CUSTOM EMOJIS ({total} total, ranked by real usage here) — Use them by writing the shortcode form :name: naturally inside your sentences (like a human would type an emoji). The system will automatically convert it to the correct renderable form before sending. Good: 'Jajaja :jaja: eso fue épico' or 'Listo :chill: ¿qué onda?'. Do not output the raw <:name:ID> form yourself. Use sparingly and only when the vibe really calls for it.]"
    if total > shown:
        header = f"[SERVER CUSTOM EMOJIS ({total} total, top {shown} by usage) — Write the shortcode :name: naturally in your text. Example: 'Listo :chill:'. The system handles making it render. Never output the full <:name:ID> yourself. Use only when it fits the tone, sparingly.]"

    block = header + "\n" + "\n".join(lines[:shown])
    return block


def get_emoji_compact_header(guild_id: int | str | None = None) -> str:
    """
    Ultra-lightweight header (1-2 lines max, ~40-80 tokens even on huge servers).
    Safe to inject more often than the full descriptive list.

    Tells the model to use clean shortcode :name: (the system upgrades it to the
    full renderable form at send time).

    The main conversational path currently only injects the full list on
    addressed turns (is_mentioned / reply_to_bot) for exactly this reason.
    This compact header is available for other use cases (tools, slash commands,
    or if we ever want weak awareness on passive turns).
    """
    _ensure_loaded()

    emojis = {}
    if guild_id:
        emojis = get_emojis_for_guild(guild_id)
    if not emojis:
        emojis = get_all_known_emojis()

    total = len(emojis)
    if total == 0:
        return ""

    # Pick a few example names (first 5 alphabetically) as a hint
    sample_names = sorted(emojis.values(), key=lambda r: r.get("name", ""))[:5]
    examples = ", ".join(f":{r['name']}:" for r in sample_names if r.get("name"))

    if examples:
        return f"[This server has {total} custom emojis (e.g. {examples}). Use the shortcode form :name: naturally in your sentences (the system will make it render). Good: 'Jajaja :jaja: eso fue épico'. Do not output raw <:name:ID>. Use sparingly when the vibe fits.]"
    else:
        return f"[This server has {total} custom emojis. Use shortcodes :name: naturally in text (system converts for rendering). Never raw full form. Use sparingly and naturally when the tone fits.]"


def get_usable_emoji_names(guild_id: int | str | None = None) -> list[str]:
    """Just the bare :name: list (for tools or very constrained prompts)."""
    _ensure_loaded()
    if guild_id:
        ems = get_emojis_for_guild(guild_id)
    else:
        ems = get_all_known_emojis()
    return [f":{r['name']}:" for r in ems.values() if r.get("name")]


# ----------------------------------------------------------------------------
# Output normalizer — upgrade model shortcodes to full <:name:ID> for reliable bot rendering
# ----------------------------------------------------------------------------

_EMOJI_RAW_PATTERN = re.compile(r"<:?(a:)?([a-zA-Z0-9_]+):\d+>")


def normalize_bot_emoji_output(text: str, guild_id: int | str | None = None) -> str:
    """
    Safety net for bot emoji output.

    The model is instructed to output clean, natural shortcodes like :name: (Grok-like).
    Before sending to Discord, this upgrades any :name: (and fixes any malformed
    raw forms) into the exact full `<:name:REAL_ID>` (or `<a:name:ID>`) that a bot
    must use so the custom emoji reliably renders as an image.

    We only act on emojis we know belong to the guild.
    """
    if not text:
        return text

    # Build lookup: name -> correct full token
    name_to_full: dict[str, str] = {}
    if guild_id:
        recs = get_emojis_for_guild(guild_id)
    else:
        recs = get_all_known_emojis()

    for rec in recs.values():
        name = rec.get("name")
        eid = rec.get("id")
        if not name or not eid:
            continue
        animated = bool(rec.get("animated"))
        if animated:
            name_to_full[name] = f"<a:{name}:{eid}>"
        else:
            name_to_full[name] = f"<:{name}:{eid}>"

    if not name_to_full:
        return text

    # Fix any raw/malformed full forms to the correct full token
    def _raw_replacer(match: re.Match) -> str:
        name = match.group(2)
        if name in name_to_full:
            return name_to_full[name]
        return match.group(0)

    text = _EMOJI_RAW_PATTERN.sub(_raw_replacer, text)

    # Catch <name:ID> without the colon right after <
    text = re.sub(
        r"<([a-zA-Z0-9_]+):\d+>",
        lambda m: name_to_full.get(m.group(1), m.group(0)),
        text
    )

    # Upgrade bare shortcodes :name: to the correct full token
    def _shortcode_replacer(m: re.Match) -> str:
        name = m.group(1)
        return name_to_full.get(name, m.group(0))

    shortcode_pattern = re.compile(r":([a-zA-Z0-9_]+):")
    text = shortcode_pattern.sub(_shortcode_replacer, text)

    return text


def get_emoji_stats() -> dict[str, Any]:
    """Lightweight stats for health / dashboard."""
    _ensure_loaded()
    guilds = _EMOJI_KNOWLEDGE.get("guilds", {})
    total = sum(len(g.get("emojis", {})) for g in guilds.values())
    # Rough sense of how "active" the tracked set is
    with_usage = 0
    total_usage = 0
    for gdata in guilds.values():
        for rec in gdata.get("emojis", {}).values():
            uc = int(rec.get("usage_count", 0))
            if uc > 0:
                with_usage += 1
            total_usage += uc
    return {
        "total_emotes": total,
        "emotes_with_usage": with_usage,
        "total_usage_events": total_usage,
        "guilds_with_emotes": len(guilds),
        "cache_file": str(_KNOWLEDGE_FILE),
    }


# Convenience: allow manual re-scan from outside (e.g. a future slash command or skill)
async def force_rescan(client: Any, guild_id: int | str | None = None) -> int:
    """Force a full re-scan (still only does vision for truly new ids)."""
    _ensure_loaded()
    total = 0
    if guild_id:
        # Find the guild object
        for g in getattr(client, "guilds", []) or []:
            if str(getattr(g, "id", "")) == str(guild_id):
                total = await scan_guild_emojis(g)
                break
    else:
        await scan_all_accessible_emojis(client)
        total = sum(len(g.get("emojis", {})) for g in _EMOJI_KNOWLEDGE.get("guilds", {}).values())
    return total


# Auto-load on import so first call is fast
_ensure_loaded()
