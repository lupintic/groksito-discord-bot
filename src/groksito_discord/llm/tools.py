"""
Hybrid custom tool system for Groksito.

This module defines the custom Discord tools available to Groksito
and provides the execution dispatcher used by the LLM layer.

Key features:
- Tiered tool selection (lightweight core for normal turns, ultra-minimal on continuations)
- Core Discord delivery actions exposed as native tools (reply_to_user, react_to_message, create_thread)
  so the model can choose *how* to deliver responses (reply vs reaction vs threaded) for greater agency.
- Uses media_tools for image/video generation
- Respects the centralized config (ENABLE_VIDEO_GENERATION etc.)
- Light decision tools (get_recent_context, respond_directly) for on-demand context and explicit final replies
- On addressed turns, generate_image + generate_video (tiny schemas) are offered together so Grok
  decides natively — no keyword gate for tool availability (xAI subscription limits apply at execution)
"""

from __future__ import annotations

# Tool descriptions for media/delivery are intentionally detailed here (user-visible effect).
# High-level "when to use search / vision / recent context" guidance lives in prompt_builder.

import asyncio
import logging
from typing import Any

from ..utils.correlation import cid_prefix

from .. import context
from ..media.delivery import register_image_request, consume_image_request
from .prompt_builder import GET_RECENT_CONTEXT_TOOL_DESCRIPTION
from .media_tools import (
    _generate_video_schema,
    _generate_video_schema_tiny,
    _handle_generate_video,
    _handle_generate_image,
    _handle_edit_image,
    _generate_audio_schema,
    _handle_generate_audio,
    ENABLE_VIDEO_GENERATION,
    has_explicit_audio_intent,
)

logger = logging.getLogger("groksito.tools")
tools_logger = logging.getLogger("groksito.tools")  # dedicated for structured tool logs


# =============================================================================
# Tool Schemas (simplified but functional set for the conversational bot)
# =============================================================================

def _generate_image_schema() -> dict:
    return {
        "type": "function",
        "name": "generate_image",
        "description": (
            "Generate an image using Grok Imagine (grok-imagine-image). Best for explicit user requests to create or visualize a scene, character, or concept.\n\n"
            "**PERMISSIVE RULE FOR SUGGESTIVE CONTENT (fantasy/anime style - Grok-native):**\n"
            "The API allows quite suggestive content in stylized styles: curvy, busty, tetona, culona, sexy, voluptuous, bikini, lingerie, tight dress, sexy pose, etc.\n\n"
            "- **Allow and preserve the essence** for 'chica tetona en bikini', 'waifu culona gótica', 'sexy curvy en lencería', 'busty micro bikini', 'sexy pose', 'voluptuous'. Keep curves/bust/hips/pose/revealing clothing (no explicit). NO to generic 'elegant'.\n"
            "- **ONLY reformulate** on probable hard block: full nude/desnuda/naked (sexual), explicit genitals in act (pussy/cock/penis), explicit sex (follar/penetración/blowjob), 'spread legs' porn, ultra-vulgar that leads to explicit porn.\n"
            "- Goal: **as close as possible** to the request (even if very curvy/busty/sexy or 'ridiculous'), always without explicit nude or sexual act.\n\n"
            "Examples: 'chica tetona en bikini' → 'busty tetona girl in sexy bikini, curvy, wide hips...'; 'zero two culona sexy' → keep 'curvy culona figure, sexy gothic...'.\n\n"
            "Silent retry only for hard safety cases; user NEVER sees meta about 'explicit prompt'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "Prompt (keep suggestive: tetona/busty/curvy/sexy/bikini/lingerie; only reformulate on explicit nude or hard sex)"},
                "count": {"type": "integer", "default": 1},
                "aspect_ratio": {"type": "string"}
            },
            "required": ["prompt"]
        }
    }


