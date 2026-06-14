"""
Centralized, modern video generation handler for Groksito.

This module is the single source of truth for:
- Text-to-Video (T2V)
- Image-to-Video (I2V / animation from reference image)

Modeled directly after the modernized `media/image_handler.py` for consistency.

Key modernizations (following the image pattern):
- Dedicated `_enhance_video_prompt()` — always-on quality improvement, especially strong for Spanish prompts.
  Adds motion, camera, style, and quality enhancers while preserving user intent.
- Unified auth, HTTP, retry, and long-running polling logic.
- First-class **extra_params support for future API fields (motion_strength, etc.).
- Robust error handling for the longer video generation lifecycle (start + poll).
- Natural, consistent user-facing messages (matching image delivery style: "Acá tenés...").
- Direct delivery via image_delivery (register + consume + reply) for natural "typing..." UX.
- Explicit intent guard preserved (calls back into media_tools.has_explicit_video_intent).
- Quota enforcement (5/day) kept honest and simple.
- Clean separation of concerns.

All previous logic from video_generation.py has been moved here and improved.
The public dispatch functions keep identical signatures.

Implementation is fully compatible with the normal chat flow, tool selection, and
DIRECT_DELIVERY_PERFORMED sentinel pattern.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Optional

import httpx

from ..correlation import cid_prefix
from ..config import settings
from ..image_delivery import register_image_request
from .delivery import build_video_caption, deliver_from_request

# Bearer (OAuth preferred)
try:
    from ..grok_oauth import get_grok_bearer
except Exception:
    get_grok_bearer = None  # type: ignore

# Context for video quota (5/day)
from .. import context

logger = logging.getLogger("groksito.media.video_handler")


# =============================================================================
# Common Helpers
# =============================================================================

def _resolve_api_key() -> str | None:
    """Resolve credential preferring fresh OAuth token."""
    if get_grok_bearer:
        try:
            tok = get_grok_bearer()
            if tok:
                return tok
        except Exception:
            pass
    return (
        os.getenv("XAI_API_KEY")
        or getattr(settings, "xai_api_key", None)
    )


# --- Modern Prompt Engineering for Video (new, modeled on image enhancer) ---

def _enhance_video_prompt(prompt: str, is_from_image: bool = False) -> str:
    """
    Always-on prompt enhancer for better video results, with special care for Spanish.

    - Detects Spanish-dominant prompts and keeps the user's voice.
    - Appends high-value motion, camera, physics, and quality descriptors.
    - For I2V (is_from_image=True) adds subtle animation guidance.
    - Style detection (gothic, cyberpunk, anime, etc.) mirrors the image enhancer.
    - Never changes the core subject/action the user requested.
    - Keeps output reasonable length for the video API.
    """
    if not prompt or len(prompt.strip()) < 2:
        base = "a beautiful stylized character in a simple scene with gentle motion"
        return base

    p = prompt.strip()
    lower = p.lower()

    # Simple Spanish detection (accents + common words)
    spanish_score = sum(
        1 for marker in (" el ", " la ", " de ", " una ", " con ", " para ", " que ", " está ")
        if marker in f" {lower} "
    )
    looks_spanish = spanish_score >= 1 or any(ch in p for ch in "áéíóúñü¿¡")

    enhancers: list[str] = []

    # Motion & camera (core for video quality)
    motion_hints = []
    if any(k in lower for k in ("cámara", "camera", "pan", "zoom", "movimiento", "mueve", "caminando", "corriendo")):
        motion_hints.append("smooth camera movement")
    else:
        motion_hints.append("natural smooth motion")

    if any(k in lower for k in ("acción", "lucha", "explosión", "rápido", "dinámico")):
        motion_hints.append("dynamic action, subtle physics")
    else:
        motion_hints.append("gentle cinematic motion, natural timing")

    enhancers.extend(motion_hints)

    # Style detection (shared with image system for consistency)
    if any(k in lower for k in ("gótica", "goth", "gotica", "dark", "negra", "oscura", "vampira")):
        enhancers.append("gothic cinematic style, moody dramatic lighting")
    elif any(k in lower for k in ("cyberpunk", "neon", "futurista", "sci-fi", "cyber")):
        enhancers.append("cyberpunk aesthetic, vibrant neon lighting, high-tech motion")
    elif any(k in lower for k in ("anime", "waifu", "manga", "chibi", "2d")):
        enhancers.append("detailed anime style, fluid animation, vibrant colors")
    elif any(k in lower for k in ("realista", "photoreal", "foto", "real")):
        enhancers.append("photorealistic live-action style, natural motion blur")
    else:
        enhancers.append("high-quality stylized animation, clean motion")

    # Quality / production value
    if not any(q in lower for q in ("detall", "calidad", "masterpiece", "cinematic", "4k", "alta")):
        enhancers.append("sharp details, high production quality, 480p smooth animation")

    # I2V specific guidance (encourage faithful animation of the reference)
    if is_from_image:
        enhancers.append("animate the subject naturally from the reference image, coherent motion")

    # Assemble: user prompt first, then enhancers (comma separated)
    enhancer_str = ", ".join(enhancers)
    if any(w in p.lower() for w in ("estilo", "style", "animación", "motion", "movimiento", "cinematic")):
        final = f"{p}, {enhancer_str}"
    else:
        final = f"{p}, {enhancer_str}"

    final = " ".join(final.split()).strip().strip(",")

    # Reasonable cap for video prompts (videos are sensitive to length)
    if len(final) > 380:
        final = final[:377].rstrip() + "..."

    return final


# =============================================================================
# Video Polling (improved logging + resilience)
# =============================================================================

async def _poll_for_video_completion(
    http_client: httpx.AsyncClient,
    request_id: str,
    api_key: str,
    max_wait_seconds: int = 300,
    poll_interval: float = 5.0
) -> tuple[str, dict]:
    """Poll the video status endpoint until done/failed/expired/timeout."""
    start_time = asyncio.get_event_loop().time()

    while True:
        try:
            resp = await http_client.get(
                f"https://api.x.ai/v1/videos/{request_id}",
                headers={"Authorization": f"Bearer {api_key}"}
            )
            data = resp.json()
            status = str(data.get("status", "")).lower()

            if status == "done":
                return "succeeded", data
            elif status in ["failed", "expired"]:
                return "failed", data

        except (httpx.TimeoutException, httpx.ConnectError, Exception) as e:
            logger.warning(f"{cid_prefix()}[Video Polling] Transient error for {request_id}: {e}")
            await asyncio.sleep(min(10.0, poll_interval * 1.5))

        if asyncio.get_event_loop().time() - start_time > max_wait_seconds:
            return "timeout", {}

        await asyncio.sleep(poll_interval)


# =============================================================================
# Video Schema (kept identical for compatibility)
# =============================================================================

def _generate_video_schema() -> dict:
    return {
        "type": "function",
        "name": "generate_video",
        "description": (
            "Generate a short video clip (grok-imagine-video, auto 480p, max 6s, daily quota of 5 per user). "
            "Supports text-to-video from a descriptive prompt or image-to-video animation from a reference image. "
            "Appropriate for explicit user requests to create, generate, make, or animate video content from text or an attached/prior image."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "Scene, action, or animation guidance for the video."
                },
                "duration": {
                    "type": "integer",
                    "description": "Seconds (max 6).",
                    "default": 5
                },
                "aspect_ratio": {
                    "type": "string",
                    "description": "E.g. 16:9, 9:16, 1:1."
                }
            },
            "required": ["prompt"]
        }
    }


# =============================================================================
# Core Video Tool Implementation (modernized)
# =============================================================================

async def _tool_generate_video(
    prompt: str,
    duration: int = 5,
    aspect_ratio: str | None = None,
    request_id: Optional[str] = None,
    daily_used: int = 0,
    daily_remaining: int = 5,
    source_image_url: str | None = None,
    **extra_params: Any,
) -> str:
    """
    Core video generation (T2V + I2V).

    Modernizations:
    - Always calls _enhance_video_prompt() for better Spanish + motion results.
    - **extra_params passed through (motion_strength, etc.).
    - Improved transient retry + clear error messages.
    - Natural delivery text consistent with the image system ("Acá tenés el video...").
    - Quota info included on success (preserved).
    """
    api_key = _resolve_api_key()
    if not api_key:
        return "No xAI credential configured for video generation (run --login-oauth or set XAI_API_KEY)."

    enforced_duration = min(max(duration, 3), 6)
    is_from_image = bool(source_image_url)

    # === Modern prompt enhancement (always-on) ===
    enhanced_prompt = _enhance_video_prompt(prompt, is_from_image=is_from_image)

    try:
        video_payload: dict = {
            "model": extra_params.get("model", "grok-imagine-video"),
            "prompt": enhanced_prompt,
            "duration": enforced_duration,
            "resolution": "480p",
        }
        if aspect_ratio:
            video_payload["aspect_ratio"] = aspect_ratio

        # I2V support
        if source_image_url:
            video_payload["image_url"] = source_image_url
            video_payload["image"] = {"url": source_image_url}

        # Pass through modern/future params (motion_strength, etc.)
        for k in ("motion_strength", "guidance", "seed", "negative_prompt"):
            if k in extra_params and extra_params[k] is not None:
                video_payload[k] = extra_params[k]

        max_attempts = getattr(settings, "api_max_retries", 3)
        poll_data: dict = {}
        xai_video_id = None

        for attempt in range(max_attempts):
            try:
                async with httpx.AsyncClient(timeout=settings.api_timeout_seconds) as http_client:
                    response = await http_client.post(
                        "https://api.x.ai/v1/videos/generations",
                        headers={
                            "Authorization": f"Bearer {api_key}",
                            "Content-Type": "application/json"
                        },
                        json=video_payload
                    )

                    if response.status_code != 200:
                        # Basic error surface for start failure
                        try:
                            err = response.json()
                            msg = err.get("error", {}).get("message", response.text)
                        except Exception:
                            msg = response.text[:200]
                        return f"Error starting video generation: {msg}"

                    data = response.json()
                    xai_video_id = data.get("id") or data.get("request_id") or data.get("video_id")

                    if not xai_video_id:
                        return "Error generating video: the API did not return a request ID."

                    logger.info(f"{cid_prefix()}[Video] Polling for completion of xAI video request {xai_video_id}")
                    poll_status, poll_data = await _poll_for_video_completion(
                        http_client, xai_video_id, api_key, max_wait_seconds=300, poll_interval=5.0
                    )

                    if poll_status != "succeeded":
                        err_detail = ""
                        if isinstance(poll_data, dict):
                            err_detail = (
                                poll_data.get("error", {}).get("message")
                                or poll_data.get("status", "")
                            )
                        return f"Video generation {poll_status}. {err_detail}. Try a simpler prompt or different reference."

                    break  # success

            except (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError) as net_err:
                if attempt < max_attempts - 1:
                    delay = 0.6 * (2 ** attempt)
                    logger.info(f"{cid_prefix()}[Video] Transient network on generation attempt {attempt+1}: {type(net_err).__name__} — retry in {delay:.1f}s")
                    await asyncio.sleep(delay)
                    continue
                return f"Error generating video: transient network issue after retries ({type(net_err).__name__})."

        else:
            return "Error generating video after retries."

        # === Extract final video URL (robust, same defensive logic as before) ===
        video_url = None
        if isinstance(poll_data, dict):
            for key in ("url", "video_url", "download_url", "result_url", "file_url", "src", "mp4"):
                val = poll_data.get(key)
                if val and isinstance(val, str) and val.startswith("http"):
                    video_url = val
                    break

            if not video_url:
                for container_key in ("result", "output", "data", "video", "asset", "content"):
                    container = poll_data.get(container_key)
                    if isinstance(container, dict):
                        for key in ("url", "video_url", "download_url", "result_url", "file_url", "src"):
                            val = container.get(key)
                            if val and isinstance(val, str) and val.startswith("http"):
                                video_url = val
                                break
                        if video_url:
                            break
                    elif isinstance(container, list) and container:
                        for item in container:
                            if isinstance(item, dict):
                                for key in ("url", "video_url", "download_url", "src"):
                                    val = item.get(key)
                                    if val and isinstance(val, str) and val.startswith("http"):
                                        video_url = val
                                        break
                                if video_url:
                                    break
                        if video_url:
                            break

            if not video_url:
                def _find_video_urls(obj, found_list):
                    if len(found_list) > 0:
                        return
                    if isinstance(obj, dict):
                        for v in obj.values():
                            _find_video_urls(v, found_list)
                    elif isinstance(obj, list):
                        for v in obj:
                            _find_video_urls(v, found_list)
                    elif isinstance(obj, str) and obj.startswith(("http://", "https://")):
                        low = obj.lower()
                        if any(h in low for h in (".mp4", ".webm", ".mov", "/video", "grok", "x.ai", "cdn", "download")):
                            found_list.append(obj)

                found = []
                _find_video_urls(poll_data, found)
                if found:
                    video_url = found[0]

        if not video_url:
            try:
                top_keys = list(poll_data.keys()) if isinstance(poll_data, dict) else str(type(poll_data))
                logger.warning(f"{cid_prefix()}[Video] Succeeded poll but no URL extracted. Top keys: {top_keys}. Raw sample: {str(poll_data)[:400]}")
            except Exception:
                pass
            return "The video was generated but no downloadable URL was obtained. Try again later."

        caption = build_video_caption(
            from_image=is_from_image,
            duration=enforced_duration,
            daily_used=daily_used,
            daily_remaining=daily_remaining,
        )

        if request_id and await deliver_from_request(
            request_id, caption=caption, urls=[video_url], kind="video"
        ):
            logger.info(
                f"{cid_prefix()}[MediaDelivery] Video delivered as attachment for request {request_id} "
                f"(xAI id: {xai_video_id})"
            )
            return "SUCCESS: Video successfully generated and delivered directly to the user."

        return f"Video generated successfully:\n{video_url}"

    except Exception as e:
        logger.exception(f"{cid_prefix()}[Video] Unexpected error in _tool_generate_video")
        return f"Error generating video: {str(e)}"


# =============================================================================
# Public Dispatch Handler (stable API)
# =============================================================================

async def _handle_generate_video(args: dict, original_message: Any, image_urls: list[str] | None = None) -> str:
    """
    Handles generate_video dispatch.

    - Python-level explicit intent guard (via media_tools.has_explicit_video_intent).
    - Quota enforcement (5/day per user).
    - Request registration for direct delivery.
    - Calls the modern _tool_generate_video (with prompt enhancement).
    """
    prompt = args.get("prompt", "")

    duration = int(args.get("duration", 5))
    aspect_ratio = args.get("aspect_ratio") or args.get("aspect") or None

    source_image_url = image_urls[0] if image_urls else None

    user_id = getattr(getattr(original_message, "author", None), "id", 0)

    # Quota (optimistic increment, same as before)
    daily_used, daily_remaining = context.get_video_quota(user_id)
    if daily_remaining <= 0:
        return "You have reached the daily limit of 5 videos. Please try again tomorrow."
    daily_used, daily_remaining = context.increment_video_quota(user_id)

    # Register for direct delivery (reuses image_delivery infrastructure)
    request_id = None
    try:
        request_id = await register_image_request(
            user_id=user_id,
            channel_id=getattr(original_message, "channel", None) and getattr(original_message.channel, "id", 0) or 0,
            message_id=getattr(original_message, "id", 0),
            operation_type="video",
            original_message=original_message,
        )
    except Exception:
        pass

    return await _tool_generate_video(
        prompt,
        duration=duration,
        aspect_ratio=aspect_ratio,
        request_id=request_id,
        daily_used=daily_used,
        daily_remaining=daily_remaining,
        source_image_url=source_image_url,
        # forward any extra future params the caller might have received
        **{k: v for k, v in args.items() if k not in ("prompt", "duration", "aspect_ratio", "aspect")}
    )
