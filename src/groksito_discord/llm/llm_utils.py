"""
LLM Utility Helpers for Groksito.

Pure helpers and side-effect functions supporting the LLM layer.
Called by llm.py (orchestrator) and llm_input.py.

Input construction logic lives exclusively in llm_input.py.
"""

from __future__ import annotations

import asyncio
import logging
import random
import unicodedata
from typing import Any

from ..utils.correlation import cid_prefix

from openai import (
    AsyncOpenAI,
    RateLimitError,
    APIError,
    APITimeoutError,
    APIConnectionError,
)

from ..config import settings
from .prompt_builder import SUMMARIZATION_PROMPT, get_native_search_descriptions
from ..context import (
    get_estimated_history_tokens,
    get_messages_for_summarization,
    update_channel_summary,
    is_pure_image_generation_request,
)
from ..utils.token_usage import (
    log_usage,
    log_cache_metrics,
)

# Centralized light intent detectors (post #24 cleanup of heavy versions).
# Re-exported here for backward compat with any remaining imports in
# llm.py / conversation.py / call sites. Light non-brittle implementations
# live in intents.py.
from ..core.intent import (
    _detect_visual_intent,
    _detect_image_creation_intent,
    is_image_edit_request,
    is_pure_video_generation_request,
)

logger = logging.getLogger("groksito.llm")


def _extract_final_text(response: Any) -> str | None:
    """Extract the final assistant text from a Responses API response."""
    if response is None:
        return None

    if hasattr(response, "output_text") and response.output_text:
        return str(response.output_text)

    output = getattr(response, "output", None) or []
    for item in output:
        if getattr(item, "type", None) == "message":
            content = getattr(item, "content", None) or []
            for c in content:
                if getattr(c, "type", None) == "text":
                    txt = getattr(c, "text", None)
                    if txt:
                        return str(txt)
                if hasattr(c, "text") and getattr(c, "text"):
                    return str(getattr(c, "text"))

        if isinstance(item, dict) and item.get("type") == "message":
            for c in item.get("content", []) or []:
                if isinstance(c, dict) and c.get("type") == "text":
                    if c.get("text"):
                        return str(c.get("text"))
                if isinstance(c, dict) and c.get("text"):
                    return str(c.get("text"))

    try:
        if output:
            last = output[-1]
            if hasattr(last, "text") and last.text:
                return str(last.text)
            if isinstance(last, dict) and last.get("text"):
                return str(last.get("text"))
    except Exception:
        pass

    return None


def _build_stub_response(
    user_message: str, author_name: str, image_urls: list[str] | None
) -> str:
    """Fallback when no API key or during early development."""
    vision_note = f"\n[Imágenes: {len(image_urls or [])}]" if image_urls else ""
    return (
        f"✅ Groksito recibió tu mensaje (modo sin API key / desarrollo).\n\n"
        f"Usuario: {author_name}\n"
        f"Mensaje: {(user_message or '')[:300]}{vision_note}"
    )


def _get_prompt_cache_key(original_message: Any) -> str:
    """
    Returns a stable per-user prompt_cache_key for xAI Prompt Caching.
    (Exact original implementation preserved.)
    """
    try:
        author = getattr(original_message, "author", None)
        user_id = getattr(author, "id", None) if author else None
        if user_id:
            return f"groksito-user-{user_id}"
    except Exception:
        pass
    return "groksito-default-user"


def _build_native_search_tools(
    query_text: str,
    context_need: str,
    has_visual_intent: bool,
    has_attached_images: bool,
) -> list[dict]:
    """
    Build native xAI web_search + x_search tool schemas.

    Prompt-driven philosophy (#48): on normal/rich addressed turns, offer both native
    search tools and let Grok decide when to call them. Skip only on ultra-light paths
    (casual/minimal/image_gen) where search is unlikely to help.
    """
    if context_need in ("casual", "minimal", "image_gen"):
        return []

    try:
        if is_pure_image_generation_request(query_text) and not has_attached_images:
            return []
    except Exception:
        pass

    # Concise schemas (prompt-driven #48); descriptions from prompt_builder (single source).
    web_desc, x_desc = get_native_search_descriptions(query_text)

    web_tool: dict = {
        "type": "web_search",
        "description": web_desc,
    }

    x_tool: dict = {
        "type": "x_search",
        "description": x_desc,
    }

    if has_visual_intent or has_attached_images or _detect_visual_intent(query_text):
        web_tool["enable_image_search"] = True
        web_tool["enable_image_understanding"] = True

    return [web_tool, x_tool]


def _infer_tools_set_name(
    query_need: str, has_visual_intent: bool, is_continuation: bool
) -> str:
    """Produces a short, consistent label for the custom tool set used (for logging)."""
    if is_continuation:
        return "continuation-visual" if has_visual_intent else "continuation-minimal"
    if query_need == "casual":
        return "casual-none"
    if query_need == "minimal":
        return "minimal-core"
    if query_need == "rich":
        return "rich"
    return "normal"


