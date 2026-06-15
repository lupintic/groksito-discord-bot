"""
Core implementation for Groksito conversation context.

This module contains the actual state, persistence, update logic, classification,
video quotas, and helper functions.

The package __init__.py re-exports the public surface for backward compatibility
with existing imports like `from . import context` and `from .context import ...`.

Module-level side effects (loading context on import) are preserved for now
but isolated here. See __init__.py for public API.
"""

from __future__ import annotations

import json
import logging
import time
from collections import defaultdict, deque
from datetime import date, datetime
from pathlib import Path
from typing import Any

from ..utils.correlation import cid_prefix

from ..config import settings

# Light intent predicates (post #22/#24 cleanup of heavy classification).
# Only the small essential helpers are imported. All heavy keyword lists,
# tiering rules, and the classify_query_context_need function itself removed.
from ..core.intent import (
    is_conversation_meta_question,
    is_pure_image_generation_request,
    should_generate_recent_summary,
    should_offer_light_decision_tools,
)

logger = logging.getLogger("groksito.context")


# =============================================================================
# Configuration
# =============================================================================
MAX_CHANNEL_HISTORY = 150
MAX_USER_RECENT = 6
PERSISTENCE_ENABLED = True

# =============================================================================
# In-memory State
# =============================================================================
_channel_histories: dict[int, deque[dict[str, Any]]] = defaultdict(
    lambda: deque(maxlen=MAX_CHANNEL_HISTORY)
)

_user_profiles: dict[int, dict[str, Any]] = defaultdict(
    lambda: {
        "recent_messages": deque(maxlen=MAX_USER_RECENT),
        "last_seen": 0.0,
        "display_name": "",
    }
)

# New: lightweight rolling channel summaries (key for token reduction)
_channel_summaries: dict[int, dict[str, Any]] = defaultdict(
    lambda: {"summary": "", "last_updated": 0.0, "message_count_at_update": 0}
)

# Simple per-user daily video quota (5/day). Persisted with the rest of context.
# Only today's count is kept (old days don't affect the limit).
_video_quotas: dict[int, dict[str, int]] = {}

# History buffer maxlen for optional summarization / legacy.
# No longer used for default injection (only referenced on bot replies).
MAX_RAW_HISTORY = 8


def update_channel_summary(channel_id: int, new_summary: str) -> None:
    """Update or create a compact rolling summary for the channel (called by tool or meta logic)."""
    if not new_summary or len(new_summary) < 20:
        return
    _channel_summaries[channel_id]["summary"] = new_summary.strip()[:600]
    _channel_summaries[channel_id]["last_updated"] = time.time()
    _channel_summaries[channel_id]["message_count_at_update"] = len(
        _channel_histories.get(channel_id, [])
    )
    logger.info(
        f"{cid_prefix()}[Context] Updated channel summary for {channel_id} ({len(new_summary)} chars)"
    )


# =============================================================================
# Video quota (simple per-user daily limit of 5, for honest "5 videos/day" claim)
# =============================================================================
# Reuses the existing context persistence (no new files). Only today's count is
# relevant; we filter on load/save. Increment is optimistic (before API call)
# for minimal code. Videos are rare so save on every change is fine.


def get_video_quota(user_id: int) -> tuple[int, int]:
    """Return (used_today, remaining). Never negative remaining."""
    today = date.today().isoformat()
    used = _video_quotas.get(user_id, {}).get(today, 0)
    return used, max(0, 5 - used)


def increment_video_quota(user_id: int) -> tuple[int, int]:
    """Increment for today and return (new_used, new_remaining). Persists."""
    today = date.today().isoformat()
    if user_id not in _video_quotas:
        _video_quotas[user_id] = {}
    _video_quotas[user_id][today] = _video_quotas[user_id].get(today, 0) + 1
    used = _video_quotas[user_id][today]
    remaining = max(0, 5 - used)
    save_context()  # cheap and rare (videos limited)
    return used, remaining


# =============================================================================
# Persistence (integrated with new config)
# =============================================================================
def _get_context_file_path() -> Path:
    """Return the context persistence file path, with defensive handling."""
    try:
        p = settings.context_file
        # Defensive: if misconfigured to a directory (or no .json), fall back to standard file under data_dir
        if p.is_dir() or p.suffix.lower() != ".json":
            fallback = settings.data_dir / "pantsu_context.json"
            logger.warning(
                f"[Context] context_file resolved to non-file {p}, using fallback {fallback}"
            )
            return fallback
        return p
    except Exception as e:
        # Default filename kept for data compat (legacy pantsu name)
        fallback = Path.cwd() / "data" / "pantsu_context.json"
        logger.error(
            f"[Context] Error resolving context_file, using safe fallback {fallback}: {e}"
        )
        return fallback


