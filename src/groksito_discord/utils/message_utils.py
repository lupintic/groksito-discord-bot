"""
Discord message splitting and chained delivery utilities.

Splits long bot responses into multiple messages that respect Discord's
~2000 character limit, preferring paragraph boundaries and preserving
fenced code blocks when possible.
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger("groksito.response_safety")

DISCORD_SAFE_LENGTH = 1900

_FENCE_RE = re.compile(r"(```[^\n]*\n[\s\S]*?```)")

_context_mod = None


def _get_context():
    global _context_mod
    if _context_mod is None:
        from .. import context as _ctx
        _context_mod = _ctx
    return _context_mod


def log_bot_utterance(target_message: Any, content: str) -> None:
    try:
        ch = getattr(target_message, "channel", None)
        ch_id = getattr(ch, "id", None) if ch else None
        if ch_id:
            ctx = _get_context()
            ctx.update_from_message(
                channel_id=ch_id,
                user_id=0,
                author_name="Groksito",
                content=content,
                is_bot=True,
            )
    except Exception:
        pass


def _fence_balance(text: str) -> int:
    return text.count("```")


def _inline_backtick_balance(text: str) -> int:
    return text.count("`")


def _best_break(text: str, limit: int) -> int:
    """Find the best split index within text[:limit]."""
    if len(text) <= limit:
        return len(text)

    window = text[:limit]

    if _fence_balance(window) % 2 == 1:
        fence_start = window.rfind("```")
        if fence_start > limit * 0.3:
            return fence_start

    for sep in ("\n\n", "\n", " "):
        idx = window.rfind(sep)
        if idx > limit * 0.5:
            if sep == " ":
                candidate = idx
                if _inline_backtick_balance(window[:candidate]) % 2 == 1:
                    safer = window.rfind("`", 0, candidate)
                    if safer > limit * 0.4:
                        return safer
                return candidate
            return idx + len(sep)

    return limit


def _split_plain_text(text: str, max_length: int) -> list[str]:
    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= max_length:
            chunks.append(remaining)
            break
        cut = _best_break(remaining, max_length)
        if cut <= 0:
            cut = max_length
        chunk = remaining[:cut].rstrip()
        if chunk:
            chunks.append(chunk)
        remaining = remaining[cut:].lstrip("\n")
        if cut == 0:
            break
    return chunks


def _split_code_block(block: str, max_length: int) -> list[str]:
    lines = block.split("\n")
    if not lines:
        return _split_plain_text(block, max_length)

    opener = lines[0]
    has_closer = lines[-1].strip() == "```"
    closer = "```" if has_closer else ""
    body_lines = lines[1:-1] if has_closer else lines[1:]

    chunks: list[str] = []
    current = opener + "\n"

    for line in body_lines:
        addition = line + "\n"
        candidate = current + addition
        if closer:
            candidate_with_close = candidate + closer
        else:
            candidate_with_close = candidate

        if len(candidate_with_close) > max_length and current != opener + "\n":
            chunks.append(current.rstrip() + ("\n" + closer if closer else ""))
            current = opener + "\n" + addition
        else:
            current += addition

    if current.strip():
        chunks.append(current.rstrip() + ("\n" + closer if closer else ""))

    if not chunks:
        return _split_plain_text(block, max_length)
    return chunks


def _segment_text(text: str) -> list[tuple[str, bool]]:
    parts = _FENCE_RE.split(text)
    segments: list[tuple[str, bool]] = []
    for part in parts:
        if not part:
            continue
        segments.append((part, part.startswith("```")))
    return segments


def split_message_text(text: str, max_length: int = DISCORD_SAFE_LENGTH) -> list[str]:
    """
    Split text into chunks that fit within max_length.

    Prefers paragraph breaks, then line breaks, then spaces. Avoids splitting
    inside fenced code blocks when possible; oversized blocks are split at
    line boundaries with balanced fences per chunk.
    """
    text = str(text).strip()
    if not text:
        return []
    if len(text) <= max_length:
        return [text]

    chunks: list[str] = []
    current = ""

    for segment, is_fence in _segment_text(text):
        if len(segment) <= max_length:
            candidate = current + segment
            if len(candidate) <= max_length:
                current = candidate
                continue
            if current:
                chunks.append(current.rstrip())
            current = segment
            continue

        if current:
            chunks.append(current.rstrip())
            current = ""

        if is_fence:
            sub_chunks = _split_code_block(segment, max_length)
        else:
            sub_chunks = _split_plain_text(segment, max_length)

        if sub_chunks:
            chunks.extend(sub_chunks[:-1])
            current = sub_chunks[-1]

    if current:
        chunks.append(current.rstrip())

    return [chunk for chunk in chunks if chunk]


async def send_split_messages(
    target_message: Any,
    content: str,
    mention_author: bool = False,
    max_length: int = DISCORD_SAFE_LENGTH,
) -> None:
    """
    Send content as one or more Discord messages.

    The first chunk replies to target_message (typically the user's message).
    Subsequent chunks are sent as plain channel messages so the continuation
    reads like back-to-back messages, not a self-reply chain.
    """
    parts = split_message_text(content, max_length)
    if not parts:
        return

    channel = getattr(target_message, "channel", None)

    for index, part in enumerate(parts):
        if index == 0:
            await target_message.reply(part, mention_author=mention_author)
        elif channel is not None:
            await channel.send(part)
        else:
            await target_message.reply(part, mention_author=False)
        log_bot_utterance(target_message, part)