def _extract_and_log_token_usage(
    response: Any,
    model: str,
    has_images: bool = False,
    category: str = "Conversation",
    is_tool_continuation: bool = False,
    cache_context: dict | None = None,
) -> None:
    """
    Safely extracts usage information from a Responses API response and logs it.
    (Exact original implementation preserved, including all fallback paths.)
    """
    try:
        usage = getattr(response, "usage", None)
        if usage is None and isinstance(response, dict):
            usage = response.get("usage")

        if not usage:
            logger.debug(
                f"{cid_prefix()}[TOKENS] No usage object found on response for category={category}"
            )
            return

        prompt = 0
        completion = 0
        total = 0

        if hasattr(usage, "input_tokens"):
            prompt = getattr(usage, "input_tokens", 0) or 0
            completion = getattr(usage, "output_tokens", 0) or 0
            total = getattr(usage, "total_tokens", prompt + completion) or 0
        elif isinstance(usage, dict) and "input_tokens" in usage:
            prompt = usage.get("input_tokens", 0)
            completion = usage.get("output_tokens", 0)
            total = usage.get("total_tokens", prompt + completion)

        if (prompt == 0 and completion == 0) and hasattr(usage, "prompt_tokens"):
            prompt = getattr(usage, "prompt_tokens", 0) or 0
            completion = getattr(usage, "completion_tokens", 0) or 0
            total = getattr(usage, "total_tokens", prompt + completion) or 0
        elif (prompt == 0 and completion == 0) and isinstance(usage, dict):
            prompt = usage.get("prompt_tokens", 0)
            completion = usage.get("completion_tokens", 0)
            total = usage.get("total_tokens", prompt + completion)

        if prompt == 0 and completion == 0:
            logger.debug(
                f"{cid_prefix()}[TOKENS] Usage object found but no token numbers for category={category}"
            )
            return

        cached = 0
        try:
            # Responses API path (primary)
            if hasattr(usage, "input_tokens_details"):
                details = getattr(usage, "input_tokens_details", None)
                if details:
                    cached = getattr(details, "cached_tokens", 0) or 0
            elif isinstance(usage, dict):
                details = usage.get("input_tokens_details", {})
                if isinstance(details, dict):
                    cached = details.get("cached_tokens", 0)

            # Chat Completions / fallback compatibility (some responses or SDK paths use prompt_tokens_details)
            if not cached:
                if hasattr(usage, "prompt_tokens_details"):
                    pdetails = getattr(usage, "prompt_tokens_details", None)
                    if pdetails:
                        cached = getattr(pdetails, "cached_tokens", 0) or 0
                elif isinstance(usage, dict):
                    pdetails = usage.get("prompt_tokens_details", {})
                    if isinstance(pdetails, dict):
                        cached = pdetails.get("cached_tokens", 0) or cached

            # Guard against nonsense / block-granularity artifacts (xAI often reports exactly 128
            # for the first cache block on light prefixes; our ultra-minimal design frequently hits
            # this minimum reusable chunk size. Not a code bug, but we surface it cleanly and
            # never let cached exceed the actual prompt tokens reported).
            if cached and prompt > 0:
                cached = min(cached, prompt)
            if cached < 0:
                cached = 0
        except Exception:
            pass

        log_usage(
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=total,
            model=model,
            category=category,
            has_images=has_images,
            tool_round=is_tool_continuation,
            cached_tokens=cached,
        )

        if cache_context:
            try:
                log_cache_metrics(
                    prompt_tokens=prompt,
                    completion_tokens=completion,
                    total_tokens=total,
                    cached_tokens=cached,
                    **cache_context,
                )
            except Exception as cache_log_err:
                logger.debug(
                    f"{cid_prefix()}[CACHE] metrics logging failed: {cache_log_err}"
                )

        # Observability for the frequent "128" reports: with our extreme-light prefixes (tiny sys + zero ctx on most turns)
        # the effective cacheable stable prefix is often exactly one 128-token block. This is *expected normal behavior*
        # of xAI prompt cache block granularity + our "maximum nativeness / ultra-light" choices (not a bug or stuck counter).
        # Extraction is now more accurate (supports both details shapes + guards). Log at debug for visibility.
        if cached == 128 and prompt < 300:
            logger.debug(
                f"{cid_prefix()}[TOKENS] cached=128 (normal min-block granularity for light prefix) prompt={prompt} cat={category}"
            )

    except Exception as err:
        logger.warning(
            f"{cid_prefix()}[TOKENS] Exception while extracting usage: {err}"
        )


# =============================================================================
# Lightweight Responses API resilience (for main conversational calls + optional summary)
# =============================================================================


