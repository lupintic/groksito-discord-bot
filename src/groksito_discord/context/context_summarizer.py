"""
Recent Conversation Context Summarizer for Groksito.

Purpose:
- Generate a short, high-quality, high-signal summary of the recent conversation
  in a channel **only** when Groksito is directly addressed (mentioned or replied to).
- The summary helps the model maintain coherence without dumping raw history.
- Extremely lightweight: dedicated stable prompt, bounded input, targeted output size,
  graceful degradation, and prompt-caching friendly (stable instruction prefix).

The summary is injected as its own system message in llm_input.py, right after the
main SYSTEM_PROMPT and any high-priority referenced context. This structure is good
for xAI prompt caching.

Design goals:
- Short: target 250-450 tokens (controlled by recent_context_max_tokens).
- Focused: main topics, open questions/pending items, user preferences/decisions, conversation flow.
- Factual: no hallucination.
- Language-aware: produce summary in Spanish for Spanish-dominant conversations.
- Conservative: only runs on addressed turns; skips when there is little signal.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from ..config import settings
from ..utils.correlation import cid_prefix

logger = logging.getLogger("groksito.context.summarizer")

# =============================================================================
# Dedicated, stable summarization prompt (excellent for prompt caching)
# =============================================================================
# This prompt is intentionally fixed and small. The varying part (the actual
# recent messages) is sent as the user message. This maximizes KV cache reuse
# across different turns for the same user (via prompt_cache_key in the caller).
#
# Key qualities:
# - Very explicit focus areas.
# - Strong "be concise + factual" rules.
# - Language matching instruction.
# - Clear "no signal" fallback output.
# =============================================================================

RECENT_CONTEXT_SUMMARIZATION_PROMPT = """You are an expert conversation context extractor for a helpful AI assistant in Discord.

Your job is to produce a compact, high-signal summary of the *recent conversation flow* so the assistant can reply coherently without needing the full raw history.

Given the recent messages (oldest first), focus **only** on what is currently relevant:

- Main topics or subjects actively being discussed.
- Open questions, pending requests, or things participants are still waiting on.
- Explicit user preferences, decisions, agreements, corrections, or constraints mentioned recently.
- The overall conversation flow or tone, if it changes how to respond (e.g. "users are debugging a technical issue together", "light casual chat shifting to planning").

Strict constraints:
- Be extremely concise. Target 150-350 tokens. Prefer 4-8 tight lines or 1-2 short paragraphs.
- Write the summary in Spanish if the majority of the provided messages are in Spanish (look for common Spanish words like 'el', 'la', 'de', 'que', 'por', 'dólar', 'jugadores', etc.). Otherwise use clear natural English. Match the dominant language of the conversation.
- Never invent, speculate, or add details that are not directly supported by the messages.
- Pay special attention to recent speakers ("the user", "el usuario") and their posts, images, or statements — someone may later refer to them as "what the user said", "the image the user posted", "arriba", "el anterior", etc. Note who posted what when it could serve as a referent.
- Ignore pure greetings, jokes, reactions, and low-signal small talk unless they clearly define the current topic.
- If there are no clear ongoing topics or the messages are mostly unrelated casual chatter, output exactly this sentence and nothing else: "Conversación casual sin temas pendientes claros."
- Do not include meta commentary about the summary itself.
- Output ONLY the summary text. No headings, no "Resumen:", no bullet list wrapper unless it is the most compact form, no markdown code fences.

