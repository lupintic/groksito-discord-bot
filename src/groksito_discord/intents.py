"""
Light intent predicates and helpers (centralized surface).

Per #22 (Deprecate Heavy Custom Classification and Decision Heuristics):
The previous elaborate keyword-driven classification system (large lists + complex
rules to force "casual"/"minimal" tiers that suppressed native search/custom tools)
is being progressively deprecated. Goal: rely on Grok's native reasoning and
tool-calling judgment (augmented only by minimal Discord guardrails + strict
safety/cost gates for media generation).

This module now only hosts small, auditable, non-brittle predicates needed for:
- pure first-turn image/video creation detection (ultra-light schemas + quota safety)
- meta-conversation detection (for recent context)
- decisions around offering light decision tools on addressed turns (lets model
  choose reply/react/direct/get_context natively)
- conservative summary trigger

Heavy lists and pre-decision heuristics live only in legacy paths (being removed
over time). No large tables here.
"""

from __future__ import annotations
from typing import Any


# =============================================================================
# Essential light detectors (kept; no heavy keyword tables)
# =============================================================================

def is_pure_image_generation_request(text: str | None) -> bool:
    """Strong signals for first-turn pure text-to-image (not edit, not analysis, not video)."""
    if not text or len(text.strip()) < 5:
        return False
    t = text.lower()
    # Tight positive signals only
    positives = (
        "genera una imagen", "generame una imagen", "genera imagen de",
        "haz una imagen", "hazme una imagen", "crea una imagen", "creame una imagen",
        "dibuja un", "dibujame", "pinta una", "una imagen de un", "imagen de una",
        "generate an image of", "draw a picture of", "make an image", "create a picture",
        "genera una foto de", "haz un dibujo",
    )
    if not any(p in t for p in positives):
        return False
    # Negatives: edits/refs/analysis/video signals -> not pure gen
    negatives = (
        "esta ", "la imagen", "la foto", "referencia", "edit", "edita", "sobre esta",
        "de esta", "analiza", "describe", "qué ves", "video", "anim",
    )
    if any(n in t for n in negatives):
        return False
    return True


def is_conversation_meta_question(text: str | None) -> bool:
    """Detects explicit requests about the prior conversation (needs recent context)."""
    if not text:
        return False
    t = text.lower()
    keys = (
        "resumen", "resumí", "resumen de", "de qué habl", "qué dijimos", "continúa",
        "la charla", "tema anterior", "antes habl", "qué pasó antes", "recap",
        "de qué se trataba", "qué dijo", "último que", "resumamos",
    )
    return any(k in t for k in keys)


def should_generate_recent_summary(
    *, message_count: int = 0, last_summary_count: int = 0, **_: Any
) -> bool:
    """Conservative trigger for proactive channel summary (rarely true)."""
    delta = max(0, message_count - last_summary_count)
    return message_count > 25 and delta > 18


def should_offer_light_decision_tools(
    *, is_mentioned: bool = False, is_reply_to_bot: bool = False,
    context_need: str = "normal", user_message: str = "", **_: Any
) -> bool:
    """
    Offer the light decision + delivery tools on plain addressed turns.
    This is the primary path for giving Grok native choice (respond_directly,
    get_recent_context, reply_to_user, etc.) without heavy custom tool bloat.
    """
    if not (is_mentioned or is_reply_to_bot):
        return False
    # Skip only on trivial non-query greetings
    if context_need == "casual" and len((user_message or "").split()) <= 3:
        return False
    return True


# =============================================================================
# Transitional shims for deprecated heavy lists (referenced by legacy classify paths)
# Will be removed once classify_query_context_need and callers are fully cleaned (#22).
# =============================================================================

_CASUAL_CHAT_HINTS: list[str] = ["hola", "hi", "hey", "jaja", "jeje", "lol", "xd", "buenas", "saludos", "gracias"]
_SIMPLE_FACTUAL_HINTS: list[str] = ["qué es", "que es", "capital de", "quién es", "definición", "fórmula"]
_COMPLEX_OR_PERSONAL_HINTS: list[str] = ["mi ", "yo ", "me ", "mis ", "recuerda", "acord", "personal"]
_FRESH_OR_TOOL_HINTS: list[str] = ["hoy", "ahora", "precio", "última", "reciente", "breaking"]
