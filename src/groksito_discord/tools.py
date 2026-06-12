"""
Hybrid custom tool system for Groksito.

This module defines the custom Discord tools available to Groksito
and provides the execution dispatcher used by the LLM layer.

Key features:
- Tiered tool selection (lightweight core for normal turns, ultra-minimal on continuations)
- Uses media_tools for image/video generation
- Respects the centralized config (ENABLE_VIDEO_GENERATION etc.)
- Sandboxed power tools (code_execution, playwright_browser) live in sandbox.py
  and are only offered when an approved skill explicitly declares them.
- Skill meta/decision tools (create_skill, edit_skill, use_skill, etc. + testing harness + custom schema augmentation)
  live in skill_tools.py to keep this file focused on the public surface + core media/reply dispatch + tiering.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from .correlation import cid_prefix

from . import context
from .image_delivery import register_image_request, consume_image_request
from .media_tools import (
    _generate_video_schema,
    _handle_generate_video,
    _handle_generate_image,
    _handle_edit_image,
    _generate_audio_schema,
    _handle_generate_audio,
    ENABLE_VIDEO_GENERATION,
    has_explicit_audio_intent,
)

from .skill_tools import (
    # Skill meta schemas (re-exported so get_tools_for_request and internal references continue to work)
    _create_skill_schema,
    _get_recent_context_schema,
    _use_skill_schema,
    _edit_skill_schema,
    _respond_directly_schema,
    _code_execution_schema,
    _playwright_browser_schema,
    # Augmentation helpers (still part of the public-ish surface used by llm.py)
    get_skill_specific_custom_schemas,
    augment_custom_tools_with_skill_customs,
    # Extracted handlers for the meta tools
    handle_create_skill,
    handle_edit_skill,
    handle_use_skill,
    handle_get_recent_context,
    handle_respond_directly,
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
            "Generate an image using Grok Imagine (grok-imagine-image).\n\n"
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
            "Generate image (ultra-light mode).\n"
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
        "description": "Edit reference image. Artistic/stylized instructions. Avoid requesting explicit or sexual transformations (guide style/atmosphere; refs are already visual, no explicit anatomy).",
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "artistic instructions"},
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
        if name == "get_channel_context":
            limit = int(args.get("limit", 8))
            return context.get_channel_context(
                getattr(original_message, "channel", None).id if original_message else 0,
                max_lines=limit
            )

        # (search_discord_messages tool fully removed for simplification; only referenced replies + native tools remain)

        if name == "reply_to_user":
            content = args.get("content", "")
            if original_message:
                # Normalize emoji shortcodes so custom emotes actually render.
                # The model sometimes emits <:name:ID> instead of the clean :name: shortcode.
                try:
                    from . import emoji_registry
                    gid = getattr(getattr(original_message, "guild", None), "id", None)
                    content = emoji_registry.normalize_bot_emoji_output(content, gid)
                except Exception:
                    pass

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

        if name == "code_execution":
            # Delegated to the isolated sandbox module (only reachable for skills that explicitly allow it).
            from .sandbox import run_code_execution
            code = str(args.get("code", ""))[:10000]
            timeout = float(args.get("timeout_seconds", 30))
            return await run_code_execution(code, timeout_seconds=timeout)

        if name == "playwright_browser":
            # Delegated to the isolated sandbox module (only reachable for skills that explicitly allow it).
            # The sandbox implementation uses safe config passing (env var + json) instead of
            # host-side f-string interpolation of model-controlled values into the executed Python source.
            from .sandbox import run_playwright_browser
            action = str(args.get("action", "extract_text")).lower()
            url = str(args.get("url", "")).strip()
            instructions = str(args.get("instructions", "")).strip()[:500]
            selector = str(args.get("selector", "")).strip()
            timeout_ms = int(args.get("timeout_ms", 30000))
            return await run_playwright_browser(
                url=url,
                action=action,
                instructions=instructions,
                selector=selector,
                timeout_ms=timeout_ms,
            )

        if name == "create_skill":
            # Delegated to skill_tools.py (keeps tools.py focused; all the heavy prescriptive schema
            # logic + testing harness lives there).
            return await handle_create_skill(args, original_message)

        if name == "get_recent_context":
            return await handle_get_recent_context(args, original_message)

        if name == "use_skill":
            return await handle_use_skill(args, original_message)

        if name == "edit_skill":
            return await handle_edit_skill(args, original_message)

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
# Note: Skill-related meta schemas (_create_skill_schema, _use_skill_schema, _edit_skill_schema, etc.)
# and the testing harness live in skill_tools.py. They are imported above for use in get_tools_for_request
# and execute_hybrid_tool delegation.


def _get_channel_context_schema_light() -> dict:
    """Minimal version for normal turns."""
    return {
        "type": "function",
        "name": "get_channel_context",
        "description": "Recent messages from the channel.",
        "parameters": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "default": 8}
            }
        }
    }


def _get_reply_to_user_schema_light() -> dict:
    return {
        "type": "function",
        "name": "reply_to_user",
        "description": "Direct reply.",
        "parameters": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "text"}
            },
            "required": ["content"]
        }
    }


# (All skill meta schemas, custom schemas, helpers and handlers extracted to skill_tools.py — imported at top of this file.)

def get_continuation_tools(
    has_visual_intent: bool = False,
    has_explicit_video_intent: bool = False,
    has_explicit_audio_intent: bool = False,
    offer_create_skill_tool: bool = False,  # accepted for API compatibility but ignored on continuations (creation is first-turn meta)
    offer_decision_tools: bool = False,  # accepted for API compatibility but ignored on continuations (decision tools are first-turn only)
    offer_light_decision_tools: bool = False,  # accepted for compat; never on cont
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
    - History tools (search/channel) dropped on continuation in minimal path (simplified model: only referenced message injected on replies; rely on Grok's native state).

    Heavy media tools (including video) are still offered when we were in a visual flow.
    Video is only re-offered if explicit video intent was previously detected.
    """
    tools: list[dict] = []

    # Safe import (works both in normal package usage and direct script runs)
    try:
        from ..config import settings as _settings
        use_minimal = getattr(_settings, "aggressive_continuation_tool_minimization", True)
    except Exception:
        use_minimal = True  # safe default

    if use_minimal:
        # Recommended aggressive mode: only the tool needed for final delivery.
        tools.append(_get_reply_to_user_schema_light())
    else:
        # Safer fallback mode (if problems are observed)
        tools.append(_get_reply_to_user_schema_light())
        tools.append(_get_channel_context_schema_light())

    # Re-offer heavy media tools only if we were already in a visual *creation* flow.
    if has_visual_intent:
        tools.extend(
            get_heavy_tools(
                has_visual_intent=True,
                has_explicit_video_intent=has_explicit_video_intent,
                has_explicit_audio_intent=has_explicit_audio_intent,
            )
        )

    # Audio can also be re-offered on continuations if it was relevant
    if has_explicit_audio_intent:
        try:
            tools.append(_generate_audio_schema())
        except Exception:
            pass

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
    Structured logging for tool schema decisions.
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


