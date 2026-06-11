"""
LLM Input Builder for Groksito (Responses API).

Responsibilities:
- Context classification (casual/minimal/normal/rich/image_gen) via classify (drives tool offering: native web/x only on normal/rich; zero custom on casual/minimal)
- Minimal context injection: the high-priority referenced [R:...] message for direct replies *to the bot* OR when the bot is directly @mentioned while the user replies to another message (the "describe this YT link my friend posted" case)
- No automatic per-user memory injection ("let Grok be Grok")
- Dynamic context as separate system msg (for prompt caching friendliness)
- Multimodal vision (input_image high detail)

The concise SYSTEM_PROMPT from prompt.py is used exclusively.
This is the single source of truth for building the input sent to the Responses API.
"""

from __future__ import annotations

import logging
from typing import Any, TypedDict

from .correlation import cid_prefix

from .config import settings
from .prompt import SYSTEM_PROMPT
from .context import (
    classify_query_context_need,
    is_pure_image_generation_request,
    is_conversation_meta_question,
)
from .token_usage import log_context_injection


class ResponsesInputData(TypedDict):
    """Lightweight structural type for the return value of build_responses_input.

    This documents the shape without changing any runtime behavior.
    Used by the LLM orchestrator to unpack initial_input, classification need,
    and various context blocks for logging/caching decisions.
    """
    initial_input: list[dict]
    stable_prefix_len: int
    need: str
    user_id: str
    user_message_text: str
    dynamic_context_block: str
    recent_context_block: str
    emoji_full_block: str

logger = logging.getLogger("groksito.llm")


def _build_multimodal_user_content(
    user_message: str,
    image_urls: list[str] | None,
) -> list[dict] | str:
    """
    Constructs the 'content' for a user message in the Responses API.

    When images are present, uses the multimodal format required by
    the xAI /v1/responses endpoint:

        [
            {"type": "input_text", "text": "..."},
            {"type": "input_image", "image_url": "https://...", "detail": "high"},
            ...
        ]

    Returns a plain string when there are no images.
    """
    if not image_urls:
        return user_message or ""

    content: list[dict] = []
    text = (user_message or "").strip()
    content.append({"type": "input_text", "text": text})

    for url in image_urls[:3]:
        content.append({
            "type": "input_image",
            "image_url": url,
            "detail": "high",
        })

    return content


def _maybe_inject_raw_recent_fallback(
    *,
    recent_context_block: str,
    user_message_text: str,
    is_mentioned: bool,
    channel_id: int,
) -> str | None:
    """Mechanical extraction of the raw recent history fallback injection.

    Preserves the rule: when we have no high-quality recent_context summary,
    and the turn is either a meta question or a direct mention, we fall back
    to a very small raw transcript (with [image]/[link] markers) so the model
    can resolve referents like "the user", "the image the user posted", etc.
    This is intentionally lightweight and only used in strong-address cases.
    """
    need_raw_recent = not recent_context_block and (
        is_conversation_meta_question(user_message_text) or is_mentioned
    )
    if not need_raw_recent:
        return None
    try:
        from .context import get_recent_channel_messages
        raw_msgs = get_recent_channel_messages(channel_id, limit=6)
        if raw_msgs:
            lines = []
            for m in raw_msgs:
                a = "Groksito" if m.get("is_bot") else m.get("author", "User")
                c = (m.get("content", "") or "").strip()[:80]
                extras = []
                if m.get("image_urls"):
                    extras.append("[image]")
                if m.get("links"):
                    extras.append(f"[{len(m['links'])} links]")
                line = f"{a}: {c}" + (" " + " ".join(extras) if extras else "")
                lines.append(line)
            basic = " | ".join(lines)
            reason = "meta question" if is_conversation_meta_question(user_message_text) else "direct mention + possible recent referent"
            logger.info(f"{cid_prefix()}[CONTEXT] Injected basic raw recent context fallback ({reason})")
            return f"[Recent Conversation Context]\nRecent messages in this channel (for referent resolution): {basic}"
    except Exception:
        pass
    return None