async def _call_responses_with_retry(client: AsyncOpenAI, **kwargs) -> Any:
    """
    Call client.responses.create with improved resilience for transient errors only.

    Retries (with exp backoff + jitter):
      - RateLimitError (429)
      - APITimeoutError / APIConnectionError (network)
      - APIError with 5xx status (server errors)
    Fail fast (no retry): auth (401/403), bad requests (4xx non-429), policy/422 content errors, client errors.
    This keeps happy-path latency unchanged and respects "fail fast for user errors".

    Configuration comes from central settings (api_max_retries, api_retry_base_delay_seconds)
    so behavior is tunable without code changes. Defaults preserve prior behavior.
    """
    max_attempts = getattr(settings, "api_max_retries", 3)
    base_delay = getattr(settings, "api_retry_base_delay_seconds", 0.5)
    max_delay = 8.0  # pragmatic cap to avoid excessive waits even on long backoff

    for attempt in range(1, max_attempts + 1):
        try:
            return await client.responses.create(**kwargs)
        except (RateLimitError, APITimeoutError, APIConnectionError) as e:
            if attempt == max_attempts:
                logger.warning(
                    f"{cid_prefix()}[LLM][RETRY] Transient error after {max_attempts} attempts: {type(e).__name__} (exhausted)"
                )
                raise
            # Full jitter: random delay in [0, base * 2**(attempt-1)] — recommended to avoid thundering herd on rate limits
            raw_delay = base_delay * (2 ** (attempt - 1))
            delay = min(max_delay, random.uniform(0, raw_delay))
            logger.info(
                f"{cid_prefix()}[LLM][RETRY] Transient ({type(e).__name__}) on responses.create "
                f"attempt {attempt}/{max_attempts} — retry in {delay:.2f}s (jittered)"
            )
            await asyncio.sleep(delay)
            continue
        except APIError as e:
            status = getattr(e, "status_code", None)
            is_server_error = status and 500 <= status < 600
            is_rate = status == 429
            if (is_server_error or is_rate) and attempt < max_attempts:
                raw_delay = base_delay * (2 ** (attempt - 1))
                delay = min(max_delay, random.uniform(0, raw_delay))
                logger.info(
                    f"{cid_prefix()}[LLM][RETRY] Server/rate error {status} on responses.create "
                    f"attempt {attempt}/{max_attempts} — retry in {delay:.2f}s"
                )
                await asyncio.sleep(delay)
                continue
            # Non-transient (auth 401/403, bad payload 400, policy, etc.) or final attempt: propagate
            # so outer handlers (vision special case, user message classification) can decide.
            raise
        except Exception:
            # Unknown / unexpected — never swallow. Let caller (call_grok_for_groksito) classify.
            # This includes things like validation errors in the openai lib itself.
            raise


async def _maybe_proactive_summarize(
    channel_id: int,
    original_message: Any,
    client: AsyncOpenAI,
) -> None:
    """
    (Optional) Proactive summarization of older channel history.
    Disabled by default for maximum Grok nativeness — the base model + full history
    (plus smart/referenced context) is trusted. Only runs if explicitly enabled in config.
    """
    try:
        if not getattr(settings, "summarization_enabled", False):
            return

        threshold = int(getattr(settings, "summarization_threshold_tokens", 6000))
        keep_recent = 6

        estimated_tokens = get_estimated_history_tokens(channel_id)

        if estimated_tokens < threshold:
            return

        older_messages = get_messages_for_summarization(
            channel_id, keep_recent=keep_recent
        )
        if not older_messages or len(older_messages) < 5:
            return

        conversation_text = "\n".join(
            f"[{m.get('author', '?')}]: {m.get('content', '')[:400]}"
            for m in older_messages
        )

        summary_model = getattr(settings, "grok_model", None) or "grok-4.3"
        summary_response = await _call_responses_with_retry(
            client,
            model=summary_model,
            input=[
                {
                    "role": "system",
                    "content": SUMMARIZATION_PROMPT.format(
                        conversation_text=conversation_text
                    ),
                },
                {
                    "role": "user",
                    "content": "Resume los mensajes antiguos de forma concisa y útil.",
                },
            ],
        )

        summary_text = _extract_final_text(summary_response) or ""
        if not summary_text or len(summary_text) < 30:
            return

        if (
            "sin información" in summary_text.lower()
            or "sin contenido" in summary_text.lower()
        ):
            return

        update_channel_summary(channel_id, summary_text)

        summarized_tokens = sum(len(m.get("content", "")) for m in older_messages) // 4
        new_summary_tokens = len(summary_text) // 4

        logger.info(
            f"{cid_prefix()}[CONTEXT] Proactive conversation summarization for channel {channel_id}: "
            f"old~{summarized_tokens}t → summary~{new_summary_tokens}t "
            f"(kept last {keep_recent}). Savings ~{max(0, summarized_tokens - new_summary_tokens)}t. "
            "Older history compacted automatically."
        )

    except Exception as e:
        logger.warning(
            f"{cid_prefix()}[CONTEXT] Proactive summarization failed for channel {channel_id}: {e}"
        )
