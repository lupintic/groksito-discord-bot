"""
Core implementation for Groksito short-term *channel* context.

What IS persisted and available on demand:
- Per-channel message buffers (``_channel_histories``) — used by ``get_recent_context`` tool
- Per-channel rolling summaries (``_channel_summaries``) — optional compact history

What is NOT stored or injected into LLM prompts:
- Per-user memory / profile buffers (removed in #112; legacy ``profiles`` key ignored on load)
- Automatic injection of channel history into ``build_responses_input`` (see ``llm_input.py``)

Persistence file: ``data/pantsu_context.json`` — legacy filename kept for existing deployments
(see ARCHITECTURE.md). Override via ``settings.context_file`` / ``pantsu_context_file``.

Module-level side effects (loading context on import) are preserved. See ``__init__.py``.
"""

from __future__ import annotations

import json
import logging
import time
from collections import defaultdict, deque
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
PERSISTENCE_ENABLED = True

# =============================================================================
# In-memory State
# =============================================================================
_channel_histories: dict[int, deque[dict[str, Any]]] = defaultdict(
    lambda: deque(maxlen=MAX_CHANNEL_HISTORY)
)

# Lightweight rolling channel summaries (optional compact history for on-demand tools)
_channel_summaries: dict[int, dict[str, Any]] = defaultdict(
    lambda: {"summary": "", "last_updated": 0.0, "message_count_at_update": 0}
)

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
# Persistence (integrated with new config)
# =============================================================================
def _get_context_file_path() -> Path:
    """Return the context persistence file path, with defensive handling.

    Default filename ``pantsu_context.json`` is a legacy name from pre-standalone extraction;
    it is intentionally unchanged so existing deployments keep working without migration.
    """
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

        legacy_profiles = data.get("profiles")
        if legacy_profiles:
            logger.debug(
                f"{cid_prefix()}[Context] Ignoring legacy per-user profiles "
                f"({len(legacy_profiles)} entries); per-user memory was removed in #112"
            )

        # Load channel summaries
        for ch_str, sum_data in data.get("channel_summaries", {}).items():
            ch_id = int(ch_str)
            _channel_summaries[ch_id].update(sum_data)

        logger.info(
            f"{cid_prefix()}✅ Context loaded from {path} (channels={len(_channel_histories)})"
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

        # Persist compact channel summaries
        summaries_serial = {}
        for ch_id, sdata in _channel_summaries.items():
            if sdata.get("summary"):
                summaries_serial[str(ch_id)] = {
                    "summary": sdata.get("summary", ""),
                    "last_updated": sdata.get("last_updated", 0),
                    "message_count_at_update": sdata.get("message_count_at_update", 0),
                }

        payload = {
            "version": 1,
            "saved_at": time.time(),
            "channels": channels_serial,
            "channel_summaries": summaries_serial,
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

    if PERSISTENCE_ENABLED and (len(_channel_histories[channel_id]) % 8 == 0):
        save_context()


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


logger.info("✅ Context module loaded (per-channel buffers only; no per-user memory)")


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