def get_heavy_tools(
    has_visual_intent: bool = False,
    has_explicit_video_intent: bool = False,
    has_explicit_audio_intent: bool = False,
) -> list[dict]:
    """
    Returns the expensive tools (media generation).
    Only include when the query or signals clearly justify them (part of lazy/nativeness strategy).

    has_visual_intent here means *strict image creation/edit intent* (not mere presence of images or analysis).
    This prevents offering generate_image/edit_image in mixed cases (e.g. reply-to-image + factual/current question).

    Video is guarded by both visual intent + explicit keyword + feature flag.
    Audio (TTS) is offered on explicit audio requests (can be text-only or referring to previous content).
    """
    tools: list[dict] = []

    if has_visual_intent:
        tools.append(_generate_image_schema())
        tools.append(_edit_image_schema())
        if ENABLE_VIDEO_GENERATION and has_explicit_video_intent:
            tools.append(_generate_video_schema())

    if has_explicit_audio_intent:
        try:
            tools.append(_generate_audio_schema())
        except Exception:
            pass

    return tools


def get_tools_for_request(
    query_need: str = "normal",
    has_visual_intent: bool = False,
    has_explicit_video_intent: bool = False,
    has_explicit_audio_intent: bool = False,
    is_tool_continuation: bool = False,
    pure_image_gen: bool = False,
    offer_create_skill_tool: bool = False,
    offer_decision_tools: bool = False,
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
      has_visual_intent is now strict (clear gen/edit/transform signals); presence of images in a reply
      no longer auto-offers generate_image/edit_image (fixes mixed-signal token bloat).
    - The ultra-minimal SYSTEM_PROMPT + zero/minimal context = closest possible to native Grok.
    """
    if is_tool_continuation:
        # Use the dedicated ultra-minimal continuation set.
        # This is the main optimization for repeated savings on tool chains.
        return get_continuation_tools(
            has_visual_intent=has_visual_intent,
            has_explicit_video_intent=has_explicit_video_intent,
            has_explicit_audio_intent=has_explicit_audio_intent,
            offer_decision_tools=False,
            offer_light_decision_tools=False,
        )

    # === ULTRA-AGGRESSIVE "image_gen" MODE (Opción 1) ===
    # For pure first-turn text-to-image ("genera una imagen de..."):
    # - ONLY the generate_image tool (tiny schema)
    # - Never edit_image (edit cases are caught by the detector or have explicit edit signals)
    # - No other custom tools, and native search is not offered (handled in _build_native_search_tools)
    # This + zero context + short system = the path to <1000 tokens.
    if pure_image_gen:
        # Pure first-turn creation requests get an ultra-light custom tool set.
        # - Pure image: only the tiny generate_image schema.
        # - Pure video (T2V, routed here via updated classify + detector): only the generate_video schema.
        #   Avoids sending an irrelevant tiny image schema on "genera un video de..." style requests.
        tools: list[dict] = []
        is_explicit_video = ENABLE_VIDEO_GENERATION and has_explicit_video_intent

        if is_explicit_video:
            try:
                from .media_tools import _generate_video_schema
                tools.append(_generate_video_schema())
            except Exception:
                pass
        elif has_explicit_audio_intent:
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
    # reply_to_user is available on continuations (via previous_response_id) for direct delivery.
    # This is the key change for "feels like real Grok who happens to be named Groksito".
    if query_need == "casual" and not offer_light_decision_tools and not offer_decision_tools:
        return []

    if query_need in ("minimal", "image_gen") and not has_visual_intent and not offer_light_decision_tools and not offer_decision_tools:
        return []

    # Build custom tools. No internal search or channel history tools (removed for simplification).
    # Native web/x are offered separately in llm.py based on need (normal/rich).
    tools: list[dict] = []

    if has_visual_intent:
        tools.append(_generate_image_schema())
        tools.append(_edit_image_schema())
        if ENABLE_VIDEO_GENERATION and has_explicit_video_intent:
            try:
                from .media_tools import _generate_video_schema
                tools.append(_generate_video_schema())
            except Exception:
                pass

    if has_explicit_audio_intent:
        try:
            tools.append(_generate_audio_schema())
        except Exception:
            pass

    if offer_create_skill_tool or offer_decision_tools:
        try:
            tools.append(_create_skill_schema())
        except Exception:
            pass

    if offer_decision_tools:
        try:
            tools.append(_edit_skill_schema())
            tools.append(_get_recent_context_schema())
            tools.append(_use_skill_schema())
            tools.append(_respond_directly_schema())
        except Exception:
            pass
    elif offer_light_decision_tools:
        # Light decision only (plain addressed normal/minimal): the two small schemas only.
        # Keeps schema size under control; heavy (create/edit/use) stay behind strong signals.
        try:
            tools.append(_get_recent_context_schema())
            tools.append(_respond_directly_schema())
        except Exception:
            pass

    return tools
