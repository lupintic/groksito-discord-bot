"""
LLM / Responses API layer for Groksito (standalone).

Clean modular orchestrator:
- Input construction lives exclusively in llm_input.py (single source of truth)
- Helpers in llm_utils.py
- This file focuses on call flow, multi-round tool execution, and orchestration.

Public API preserved for compatibility:
- call_grok_for_groksito (and alias call_grok_with_tools)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any, Optional

from ..utils.correlation import cid_prefix

from openai import AsyncOpenAI, RateLimitError, APITimeoutError, APIConnectionError, APIError

from ..config import settings
from ..media.delivery import DIRECT_DELIVERY_PERFORMED
from .tools import (
    get_tools_for_request,
    log_tool_selection,
    execute_hybrid_tool,
)

# For the explicit video intent guard (Python-level safety net in addition to prompt/schema)
from .media_tools import has_explicit_video_intent, has_explicit_audio_intent

# Import from the new sibling modules (clean separation)
from .llm_input import build_responses_input
from ..context import should_offer_light_decision_tools
from .llm_utils import (
    _extract_final_text,
    _build_stub_response,
    _get_prompt_cache_key,
    _build_native_search_tools,
    _detect_visual_intent,
    _detect_image_creation_intent,
    is_image_edit_request,
    _infer_tools_set_name,
    _extract_and_log_token_usage,
    _maybe_proactive_summarize,
    is_pure_image_generation_request,
    _call_responses_with_retry,
)

# Lightweight Skills + Decision layer (normal chat). Lazy imports inside functions to avoid cycles.
try:
    from ..config import settings as _settings
except Exception:
    _settings = None  # type: ignore

# OAuth / unified bearer support (lazy; central resolver prefers OAuth token when available)
try:
    from ..core.grok_oauth import get_grok_bearer as _get_grok_bearer
except Exception:
    _get_grok_bearer = None  # type: ignore

logger = logging.getLogger("groksito.llm")


async def call_grok_for_groksito(
    user_message: str,
    author_name: str,
    channel_id: int,
    original_message: Any = None,
    image_urls: list[str] | None = None,
    referenced_context: dict | None = None,
    reply_chain_contexts: list[dict] | None = None,  # deeper reply ancestors for text referents (links, "what the user said", etc.)
    has_visual_intent: bool = False,
    is_reply_continuation: bool = False,
    has_x_link_intent: bool = False,
    is_reply_to_bot: bool = False,  # Direct reply to one of our messages (affects referenced context + some heuristics)
    is_mentioned: bool = False,
) -> str | object:
    """
    Main entry point from the conversational flow (Responses API + hybrid tool loop).

    Key efficiency features on continuations (using previous_response_id):
    - Custom tools are minimized via get_continuation_tools (only reply_to_user by default).
    - Native search tools (web_search + x_search) are NOT re-sent by default.
      (x_search is offered less frequently overall due to stricter signal checks on first turn.)
      We rely on the model retaining prior tool declarations via previous_response_id
      (see comment in the continuation block for details + conditional re-inclusion
      if search was used in the prior round). This reduces token bloat from tool
      schemas/descriptions on follow-up turns.

    Returns:
        - str: normal final assistant text
        - DIRECT_DELIVERY_PERFORMED sentinel: a media tool already replied directly
    """
    cid_p = cid_prefix()
    logger.debug(f"{cid_p}[LLM] call_grok_for_groksito called for {author_name}")

    model = getattr(settings, "grok_model", None) or "grok-4.3"

    # Resolve bearer via central helper: prefers valid OAuth token (proactive refresh) when available,
    # with seamless fallback to XAI_API_KEY. Works for GROK_AUTH_MODE=auto / oauth / (even api_key if token present).
    bearer: Optional[str] = None
    if _get_grok_bearer:
        bearer = _get_grok_bearer()
    if not bearer:
        bearer = getattr(settings, "xai_api_key", None) or os.getenv("XAI_API_KEY")  # last-ditch env
    if not bearer:
        logger.warning("[LLM] No Grok credential (no OAuth token and no XAI_API_KEY). Using stub response.")
        return _build_stub_response(user_message, author_name, image_urls)

    if settings.using_oauth or (settings.auth_mode == "auto" and bearer and len(bearer) < 100):  # rough heuristic: oauth tokens are JWT-ish
        logger.debug("[LLM] Using xAI Grok OAuth bearer (SuperGrok / X Premium+)")
    else:
        logger.debug("[LLM] Using XAI_API_KEY (api_key mode or oauth fallback)")

    try:
        # Apply configured timeout to the client (affects all responses.create calls).
        # This prevents indefinite hangs on slow/stuck xAI endpoints.
        client = AsyncOpenAI(
            api_key=bearer,
            base_url="https://api.x.ai/v1",
            timeout=settings.api_timeout_seconds,
        )

        # Proactive summarization ΓÇö disabled by default (maximum nativeness).
        # Grok's large context handles long threads naturally. Enable only for pathological cases.
        if getattr(settings, "summarization_enabled", False):
            await _maybe_proactive_summarize(channel_id, original_message, client)

        # Compute pure image gen intent *early* so we can pass it to the input builder for the dedicated "image_gen" mode.
        pure_image_gen_intent = False
        try:
            # Pure image gen intent is driven by the (light) is_pure detector for the ultra-minimal
            # image_gen context+tools path. Do NOT AND with _detect_visual_intent (that detector
            # explicitly returns False on creation verbs to avoid misrouting "show pictures" search cases).
            pure_image_gen_intent = (
                is_pure_image_generation_request(user_message)
                and not bool(image_urls)
                and not is_reply_continuation
            )
        except Exception:
            pure_image_gen_intent = False

        # === SINGLE CALL to the authoritative input builder (llm_input.py) ===
        # This is the single source of truth for initial_input.
        # Context injection: high-prio [R:] + deeper reply_chain_contexts (text ancestors for links/referents) on addressed turns.
        # No recent summary pre-injection (recent context is on-demand via tool only; see #19).
        input_data = await build_responses_input(
            user_message=user_message,
            channel_id=channel_id,
            original_message=original_message,
            image_urls=image_urls,
            referenced_context=referenced_context,
            reply_chain_contexts=reply_chain_contexts,
            is_reply_continuation=is_reply_continuation,
            has_x_link_intent=has_x_link_intent,
            image_gen_intent=pure_image_gen_intent,
            is_reply_to_bot=is_reply_to_bot,
            is_mentioned=is_mentioned,
        )

        # Unpack what we need for the rest of the flow
        initial_input = input_data["initial_input"]
        stable_prefix_len = input_data["stable_prefix_len"]
        need = input_data["need"]
        user_id = input_data["user_id"]
        user_message_text = input_data["user_message_text"]

        is_addressed = bool(is_mentioned or is_reply_to_bot)

        # =====================================================================
        # LIGHTWEIGHT SKILLS + DECISION LAYER (normal chat only)
        # Runs a tiny cache-friendly decision call, then optionally injects an
        # approved skill's instructions + restricts tool offering.
        # Never turns normal chat into an autonomous agent. User approval required.
        # =====================================================================
        decision = None
        skill_injection = None
        post_tool_skill_injection = None  # set when use_skill tool is successfully called mid-flow

        try:
            enable_layer = bool(getattr(_settings, "enable_skill_decision_layer", True)) if _settings else True
            if enable_layer and need in ("normal", "rich"):
                from ..skills.decision import make_decision as _make_decision
                from ..skills.skill_executor import prepare_skill_injection as _prepare_injection
                from ..skills.skill_registry import get_skill_registry as _get_reg

                reg = _get_reg()
                approved_names = [s.name for s in reg.list_approved()]

                decision = await _make_decision(
                    user_message=user_message_text,
                    author_name=author_name,
                    is_mentioned=is_mentioned,
                    is_reply_to_bot=is_reply_to_bot,
                    recent_signals=None,
                    approved_skill_names=approved_names,
                    context_need=need,   # pass classify result so heuristic/prompt can be smarter
                )

                # Apply skill if the decision selected an approved one
                if decision and decision.action.value == "use_skill" and decision.use_skill:
                    skill_injection = _prepare_injection(
                        decision_skill_id=decision.use_skill,
                        user_message=user_message_text,
                    )
                    if skill_injection:
                        # Mutate the input we are about to send (high-priority skill instructions)
                        from ..skills.skill_executor import inject_skill_into_responses_input as _inject
                        initial_input = _inject(initial_input, skill_injection)
                        logger.info(f"{cid_p}[SKILLS] Skill '{skill_injection.skill.name}' injected into prompt")
        except Exception as dec_err:
            logger.debug(f"{cid_p}[SKILLS] Decision layer skipped (non-fatal): {dec_err}")

        # === Smart Tool Selection + Native Tools ===
        # has_visual_intent from upstream is now STRICT image *creation/edit* intent (not just "image present").
        # We keep a separate broader signal only for enabling image_understanding / image_search flags on native web_search.
        creation_visual_intent = (
            has_visual_intent
            or _detect_image_creation_intent(
                user_message_text,
                has_reference_image=bool(image_urls),
            )
            or (bool(image_urls) and is_image_edit_request(user_message_text, has_reference_image=True))
        )

        # Broader signal: presence of images (for vision) OR query mentions visuals -> controls img_* flags on search tools.
        # This allows useful vision+search in mixed cases (image in reply + current events question) without
        # polluting with gen/edit tool schemas.
        vision_or_visual_query = bool(image_urls) or _detect_visual_intent(user_message_text) or creation_visual_intent

        # For custom tool schemas and "visual flow" continuations we use the strict creation signal.
        effective_visual_intent = creation_visual_intent

        # Explicit video intent ΓÇö used as Python-level guard for offering/allowing generate_video
        # (complements the strict description in the tool schema and the guard inside the handler)
        explicit_video_intent = has_explicit_video_intent(user_message_text)
        explicit_audio_intent = has_explicit_audio_intent(user_message_text)

        # pure_image_gen_intent was already computed before build_responses_input (see above)
        # so we can pass a clean "image_gen" mode to both the context builder and the tool selector.

        if has_x_link_intent:
            logger.info(f"{cid_p}[LLM] X/Link intent detected in reply ΓÇö boosting context awareness for referenced links")

        # === Internal decision tools via native tool calling (unified reasoning path) ===
        # We offer a small set of internal decision/action tools (get_recent_context, use_skill,
        # create_skill, edit_skill, respond_directly) only on relevant turns. This lets the *model* reason
        # about whether to fetch recent context (on-demand), activate an existing skill, create a new one,
        # edit an existing one, or answer directly ΓÇö replacing much of the previous hardcoded decision/prompt logic.
        # The offer is gated by a cheap pre-filter so we stay token-efficient and don't turn
        # normal chat into an agent loop. (Recent context pre-injection removed in #19.)
        offer_decision_tools = False
        try:
            is_addressed = bool(is_mentioned or is_reply_to_bot)
            tlow = (user_message_text or user_message or "").lower()

            # Reuse/extend the cheap creation intent + pattern detector
            from ..skills.skill_proposer import should_offer_create_skill_tool, _has_explicit_edit_intent
            creation_candidate = should_offer_create_skill_tool(
                user_message_text or user_message,
                channel_id=channel_id,
                user_id=int(user_id) if (user_id and str(user_id).isdigit()) else 0,
            )

            edit_candidate = _has_explicit_edit_intent(user_message_text or user_message or "")

            # Other signals that make decision tools worth offering (addressed + needs context/data/skill lang)
            has_context_signal = any(k in tlow for k in ("antes", "dijimos", "habl├íbamos", "qu├⌐ pas├│", "contin├║a", "de qu├⌐", "resumen de la", "la charla", "tema anterior", "what did we", "earlier", "previous", "contexto"))
            has_data_or_skill_signal = any(k in tlow for k in ("hoy", "ahora", "actual", "en vivo", "live", "pico", "jugadores", "precio", "cu├íntos", "crea", "create", "skill", "habilidad", "haz una", "quiero una", "mejora", "edita", "actualiza"))

            # Offer the heavy decision meta tools (create/edit/use_skill, get_recent_context, respond_directly)
            # ONLY on explicit signals or strong candidates. Bare "is_addressed" (plain @mention) is
            # NOT enough ΓÇö it would bloat every normal question with 15k+ chars of meta schemas
            # (create_skill descriptions etc.) even when no skill management is relevant.
            # Recent context is available on-demand via the get_recent_context tool (offered on
            # addressed turns via light decision tools or full); no pre-injection (see #19).
            if creation_candidate or edit_candidate or has_context_signal or has_data_or_skill_signal:
                offer_decision_tools = True
        except Exception:
            offer_decision_tools = False

        offer_light_decision_tools = False
        try:
            if should_offer_light_decision_tools(
                user_message_text or user_message,
                is_mentioned=is_mentioned,
                is_reply_to_bot=is_reply_to_bot,
                context_need=need,
            ) and not offer_decision_tools:
                offer_light_decision_tools = True
        except Exception:
            offer_light_decision_tools = False

        custom_tools = get_tools_for_request(
            query_need=need,
            has_visual_intent=effective_visual_intent,
            has_explicit_video_intent=explicit_video_intent,
            has_explicit_audio_intent=explicit_audio_intent,
            is_tool_continuation=False,
            pure_image_gen=pure_image_gen_intent,
            offer_create_skill_tool=offer_decision_tools,  # create_skill is included in the decision tool set
            offer_decision_tools=offer_decision_tools,
            offer_light_decision_tools=offer_light_decision_tools,
        )

        # When a skill is (or will be) active and declares custom tools such as code_execution
        # or playwright_browser, inject their full schemas into the list offered to the model.
        # This is how skills get access to powerful/specialized tools without polluting normal chat turns.
        # The subsequent skill filter will keep only the ones the skill explicitly allows.
        if skill_injection and getattr(skill_injection, "allowed_custom", None):
            try:
                from .tools import get_skill_specific_custom_schemas, augment_custom_tools_with_skill_customs
                custom_tools = augment_custom_tools_with_skill_customs(
                    custom_tools, skill_injection.allowed_custom
                )
            except Exception:
                pass

        # === Native search tool offering (first-turn) ===
        # For first-turn messages where need in ("normal", "rich"), we *consider* native search tools.
        # (casual/minimal/image_gen get none ΓÇö major token saver).
        #
        # web_search is offered for most normal/rich (fresh-info potential).
        # x_search is offered only on *clear* X/Twitter signals (stricter filter in _build_native_search_tools)
        # to lower average offers of the x_search schema and reduce invocations of its result payloads.
        #
        # Native search offering decision (which schemas to declare) uses lightweight signals + need only.
        # There is ZERO heuristic that decides whether the *model should call* search ΓÇö that is 100% Grok
        # (via SYSTEM_PROMPT + the efficiency-focused tool descriptions). The model can still use web_search
        # for social info if x_search schema was not sent.
        # - We only skip for the explicitly lazy cases (casual/minimal/image_gen).
        # - On continuation turns we further minimize (no re-send of native search by default).
        # - Combined with minimal injection (only [R:] on bot replies) = maximum nativeness + low tokens.
        #
        # On continuation turns (see below), we do NOT re-send native_search_tools by default.
        # We rely on previous_response_id for the model to retain knowledge of previously offered
        # tools (similar to how custom continuation tools are minimized). This saves significant
        # tokens on multi-round flows (the search tool descriptions are non-trivial).
        if need in ("casual", "image_gen") or (need == "minimal" and not is_addressed):
            # Explicit laziness in the main llm flow (builder will also return []).
            # Relaxed for addressed minimal (Phase 1 agentic): plain @mentions that classify minimal
            # still get native search schemas so Grok + respond_directly can decide; keeps classify for extremes.
            native_search_tools = []
        else:
            # Prompt-driven (#48): offer native search on normal/rich turns; Grok decides usage.
            native_search_tools = _build_native_search_tools(
                query_text=user_message_text,
                context_need=need,
                has_visual_intent=vision_or_visual_query,
                has_attached_images=bool(image_urls),
            )

        # === Apply skill restrictions to tool offering (if a skill is active) ===
        try:
            from ..skills.skill_executor import (
                filter_native_search_tools as _filter_native,
                filter_custom_tools as _filter_custom,
            )
            if skill_injection:
                native_search_tools = _filter_native(native_search_tools, skill_injection)
                custom_tools = _filter_custom(custom_tools, skill_injection)
                logger.debug(f"{cid_p}[SKILLS] Tools restricted to skill allowances (native={len(native_search_tools)}, custom={len(custom_tools)})")
        except Exception:
            pass

        # (Recent context force logic removed in #19: summaries are generated exclusively on-demand
        # inside the get_recent_context tool handler when the model explicitly calls it. No
        # pre-injection or decision-forced injection occurs before or during the first turn.)

        # Structured tool selection logging
        offered_custom_tool_names = {t.get("name") for t in custom_tools if t.get("name")}
        try:
            img_search = False
            img_understand = False
            for t in native_search_tools:
                if t.get("type") == "web_search":
                    img_search = t.get("enable_image_search", False)
                    img_understand = t.get("enable_image_understanding", False)
                    break

            log_tool_selection(
                turn_type="first_turn",
                query_need=need,
                has_visual_intent=effective_visual_intent,
                custom_tools=custom_tools,
                native_search_tools=native_search_tools,
                enable_image_search=img_search,
                enable_image_understanding=img_understand,
            )
        except Exception as log_err:
            logger.debug(f"{cid_p}[TOOLS] selection logging failed: {log_err}")

        # Phase 1 (Ticket #7): lightweight instrumentation for addressed turns only.
        # Capture start for first-turn + full tool loop latency; flag for search schemas offered.
        addressed_turn_start = time.time() if is_addressed else None
        search_schemas_offered = bool(native_search_tools)

        # First call to Responses API (vision images are sent here)
        # Uses retry helper for transients (429/5xx/timeout); non-transients (policy, auth, bad payload) fail fast for caller classification.
        try:
            cache_key = _get_prompt_cache_key(original_message)
            response = await _call_responses_with_retry(
                client,
                model=model,
                input=initial_input,
                tools=[
                    *native_search_tools,
                    *custom_tools,
                ],
                extra_body={"prompt_cache_key": cache_key},
            )
        except Exception as api_err:
            err_str = str(api_err)
            err_lower = err_str.lower()
            is_image_fetch_404 = bool(image_urls) and (
                "fetching image failed" in err_lower
                or ("404" in err_lower and ("image" in err_lower or "not found" in err_lower))
                or "unrecoverable data loss" in err_lower  # the wrapper message from xAI for bad image fetches
            )
            if is_image_fetch_404:
                logger.warning(
                    f"{cid_p}[LLM][VISION] Image fetch 404 from xAI backend for {len(image_urls)} provided URL(s). "
                    f"These were likely stale Discord signed attachment URLs or transient embed previews from recent channel history. "
                    f"Retrying first turn WITHOUT images so the response can still succeed using text + tools (e.g. x_search for X links)."
                )
                try:
                    # Rebuild a text-only input (the llm_input builder + its internal filter will produce plain text content).
                    plain_input_data = await build_responses_input(
                        user_message=user_message,
                        channel_id=channel_id,
                        original_message=original_message,
                        image_urls=[],
                        referenced_context=referenced_context,
                        reply_chain_contexts=reply_chain_contexts,
                        is_reply_continuation=is_reply_continuation,
                        has_x_link_intent=has_x_link_intent,
                        image_gen_intent=pure_image_gen_intent,
                        is_reply_to_bot=is_reply_to_bot,
                        is_mentioned=is_mentioned,
                    )
                    plain_initial_input = plain_input_data["initial_input"]
                    response = await _call_responses_with_retry(
                        client,
                        model=model,
                        input=plain_initial_input,
                        tools=[
                            *native_search_tools,
                            *custom_tools,
                        ],
                        extra_body={"prompt_cache_key": cache_key},
                    )
                    # Success on retry: clear the image flag so downstream logging/metrics treat this as non-vision.
                    image_urls = []
                    logger.info(f"{cid_p}[LLM][VISION] First-turn retry without images succeeded after fetch failure.")
                except Exception as retry_err:
                    logger.error(f"{cid_p}[LLM][VISION] Retry without images also failed: {retry_err}")
                    # Fall through to original hard error path below (will produce the "describe the image" user message).
                    if image_urls:
                        logger.error(
                            f"{cid_p}[LLM][VISION] Responses API call FAILED while sending {len(image_urls)} image(s). "
                            f"Model={model}. Error: {api_err}"
                        )
                        raise RuntimeError(
                            f"Vision request to xAI Responses API failed (likely payload format or 4xx). "
                            f"Images: {len(image_urls)}. Original error: {api_err}"
                        ) from api_err
                    raise
            elif image_urls:
                logger.error(
                    f"{cid_p}[LLM][VISION] Responses API call FAILED while sending {len(image_urls)} image(s). "
                    f"Model={model}. Error: {api_err}"
                )
                raise RuntimeError(
                    f"Vision request to xAI Responses API failed (likely payload format or 4xx). "
                    f"Images: {len(image_urls)}. Original error: {api_err}"
                ) from api_err
            else:
                raise

        # Capture first-turn prompt tokens for addressed metrics (minimal extraction, reuse patterns from _extract)
        first_turn_prompt_tokens = 0
        try:
            usage = getattr(response, "usage", None)
            if usage is None and isinstance(response, dict):
                usage = response.get("usage")
            if usage:
                if hasattr(usage, "input_tokens"):
                    first_turn_prompt_tokens = getattr(usage, "input_tokens", 0) or 0
                elif isinstance(usage, dict):
                    first_turn_prompt_tokens = usage.get("input_tokens", 0) or 0
        except Exception:
            first_turn_prompt_tokens = 0

        # Token logging for first turn
        category = "Vision" if image_urls else "Conversation"
        first_turn_cache_context = {
            "turn_type": "first_turn",
            "query_need": need,
            "has_visual_intent": effective_visual_intent,
            "custom_tools_count": len(custom_tools),
            "custom_tools_set": _infer_tools_set_name(need, effective_visual_intent, False),
            "user_id": user_id,
            "prefix_stability_indicator": f"sys~{stable_prefix_len}",
        }
        _extract_and_log_token_usage(
            response,
            model=model,
            has_images=bool(image_urls),
            category=category,
            is_tool_continuation=False,
            cache_context=first_turn_cache_context,
        )

        # === Multi-round tool calling loop (core orchestration responsibility) ===
        max_tool_rounds = 3
        direct_delivery_performed = False
        MEDIA_ACTION_TOOLS = {"generate_image", "edit_image", "generate_video", "generate_audio", "reply_to_user"}

        def _is_direct_delivery_success(result_str: str, tool_name: str) -> bool:
            """Nested mechanical extraction (Phase 4) of the direct delivery detection.

            Preserves the exact rule that only explicit "delivered directly" success
            phrases from media/reply_to_user tools cause us to set the flag and
            short-circuit / return the DIRECT_DELIVERY_PERFORMED sentinel.
            This invariant is what guarantees no duplicate replies.
            """
            lowered = result_str.lower()
            direct_delivery_success_phrases = [
                "success: image(s) generated and delivered directly",
                "success: edited image(s) delivered directly",
                "success: audio generated and delivered directly",
                "success: video successfully generated and delivered directly",
                "delivered directly to the user",
                "clean direct message delivered to the user",
                "policy blocked; clean direct message delivered",
                "message sent directly to the user"
            ]
            if any(phrase in lowered for phrase in direct_delivery_success_phrases):
                logger.info(f"{cid_p}[LLM] Direct delivery SUCCESS for tool '{tool_name}' ΓÇö suppressing final text reply")
                return True

            if "policy blocked" in lowered and "clean direct message delivered" in lowered:
                return True
            return False

        def _finalize_response(response: Any, direct_delivery_performed: bool, cid_p: str) -> str | object:
            """Mechanical extraction of the final response handling.

            Preserves two key invariants:
            1. If any media or reply_to_user tool performed a direct Discord reply,
               we must return the DIRECT_DELIVERY_PERFORMED sentinel (identity-checked
               in conversation.py) so that no secondary text reply is ever sent.
            2. Otherwise we return the extracted final text (or a generic success message).
            """
            final_text = _extract_final_text(response)

            if direct_delivery_performed:
                logger.info(f"{cid_p}[LLM] Direct media/action delivery performed ΓÇö returning DIRECT_DELIVERY_PERFORMED sentinel (no second reply)")
                return DIRECT_DELIVERY_PERFORMED

            if final_text:
                return final_text.strip()

            return "Γ£à Groksito proces├│ tu mensaje usando las herramientas (respuesta v├¡a Responses API)."

        # Choice flags for addressed metrics (set when we see the model invoke these during tool loop)
        model_chose_search = False
        model_chose_direct = False

        for round_num in range(1, max_tool_rounds + 1):
            client_tool_outputs = []

            output_items = getattr(response, "output", None) or []
            for item in output_items:
                if getattr(item, "type", None) != "function_call":
                    continue

                name = getattr(item, "name", None)
                if name is None and isinstance(item, dict):
                    name = item.get("name")

                # Track model choice for addressed-turn instrumentation (native search vs respond_direct)
                if name in ("web_search", "x_search"):
                    model_chose_search = True
                if name == "respond_directly":
                    model_chose_direct = True

                raw_args = getattr(item, "arguments", None)
                if raw_args is None and isinstance(item, dict):
                    raw_args = item.get("arguments", {})
                if isinstance(raw_args, str):
                    try:
                        raw_args = json.loads(raw_args)
                    except Exception:
                        raw_args = {}

                call_id = getattr(item, "call_id", None) or getattr(item, "id", None)
                if call_id is None and isinstance(item, dict):
                    call_id = item.get("call_id") or item.get("id")

                # Simple debug logging for tool selection *decisions by Grok* (ticket #25).
                # Lightweight + gated by the existing log_tool_selection flag (easy to enable/disable;
                # default True). Focuses on main decision points the model reaches via native tool calling
                # (when search chosen, when recent context requested via get_recent_context, respond_directly
                # vs tools, skill activation, etc.). Particularly useful on normal @mentions where the
                # light decision tool pair (respond_directly + get_recent_context) is offered to let the
                # model decide. Non-intrusive, best-effort, no sensitive values logged, trivial to remove.
                # Uses tools_logger to match other structured tool/decision logs (log_tool_selection etc).
                try:
                    from ..config import settings as _s
                    if getattr(_s, "log_tool_selection", True):
                        from .tools import tools_logger as _tl
                        DECISION_TOOL_NAMES = {
                            "web_search",
                            "x_search",
                            "get_recent_context",
                            "respond_directly",
                            "use_skill",
                            "create_skill",
                            "edit_skill",
                        }
                        if name in DECISION_TOOL_NAMES:
                            _arg_keys = (
                                list((raw_args or {}).keys())
                                if isinstance(raw_args, dict)
                                else []
                            )
                            _tl.info(
                                f"{cid_p}[GROK_CHOICE] tool={name} | round={round_num} | keys={_arg_keys} | addressed={is_addressed}"
                            )
                except Exception:
                    pass

                logger.info(f"{cid_p}[LLM] Round {round_num}: executing custom tool '{name}' (args keys: {list((raw_args or {}).keys()) if isinstance(raw_args, dict) else 'n/a'})")

                available = (name in offered_custom_tool_names) if 'offered_custom_tool_names' in locals() else "unknown"
                tool_type = "custom" if name in (offered_custom_tool_names or set()) else "native-or-unknown"
                try:
                    from .tools import tools_logger
                    tools_logger.debug(
                        f"{cid_p}[TOOLS] execution | tool={name} | available={str(available).lower()} | type={tool_type} | round={round_num}"
                    )
                except Exception:
                    pass

                try:
                    result = await execute_hybrid_tool(
                        name=name or "unknown_tool",
                        args=raw_args if isinstance(raw_args, dict) else {},
                        original_message=original_message,
                        image_urls=image_urls,
                    )
                except Exception as tool_exec_err:
                    logger.exception(f"{cid_p}Tool execution failed for {name}")
                    result = f"Error executing tool {name}: {tool_exec_err}"

                result_str = str(result)

                # Post-tool use_skill activation: prepare injection so we can filter tools
                # on the next continuation round (strong enforcement of allowed tools + the
                # instructions block is already in the tool result the model will see).
                if name == "use_skill" and "__USE_SKILL_ACTIVATED__:" in result_str:
                    try:
                        skill_id = result_str.split("__USE_SKILL_ACTIVATED__:")[1].split("\n", 1)[0].strip()
                        from ..skills.skill_executor import prepare_skill_injection as _prep_inj
                        post_tool_skill_injection = _prep_inj(
                            decision_skill_id=skill_id,
                            user_message=user_message_text,
                        )
                        if post_tool_skill_injection:
                            logger.info(f"{cid_p}[SKILLS] use_skill tool activated post-injection for {skill_id}")
                    except Exception as inj_err:
                        logger.debug(f"{cid_p}[SKILLS] Could not prepare post use_skill injection: {inj_err}")
                logger.info(f"{cid_p}[LLM] Round {round_num}: tool '{name}' completed, result length={len(result_str)}")

                if name in MEDIA_ACTION_TOOLS:
                    if _is_direct_delivery_success(result_str, name):
                        direct_delivery_performed = True

                client_tool_outputs.append({
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": result_str[:4000],
                })

            if not client_tool_outputs:
                logger.info(f"{cid_p}[LLM] Round {round_num}: no more client-side tool calls ΓÇö conversation complete.")
                break

            # Short-circuit optimization for direct media delivery (image/edit/video/audio success path):
            # The tool handler already did orig_msg.reply() with the fun Grok-style message + attachment/URLs.
            # No need to send the SUCCESS result back to the model (which would trigger a 2nd API call
            # just to have the model "stay silent" or say a confirmation we then discard).
            # This makes media generation feel much lighter, faster, and more natural (single roundtrip).
            if direct_delivery_performed:
                logger.info(f"{cid_p}[LLM] Round {round_num}: direct delivery performed ΓÇö short-circuiting (skip sending tool results back + no further rounds). Natural + cheap.")
                break

            logger.info(f"{cid_p}[LLM] Round {round_num}: sending back {len(client_tool_outputs)} tool result(s) using previous_response_id")

            try:
                prev_id = getattr(response, "id", None)
                cache_key = _get_prompt_cache_key(original_message)

                continuation_tools = get_tools_for_request(
                    query_need=need,
                    has_visual_intent=effective_visual_intent,
                    has_explicit_video_intent=explicit_video_intent,
                    has_explicit_audio_intent=explicit_audio_intent,
                    is_tool_continuation=True,
                    pure_image_gen=pure_image_gen_intent,  # ignored inside when is_tool_continuation, but harmless
                    offer_create_skill_tool=False,  # never offer creation/decision tools on continuations
                    offer_decision_tools=False,
                )

                # === Native search tools on continuation ===
                # By default, do NOT re-send native_search_tools (web_search + x_search) on
                # continuation rounds. This is the main token-saving change.
                #
                # Philosophy:
                # - Rely on previous_response_id: the model retains knowledge of tools that were
                #   offered in prior turns of the conversation (see similar logic for custom tools
                #   in get_continuation_tools and aggressive_continuation_tool_minimization).
                # - Re-sending the native tools (especially with their descriptions) on every cont
                #   adds unnecessary prompt tokens.
                # - Custom tools are already aggressively minimized on cont.
                #
                # To still allow follow-up searches when genuinely useful:
                # - We inspect the *previous* response's output for evidence of search usage
                #   (web_search_call items etc.). If search happened in the just-completed round,
                #   we re-include the native tools so Grok can easily do a refined/follow-up search.
                # - If no recent search, send empty list for native on this cont.
                # - This is pragmatic: most continuations after custom tools don't need fresh
                #   search, but if they do (e.g. after seeing a custom result that prompts more
                #   research), we enable it.
                continuation_native_search_tools: list[dict] = []
                try:
                    prev_output = getattr(response, "output", None) or []
                    for item in prev_output:
                        itype = getattr(item, "type", None)
                        if isinstance(item, dict):
                            itype = item.get("type")
                        itype_str = str(itype or "").lower()
                        if itype_str and ("web_search" in itype_str or "x_search" in itype_str or "search_call" in itype_str):
                            continuation_native_search_tools = native_search_tools
                            break
                except Exception:
                    pass

                # Apply post-tool use_skill (or pre-decided) injection filters on continuation tools.
                # This restricts what the model can call in the round where it will produce the
                # final answer after activating a skill via tool. Combined with the strong
                # [SKILL ACTIVE] block in the tool result, this makes the model follow the skill
                # instructions much more reliably.
                try:
                    from ..skills.skill_executor import (
                        filter_native_search_tools as _filter_native,
                        filter_custom_tools as _filter_custom,
                    )
                    from .tools import augment_custom_tools_with_skill_customs
                    active_inj = skill_injection or post_tool_skill_injection
                    if active_inj:
                        # First augment with any skill-specific custom tool schemas (playwright, code exec, etc.)
                        continuation_tools = augment_custom_tools_with_skill_customs(
                            continuation_tools, getattr(active_inj, "allowed_custom", None)
                        )
                        continuation_native_search_tools = _filter_native(continuation_native_search_tools, active_inj)
                        continuation_tools = _filter_custom(continuation_tools, active_inj)
                        logger.debug(f"{cid_p}[SKILLS] Applied active/post-use_skill injection tool filters + custom schema augmentation on continuation")
                except Exception:
                    pass

                offered_custom_tool_names = {t.get("name") for t in continuation_tools if t.get("name")}
                try:
                    img_search = False
                    img_understand = False
                    for t in continuation_native_search_tools:
                        if t.get("type") == "web_search":
                            img_search = t.get("enable_image_search", False)
                            img_understand = t.get("enable_image_understanding", False)
                            break

                    log_tool_selection(
                        turn_type="continuation",
                        query_need=need,
                        has_visual_intent=effective_visual_intent,
                        custom_tools=continuation_tools,
                        native_search_tools=continuation_native_search_tools,
                        enable_image_search=img_search,
                        enable_image_understanding=img_understand,
                    )
                except Exception as log_err:
                    logger.debug(f"{cid_p}[TOOLS] selection logging failed (continuation): {log_err}")

                response = await _call_responses_with_retry(
                    client,
                    model=model,
                    input=client_tool_outputs,
                    previous_response_id=prev_id,
                    tools=[
                        *continuation_native_search_tools,
                        *continuation_tools,
                    ],
                    extra_body={"prompt_cache_key": cache_key},
                )
            except Exception as continue_err:
                # After internal retries, only persistent errors reach here
                logger.warning(f"{cid_p}[LLM] Continuation with previous_response_id failed: {continue_err} ΓÇö stopping tool loop")
                break

            # Token logging for continuation
            continuation_cache_context = {
                "turn_type": "continuation",
                "query_need": need,
                "has_visual_intent": effective_visual_intent,
                "custom_tools_count": len(continuation_tools),
                "custom_tools_set": _infer_tools_set_name(need, effective_visual_intent, True),
                "user_id": user_id,
                "prefix_stability_indicator": f"sys~{stable_prefix_len}",
            }

            _extract_and_log_token_usage(
                response,
                model=model,
                has_images=bool(image_urls),
                category="Tool",
                is_tool_continuation=True,
                cache_context=continuation_cache_context,
            )

        # Emit addressed-turn metrics (Ticket #7 Phase 1 instrumentation) only for addressed; lightweight + defensive.
        if is_addressed and addressed_turn_start is not None:
            try:
                latency_ms = (time.time() - addressed_turn_start) * 1000.0
                from ..utils.token_usage import log_addressed_turn_metrics
                log_addressed_turn_metrics(
                    latency_ms=latency_ms,
                    prompt_tokens=first_turn_prompt_tokens,
                    search_schemas_offered=search_schemas_offered,
                    model_chose_search=model_chose_search,
                    model_chose_direct=model_chose_direct,
                    query_need=need or "unknown",
                )
            except Exception:
                pass  # never break main flow for metrics

        # =====================================================================
        # AUTOMATIC SKILL CREATION (recurring need detection) ΓÇö after the main work
        # When a clear recurring pattern is detected (conservative: multiple times in a short
        # recent window + semantic filters against game "skills"/builds), we create the skill
        # directly with approved=True. A short natural confirmation is sent as a follow-up.
        # No proposal/approval dance. Old proposal flow is bypassed.
        # =====================================================================
        # Defensive: may not be set if we short-circuited before the tool loop
        if "direct_delivery_performed" not in locals():
            direct_delivery_performed = False

        try:
            auto_enabled = True
            try:
                auto_enabled = bool(getattr(_settings, "enable_skill_auto_creation", True))
            except Exception:
                pass

            # Only attempt if we didn't just use an existing skill and auto-create is on.
            # When we offered the decision tool set (including create_skill) to the model this turn,
            # we let the model decide via native tool calling instead of the old rule-based detector.
            can_auto_create = (
                auto_enabled
                and not locals().get("offer_decision_tools", False)
                and (decision is None or getattr(decision, "action", None) is None or decision.action.value != "use_skill")
            )

            if can_auto_create and original_message is not None:
                from ..skills.skill_proposer import detect_and_create_skill
                creation = await detect_and_create_skill(
                    channel_id=channel_id,
                    user_id=int(user_id) if user_id and user_id.isdigit() else 0,
                    current_message=user_message_text,
                    # min_occurrences and window are read inside the function from settings
                )
                if creation and creation.confirmation_message:
                    conf_text = creation.confirmation_message
                    # Fire a non-blocking, short, natural confirmation after the main reply
                    async def _send_creation_confirmation(msg=original_message, text=conf_text):
                        try:
                            await asyncio.sleep(1.1)
                            await msg.channel.send(text)
                            logger.info(f"{cid_p}[SKILLS] Sent auto-creation confirmation for '{creation.skill.name}'")
                        except Exception as send_err:
                            logger.debug(f"{cid_p}[SKILLS] Could not send creation confirmation: {send_err}")

                    try:
                        if not direct_delivery_performed:
                            asyncio.create_task(_send_creation_confirmation())
                    except Exception:
                        pass
        except Exception as create_err:
            logger.debug(f"{cid_p}[SKILLS] Auto skill creation check skipped (non-fatal): {create_err}")

        # Final extraction + sentinel handling
        return _finalize_response(response, direct_delivery_performed, cid_p)

    except Exception as e:
        logger.exception(f"{cid_p}Error during real Responses API call + tool loop")

        if image_urls:
            logger.warning(f"{cid_p}[LLM][VISION] Failing request had {len(image_urls)} image(s) attached.")
            return (
                "Tuve problemas procesando la(s) imagen(es) que enviaste. "
                "El servicio de visi├│n de Grok est├í teniendo dificultades con este archivo en este momento. "
                "Por favor, describe lo que ves en la imagen con palabras y te ayudo con eso."
            )

        # Lightweight classification for common transient/user-facing cases (after retries exhausted in helper)
        if isinstance(e, (RateLimitError,)) or "rate" in str(e).lower() or "429" in str(e).lower():
            return "Estoy recibiendo muchas solicitudes ahora mismo (rate limit). Dame un minuto e intenta de nuevo."
        if isinstance(e, (APITimeoutError, APIConnectionError)) or "timeout" in str(e).lower() or "connection" in str(e).lower():
            return "Tuve un problema de conexi├│n con Grok. Intenta de nuevo en unos segundos."

        # Server errors after retries
        if isinstance(e, APIError):
            status = getattr(e, "status_code", None)
            if status and 500 <= status < 600:
                return "El servicio de Grok est├í teniendo problemas temporales (5xx). Intenta de nuevo en un momento."
            if status in (401, 403):
                # Common with expired/revoked OAuth or tier gates on the oauth surface
                hint = ""
                try:
                    from ..core.grok_oauth import get_grok_bearer
                    if settings.auth_prefers_oauth or settings.using_oauth:
                        hint = " (OAuth token may be invalid/expired or tier-restricted ΓÇö try `python -m src.groksito_discord --login-oauth` or switch to XAI_API_KEY)"
                except Exception:
                    pass
                return f"Problema de autenticaci├│n con Grok (401/403).{hint}"

        return f"Lo siento, tuve un problema conectando con Grok: {e}"

    # (auth error hints are also emitted by get_grok_bearer / refresh logic and --test-auth)


# Backwards-compatibility aliases (used by some older wiring / tests)
_call_grok_with_tools = call_grok_for_groksito
_call_grok_responses_api = call_grok_for_groksito

# Public alias for call_grok_with_tools (kept for compatibility)
# This allows existing call sites (e.g. conversation.py) to continue working without immediate breakage.
call_grok_with_tools = call_grok_for_groksito