def _load_context() -> None:
    if not PERSISTENCE_ENABLED:
        return

    path = _get_context_file_path()
    try:
        # Ensure parent exists (in case data dir was removed externally)
        path.parent.mkdir(parents=True, exist_ok=True)
    except PermissionError as e:
        logger.error(
            f"[Context] Cannot create context directory {path.parent}: {e}. Persistence will be disabled for this run."
        )
        return
    except Exception as e:
        logger.warning(
            f"[Context] Unexpected error ensuring context dir {path.parent}: {e}"
        )

    if not path.exists():
        logger.debug(f"No context file yet: {path}")
        return
    if not path.is_file():
        logger.warning(f"[Context] Context path exists but is not a file: {path}")
        return

    try:
        data = json.loads(path.read_text(encoding="utf-8"))

        for ch_str, msgs in data.get("channels", {}).items():
            ch_id = int(ch_str)
            dq = deque(maxlen=MAX_CHANNEL_HISTORY)
            for m in msgs[-MAX_CHANNEL_HISTORY:]:
                dq.append(m)
            _channel_histories[ch_id] = dq

        for uid_str, prof in data.get("profiles", {}).items():
            uid = int(uid_str)
            profile = _user_profiles[uid]
            profile["display_name"] = prof.get("display_name", "")
            profile["last_seen"] = prof.get("last_seen", 0.0)

            recent = prof.get("recent_messages", [])[-MAX_USER_RECENT:]
            dq = deque(maxlen=MAX_USER_RECENT)
            for m in recent:
                dq.append(m)
            profile["recent_messages"] = dq

        # Load channel summaries (new compact feature)
        for ch_str, sum_data in data.get("channel_summaries", {}).items():
            ch_id = int(ch_str)
            _channel_summaries[ch_id].update(sum_data)

        # Load video quotas (only keep today's; daily limit resets on new day)
        today = date.today().isoformat()
        for uid_str, qdata in data.get("video_quotas", {}).items():
            uid = int(uid_str)
            if today in qdata and qdata[today] > 0:
                _video_quotas[uid] = {today: qdata[today]}

        logger.info(
            f"{cid_prefix()}✅ Context loaded from {path} (channels={len(_channel_histories)}, users={len(_user_profiles)})"
        )
    except PermissionError as e:
        logger.error(
            f"[Context] Permission denied loading context file {path}: {e}. Check that the bot process can read/write the data directory."
        )
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        logger.warning(
            f"[Context] Context file {path} is corrupted or invalid JSON: {e}. Starting with fresh in-memory context (old file left in place)."
        )
    except OSError as e:
        logger.warning(f"[Context] OS error loading context from {path}: {e}")
    except Exception as e:
        logger.warning(f"Failed to load context from JSON: {e}")


def save_context() -> bool:
    if not PERSISTENCE_ENABLED:
        return False

    path = _get_context_file_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except PermissionError as e:
        logger.error(
            f"[Context] Permission denied creating context directory {path.parent}: {e}"
        )
        return False
    except Exception as e:
        logger.warning(f"[Context] Error ensuring directory for {path}: {e}")
        return False

    try:
        channels_serial = {}
        for ch_id, dq in _channel_histories.items():
            channels_serial[str(ch_id)] = list(dq)[-MAX_CHANNEL_HISTORY:]

        profiles_serial = {}
        for uid, prof in _user_profiles.items():
            profiles_serial[str(uid)] = {
                "display_name": prof.get("display_name", ""),
                "last_seen": prof.get("last_seen", 0.0),
                "recent_messages": list(prof.get("recent_messages", []))[
                    -MAX_USER_RECENT:
                ],
            }

        # Persist compact channel summaries
        summaries_serial = {}
        for ch_id, sdata in _channel_summaries.items():
            if sdata.get("summary"):
                summaries_serial[str(ch_id)] = {
                    "summary": sdata.get("summary", ""),
                    "last_updated": sdata.get("last_updated", 0),
                    "message_count_at_update": sdata.get("message_count_at_update", 0),
                }

        # Persist video quotas (only today's count)
        today = date.today().isoformat()
        video_serial = {}
        for uid, qs in _video_quotas.items():
            c = qs.get(today, 0)
            if c > 0:
                video_serial[str(uid)] = {today: c}

        payload = {
            "version": 1,
            "saved_at": time.time(),
            "channels": channels_serial,
            "profiles": profiles_serial,
            "channel_summaries": summaries_serial,
            "video_quotas": video_serial,
        }

        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        logger.debug(f"Context saved to {path}")
        return True
    except PermissionError as e:
        logger.error(
            f"[Context] Permission denied writing context file {path}: {e}. Data dir must be writable."
        )
        return False
    except OSError as e:
        logger.warning(f"[Context] OS/IO error saving context to {path}: {e}")
        return False
    except Exception as e:
        logger.debug(f"Failed to save context: {e}")
        return False