def _generate_image_schema_tiny() -> dict:
    """Absolute minimal for image gen (used in pure first-turn image_gen mode)."""
    return {
        "type": "function",
        "name": "generate_image",
        "description": (
            "Generate image (ultra-light mode for pure creation requests).\n"
            "**PERMISSIVE RULE:** For suggestive fantasy 'tetona en bikini', 'culona sexy', 'sexy curvy', 'busty en lencería' → PASS AS CLOSE AS POSSIBLE (keep 'busty','curvy','sexy','tetona','culona',bikini/lingerie). Only reformulate on full nude/explicit sex/explicit genitals. API allows lots of suggestive anime/fantasy. User never sees filter meta."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "prompt (preserve suggestive busty/tetona/curvy/sexy/bikini; only change for nude or explicit acts)"},
                "count": {"type": "integer", "default": 1},
                "aspect_ratio": {"type": "string"}
            },
            "required": ["prompt"]
        }
    }


def _edit_image_schema() -> dict:
    return {
        "type": "function",
        "name": "edit_image",
        "description": (
            "Edit or transform the user's attached/reference image(s). REQUIRED when the user asks to modify, "
            "retouch, or restyle an uploaded or referenced photo (hair, makeup, clothing, mood, background, etc.). "
            "Reference images are already available from the user's message — call this tool with the transformation "
            "prompt. Do NOT claim an edited image is ready without calling this tool; successful calls deliver the "
            "result as a Discord attachment automatically."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "Artistic or stylistic transformation instructions (style, atmosphere, composition, reimagination cues)."},
                "aspect_ratio": {"type": "string"}
            },
            "required": ["prompt"]
        }
    }


# =============================================================================
# Tool Execution Dispatcher
# =============================================================================

