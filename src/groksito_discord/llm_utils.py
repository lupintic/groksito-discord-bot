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

from .correlation import cid_prefix

from openai import AsyncOpenAI, RateLimitError, APIError, APITimeoutError, APIConnectionError

from .config import settings
from .prompt import SUMMARIZATION_PROMPT
from .context import (
    get_estimated_history_tokens,
    get_messages_for_summarization,
    update_channel_summary,
    is_pure_image_generation_request,
)
from .token_usage import (
    log_usage,
    log_cache_metrics,
)

# Centralized intent detectors (Phase 5). Re-exported here so that all
# existing imports `from .llm_utils import _detect_*` (in llm.py and
# conversation.py) continue to work without any changes.
from .intents import (
    _detect_visual_intent,
    _detect_image_creation_intent,
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
    Builds the native xAI web_search + x_search tool schemas (with optional image capabilities).

    PHILOSOPHY (maximum nativeness, minimal heuristics, token efficiency):
    - On first-turn "normal" and "rich" queries: offer native search tools BROADLY by default
      (web_search for fresh info; x_search only on clear X/Twitter signals — see below).
      This lets Grok itself decide (using its judgment + the improved SYSTEM_PROMPT and rich
      tool descriptions we provide) whether it actually needs fresh/current information or
      can answer from training knowledge.
    - The primary control for *whether any native search schema is sent at all* is context_need
      (casual/minimal/image_gen -> zero; normal/rich -> consider). Upstream classify_query_context_need
      + has_x_link_intent in llm_input also influence "normal" vs lower for borderline cases.
      There are *no* heuristics that decide *whether the model should call* a search; only which
      schemas to declare (to avoid shipping heavy descriptions for irrelevant tools).
    - Strict laziness preserved: ZERO native search tools on "casual", "minimal", and "image_gen"
      turns (and pure image gen cases). This keeps prompt size small where search is unlikely needed.
    - The model (via Responses API + our prompt/descs) does the real decision of *when* to call
      and *how to synthesize results*.
    - web_search and x_search are offered *conditionally/independently*: general fresh -> web_search;
      only clear X signals -> x_search. This saves the cost of the unused tool's description
      (~90-95 tokens) on many turns.
    - x_search offering is deliberately (and iteratively) STRICTER (clear X/Twitter signals only)
      to reduce how often its schema + potential heavy result payloads are injected on normal turns.
      This pass further removed remaining noisy terms (see signal comment below). Grok can still
      fall back to web_search (or knowledge) for marginal social queries. This is the main lever
      for the "reduce x_search offers" goal without losing nativeness.

    We include focused, efficiency-oriented descriptions in the schemas so Grok knows exactly
    the purpose of each, when to use which, and — critically — how to use results concisely.
    Combined with SYSTEM_PROMPT, this keeps behavior native while cutting average tokens.

    Continuation turns re-use whatever was decided on first turn (kept as-is for minimalism on custom side).
    """
    # Strong laziness for nativeness: never offer search tools on the lightest turns.
    # This now also covers "minimal" so that even "minimal" turns with time words
    # get zero native tools (very low context + no search schemas).
    if context_need in ("casual", "minimal", "image_gen"):
        return []

    # Pure image generation almost never needs external search (even if query text had fresh-ish words).
    # This is an additional safety net (in addition to the image_gen context_need path).
    try:
        if is_pure_image_generation_request(query_text) and not has_attached_images:
            return []
    except Exception:
        pass

    # Normalize for lightweight signal detection (to decide web vs x independently, saving tokens
    # by not sending unnecessary tool descriptions when only one is relevant).
    q = unicodedata.normalize('NFKD', (query_text or '').lower())
    q = ''.join(c for c in q if not unicodedata.combining(c))

    # STRICTER X-SPECIFIC SIGNAL DETECTION (refinement pass for even fewer x_search offers):
    # Goal: offer the x_search tool schema (and its result payloads) ONLY on *clear, unambiguous*
    # signals of X/Twitter intent. On normal turns this saves shipping the (lengthy) x_search
    # description + reduces likelihood the model will invoke the often-verbose X results tool
    # on marginal social/general "what do people think" queries (where web_search or knowledge
    # suffice).
    #
    # This is a *further* tightening on top of prior cleanup:
    # - Dropped "twit" entirely (false-positives on "twitch" streaming discussions + generic
    #   short "twit" / witty comments unrelated to X).
    # - Replaced standalone "tendencia" with "en tendencia" ( "tendencia" is extremely common
    #   for any trend: design, market, fashion, crypto; "en tendencia" is the idiomatic X
    #   "trending" phrasing).
    # - Removed the broad social-listening group ("qué dicen", "que dicen", "qué se dice",
    #   "opiniones en", "reacciones en"). These are *very* noisy in Spanish: they match
    #   "qué dicen los expertos", "que dicen las noticias", "opiniones en el foro",
    #   "reacciones en el video", "qué se dice de la película", "qué dicen en la calle", etc.
    #   — almost never X-specific.
    # - Coverage for legitimate X social queries is preserved *without* the broad terms:
    #     * "qué dicen en x...", "reacciones en x", "opiniones en x", "qué se dice en x" →
    #       caught by the "en x" guard below.
    #     * "... en twitter", "... en x.com" → hit "twitter"/"x.com".
    #     * Any mention of "tweet"/"tweets", "trending", "post en x", "este tweet" etc. still work.
    # - Still a flat list + the single existing bounded guard. Simple, non-brittle, no regex
    #   or per-word tokenization. "Nativo" preserved: Grok decides calls; we only control
    #   expensive schema presence. Signals used *only* for token-saving schema decisions.
    x_signals = [
        "tweet", "tweets",                   # core X terminology (very strong, safe signal)
        "x.com", "twitter.com", "twitter",   # direct links + mentions (covers "en twitter" etc.)
        "trending",                          # English "trending" is highly X-specific
        "en tendencia",                      # precise Spanish X phrasing (replaces noisy bare "tendencia")
        "este tweet", "el tweet", "tweet de", "tweets sobre",  # tweet-specific reply phrasing
        "post en x", "posts en x", "post de x",  # only qualified "post" that mention the platform
        # (broad social phrases removed; see comment above for rationale + how X cases remain covered)
    ]
    include_x = any(sig in q for sig in x_signals)

    # Extra guard for the ambiguous "en x" platform reference (Spanish "en X" = on X).
    # This guard is now *even more important* because we removed the broad "qué dicen / opiniones en / reacciones en"
    # from the main list; many natural X queries ("qué dicen en x", "reacciones en x al anuncio", "opiniones en x")
    # will land here.
    # Only treat as X signal if "en x" occurrence is not part of common time/quantity false-positives.
    # Uses bounded (non-alpha adjacent) matching so "ahora" does not match "hora", "paso" etc.
    if not include_x and "en x" in q:
        false_time_or_other = [
            "minuto", "minutos", "hora", "horas", "dia", "días", "día", "segundo", "segundos",
            "vez", "veces", "ocasión", "version", "versión", "formato", "punto", "caso",
        ]
        def _has_bounded(needle: str, hay: str) -> bool:
            if needle not in hay:
                return False
            idx = hay.find(needle)
            before = hay[idx-1] if idx > 0 else " "
            after = hay[idx + len(needle)] if idx + len(needle) < len(hay) else " "
            # true only if the occurrence is bounded by non-letters (word-like boundary)
            return not before.isalpha() and not after.isalpha()
        if not any(_has_bounded(fp, q) for fp in false_time_or_other):
            # Accept as X platform signal (weak but useful). This is the main path for
            # "qué dicen en x ...", "reacciones/opiniones en x", "novedades en x", "pasa en x" etc.
            # (Combined with has_general it usually still offers web too; pure cases drop web.)
            include_x = True

    # For broad offering on normal/rich: include web by default.
    # But for pure X-specific (no general fresh keywords) and not rich: offer only x_search to save
    # the web description tokens (~90 tokens).
    general_keywords = ["hoy", "ahora", "actual", "última", "ayer", "anoche", "reciente",
                        "noticia", "noticias", "news", "breaking", "precio", "precios", "dólar",
                        "busca", "search", "clima", "partido", "en vivo", "pasó", "paso"]
    has_general = any(kw in q for kw in general_keywords)
    include_web = True
    if include_x and not has_general and context_need != "rich":
        include_web = False

    web_tool = None
    x_tool = None

    if include_web:
        web_tool = {
            "type": "web_search",
            "description": (
                "Search the web for up-to-date information (news, prices e.g. USD, weather, sports, recent events, "
                "changing data). Use proactively ONLY for time-sensitive or variable facts that improve accuracy "
                "over your training cutoff: 'what is happening', current prices (e.g. dólar), live scores, "
                "breaking news, weather, or very recent events. NEVER use for general knowledge, history, "
                "definitions, math, code, or timeless topics — answer from your knowledge.\n\n"
                "EFFICIENT USAGE (MANDATORY - this directly reduces tokens on every search turn):\n"
                "- MUST craft the *narrowest, most specific* query possible that targets exactly the fact (e.g. "
                "'dólar blue hoy Argentina site:ambito.com' or 'resultado boca river live' — never broad 'dólar' or 'partido').\n"
                "- From results: extract and retain *AT MOST the 1-2 most relevant facts or data points*. "
                "Immediately discard ads, nav, related stories, full pages, boilerplate, and anything not directly answering.\n"
                "- FINAL ANSWER SYNTHESIS RULE (critical): the searched info must contribute *at most 1 crisp sentence* "
                "(or 1 tiny bullet if user explicitly wanted a list). State as established fact. Optional tiny (source). "
                "NEVER: quote excerpts, list multiple results, repeat the query, explain that you searched, show alternatives considered, or let search increase overall answer length.\n"
                "Internal reasoning over results must stay minimal and invisible to user. Goal: freshest facts, lowest possible token cost."
            ),
        }

    if include_x:
        x_tool = {
            "type": "x_search",
            "description": (
                "Search posts, threads, opinions and recent activity on X (Twitter). Use ONLY for clear "
                "X/Twitter-specific needs: tweets, real-time reactions, trending topics, current event "
                "commentary on X, profiles, or content behind x.com links. Mentions of 'on X', 'tweet', "
                "x.com links, 'qué dicen en X/Twitter', or 'trending' are strong signals. Ideal for social "
                "pulse/context that web_search captures poorly.\n\n"
                "EFFICIENT USAGE (MANDATORY - X result sets are especially verbose; this is a major token lever):\n"
                "- Use *only* when query is explicitly about X activity (do not default here for general 'qué opinan' or 'qué dicen').\n"
                "- From results: keep *AT MOST 1-2 posts/reactions* (the most on-point). Discard noise, duplicates, low-signal replies, full threads, and timelines immediately.\n"
                "- FINAL ANSWER SYNTHESIS RULE (strict): distill to *1 short sentence or 1-2 word summary of prevailing sentiment/fact*. Optional minimal source. "
                "NEVER paste posts, usernames, raw text, multiple conflicting items, or 'here is what X is saying...'. Do not let X results inflate answer length or add meta/reasoning.\n"
                "Combine with web_search only when both genuinely add unique value. Internal use of results must be invisible."
            ),
        }

    enable_image_search = False
    enable_image_understanding = False

    visual_signal = (
        has_visual_intent
        or has_attached_images
        or _detect_visual_intent(query_text)
    )

    if visual_signal:
        enable_image_search = True
        enable_image_understanding = True
    elif context_need == "rich":
        # On rich turns we are more generous with image understanding for search results
        enable_image_understanding = True

        qq = (query_text or "").lower()
        if any(kw in qq for kw in ("imágenes", "imagenes", "fotos", "pictures", "images of", "muéstrame", "show me images", "fotos de", "imágenes de", "busca imágenes")):
            enable_image_search = True

    if include_web:
        if enable_image_search:
            web_tool["enable_image_search"] = True
        if enable_image_understanding:
            web_tool["enable_image_understanding"] = True

    tools = []
    if include_web:
        tools.append(web_tool)
    if include_x:
        tools.append(x_tool)

    return tools


def _infer_tools_set_name(query_need: str, has_visual_intent: bool, is_continuation: bool) -> str:
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
            logger.debug(f"{cid_prefix()}[TOKENS] No usage object found on response for category={category}")
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
            logger.debug(f"{cid_prefix()}[TOKENS] Usage object found but no token numbers for category={category}")
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
                logger.debug(f"{cid_prefix()}[CACHE] metrics logging failed: {cache_log_err}")

        # Observability for the frequent "128" reports: with our extreme-light prefixes (tiny sys + zero ctx on most turns)
        # the effective cacheable stable prefix is often exactly one 128-token block. This is *expected normal behavior*
        # of xAI prompt cache block granularity + our "maximum nativeness / ultra-light" choices (not a bug or stuck counter).
        # Extraction is now more accurate (supports both details shapes + guards). Log at debug for visibility.
        if cached == 128 and prompt < 300:
            logger.debug(f"{cid_prefix()}[TOKENS] cached=128 (normal min-block granularity for light prefix) prompt={prompt} cat={category}")

    except Exception as err:
        logger.warning(f"{cid_prefix()}[TOKENS] Exception while extracting usage: {err}")


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
                logger.warning(f"{cid_prefix()}[LLM][RETRY] Transient error after {max_attempts} attempts: {type(e).__name__} (exhausted)")
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

        older_messages = get_messages_for_summarization(channel_id, keep_recent=keep_recent)
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
                    "content": SUMMARIZATION_PROMPT.format(conversation_text=conversation_text),
                },
                {"role": "user", "content": "Resume los mensajes antiguos de forma concisa y útil."},
            ],
        )

        summary_text = _extract_final_text(summary_response) or ""
        if not summary_text or len(summary_text) < 30:
            return

        if "sin información" in summary_text.lower() or "sin contenido" in summary_text.lower():
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
        logger.warning(f"{cid_prefix()}[CONTEXT] Proactive summarization failed for channel {channel_id}: {e}")