Recent messages will be provided next. Produce the summary now.
"""

# Safety cap on the raw summary text we will accept (in characters).
# We aim for the configured recent_context_max_tokens (default 400).
MAX_SUMMARY_CHARS = 2200  # generous upper bound before truncation; real control is via prompt + post-trim


async def summarize_recent_conversation(channel_id: int) -> str | None:
    """
    Generate a short recent-conversation summary for the given channel.

    This should only be called when the bot has been directly addressed
    (mention or reply to bot), as decided by the caller (llm_input.py).

    Returns:
        A clean summary string (plain text, ready to be wrapped in the context block),
        or None if there is insufficient signal or an error occurred.
    """
    cid_p = cid_prefix()

    try:
        # Lazy import to keep module lightweight and avoid import cycles at startup.
        from .core import get_recent_channel_messages
        from ..llm.llm_utils import _call_responses_with_retry, _extract_final_text

        # Bearer resolution (same logic as the main LLM path for consistency).
        # Prefers valid OAuth if available, falls back to XAI_API_KEY.
        try:
            from ..core.grok_oauth import get_grok_bearer as _get_grok_bearer
        except Exception:
            _get_grok_bearer = None  # type: ignore

        bearer: str | None = None
        if _get_grok_bearer:
            try:
                bearer = _get_grok_bearer()
            except Exception:
                bearer = None
        if not bearer:
            bearer = getattr(settings, "xai_api_key", None) or os.getenv("XAI_API_KEY")

        if not bearer:
            logger.debug(f"{cid_p}[CONTEXT] No Grok credential available for recent context summary")
            return None

        # How many messages to consider (bounded for both quality and cost).
        limit = max(4, min(int(getattr(settings, "recent_context_message_limit", 20)), 40))
        raw_msgs = get_recent_channel_messages(channel_id, limit=limit)

        if not raw_msgs or len(raw_msgs) < 3:
            # Not enough signal to produce a useful summary.
            return None

        # Build a compact, token-efficient transcript.
        # We put oldest first so the model sees chronological flow.
        transcript_lines: list[str] = []
        for m in raw_msgs:
            author = "Groksito" if m.get("is_bot") else (m.get("author") or "Usuario")
            content = (m.get("content") or "").strip()
            imgs = m.get("image_urls") or []
            lnks = m.get("links") or []
            parts = []
            if content:
                parts.append(content)
            if imgs:
                parts.append("[image]")
            if lnks:
                # keep very light; first short link or count
                short = lnks[0][:60] if lnks else ""
                parts.append(f"[link: {short}]" if len(lnks) == 1 else f"[{len(lnks)} links]")
            if not parts:
                continue
            line = " ".join(parts)
            # Aggressive per-message truncation keeps the whole block small.
            if len(line) > 260:
                line = line[:257] + "..."
            transcript_lines.append(f"{author}: {line}")

        if len(transcript_lines) < 3:
            return None

        transcript = "\n".join(transcript_lines)

        # Target size guidance for the model (the prompt already emphasizes conciseness).
        target_tokens = int(getattr(settings, "recent_context_max_tokens", 400))
        # Rough character guidance passed in the user message (helps the model self-regulate).
        target_chars = max(300, min(target_tokens * 4, 1600))

        # Compose the input for the summarization call.
        # The system prompt is completely stable (great for caching).
        # The user message contains only the variable recent transcript + a tiny size hint.
        summarization_input = [
            {"role": "system", "content": RECENT_CONTEXT_SUMMARIZATION_PROMPT},
            {
                "role": "user",
                "content": (
                    f"Produce the recent context summary now. "
                    f"Keep the total summary under approximately {target_chars} characters.\n\n"
                    f"Recent messages (oldest first):\n\n{transcript}"
                ),
            },
        ]

        # Use the same resilient caller as the main conversation path.
        client = None  # created inside the retry helper usage below
        from openai import AsyncOpenAI

        client = AsyncOpenAI(
            api_key=bearer,
            base_url="https://api.x.ai/v1",
            timeout=getattr(settings, "api_timeout_seconds", 45.0),
        )

        # We do not pass prompt_cache_key here because the transcript changes frequently.
        # The *instruction* (system message) is stable, which still gives good prefix caching.
        response = await _call_responses_with_retry(
            client,
            model=getattr(settings, "grok_model", "grok-4.3"),
            input=summarization_input,
            # Keep it cheap: no extra tools needed for summarization.
        )

        summary_text = _extract_final_text(response) or ""
        summary_text = summary_text.strip()

        if not summary_text:
            return None

        # Post-process / safety truncation.
        # The prompt asks for the right length; we still enforce a hard cap.
        max_chars = min(MAX_SUMMARY_CHARS, target_chars + 400)
        if len(summary_text) > max_chars:
            # Try to cut at a sentence boundary.
            cut = summary_text[:max_chars]
            last_period = max(cut.rfind("."), cut.rfind("!"), cut.rfind("?"))
            if last_period > max_chars * 0.6:
                summary_text = cut[:last_period + 1]
            else:
                summary_text = cut.rstrip() + "..."

        # Special "no meaningful context" signal from the prompt.
        lowered = summary_text.lower()
        if "sin temas pendientes claros" in lowered or "no hay contexto" in lowered or "no relevant" in lowered:
            return None

        # Final sanity: if it's ridiculously short or just noise, drop it.
        if len(summary_text) < 25:
            return None

        logger.debug(f"{cid_p}[CONTEXT] Generated recent context summary (~{len(summary_text)} chars)")
        return summary_text

    except Exception as e:
        # Never break the main conversation because of summarization.
        logger.warning(f"{cid_p}[CONTEXT] Recent conversation summarization failed (non-fatal): {e}")
        return None


def format_recent_context_block(summary: str) -> str:
    """
    Wraps the raw summary text into the exact block format expected by the prompt.

    This produces a clearly delineated system message that is easy for the model
    to attend to and is stable-ish across turns (good for caching when the summary
    doesn't change drastically).
    """
    if not summary or not summary.strip():
        return ""

    clean = summary.strip()
    # The header the user specifically requested in the requirements.
    return f"[Contexto reciente de la conversación]\n{clean}"


# Optional helper for tests / debugging / direct use.
async def _summarize_messages_for_test(messages: list[dict[str, Any]]) -> str | None:
    """
    Internal/test helper: summarize a list of message dicts directly.
    Not used in production flow (production always goes through channel history).
    """
    # This is intentionally minimal; real usage should go through summarize_recent_conversation.
    # Kept for future unit tests if needed.
    if not messages:
        return None
    # For now just delegate to the normal path isn't possible without channel_id,
    # so we return a stub. Real tests would mock the API call.
    return None