async def execute_hybrid_tool(
    name: str,
    args: dict[str, Any],
    original_message: Any = None,
    image_urls: list[str] | None = None,
) -> str:
    """
    Executes one of the custom tools.
    This is called from the LLM layer during tool use.
    """
    try:
        # Lightweight structured logging of *actual model decisions* to invoke specific tools.
        # Complements log_tool_selection (schemas offered vs. decisions taken).
        # Gated by same flag; only logs keys (no arg values) for privacy/low overhead.
        try:
            from ..config import settings
            if getattr(settings, "log_tool_selection", True):
                arg_keys = sorted(args.keys()) if isinstance(args, dict) else []
                tools_logger.info(
                    f"{cid_prefix()}[TOOLS] decision | tool={name} | arg_keys={arg_keys}"
                )
        except Exception:
            pass

        # (search_discord_messages and get_channel_context tools removed; use get_recent_context on light turns)

        if name == "reply_to_user":
            content = args.get("content", "")
            if original_message:
                # Normalize emoji shortcodes so custom emotes actually render.
                # The model sometimes emits <:name:ID> instead of the clean :name: shortcode.
                try:
                    from ..utils import emoji_registry
                    gid = getattr(getattr(original_message, "guild", None), "id", None)
                    gobj = getattr(original_message, "guild", None)
                    content = emoji_registry.normalize_bot_emoji_output(content, gid, guild_obj=gobj)
                except Exception as emoji_norm_err:
                    tools_logger.debug(f"[Emoji] reply_to_user normalization skipped (non-fatal): {emoji_norm_err}")

                await original_message.reply(content, mention_author=False)
                # Log bot utterance (for buffer / optional summary / legacy completeness).
                try:
                    ch = getattr(original_message, "channel", None)
                    ch_id = getattr(ch, "id", None) if ch else None
                    if ch_id:
                        from . import context as ctx
                        ctx.update_from_message(
                            channel_id=ch_id,
                            user_id=0,
                            author_name="Groksito",
                            content=content or "",
                            is_bot=True,
                        )
                except Exception:
                    pass
                # Clean result for the model. LLM layer will set suppression flag
                # so conversation.py does not send a duplicate final reply.
                return "Message sent directly to the user."
            return content

        if name == "react_to_message":
            if not original_message:
                return "Cannot react: missing original message context for this turn."
            emoji = str(args.get("emoji", "")).strip()
            if not emoji:
                return "react_to_message requires a non-empty 'emoji' (Unicode emoji or custom markup)."
            try:
                # Best-effort normalization for custom guild emojis (if model used :shortcode:).
                try:
                    from ..utils import emoji_registry
                    gid = getattr(getattr(original_message, "guild", None), "id", None)
                    gobj = getattr(original_message, "guild", None)
                    emoji = emoji_registry.normalize_bot_emoji_output(emoji, gid, guild_obj=gobj)
                except Exception as emoji_norm_err:
                    tools_logger.debug(f"[Emoji] react_to_message normalization skipped (non-fatal): {emoji_norm_err}")
                await original_message.add_reaction(emoji)
                return f"Reaction {emoji} added successfully."
            except Exception as e:
                # Common failures: invalid emoji, missing 'Add Reactions' permission, trying to react to a message the bot can't see.
                return f"Failed to add reaction '{emoji}': {str(e)[:120]}. Verify emoji is valid Unicode or full <:name:ID> custom form and that the bot has Add Reactions permission in this channel."

        if name == "create_thread":
            if not original_message:
                return "Cannot create thread: missing original message context for this turn."
            thread_name = str(args.get("name", "Discussion")).strip()[:100] or "Discussion"
            content = str(args.get("content", "")).strip()
            if not content:
                return "create_thread requires non-empty 'content' for the initial thread message."
            try:
                # Normalize any custom emoji shortcodes in the posted content.
                try:
                    from ..utils import emoji_registry
                    gid = getattr(getattr(original_message, "guild", None), "id", None)
                    gobj = getattr(original_message, "guild", None)
                    content = emoji_registry.normalize_bot_emoji_output(content, gid, guild_obj=gobj)
                except Exception as emoji_norm_err:
                    tools_logger.debug(f"[Emoji] create_thread normalization skipped (non-fatal): {emoji_norm_err}")

                thread = await original_message.create_thread(name=thread_name)
                # Send the opening content inside the thread.
                await thread.send(content)

                # Log the bot utterance under the *thread's* channel id so context/summaries work if user continues there.
                try:
                    from . import context as ctx
                    ctx.update_from_message(
                        channel_id=thread.id,
                        user_id=0,
                        author_name="Groksito",
                        content=content or "",
                        is_bot=True,
                    )
                except Exception:
                    pass

                return f"Thread created successfully with name '{thread_name}'. Initial message posted. Future conversation can continue inside the thread (ID={thread.id})."
            except Exception as e:
                # Common: missing Create Threads / Send Messages in Threads permission, rate limits, bad name chars, archived parent, etc.
                return f"Failed to create thread '{thread_name}' or post initial message: {str(e)[:140]}. You can still deliver via reply_to_user as a safe fallback."

        if name == "generate_image":
            return await _handle_generate_image(args, original_message)

        if name == "edit_image":
            return await _handle_edit_image(args, original_message, image_urls)

        if name == "generate_video":
            if not ENABLE_VIDEO_GENERATION:
                return "Video generation is temporarily disabled."
            return await _handle_generate_video(args, original_message, image_urls)

        if name == "generate_audio":
            return await _handle_generate_audio(args, original_message)

        if name == "get_recent_context":
            return await handle_get_recent_context(args, original_message)

        if name == "respond_directly":
            return await handle_respond_directly(args, original_message)

        return f"Tool '{name}' is not implemented yet in this version."

    except Exception as e:
        logger.exception(f"{cid_prefix()}Error executing tool {name}")
        return f"Error executing {name}: {e}"


# =============================================================================
# Tiered Tool Schema System (for token efficiency)
# =============================================================================
# Goal: Dramatically reduce tool definition tokens on most turns.
# Strategy: Small lightweight "core" set by default + conditional full schemas
# for heavy tools (media) only when likely needed.
#
# This (tiered + lazy + tiny schemas for media) is the main lever against heavy tool schema overhead (full image gen was >2700 chars).
#