# Load on module import
_load_context()


# =============================================================================
# Main Public API
# =============================================================================
def update_from_message(
    channel_id: int,
    user_id: int,
    author_name: str,
    content: str,
    is_bot: bool = False,
    timestamp: float | None = None,
    image_urls: list[str] | None = None,
    links: list[str] | None = None,
) -> None:
    ts = timestamp or time.time()
    short_content = content[:280] if content else ""

    _channel_histories[channel_id].append(
        {
            "ts": ts,
            "author_id": user_id,
            "author": author_name,
            "content": short_content,
            "is_bot": is_bot,
            "image_urls": image_urls or [],
            "links": links or [],
        }
    )

    profile = _user_profiles[user_id]
    profile["display_name"] = author_name
    profile["last_seen"] = ts

    profile["recent_messages"].append(
        {
            "ts": ts,
            "channel_id": channel_id,
            "content": short_content,
        }
    )

    if PERSISTENCE_ENABLED and (len(_channel_histories[channel_id]) % 8 == 0):
        save_context()


def get_channel_context(
    channel_id: int,
    max_lines: int = 8,
    for_meta_question: bool = False,
    exclude_current: bool = False,
) -> str:
    """
    Raw recent channel messages (short excerpts only).
    Used by the get_channel_context custom tool (offered in non-minimal continuation fallback)
    and for optional proactive summarization.
    Not used for default prompt injection (only [R:] ref on bot replies in llm_input).
    """
    hist = _channel_histories.get(channel_id)
    if not hist:
        return ""

    msgs = list(hist)
    if exclude_current and msgs:
        msgs = msgs[:-1]

    effective_max = min(max_lines, MAX_RAW_HISTORY)
    recent = msgs[-effective_max:]
    lines = []
    for idx, m in enumerate(recent, 1):
        author = m.get("author", "???")
        content = m.get("content", "").strip() or "(no text)"

        # Omit full links and shorten excerpts aggressively for lower token use.
        prefix = "[G]" if m.get("is_bot") else f"[{author}]"

        # Conservative lengths: 60-100 chars. Never full messages.
        if for_meta_question:
            max_content = 100
            ts_val = m.get("ts")
            ts_str = ""
            if isinstance(ts_val, (int, float)):
                try:
                    ts_str = datetime.fromtimestamp(ts_val).strftime("%H:%M")
                except Exception:
                    ts_str = "??:??"
            lines.append(f"[{ts_str}] {prefix}: {content[:max_content]}")
        else:
            max_content = 90 if max_lines >= 3 else 60
            lines.append(f"{prefix}: {content[:max_content]}")

    if not lines:
        return ""

    header = "Channel:\n"
    if for_meta_question:
        header = "Channel (meta):\n"

    return header + "\n".join(lines)


def get_estimated_history_tokens(channel_id: int) -> int:
    """Rough token estimate of the current channel history (for proactive summarization decisions)."""
    hist = _channel_histories.get(channel_id, [])
    if not hist:
        return 0
    total_chars = sum(len(m.get("content", "")) for m in hist)
    return total_chars // 4


def get_messages_for_summarization(channel_id: int, keep_recent: int = 6) -> list[dict]:
    """Returns the older messages that should be summarized (excludes the most recent ones)."""
    hist = _channel_histories.get(channel_id, [])
    if len(hist) <= keep_recent:
        return []
    return list(hist)[:-keep_recent]


# (internal search accessor removed with the feature)

# (search subsystem removed)


# (_strip_accents, _is_pure..._for_classification and classify_query_context_need removed
# in #24 final cleanup. The heavy classification tiering logic is gone; only the
# light predicates from intents are used for the few remaining narrow cases.
# context_need values still appear in a few logging/compat paths for continuity.)


logger.info("✅ Context module loaded (buffers + per-user recent messages)")
# (search_discord_messages and related re-exports removed; feature fully excised for simplification)


# =============================================================================
# Public helper for the new Recent Conversation Context feature
# =============================================================================
def get_recent_channel_messages(
    channel_id: int, limit: int = 20
) -> list[dict[str, Any]]:
    """Returns the most recent messages for a channel.

    Used by the lightweight recent conversation context summarizer
    (only when the bot is directly addressed in normal chat).
    """
    hist = _channel_histories.get(channel_id)
    if not hist:
        return []
    return list(hist)[-limit:]


# Note: The actual summarizer lives in context/context_summarizer.py
# Callers import it explicitly:
#   from .context.context_summarizer import summarize_recent_conversation, format_recent_context_block
#
# This keeps heavy OpenAI client usage lazy (the functions themselves import what they need
# only when actually invoked on an addressed turn).
