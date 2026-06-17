"""
LLM Input Builder for Groksito (Responses API).

Responsibilities:
- Context classification (casual/minimal/normal/rich/image_gen) via classify (drives tool offering: native web/x only on normal/rich; zero custom on casual/minimal)
- Minimal context injection: the high-priority referenced [R:...] message for direct replies *to the bot* OR when the bot is directly @mentioned while the user replies to another message (the "describe this YT link my friend posted" case)
- No automatic per-user memory injection ("let Grok be Grok")
- No automatic recent-context pre-injection; use the get_recent_context tool on demand.
- Dynamic context as separate system msg (for prompt caching friendliness)
- Multimodal vision (input_image high detail)

The concise SYSTEM_PROMPT from prompt_builder.py is used exclusively.
This is the single source of truth for building the input sent to the Responses API.
"""

from __future__ import annotations

import logging
from typing import Any, TypedDict

from ..utils.correlation import cid_prefix

from ..config import settings
from .prompt_builder import SYSTEM_PROMPT
from ..core.intent import (
    is_pure_image_generation_request,
    is_pure_video_generation_request,
)

# classify_query_context_need removed in #24 cleanup (heavy tier logic excised).
# We use a minimal local version that only special-cases pure image_gen and
# otherwise returns "normal" for addressed conversational turns (the only time
# build_responses_input is invoked). This keeps "need" strings flowing to
# logging + native tool schema selection without reintroducing the deprecated
# keyword bloat / full classifier.
def _classify_query_context_need(text: str, is_reply_continuation: bool = False) -> str:
    t = (text or "").strip()
    if not t:
        return "minimal"
    try:
        if is_pure_video_generation_request(t) or is_pure_image_generation_request(t):
            return "image_gen"
    except Exception:
        pass
    # For normal addressed chat (mentions/replies), "normal" is the sensible
    # default post-cleanup (lets native tools be offered when Grok needs them;
    # casual/minimal paths still exist for ultra-short non-queries via the
    # forcing logic below).
    return "normal"
from ..utils.token_usage import log_context_injection

# Centralized vision URL safety filter (prevents X/Twitter pbs.twimg.com 404s on Responses vision; see #40)
from ..utils.text import filter_unreliable_vision_urls
from ..core.intent import is_image_edit_request


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

    # Last-mile safety filter: drop any unreliable hosts that slipped through harvest
    # (e.g. pbs.twimg.com / Discord external proxies from X link or web embed thumbnails).
    # Prevents 404 fetch errors in the Responses API and allows the request to proceed with text
    # (or other good images). The x.com link in the text + has_x_link_intent will steer the model
    # to x_search for accurate content.
    safe_urls = filter_unreliable_vision_urls(image_urls)
    if not safe_urls:
        return user_message or ""

    content: list[dict] = []
    text = (user_message or "").strip()
    if is_image_edit_request(text, has_reference_image=True):
        text = (
            f"{text}\n\n"
            "[System note: The user attached a reference image and wants a visual transformation. "
            "You MUST call the edit_image tool with their instructions. Do not describe a finished "
            "edit in text alone — the tool delivers the edited image as an attachment.]"
        ).strip()
    content.append({"type": "input_text", "text": text})

    for url in safe_urls[:3]:
        content.append({
            "type": "input_image",
            "image_url": url,
            "detail": "high",
        })

    return content


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

    Recent conversation context is deliberately NOT pre-injected (see #19); it is available
    on-demand only if the model calls the get_recent_context custom tool.
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
        need = _classify_query_context_need(user_message_text, is_reply_continuation=is_reply_continuation)
    except Exception:
        need = "normal"

    # Addressed turns (mention or reply-to-bot) default to "normal" so native tools are available.
    # Pure image_gen keeps its ultra-light path. The model decides which tools to call.
    if (is_mentioned or is_reply_to_bot) and need not in ("image_gen",):
        need = "normal"
    elif is_reply_continuation and need in ("casual", "minimal"):
        need = "normal"

    if smart_mode and (need == "image_gen" or image_gen_intent):
        logger.debug(f"{cid_prefix()}[CONTEXT] IMAGE_GEN ultra mode (user={user_id[:6]}...)")

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

    # === Recent Conversation Context ===
    # Recent context is on-demand only (get_recent_context tool).
    # Grok calls the get_recent_context tool (offered via light decision tools on addressed turns)
    # only when it determines the summary is necessary for coherence or referent resolution.
    # No automatic summary or raw recent blocks are injected here. This eliminates the
    # extra Responses roundtrip on simple @mentions and lets native reasoning decide.
    # Referent resolution on strong addressed cases now relies on: tool use when needed,
    # the high-priority [R:] ref (for direct bot replies / mentions-in-reply), reply chain
    # ancestors, and Grok's native long context across turns (previous_response_id).

    # === Server Custom Emojis (emote knowledge) ===
    # Metadata scanned on startup. Vision descriptions + "most used" ranking built lazily from
    # messages the bot actually sees (no Discord usage stats API exists).
    #
    # How Groksito decides when to use them (important for not being spammy):
    # - The block is ONLY injected on addressed turns (when it's actually replying).
    # - The list is ranked by real usage in the server + has vision descriptions so the model
    #   understands the *meaning* and vibe of each emote.
    # - Explicit guidance in the block: "use naturally and sparingly... only when the tone/context calls for it."
    # - On addressed turns the model sees the user's message tone + any tool-provided recent context (if called),
    #   so it has strong signals for whether the moment is playful, meme-y, sarcastic, serious, etc.
    # - Because it's the base Grok model + previous_response_id on continuations, it tends to be
    #   tasteful rather than emoji-bombing every reply.
    #
    # See emoji_registry.py for the full design. The goal is "Grok-like" natural usage, not forced emotes.
    emoji_full_block = ""
    try:
        from ..utils import emoji_registry
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
    except Exception as emoji_ctx_err:
        logger.debug(f"{cid_prefix()}[Emoji] emoji prompt injection skipped (non-fatal): {emoji_ctx_err}")

    try:
        injected_chars = len(dynamic_context_block)
        injected_tokens = max(30, injected_chars // 4)
        log_context_injection(
            injected_tokens=injected_tokens,
            description=f"smart:{need}",
            has_memory=False,
            has_summary=False,
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
        "emoji_full_block": emoji_full_block,
    }
    return result