def _get_reply_to_user_schema_light() -> dict:
    return {
        "type": "function",
        "name": "reply_to_user",
        "description": "Sends the provided content as a direct reply to the user's message in the Discord channel. This is the primary delivery mechanism for the bot's final text responses and is also used to complete media generation flows (images, video, audio) via the direct-delivery pattern without duplicate messages.\n\n"
        "Use this when you have a complete answer ready and want to deliver it as a standard reply (the most common delivery choice). The content is posted as your message in the conversation. Supports Discord-flavored markdown and emoji shortcodes (custom guild emojis will be normalized if possible). Prefer this over letting a raw assistant message be auto-sent when you want explicit control or to combine with reactions/threads in the same turn.",
        "parameters": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "The text to send as the reply (supporting emoji shortcodes and basic formatting)."}
            },
            "required": ["content"]
        }
    }


def _get_react_to_message_schema_light() -> dict:
    """Lightweight schema for reacting to the triggering user message.

    Offered alongside other delivery actions on addressed turns (via light decision / decision flags)
    and allows the model to express quick feedback, acknowledgement, or personality without a full text reply.
    """
    return {
        "type": "function",
        "name": "react_to_message",
        "description": "Adds a reaction emoji to the user's original message. This is a lightweight, non-text way to acknowledge, agree, celebrate, or express sentiment (e.g. thumbs up for a good point, eyes for interesting, or a custom guild emoji for fun).\n\n"
        "When to use:\n"
        "- Quick positive/negative feedback without cluttering the channel with another message.\n"
        "- Acknowledge receipt or completion of a request ('done' via ✅).\n"
        "- Add personality or engagement (😂, ❤️, custom emotes).\n\n"
        "Safety & scope: Only reacts to the current user message in the current channel/guild (no arbitrary targets). The bot must have 'Add Reactions' permission in the channel. Use standard Unicode emoji (👍, ✅, 🔥, 👀, ❤️) or the full custom emoji form if you know a guild emote ID (e.g. '<:groksito:123456789012345678>'). Shortcodes like :thumbsup: may be normalized automatically.\n\n"
        "You can combine this tool with reply_to_user or create_thread in parallel if it makes sense (e.g. react + thoughtful reply).",
        "parameters": {
            "type": "object",
            "properties": {
                "emoji": {"type": "string", "description": "The emoji to react with. Unicode (👍, ✅) preferred for reliability. Custom: full markup '<:name:ID>' or name if normalization available."}
            },
            "required": ["emoji"]
        }
    }


def _get_create_thread_schema_light() -> dict:
    """Lightweight schema for spawning a thread off the user's message and posting into it."""
    return {
        "type": "function",
        "name": "create_thread",
        "description": "Creates a new Discord thread attached to the user's message and posts the provided content as the first message inside that thread.\n\n"
        "Use this for:\n"
        "- Longer or multi-step explanations that would otherwise flood the main channel.\n"
        "- Side discussions, detailed troubleshooting, or topic organization (e.g. 'Build discussion for Path of Exile 2').\n"
        "- Keeping the main channel clean while still giving a rich answer.\n\n"
        "The thread is created under the original user message (visible context). The initial content you provide is sent inside the thread immediately. After creation you can be messaged in the thread on future turns if the user follows up there.\n\n"
        "Parameters:\n"
        "- name: short, clear thread title (Discord will show this prominently; keep under ~80 chars).\n"
        "- content: the full text to post as the opening message in the thread (supports formatting + emojis).\n\n"
        "Safety & scope: Thread is created in the same channel/guild as the conversation. No ability to target other channels or guilds. Bot needs 'Create Public Threads' (or Private) permission + Send Messages in Threads. Threads auto-archive after inactivity per server settings. If creation fails (perms, rate limit, name too long), the error is returned so you can fallback to a normal reply_to_user instead.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Short descriptive title for the new thread (e.g. 'PoE2 detailed build help', 'Dolar blue history thread')."},
                "content": {"type": "string", "description": "Initial message posted inside the thread. Use full answer or structured content here."}
            },
            "required": ["name", "content"]
        }
    }