async def build_responses_input(
    *,
    user_message: str,
    channel_id: int,
    original_message: Any,
    image_urls: list[str] | None,
    referenced_context: dict | None,
    reply_chain_contexts: list[dict] | None = None,  # deeper ancestors from reply chain for text referents (YouTube links, "what the user said", etc.)
    is_reply_continuation: bool,
    has_x_link_intent: bool,
    image_gen_intent: bool = False,
    is_reply_to_bot: bool = False,   # Direct reply to one of our messages
    is_mentioned: bool = False,      # Direct @mention of the bot (strong address signal; also enables referenced context + recent summary)
) -> dict[str, Any]:
    """
    The single, correct, non-duplicated input builder for the first Responses API call.

    It is the ONLY place in the codebase that constructs `initial_input`.
    Always uses the concise SYSTEM_PROMPT.

    Referenced context ([R:]) + reply_chain_contexts (deeper ancestors for text) are provided for:
    - Direct replies to the bot, or
    - Direct @mentions (with or without formal reply) when referring to recent user content
      ("what the user said", "the video/image the user posted", links in the thread, etc.).
    Enables deeper text chain walking while staying lightweight (gated + small depth/truncation).
    """
    user_id = ""
    user_message_text = user_message or ""
    try:
        if original_message and getattr(original_message, "author", None):
            user_id = str(getattr(original_message.author, "id", ""))
    except Exception:
        pass

    # === Context Classification (for tool offering decisions) ===
    # "casual" / "minimal" / "image_gen" -> zero custom tools + no native search (ultra light).
    # "normal" / "rich" -> may offer native web/x_search (x_search only on clear signals for efficiency).
    # Injection is decoupled and always minimal (only [R:] on bot replies).
    smart_mode = getattr(settings, "context_smart_mode", True)
    need = "normal"
    try:
        need = classify_query_context_need(user_message_text, is_reply_continuation=is_reply_continuation)
    except Exception:
        need = "normal"

    # For non-reply first-turn without explicit inquiry, force "casual" or "minimal"
    # (affects tool selection, not injection anymore).
    if not is_reply_continuation and not has_x_link_intent:
        if need not in ("rich", "normal"):
            need = "casual" if need == "minimal" or need == "casual" else "minimal"

    if smart_mode and (need in ("casual", "minimal", "image_gen")) and not is_reply_continuation:
        if need == "image_gen" or image_gen_intent:
            # === SPECIAL ULTRA-LIGHT "image_gen" MODE ===
            # Absolute zero dynamic context + tiny tool. Most aggressive for image prompts.
            logger.debug(f"{cid_prefix()}[CONTEXT] IMAGE_GEN ultra mode (ZERO context + 1 tiny tool) (user={user_id[:6]}...): targeting <1000 tokens")
        else:
            logger.debug(f"{cid_prefix()}[CONTEXT] ZERO context for {need}")
    # Handle reply forcing: ensure need is at least "normal" on continuations/replies
    # (affects whether native tools are offered on reply threads).
    if is_reply_continuation and need in ("casual", "minimal"):
        need = "normal"
        logger.debug(f"{cid_prefix()}[CONTEXT] Reply detected — forcing normal level for tool decisions (skipped ultra-light)")

    # On direct mentions with recent referent language ("the user", "the image the user posted", etc.),
    # ensure we are at least "normal" so native tools are available if Grok's reasoning needs them,
    # while still keeping the injection lightweight.
    if is_mentioned and (has_x_link_intent or "usuario" in user_message_text.lower() or "user" in user_message_text.lower() or "imagen" in user_message_text.lower() or "image" in user_message_text.lower()):
        if need in ("casual", "minimal"):
            need = "normal"
            logger.debug(f"{cid_prefix()}[CONTEXT] Direct mention + recent referent signals — forcing normal for context/tools")

    # === MINIMAL CONTEXT INJECTION ===
    # High-priority referenced message [R:] is injected when:
    # - Direct reply *to the bot* (is_reply_to_bot), OR
    # - The bot is directly @mentioned in a reply to another user's message.
    # The latter is the explicit "hey groksito, look at what my friend just posted (YT link, image, etc.)" case.
    # No channel history / summaries / get_channel dumps ever by default (except the opt-in recent summary on addressed turns).
    # Grok relies on its native state + the explicit ref for reply coherence.
    context_parts = []

    if referenced_context and (is_reply_to_bot or is_mentioned):
        ref_summary = referenced_context
        x_links = ref_summary.get("x_links") or []
        ext_links = ref_summary.get("external_links") or []
        addr = "reply_to_bot" if is_reply_to_bot else "mentioned_in_reply"

        if ref_summary.get("image_urls"):
            logger.info(f"{cid_prefix()}[LLM] High-priority reply context with {len(ref_summary['image_urls'])} image(s) injected (addr={addr})")
        if x_links:
            logger.info(f"{cid_prefix()}[LLM] High-priority reply context with {len(x_links)} X link(s) - model should use x_search if needed (addr={addr})")
        if ext_links and not x_links:
            logger.info(f"{cid_prefix()}[LLM] High-priority reply context with {len(ext_links)} external link(s) (e.g. YouTube) injected (addr={addr})")
        if not (ref_summary.get("image_urls") or x_links or ext_links):
            logger.info(f"{cid_prefix()}[LLM] High-priority reply context injected (text only, addr={addr})")

        ref_content = (ref_summary.get("content") or "").strip()[:150]
        high_priority_ref = f"[R:{ref_summary.get('author','?')}] {ref_content}"
        context_parts.append(high_priority_ref)

    # Deeper reply chain ancestors for text context (when direct referenced is not the original source).
    # E.g. user mentions Groksito while asking about a YouTube / statement that was in a grandparent message.
    # Injected as additional high-signal context so the model can reason about the true referent.
    # Only on addressed turns, limited to 1-2 useful ancestors, very truncated.
    if reply_chain_contexts and (is_reply_to_bot or is_mentioned):
        ancestor_lines = []
        for i, ctx in enumerate(reply_chain_contexts[1:3]):  # skip level 0 (usually the direct referenced)
            content = (ctx.get("content") or "").strip()[:100]
            author = ctx.get("author", "?")
            links = (ctx.get("external_links") or [])[:1]
            link_note = f" (link: {links[0]})" if links else ""
            if content or links:
                ancestor_lines.append(f"[Chain ancestor {i+1} by {author}]{link_note} {content}")
        if ancestor_lines:
            chain_block = "\n".join(ancestor_lines)
            context_parts.append(chain_block)
            logger.info(f"{cid_prefix()}[LLM] Injected {len(ancestor_lines)} reply chain ancestor(s) for text referent resolution")

    dynamic_context_block = ""
    if context_parts:
        dynamic_context_block = "\n\n".join(context_parts)

    # === Recent Conversation Context (new lightweight feature) ===
    # Generated when the bot is directly addressed OR the user asks about previous conversation.
    # Uses a dedicated strict summarization prompt (context/context_summarizer.py).
    # Injected as its own system message (good for caching + strong signal to the model).
    recent_context_block = ""
    # Trigger recent context on direct address (mention/reply) OR when the user explicitly asks
    # about previous conversation ("de que hablaban antes", "what were we talking about", etc.).
    # This makes the feature much more reliable for the exact use case the user is testing.
    # Prefer the explicit new flag; fall back to the legacy one for compatibility.
    _enable_recent = getattr(settings, "enable_recent_context_summary", None)
    if _enable_recent is None:
        _enable_recent = getattr(settings, "enable_recent_context", True)

    should_use_recent_context = (
        _enable_recent
        and (is_reply_to_bot or is_mentioned or is_conversation_meta_question(user_message_text))
    )
    if should_use_recent_context:
        logger.info(f"{cid_prefix()}[CONTEXT] Attempting recent conversation context summary (mentioned={is_mentioned}, reply_to_bot={is_reply_to_bot})")
        import time as _t
        _t0 = _t.time()
        try:
            from .context.context_summarizer import summarize_recent_conversation, format_recent_context_block
            summary = await summarize_recent_conversation(channel_id)
            _dt = (_t.time() - _t0) * 1000
            if summary:
                recent_context_block = format_recent_context_block(summary)
                logger.info(f"{cid_prefix()}[CONTEXT] Injected Recent Conversation Context (~{len(summary)} chars) in {_dt:.0f}ms")
            else:
                logger.info(f"{cid_prefix()}[CONTEXT] Recent conversation context was empty (no useful summary produced) after {_dt:.0f}ms")
        except Exception as e:
            logger.warning(f"{cid_prefix()}[CONTEXT] Recent conversation context summarization failed (non-fatal): {e}")

    # Extra safety for referent resolution on direct mentions or explicit meta questions about prior conversation.
    # Inject a small raw recent history (with image/link notes) so the model can precisely identify
    # "the user", "the image the user posted", "what that person said", etc. using reasoning.
    # This is lightweight and only used when the summary is missing or for strong address + referent cases.
    recent_context_block = _maybe_inject_raw_recent_fallback(
        recent_context_block=recent_context_block,
        user_message_text=user_message_text,
        is_mentioned=is_mentioned,
        channel_id=channel_id,
    ) or recent_context_block

    # === Server Custom Emojis (emote knowledge) ===
    # Metadata scanned on startup. Vision descriptions + "most used" ranking built lazily from
    # messages the bot actually sees (no Discord usage stats API exists).
    #
    # How Groksito decides when to use them (important for not being spammy):
    # - The block is ONLY injected on addressed turns (when it's actually replying).
    # - The list is ranked by real usage in the server + has vision descriptions so the model
    #   understands the *meaning* and vibe of each emote.
    # - Explicit guidance in the block: "use naturally and sparingly... only when the tone/context calls for it."
    # - On addressed turns the model also sees recent conversation context + the user's message tone,
    #   so it has strong signals for whether the moment is playful, meme-y, sarcastic, serious, etc.
    # - Because it's the base Grok model + previous_response_id on continuations, it tends to be
    #   tasteful rather than emoji-bombing every reply.
    #
    # See emoji_registry.py for the full design. The goal is "Grok-like" natural usage, not forced emotes.
    emoji_full_block = ""
    try:
        from . import emoji_registry
        gid = None
        try:
            if original_message and getattr(original_message, "guild", None):
                gid = getattr(original_message.guild, "id", None)
        except Exception:
            pass

        # Only pay the cost of the (potentially long) list when we're addressed.
        should_inject_full_emoji_list = is_reply_to_bot or is_mentioned
        if should_inject_full_emoji_list:
            emoji_full_block = emoji_registry.get_emoji_descriptions_for_prompt(gid, max_emotes=40)
            if emoji_full_block:
                logger.debug(f"{cid_prefix()}[CONTEXT] Injected full server custom emoji list (addressed turn)")
    except Exception:
        pass

    try:
        injected_chars = len(dynamic_context_block) + len(recent_context_block)
        injected_tokens = max(30, injected_chars // 4)
        log_context_injection(
            injected_tokens=injected_tokens,
            description=f"smart:{need}",
            has_memory=False,
            has_summary=bool(recent_context_block),
        )
    except Exception:
        pass

    # Multimodal content (correct format)
    user_content = _build_multimodal_user_content(user_message, image_urls)

    # =====================================================================
    # THE SINGLE AUTHORITATIVE initial_input CONSTRUCTION
    # Always uses the concise SYSTEM_PROMPT (separate system msg for
    # dynamic context + optional recent conversation context, for optimal
    # prompt caching). Recent context is only present on addressed turns.
    # =====================================================================
    system_messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]

    if dynamic_context_block:
        system_messages.append({"role": "system", "content": dynamic_context_block})

    if recent_context_block:
        system_messages.append({"role": "system", "content": recent_context_block})

    if emoji_full_block:
        system_messages.append({"role": "system", "content": emoji_full_block})

    initial_input: list[dict] = system_messages + [{"role": "user", "content": user_content}]
    stable_prefix_len = len(SYSTEM_PROMPT)

    result: ResponsesInputData = {
        "initial_input": initial_input,
        "stable_prefix_len": stable_prefix_len,
        "need": need,
        "user_id": user_id,
        "user_message_text": user_message_text,
        "dynamic_context_block": dynamic_context_block,
        "recent_context_block": recent_context_block,
        "emoji_full_block": emoji_full_block,
    }
    return result
