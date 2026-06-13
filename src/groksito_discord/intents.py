"""
Light intent predicates and helpers (centralized surface).

Post #22/#24: The heavy keyword-driven classification system and pre-decision
heuristics have been removed (see classify_query_context_need removal and
shim cleanup). We rely on Grok's native reasoning + tool-calling for decisions,
with only minimal essential light predicates for:
- pure first-turn image/video creation detection (ultra-light schemas + quota safety)
- meta-conversation detection (for recent context)
- decisions around offering light decision tools on addressed turns
- conservative summary trigger
- light signals for visual result enrichment on search (no heavy lists)

All legacy heavy lists and classify paths cleaned. No large tables here.
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


# (Transitional heavy list shims removed in #24 cleanup — classify paths fully excised.)


# =============================================================================
# Light visual / image intent detectors (centralized, post-cleanup minimal versions)
# =============================================================================
# These replace the previous _detect_* heavy implementations. Used for:
# - enriching native search results with images when query implies visual interest
# - (image_creation one kept for compat re-exports even if lightly used now)
# Kept deliberately small and non-brittle; creation gating uses the stricter
# is_pure_image_generation_request.


def _detect_visual_intent(text: str | None) -> bool:
    """Light signal that the query may benefit from image-capable search results.

    Examples: asking to "show pictures", "fotos de", "images of" something topical.
    Not triggered by pure creation requests (those are handled by dedicated
    is_pure + has_visual_intent paths for gen/edit tools).
    """
    if not text or len(text.strip()) < 4:
        return False
    t = text.lower()
    positives = (
        "imágenes", "imagenes", "fotos de", "pictures of", "photos of",
        "muéstrame", "show me pictures", "show images", "fotos", "pictures",
        "imágenes de", "fotos sobre", "drawings of", "ilustraciones",
    )
    if not any(p in t for p in positives):
        return False
    # Do not count as "visual search interest" if it's clearly a creation request
    # (creation is routed via pure_image_gen + heavy tool gates instead).
    neg = ("genera", "crea", "haz", "dibuja", "pinta", "edit", "sobre esta imagen")
    if any(n in t for n in neg):
        return False
    return True


def _detect_image_creation_intent(text: str | None) -> bool:
    """Light detector for image creation intent (compat surface)."""
    try:
        return is_pure_image_generation_request(text)
    except Exception:
        return False


# =============================================================================
# Activation guards (strict policy to avoid waking on random user-to-user replies)
# These are *minimal* and separate from the removed heavy classification tiering
# (#22/#24). They implement the conservative "only wake on strong directed signals
# when replying to another human" rule (never use broad GENERAL list for wake-up).
# Re-added here (from working pre-placeholder tree) because conversation.py + client.py
# (recovered for runnable state) depend on them. Kept small/auditable.
# Post-cleanup they coexist with the light predicates below.
# =============================================================================

STRONG_DIRECTED_KEYWORDS = [
    # Targeted questions about the referenced content
    "este tweet",
    "este post",
    "este link",
    "este enlace",
    "esta url",
    "de qué habla",
    "qué dice",
    "qué es este",
    "de qué va este",
    "este x.com",
    "este twitter",
    "el tweet que",
    "el post que",
    "qué opina",
    "analiza este",
    "resumen de este",
    "de qué trata este",
    # Explicit addressing of the bot (common even without proper @mention)
    "groksito",
    "grok",
    "@groksito",
]

# GENERAL_REPLY_INQUIRY_KEYWORDS: Broad set used *only* for context enrichment
# and chain traversal *after* we have already decided to activate for a stronger reason.
# These are intentionally permissive/common; must NEVER be sufficient by themselves
# to wake the bot on a reply to another user.
GENERAL_REPLY_INQUIRY_KEYWORDS = [
    "esto",
    "este mensaje",
    "el mensaje",
    "lo que",
    "arriba",
    "el anterior",
    "la foto",
    "la imagen",
    "el post",
    "el tweet",
    "el link",
    "qué es",
    "de qué",
    "qué dice",
    "qué opinas",
    "qué piensas",
    "qué te parece",
    "explica",
    "analiza",
    "resumen",
    "sobre esto",
    "de esto",
    "esto de",
    "la que",
    "el que",
    "lo anterior",
]

def _has_strong_directed_reply_intent(text: str | None) -> bool:
    """Conservative: only strong targeted signals + bot name wake on replies to others."""
    if not text:
        return False
    t = text.lower()
    return any(kw in t for kw in STRONG_DIRECTED_KEYWORDS)


def _has_recent_referent_intent(text: str | None) -> bool:
    """
    Detects likely reference to recent/recent-user content (for recent vision + referent
    resolution on direct mentions, and for context enrichment once activated).
    Broader than strong activation; safe because it does not control wake-up alone.
    """
    if not text:
        return False
    t = text.lower()
    base = any(kw in t for kw in GENERAL_REPLY_INQUIRY_KEYWORDS)
    referent = any(
        kw in t
        for kw in (
            "el usuario",
            "ese usuario",
            "la imagen del usuario",
            "la foto del usuario",
            "el post del usuario",
            "lo que dijo",
            "lo que puso",
            "el video de",
            "la imagen de",
            "el tweet de",
            "arriba",
            "el anterior",
            "la anterior",
            "de la foto",
            "de la imagen",
            "en la foto",
            "en la imagen",
            "ese link",
            "ese enlace",
            "esa url",
        )
    )
    return base or referent
