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
- breadth-grounding signal for completeness-oriented search descriptions (#63)

All legacy heavy lists and classify paths cleaned. No large tables here.
"""

from __future__ import annotations

import unicodedata
from typing import Any


def _normalize_query_text(text: str) -> str:
    """Lowercase + strip accents for accent-insensitive query-shape matching."""
    t = text.lower().strip()
    nfkd = unicodedata.normalize("NFKD", t)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


# =============================================================================
# Shared keyword groups (deduplicated across pure + explicit detectors)
# =============================================================================

_IMAGE_CREATION_POSITIVES = (
    "genera una imagen", "generame una imagen", "genera imagen de",
    "me generas una imagen", "me generás una imagen", "me genera una imagen",
    "generame una imagen de", "genera una de",
    "haz una imagen", "hazme una imagen", "hazme una imagen de",
    "crea una imagen", "creame una imagen", "crea una imagen de",
    "quiero una imagen", "quiero una imagen de", "quiero que generes una",
    "dibuja un", "dibujame", "dibújame", "dibuja una imagen", "dibuja me una",
    "pinta una", "pintame", "píntame",
    "una imagen de un", "imagen de una", "imagen de el", "imagen de la",
    "me generas un", "me generás un", "me genera un",
    "generame un", "hazme un", "creame un", "dibujame un", "pintame un",
    "genera un", "haz un", "crea un", "dibuja un", "pinta un",
    "generate an image of", "draw a picture of", "make an image", "create a picture",
    "generate me an image", "make me an image of", "draw me",
    "genera una foto de", "haz un dibujo",
)

_IMAGE_CREATION_NEGATIVES = (
    "esta ", "la imagen", "la foto", "referencia", "edit", "edita", "sobre esta",
    "de esta", "analiza", "describe", "qué ves", "video", "anim",
    "de la imagen", "de la foto", "de esta imagen", "sobre la imagen",
)

_VIDEO_CREATION_CORE = (
    "haz un video", "hacé un video", "hace un video",
    "genera un video", "generar un video", "generarme un video",
    "generame un video", "generá un video", "generáme un video",
    "crea un video", "crear un video", "crearme un video",
    "creame un video", "creá un video",
    "hazme un video", "haceme un video",
    "quiero un video", "necesito un video",
    "haz video", "genera video", "generar video", "crea video", "crear video",
    "generame video", "creame video",
    "un video de", "haz video de", "genera video de", "generar video de",
    "make a video", "generate a video", "create a video",
)

_VIDEO_CREATION_I2V_EXTRA = (
    "video de esta", "video de la", "video de esto", "video de eso", "video de esa",
    "video de la imagen", "video de la foto", "video de esa imagen",
    "un video con esta", "una video de", "una video con",
    "anima esta", "anima la", "anima esto", "anima esa",
    "convierte esta en video", "convierte la en video", "convierte en video",
    "animate this", "turn this into a video",
    # Add-to-video / composite-into-video (common reply-to-bot I2V phrasing)
    "agregalo a ese video", "agregalo al video", "agrégalo a ese video",
    "agregarlo a ese video", "agregarlo al video", "añadelo a ese video",
    "añadirlo a ese video", "ponlo en ese video", "ponlo en el video",
    "integrarlo en el video", "integralo en el video", "integralo al video",
    "add to that video", "add to this video", "put it in the video",
)

_VIDEO_PURE_NEGATIVES = (
    "esta imagen", "esta foto", "esa imagen", "esa foto",
    "de esta", "de esa", "la imagen", "la foto", "referencia",
    "analiza", "describe", "qué ves", "que ves", "qué es el video",
    "quiero ver", "ver un video", "ver el video", "mira el video",
    "edit", "edita", "imagen de",
)

_VIDEO_WATCH_NEGATIVES = (
    "quiero ver", "quiero mirar", "ver un video", "ver videos", "ver el video",
    "ver este video", "ver ese video", "mira el video", "mira un video",
    "watch a video", "watch this video", "watch the video", "see a video",
)

_VIDEO_GEN_HINTS = (
    "genera", "generar", "crea", "crear", "haz", "generame", "generarme",
    "creame", "crearme", "hazme", "quiero un", "necesito un", "podrias generar",
    "podrías generar", "puedes generar", "me podrias", "me podrías",
    "make a", "generate a", "create a",
)

_VIDEO_EXPLICIT_BAD_CONTEXT = (
    "qué ves", "que ves", "analiza", "describe", "qué es el video",
    "busca video", "quiero ver", "ver un video", "ver videos",
)

_AUDIO_KEYWORDS = (
    "léelo en voz alta", "lee en voz alta", "dilo en voz alta", "léelo", "léemelo",
    "genera audio", "audio de", "convierte a audio", "texto a voz", "tts",
    "dímelo en voz", "habla esto", "lee esto en voz", "en voz", "voz alta",
    "genera el audio", "haz audio", "audio para", "narra", "pronuncia",
    "read this out loud", "speak this", "text to speech", "generate audio",
    "dilo", "léelo en voz", "haz que lo diga",
)

_AUDIO_GEN_HINTS = ("genera", "crea", "haz", "quiero", "necesito", "dime", "lee", "dilo")
_AUDIO_BAD_CONTEXT = ("busca audio", "música", "qué audio", "canción")

# =============================================================================
# Essential light detectors (kept; no heavy keyword tables)
# =============================================================================

def is_pure_image_generation_request(text: str | None) -> bool:
    """Strong signals for first-turn pure text-to-image (not edit, not analysis, not video).
    This is used only for the ultra-light "image_gen" optimization path (zero context + tiny tool only).
    Tool availability for native reasoning is no longer gated exclusively behind this (see light decision tools).
    """
    if not text or len(text.strip()) < 5:
        return False
    t = text.lower()
    # Cover common natural Spanish + English phrasings for "generate an image of..."
    # Includes casual "me generas un gato con botas?", "hazme un [subject]", etc.
    # The word "imagen" is not required if the verb is a clear creation verb and no analysis/edit/video signals.
    # This detector is *only* for the ultra-light pure image_gen optimization path.
    # Tool availability for native reasoning is guaranteed via light decision tools + core tool protection.
    if not any(p in t for p in _IMAGE_CREATION_POSITIVES):
        return False
    if any(n in t for n in _IMAGE_CREATION_NEGATIVES):
        return False
    return True


def is_pure_video_generation_request(text: str | None) -> bool:
    """Strong signals for first-turn pure text-to-video (not I2V reply, not analysis).

    Mirrors is_pure_image_generation_request for the ultra-light video_gen path
    (zero context + tiny generate_video schema only). Tool availability on addressed
    turns is no longer gated exclusively behind this — light decision tools offer
    generate_video natively like generate_image.
    """
    if not text or len(text.strip()) < 5:
        return False
    t = text.lower()
    pure_phrases = _VIDEO_CREATION_CORE + (
        "quiero que generes un video",
        "un video de un", "un video de una",
        "make a video of", "generate a video of", "create a video of",
        "animate a scene", "animate a ",
    )
    if not any(p in t for p in pure_phrases):
        return False
    if any(n in t for n in _VIDEO_PURE_NEGATIVES):
        return False
    return True


# =============================================================================
# Explicit intent gates for media creation (centralized with other keyword signals)
# =============================================================================
# These are the authoritative implementations. Re-exported from llm/media_tools.py
# for compatibility with existing imports in tools/client.

def has_explicit_video_intent(text: str | None) -> bool:
    """
    Detects clear user intent to generate a video.

    Used for logging, continuation carryover, and ultra-light pure_video_gen routing —
    NOT as the sole gate for offering generate_video on addressed turns (that follows
    the same native pattern as generate_image: offered on light decision tools;
    xAI / SuperGrok subscription limits apply at the API).
    """
    if not text:
        return False
    t = text.lower()

    if any(kw in t for kw in _VIDEO_CREATION_CORE + _VIDEO_CREATION_I2V_EXTRA):
        return True

    if any(p in t for p in _VIDEO_WATCH_NEGATIVES):
        return False

    if "video" in t:
        if any(g in t for g in _VIDEO_GEN_HINTS):
            if not any(b in t for b in _VIDEO_EXPLICIT_BAD_CONTEXT):
                return True

    return False


def has_explicit_audio_intent(text: str | None) -> bool:
    """
    Detects clear user requests for text-to-speech / audio generation.
    Examples: "léelo en voz alta", "genera audio de esto", "dilo en voz", "lee esto", "tts", etc.
    Used as a hard gate (like video) so the tool is only offered on explicit requests.
    """
    if not text:
        return False
    t = text.lower()

    if any(kw in t for kw in _AUDIO_KEYWORDS):
        return True

    if ("audio" in t or "voz" in t or "habla" in t or "lee" in t or "tts" in t):
        if any(g in t for g in _AUDIO_GEN_HINTS):
            if not any(b in t for b in _AUDIO_BAD_CONTEXT):
                return True

    return False


def needs_breadth_grounding(text: str | None) -> bool:
    """Light signal that a query likely benefits from broad, multi-option coverage.

    Query-type based (recommendations, alternatives, comparisons, discovery) — not
    topic-specific. Used to tune native search descriptions toward exhaustive synthesis
    without hardcoding per-domain answers.
    """
    if not text or len(text.strip()) < 8:
        return False
    t = _normalize_query_text(text)
    patterns = (
        "alternativa", "alternative", "opciones", "options", "opcion", "option",
        "mejor forma", "best way", "ways to", "formas de", "como puedo", "how can i",
        "que usar", "what to use", "que app", "what app", "which app",
        "recomend", "recommend", "suger", "suggest", "cual es mejor", "which is better",
        "mejor para", "mejor app", "mejor programa", "best for", "best app", "top ", "mejores ", "best ",
        "compar", "compare", " vs ", " versus ", "pros and cons", "ventajas y desventajas",
        "lista de", "list of", "enumera", "listame",
        "apps para", "apps for", "herramientas para", "tools for", "servicios para",
        "software para", "programas para", "aplicaciones para",
        "como hacer", "how to", "como ver", "how to watch", "como usar", "how to use",
        "se puede", "is there a way", "hay alguna forma",
        # Casual discovery / device-use phrasing (query-shape, not topic-specific)
        "castear", "cast to", "cast ", "casting", "screen mirror", "mirroring", "airplay",
        "en la tv", "en la tele", "a la tv", "to tv", "on tv", "to the tv", "on the tv",
        "puedo usar", "can i use", "what can i use", "que puedo usar",
        "para ver en", "watch on", "stream to", "streaming to",
        # Video/clip discovery (query-shape; model still uses judgment + prompt guidance)
        "busca video", "buscar video", "busca un video", "buscar un video",
        "busca clip", "buscar clip", "enlace de youtube", "link de youtube",
        "link del video", "enlace del video", "youtube de", "clip de",
        "del clip", "pasame el video", "pasame el clip", "pásame el video",
        "find a video", "find the video", "youtube link", "video link",
    )
    if any(p in t for p in patterns):
        return True
    question_shapes = (
        "que alternativas", "what alternatives", "cuales son", "what are the",
        "hay algun", "is there any", "hay alguna", "is there a",
    )
    return any(t.startswith(s) or f" {s}" in t for s in question_shapes)


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


def is_image_edit_request(text: str | None, *, has_reference_image: bool = False) -> bool:
    """Detect when the user wants to transform/edit a reference image (not analyze it).

    When has_reference_image is True (attached or harvested reference), we accept
    broader Spanish imperatives like "le pones pelo..." that lack an explicit "edita".
    """
    if not text or len(text.strip()) < 4:
        return False
    t = text.lower()

    analysis_neg = (
        "qué ves", "que ves", "describe", "analiza", "quién es", "quien es",
        "qué opinas", "que opinas", "qué piensas", "que piensas", "what do you see",
        "who is", "describe this",
    )
    if any(n in t for n in analysis_neg):
        return False

    # Explicit edit / transform verbs (Spanish + English)
    explicit = (
        "edita", "edit ", "editar", "modifica", "modificar", "retoca", "retocar",
        "transforma", "transformar", "cambia la imagen", "cambia la foto",
        "cambia esta imagen", "cambia esta foto", "sobre esta imagen", "sobre esta foto",
        "en esta imagen", "en esta foto", "a esta imagen", "a esta foto",
        "change this image", "edit this image", "edit this photo", "transform this",
    )
    if any(p in t for p in explicit):
        return True

    # Imperative "apply X to the subject" — common when user attaches a portrait
    imperative = (
        "le pones", "le pon ", "ponle ", "póngale", "pongale", "hazle ", "hágale",
        "agregale", "agrégale", "añadele", "añádela", "anadele", "quítale", "quitale",
        "cámbiale", "cambiale", "dale ", "convíertela", "conviertela",
        "put on her", "put on him", "give her", "give him", "make her", "make him",
        "add to her", "add to his",
    )
    if has_reference_image and any(p in t for p in imperative):
        return True

    if has_reference_image:
        # Appearance / styling tweaks without an explicit "edit" verb
        styling = (
            "pelo ", "cabello", "pecas", "rubor", "maquillaje", "makeup",
            "vestido", "ropa", "outfit", "fondo ", "background", "estilo ",
            "color de", "tinte", "blush", "freckles", "hair ",
        )
        if any(s in t for s in styling):
            return True

    return False


def _detect_image_creation_intent(text: str | None, *, has_reference_image: bool = False) -> bool:
    """
    Light detector for image (or video-from-image) creation / edit intent.

    This feeds has_visual_intent for offering heavy or full media schemas.
    Per design and docstrings, video-from-img (I2V on addressed ref) counts as
    creation intent so that video is offered with the same weight and paths
    as image creation (full schemas on creation signal, parity on addressed light).
    """
    try:
        if is_pure_image_generation_request(text):
            return True
        if is_image_edit_request(text, has_reference_image=has_reference_image):
            return True
        if has_reference_image and has_explicit_video_intent(text):
            # I2V on a media referent is a creation/transform action; treat as visual
            # creation for tool selection parity with generate_image/edit_image.
            return True
        return False
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

# Discord attachment signals for reply-to-media activation (PR #49 review).
# content_type is primary; filename extension is a fallback when Discord reports
# video as application/octet-stream.
_MEDIA_CONTENT_TYPE_PREFIXES = ("image/", "video/")
_MEDIA_FILENAME_EXTENSIONS = (".mp4", ".webm", ".mov", ".mkv", ".m4v")


def referenced_has_media_attachments(message: Any | None) -> bool:
    """
    True when a Discord message has image or video file attachments.

    Used for visual follow-up intent on *addressed* turns (@mention or reply-to-bot)
    when the referenced message has image/video attachments.
    """
    if not message:
        return False
    for att in getattr(message, "attachments", []) or []:
        content_type = (getattr(att, "content_type", "") or "").lower()
        if any(content_type.startswith(prefix) for prefix in _MEDIA_CONTENT_TYPE_PREFIXES):
            return True
        # Fallback: some video uploads lack a video/* MIME type.
        filename = (getattr(att, "filename", "") or "").lower()
        if any(filename.endswith(ext) for ext in _MEDIA_FILENAME_EXTENSIONS):
            return True
    return False


# === Vision + attachment helpers for "see all files" (2026-06-20 feature) ===
# Primary = content_type. Fallback to common extensions so Discord mis-reported
# MIME types (common for GIFs, WebP from mobile) still work for metadata.
# Only jpg/jpeg/png go to actual native vision input_image per xAI limits.
_VISION_SUPPORTED_IMAGE_CT_PREFIXES = ("image/jpeg", "image/jpg", "image/png")
_VISION_SUPPORTED_IMAGE_EXTS = (".jpg", ".jpeg", ".png")
_TEXT_ATTACHMENT_EXTS = (
    ".txt", ".md", ".markdown", ".py", ".js", ".ts", ".json", ".csv",
    ".log", ".yml", ".yaml", ".toml", ".ini", ".cfg", ".sh", ".bat",
)
_TEXT_INLINE_MAX_BYTES = 64 * 1024  # 64 KiB safety cap for inlining (small files only)

def is_supported_vision_image(att: Any) -> bool:
    """Return True only for formats we will send as input_image to xAI vision."""
    if not att:
        return False
    ct = (getattr(att, "content_type", "") or "").lower()
    if any(ct.startswith(p) for p in _VISION_SUPPORTED_IMAGE_CT_PREFIXES):
        return True
    filename = (getattr(att, "filename", "") or "").lower()
    return any(filename.endswith(ext) for ext in _VISION_SUPPORTED_IMAGE_EXTS)

def is_text_attachment(att: Any) -> bool:
    """Small text-like files we may safely inline content for."""
    if not att:
        return False
    ct = (getattr(att, "content_type", "") or "").lower()
    if ct.startswith("text/"):
        return True
    filename = (getattr(att, "filename", "") or "").lower()
    return any(filename.endswith(ext) for ext in _TEXT_ATTACHMENT_EXTS)

def get_attachment_meta(att: Any) -> dict[str, Any]:
    """Lightweight dict for prompt injection. Never include secrets or huge data."""
    return {
        "filename": getattr(att, "filename", "") or "unknown",
        "content_type": getattr(att, "content_type", "") or "",
        "size": getattr(att, "size", 0) or 0,
        "url": getattr(att, "url", ""),
    }


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
