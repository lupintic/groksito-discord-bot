"""
Thin public re-export layer for Groksito media tools.

Stable import surface. All real image generation + editing logic has been
centralized in the modern `media/image_handler.py` (improved prompt engineering,
unified error handling, better Spanish support, future-proof param passing).

This module only re-exports so that existing callers (tools.py, llm.py, health.py,
video_generation.py etc.) require zero changes.

Implementation lives in:
- media/image_handler.py   (Text-to-Image + Image-Edit, modernized)
- media/video_handler.py   (Text-to-Video + Image-to-Video, modernized with prompt enhancement)
- image_delivery.py        (direct delivery + request tracking)
"""

from __future__ import annotations

from ..config import settings


# =============================================================================
# Feature Flags
# =============================================================================

ENABLE_VIDEO_GENERATION: bool = settings.enable_video_generation


# =============================================================================
# Re-exports (stable public surface — now backed by centralized image_handler)
# =============================================================================

# Video — now served from the dedicated modern handler (following the image_handler pattern)
from ..media.video_handler import (
    _generate_video_schema,
    _handle_generate_video,
)

# Image generation & editing — now served from the dedicated modern handler
from ..media.image_handler import (
    _handle_generate_image,
    _handle_edit_image,
)

# Audio / TTS — served from dedicated handler (src/groksito_discord/media/audio_handler.py)
# The handler resolves tts_default_voice / tts_default_language from settings (pydantic + .env, editable in web UI)
# with sensible fallbacks ("eve", "es"). Language is required by the official /v1/tts API.
from ..media.audio_handler import (
    _generate_audio_schema,
    _handle_generate_audio,
)

# Optional: re-export the new prompt enhancer for advanced use / tests
from ..media.image_handler import _enhance_prompt_for_api, soften_image_prompt

# =============================================================================
# Explicit Video Intent Detection
# =============================================================================

def has_explicit_video_intent(text: str | None) -> bool:
    """
    Detects clear, explicit user intent to generate a video.
    Used as a hard safety gate to prevent generate_video on generic image questions.
    """
    if not text:
        return False
    t = text.lower()

    video_keywords = [
        "haz un video", "hacé un video", "hace un video",
        "genera un video", "generame un video", "generá un video",
        "crea un video", "creame un video", "creá un video",
        "hazme un video", "haceme un video",
        "quiero un video", "necesito un video",
        "haz video", "genera video", "crea video",
        "generame video", "creame video",
        "video de esta", "video de la", "video de esto", "video de eso",
        "video de la imagen", "video de la foto",
        "un video de", "un video con esta", "una video de", "una video con",
        "anima esta", "anima la", "anima esto",
        "convierte esta en video", "convierte la en video", "convierte en video",
        "make a video", "generate a video", "create a video",
        "animate this", "turn this into a video",
    ]
    if any(kw in t for kw in video_keywords):
        return True

    # Robust fallback for typos / grammar slips ("una video", "generame una video", "genera video de una...")
    # Common when users type fast in Spanish.
    if "video" in t:
        gen_hints = ("genera", "crea", "haz", "generame", "creame", "hazme", "quiero", "necesito", "make a", "generate a", "create a")
        if any(g in t for g in gen_hints):
            # Avoid turning analysis or search into video intent
            bad = ("qué ves", "que ves", "analiza", "describe", "qué es el video", "busca video")
            if not any(b in t for b in bad):
                return True

    return False


# =============================================================================
# Explicit Audio / TTS Intent Detection (new)
# =============================================================================

def has_explicit_audio_intent(text: str | None) -> bool:
    """
    Detects clear user requests for text-to-speech / audio generation.
    Examples: "léelo en voz alta", "genera audio de esto", "dilo en voz", "lee esto", "tts", etc.
    Used as a hard gate (like video) so the tool is only offered on explicit requests.
    """
    if not text:
        return False
    t = text.lower()

    audio_keywords = [
        "léelo en voz alta", "lee en voz alta", "dilo en voz alta", "léelo", "léemelo",
        "genera audio", "audio de", "convierte a audio", "texto a voz", "tts",
        "dímelo en voz", "habla esto", "lee esto en voz", "en voz", "voz alta",
        "genera el audio", "haz audio", "audio para", "narra", "pronuncia",
        "read this out loud", "speak this", "text to speech", "generate audio",
        "dilo", "léelo en voz", "haz que lo diga",
    ]
    if any(kw in t for kw in audio_keywords):
        return True

    # Fallback for combinations (e.g. "audio de este texto", "voz para esto")
    if ("audio" in t or "voz" in t or "habla" in t or "lee" in t or "tts" in t):
        gen_hints = ("genera", "crea", "haz", "quiero", "necesito", "dime", "lee", "dilo")
        if any(g in t for g in gen_hints):
            bad = ("busca audio", "música", "qué audio", "canción")
            if not any(b in t for b in bad):
                return True

    return False