def _get_recent_context_schema() -> dict:
    return {
        "type": "function",
        "name": "get_recent_context",
        "description": GET_RECENT_CONTEXT_TOOL_DESCRIPTION,
        "parameters": {
            "type": "object",
            "properties": {
                "max_messages": {
                    "type": "integer",
                    "description": "Optional: how many recent messages to base the summary on (default ~12-15).",
                }
            },
        },
    }


def _respond_directly_schema() -> dict:
    return {
        "type": "function",
        "name": "respond_directly",
        "description": (
            "Explicit signal that you will now produce the final, user-facing reply using your built-in knowledge, "
            "the provided conversation context, referenced messages, and any information already gathered from prior tool calls. "
            "Calling this ends the current tool-calling phase for the turn and delivers the complete answer."
        ),
        "parameters": {"type": "object", "properties": {}},
    }


async def handle_get_recent_context(args: dict[str, Any], original_message: Any = None) -> str:
    try:
        logger.info(f"{cid_prefix()}[DECISION] model chose get_recent_context")
        ch = getattr(original_message, "channel", None)
        ch_id = getattr(ch, "id", 0) if ch else 0
        from ..context.context_summarizer import summarize_recent_conversation
        summary = await summarize_recent_conversation(ch_id)
        if summary:
            return (
                f"RECENT CHANNEL CONTEXT SUMMARY:\n{summary}\n\n"
                "Use this to maintain conversational coherence or answer references to prior messages."
            )
        return "No substantial recent conversation context available for this channel."
    except Exception as e:
        return f"Could not retrieve recent context: {str(e)[:150]}"


async def handle_respond_directly(args: dict[str, Any], original_message: Any = None) -> str:
    logger.info(f"{cid_prefix()}[DECISION] model chose respond_directly")
    return (
        "DECISION: respond directly. I now have enough information to formulate the final answer "
        "to the user without further tool calls or actions."
    )


def _append_visual_media_tools(
    tools: list[dict],
    *,
    has_visual_intent: bool = False,
    has_explicit_video_intent: bool = False,
    has_explicit_audio_intent: bool = False,
) -> None:
    """Append heavy media schemas when creation signals justify them (first-turn + continuation)."""
    if has_visual_intent:
        tools.append(_generate_image_schema())
        tools.append(_edit_image_schema())
        if ENABLE_VIDEO_GENERATION:
            try:
                tools.append(_generate_video_schema())
            except Exception:
                pass
    elif ENABLE_VIDEO_GENERATION and has_explicit_video_intent:
        # Continuation / non-light paths: re-offer video when the turn still has video signals.
        try:
            tools.append(_generate_video_schema_tiny())
        except Exception:
            pass

    if has_explicit_audio_intent:
        try:
            tools.append(_generate_audio_schema())
        except Exception:
            pass


def get_continuation_tools(
    has_visual_intent: bool = False,
    has_explicit_video_intent: bool = False,
    has_explicit_audio_intent: bool = False,
) -> list[dict]:
    """
    Highly optimized custom tool set for continuation rounds
    (when using previous_response_id).

    Strategy (conservative but aggressive on tokens):
    - By default we send **only** the reply_to_user light schema.
    - This is the single most important custom tool on continuations because
      the entire direct-delivery path for images, video, and audio depends on it
      (DIRECT_DELIVERY_PERFORMED sentinel).
    - We deliberately avoid sending a completely empty custom list for now.
      While the model usually retains knowledge of previously declared tools
      via previous_response_id, reply_to_user is too critical for correct
      media UX to risk removing without extensive production testing.
    - History tools dropped on continuation (use get_recent_context on addressed light turns).

    Heavy media tools are re-offered only when we were already in a visual/audio flow.
    """
    tools: list[dict] = [_get_reply_to_user_schema_light()]

    _append_visual_media_tools(
        tools,
        has_visual_intent=has_visual_intent,
        has_explicit_video_intent=has_explicit_video_intent,
        has_explicit_audio_intent=has_explicit_audio_intent,
    )

    return tools


