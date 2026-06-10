"""
Safe sending utilities for Discord messages.

Responsibilities:
- Enforce Discord's ~2000 character message limit.
- Provide intelligent truncation (prefer line breaks).
- Add clear user-facing notice when truncation occurs.

Intentionally dependency-light for use from both the conversational path
and from custom tools (image/video handlers, etc.).
"""

import logging
from typing import Any

logger = logging.getLogger("groksito.response_safety")

# Lazy import to avoid circular deps (context used only for logging bot utterances for search history).
_context_mod = None
def _get_context():
    global _context_mod
    if _context_mod is None:
        from . import context as _ctx
        _context_mod = _ctx
    return _context_mod


# Recommended safe length for Discord messages.
# Discord hard limit is 2000 characters. We use 1900 to leave headroom
# for formatting, mentions, and the truncation notice.
DISCORD_SAFE_LENGTH = 1900


async def safe_reply(
    target_message: Any,
    content: str,
    mention_author: bool = False,
    max_length: int = DISCORD_SAFE_LENGTH,
) -> None:
    """
    Send a reply safely, truncating if necessary to avoid Discord errors.

    If the content exceeds the limit, it performs smart truncation
    (preferring to cut at a recent newline) and appends a clear notice.

    This function is the single source of truth for length-controlled
    sending in the Groksito conversational path.
    """
    if not content or not str(content).strip():
        return

    text = str(content).strip()

    if len(text) <= max_length:
        await target_message.reply(text, mention_author=mention_author)
        # Log bot utterance to channel buffer (for optional summaries / legacy).
        try:
            ch = getattr(target_message, "channel", None)
            ch_id = getattr(ch, "id", None) if ch else None
            if ch_id:
                ctx = _get_context()
                ctx.update_from_message(
                    channel_id=ch_id,
                    user_id=0,  # bot
                    author_name="Groksito",
                    content=text,
                    is_bot=True,
                )
        except Exception:
            pass  # never break send on logging
        return

    # Smart truncation: try to cut at a line break or space
    cutoff = max_length - 130
    truncated = text[:cutoff]

    last_newline = truncated.rfind("\n")
    if last_newline > cutoff * 0.55:
        truncated = truncated[:last_newline]
    else:
        last_space = truncated.rfind(" ")
        if last_space > cutoff * 0.65:
            truncated = truncated[:last_space]

    truncated = truncated.rstrip()
    truncated += (
        "\n\n*(respuesta recortada por longitud — "
        "Discord tiene un límite de ~2000 caracteres)*"
    )

    try:
        await target_message.reply(truncated, mention_author=mention_author)
        try:
            ch = getattr(target_message, "channel", None)
            ch_id = getattr(ch, "id", None) if ch else None
            if ch_id:
                ctx = _get_context()
                ctx.update_from_message(
                    channel_id=ch_id,
                    user_id=0,
                    author_name="Groksito",
                    content=truncated,
                    is_bot=True,
                )
        except Exception:
            pass
    except Exception as send_err:
        logger.warning(f"[safe_reply] Error al enviar respuesta truncada: {send_err}")
        try:
            fallback = (
                "La respuesta era demasiado larga para Discord. "
                "Pedime que la resuma más corto."
            )
            await target_message.reply(fallback, mention_author=mention_author)
            try:
                ch = getattr(target_message, "channel", None)
                ch_id = getattr(ch, "id", None) if ch else None
                if ch_id:
                    ctx = _get_context()
                    ctx.update_from_message(
                        channel_id=ch_id,
                        user_id=0,
                        author_name="Groksito",
                        content=fallback,
                        is_bot=True,
                    )
            except Exception:
                pass
        except Exception:
            pass