def log_tool_selection(
    *,
    turn_type: str,                    # "first_turn" | "continuation"
    query_need: str,
    has_visual_intent: bool,
    custom_tools: list[dict],
    native_search_tools: list[dict],
    enable_image_search: bool = False,
    enable_image_understanding: bool = False,
) -> None:
    """
    Structured logging for tool *schema* decisions (what we offer the model).
    Complements runtime decision logs in execute_hybrid_tool + [DECISION] handlers.
    Called from llm.py after we finalize what tools to send.
    """
    try:
        from ..config import settings
        if not getattr(settings, "log_tool_selection", True):
            return
    except Exception:
        pass

    custom_names = [t.get("name", t.get("type", "?")) for t in custom_tools]
    custom_count = len(custom_names)

    # Determine a human-readable "set" name for easier filtering
    if turn_type == "continuation":
        set_name = "continuation-minimal"
        if has_visual_intent:
            set_name = "continuation-visual"
    elif query_need == "casual":
        set_name = "casual-none"
    elif query_need == "minimal":
        set_name = "minimal-core"
    elif query_need == "rich":
        set_name = "rich"
    else:
        set_name = "normal"

    # Approximate schema size (custom + native separately now, since native can be conditional web/x)
    try:
        import json
        schema_chars = len(json.dumps(custom_tools, ensure_ascii=False))
    except Exception:
        schema_chars = -1

    native_count = len(native_search_tools)
    native_types = ",".join(t.get("type", "?") for t in native_search_tools)
    try:
        import json
        native_schema_chars = len(json.dumps(native_search_tools, ensure_ascii=False))
    except Exception:
        native_schema_chars = -1

    msg = (
        f"{cid_prefix()}[TOOLS] selection | "
        f"turn={turn_type} | "
        f"need={query_need} | "
        f"visual={str(has_visual_intent).lower()} | "
        f"custom_count={custom_count} | "
        f"set={set_name} | "
        f"tools={custom_names} | "
        f"native_count={native_count} | "
        f"native_types={native_types} | "
        f"img_search={str(enable_image_search).lower()} | "
        f"img_understand={str(enable_image_understanding).lower()} | "
        f"custom_schema_chars={schema_chars} | "
        f"native_schema_chars={native_schema_chars}"
    )

    tools_logger.info(msg)


def get_tools_for_request(
    query_need: str = "normal",
    has_visual_intent: bool = False,
    has_explicit_video_intent: bool = False,
    has_explicit_audio_intent: bool = False,
    is_tool_continuation: bool = False,
    pure_image_gen: bool = False,
    pure_video_gen: bool = False,
    offer_light_decision_tools: bool = False,
) -> list[dict]:
    """
    Main entry point for tool selection. Now supports lazy/dynamic offering + ultra-light for image gen.

    Returns the appropriate (hopefully small) list of custom tool schemas
    depending on the situation. This is what llm.py should call.

    Strategy (extreme nativeness — "let Grok be Grok"):
    - Almost all first-turns → ZERO custom tools (very lazy).
    - Native xAI web_search + x_search: offered only for "normal"/"rich" need (see llm.py). web_search is
      included broadly for fresh-data queries; x_search inclusion is STRICTER (clear X/Twitter signals only,
      see _build_native_search_tools in llm_utils.py) to reduce schema + result bloat. The model decides
      whether/when to actually invoke via its judgment + improved tool descriptions + SYSTEM_PROMPT.
      No keyword forcing of calls — only schema presence for the specialized tool.
    - Only explicit *creation* visual needs (has_visual_intent) get the heavy gen/edit schemas (rich alone does not force extra tools).
      has_visual_intent is now strict (clear gen/edit/transform signals, *including video-from-image/I2V* via updated detector);
      presence of images in a reply no longer auto-offers heavy unless creation signal (fixes mixed-signal token bloat).
      On addressed turns light decision tools + tinies ensure native video offering parity with image (let Grok decide, no sole keyword gate).
    - The ultra-minimal SYSTEM_PROMPT + zero/minimal context = closest possible to native Grok.
    """
    if is_tool_continuation:
        # Use the dedicated ultra-minimal continuation set.
        # This is the main optimization for repeated savings on tool chains.
        return get_continuation_tools(
            has_visual_intent=has_visual_intent,
            has_explicit_video_intent=has_explicit_video_intent,
            has_explicit_audio_intent=has_explicit_audio_intent,
        )

    # === ULTRA-AGGRESSIVE "image_gen" MODE (Opción 1) ===
    # For pure first-turn text-to-image ("genera una imagen de..."):
    # - ONLY the generate_image tool (tiny schema)
    # - Never edit_image (edit cases are caught by the detector or have explicit edit signals)
    # - No other custom tools, and native search is not offered (handled in _build_native_search_tools)
    # This + zero context + short system = the path to <1000 tokens.
    if pure_video_gen and ENABLE_VIDEO_GENERATION:
        try:
            return [_generate_video_schema_tiny()]
        except Exception:
            return []

    if pure_image_gen:
        # Pure first-turn image creation: only the tiny generate_image schema.
        tools: list[dict] = []
        if has_explicit_audio_intent:
            try:
                tools.append(_generate_audio_schema())
            except Exception:
                pass
        else:
            tools.append(_generate_image_schema_tiny())
        return tools

    # === MAXIMUM LAZINESS for first-turn tools (native Grok feel) ===
    # Almost never declare custom tools on first_turn unless the query *clearly* requires them
    # (explicit visual for media). 
    # Casual / minimal / normal / rich chat: ZERO custom tools (no history tools in simplified model).
    # The base model just responds (with only referenced message injected on bot-replies).
    # reply_to_user (plus react_to_message / create_thread) become available when light/full decision tools are offered
    # (plain addressed turns) or on continuations, giving the model explicit choice over delivery style.
    # Light decision tools give the model delivery agency on addressed turns.
    if query_need == "casual" and not offer_light_decision_tools:
        return []

    if query_need in ("minimal", "image_gen") and not has_visual_intent and not offer_light_decision_tools:
        return []

    # Build custom tools. No internal search or channel history tools (removed for simplification).
    # Native web/x are offered separately in llm.py based on need (normal/rich).
    tools: list[dict] = []

    _append_visual_media_tools(
        tools,
        has_visual_intent=has_visual_intent,
        has_explicit_video_intent=has_explicit_video_intent,
        has_explicit_audio_intent=has_explicit_audio_intent,
    )

    if offer_light_decision_tools:
        # Light decision on addressed turns: delivery actions + on-demand context + respond_directly.
        try:
            tools.append(_get_reply_to_user_schema_light())
            tools.append(_get_react_to_message_schema_light())
            tools.append(_get_create_thread_schema_light())
            tools.append(_get_recent_context_schema())
            tools.append(_respond_directly_schema())
        except Exception:
            pass

        # Offer image + video generation (tiny schemas) + edit on addressed turns so Grok can
        # *natively* reason about media creation — same pattern as Grok web (model decides;
        # no keyword gate for tool availability; xAI subscription limits apply at the API).
        # Video is offered identically to image (via light block + explicit path); I2V now also
        # triggers has_visual for full schema parity.
        if not has_visual_intent:
            try:
                tools.append(_generate_image_schema_tiny())
                tools.append(_edit_image_schema())
                if ENABLE_VIDEO_GENERATION:
                    tools.append(_generate_video_schema_tiny())
            except Exception:
                pass

    return tools
