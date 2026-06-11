"""
Centralized Intent and Keyword Definitions for Groksito.

This module is the single source of truth for keyword lists and detector
functions that drive three distinct concerns:

1. Activation decisions (whether the bot should respond at all)
2. Query classification (what tier of context and tools to use)
3. Visual / creation intent detection (what tools to offer vs. just use vision)

Clear separation between groups is maintained so that the different
strictness levels and purposes remain explicit. All definitions here
are pure (no side effects, no I/O).

This centralization was performed in Phase 5 (Item A) as a conservative
follow-up to the text utility centralization (Phase 2) and classification
helper extraction (Phase 4).

Usage:
    from .intents import (
        STRONG_DIRECTED_KEYWORDS,
        _detect_image_creation_intent,
        ...
    )

Call sites in conversation.py, context/core.py, llm_utils.py, etc. continue
to work via re-exports or direct imports (behavior is 100% identical).
"""

from __future__ import annotations

# =============================================================================
# 1. Activation Keywords (primarily used in conversation.py)
# =============================================================================
# These control the strict activation policy that prevents the bot from
# replying to random user-to-user conversations.
#
# STRONG_DIRECTED_KEYWORDS:
#   - Used for *activation decisions* on replies to other users.
#   - Specific enough to indicate the user is probably talking to/about the bot
#     or a previous bot output.
#   - Only these (plus explicit visual intent and bot name) can wake the bot
#     on replies to *someone else*.
#
# GENERAL_REPLY_INQUIRY_KEYWORDS:
#   - Broad set used *only* for context enrichment and chain traversal
#     *after* we have already decided to activate for a stronger reason.
#   - Intentionally permissive and common in casual Spanish.
#   - Must NEVER be sufficient by themselves to wake the bot.
#
# The two _has_* functions encapsulate the checks and are the public
# interface for activation logic.
# =============================================================================

STRONG_DIRECTED_KEYWORDS = [
    # Targeted questions about the referenced content (from old x_link set)
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
# and chain traversal *after* we have already decided to activate for a stronger reason
# (mention, reply-to-bot, or strong directed keyword above).
# These are intentionally permissive and common in casual Spanish; they should
# never be sufficient by themselves to wake the bot on a reply to a friend.
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
    "el contenido",
]


def _has_strong_directed_reply_intent(text: str) -> bool:
    """
    Conservative check: does the reply text contain a strong signal that the user
    is directing the question at the bot / previous bot output?

    Only the SPECIFIC targeted phrases + explicit bot name mentions count here.
    Broad everyday words ("esto", "el anterior", "qué opinas" in general) do NOT
    trigger activation by themselves. This is the main defense against the
    "bot replies to random user-to-user replies" bug.
    """
    if not text:
        return False
    text_lower = text.lower()
    return any(kw in text_lower for kw in STRONG_DIRECTED_KEYWORDS)


def _has_recent_referent_intent(text: str) -> bool:
    """
    Detects when the user is likely referring to a recent message, a specific user,
    their post, or an image/link in the recent channel history (without necessarily
    using Discord's reply feature).

    Used to:
    - Gate lightweight recent vision harvesting on direct @mentions.
    - Trigger small raw recent context excerpts for precise "which user" resolution.
    - Improve reasoning about "el usuario", "the image the user posted", "arriba", etc.

    Combines the permissive GENERAL_REPLY_INQUIRY_KEYWORDS with common "user/post/image
    referent" phrases. This is intentionally broader than activation guards.
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
            "el que",
            "la que",
            "ese post",
            "ese mensaje",
            "the user",
            "that user",
            "the guy",
            "the person",
            "the one who posted",
            "what the user",
            "the image of the user",
            "la imagen del",
            "foto del usuario",
        )
    )
    return base or referent


# =============================================================================
# 2. Classification Signals (primarily used in context/core.py)
# =============================================================================
# These lists feed classify_query_context_need and determine the "need level"
# (casual / minimal / normal / rich / image_gen).
#
# The tier controls:
#   - Whether custom tools are offered at all
#   - Whether native web_search / x_search are offered
#   - How much (if any) context is injected
#
# The actual classification logic lives in context/core.py (including helpers
# extracted during Phase 4), but the source data lives here for centralization.
# =============================================================================

_SIMPLE_FACTUAL_HINTS = [
    "qué es",
    "que es",
    "quién es",
    "quien es",
    "qué significa",
    "que significa",
    "cómo se",
    "como se",
    "cuál es",
    "cual es",
    "cuánto",
    "cuanto",
    "cuándo",
    "cuando",
    "dónde",
    "donde",
    "por qué",
    "porque",
    "dime",
    "decime",
    "qué hora",
    "que hora",
    "clima",
    "tiempo",
    "temperatura",
    "busca",
    "buscá",
    "search",
    "google",
    "precio de",
    "cuesta",
    "definición",
    "definicion",
    "significado",
    "a qué hora",
    "a que hora",
    "en qué año",
    "en que año",
    # Casual/low-context greetings and short chat (to default to minimal for native feel)
    "wena",
    "hola",
    "holas",
    "q tal",
    "que tal",
    "como estas",
    "como estai",
    "bro",
    "weon",
    "wn",
    "buena",
    "buenas",
    "saludos",
    "hey",
    "oe",
    "alo",
    "aló",
]

_CASUAL_CHAT_HINTS = [
    # Pure casual chat, greetings, laughter, acknowledgments that almost never need tools or deep context
    "wena",
    "hola",
    "holas",
    "q tal",
    "que tal",
    "como estas",
    "como estai",
    "bro",
    "weon",
    "wn",
    "oe",
    "buena",
    "buenas",
    "saludos",
    "hey",
    "alo",
    "aló",
    "gracias",
    "thx",
    "ok",
    "dale",
    "y tu",
    "todo bien",
    "jajaja",
    "jaja",
    "jeje",
    "jajajaja",
    "xd",
    "lol",
    "jajaj",
    "jaj",
    "ja ja",
    "jajajajaja",
    "ja",
    "ajaj",
    "jejeje",
    "haha",
    "hehe",
]

_COMPLEX_OR_PERSONAL_HINTS = [
    "recuerda",
    "recuerdas",
    "te acordás",
    "acordas",
    "hablamos de",
    "sobre mi",
    "mi",
    "yo",
    "personal",
    "siento",
    "creo que",
    "quiero que",
    "puedes ayudarme con",
    "continua",
    "continúa",
    "sigue",
    "más detalle",
    "explica mejor",
    "contexto",
    "anteriormente",
    "antes",
    "la otra vez",
    "como te dije",
    "según lo que",
]

# Fresh/news/controversy/recency signals (centralized, Phase 5 style).
# Broadened for normal/medium topical queries (e.g. "latest controversies about X",
# "qué pasó con", "recent issues/drama") so they classify as "normal" (search offered)
# instead of demoting to minimal on wc<=7. Feeds classify + _has_fresh_or_tool_signal.
_FRESH_OR_TOOL_HINTS = [
    "latest",
    "reciente",
    "recientes",
    "controvers",
    "polémica",
    "polemica",
    "problemas",
    "issues",
    "bans",
    "anti-cheat",
    "anticheat",
    "qué pasó con",
    "que paso con",
    "what happened",
    "drama",
    "scandal",
    "recent",
    "novedades",
    "actualidad",
    "últimas",
    "ultimas",
    "pasa con",
    "pasó con",
    "paso con",
    "qué hay de nuevo",
    "que hay de nuevo",
]


# =============================================================================
# 3. Meta and Pure-Image Detectors (context/core.py)
# =============================================================================
# These are used both for classification decisions and by the LLM input builder.
#
# is_conversation_meta_question:
#   - Detects explicit requests about prior conversation ("de qué hablaban antes").
#   - Forces "rich" classification and recent-context injection.
#
# is_pure_image_generation_request:
#   - Strict detector for first-turn pure text-to-image (and T2V) requests.
#   - Used to select the ultra-light "image_gen" tier (zero context, minimal tools).
#   - Aggressively excludes anything that smells like edit, analysis, or reference.
# =============================================================================


def is_conversation_meta_question(text: str) -> bool:
    if not text or len(text.strip()) < 4:
        return False

    t = _strip_accents(text.lower())

    strong_triggers = [
        "de que estan hablando",
        "de que se esta hablando",
        "de que se está hablando",
        "que estan hablando",
        "qué están hablando",
        "de que va esto",
        "de qué va esto",
        "de que va la conversacion",
        "de que se trata",
        "de qué se trata",
        "cual es el tema",
        "cuál es el tema",
        "cual es el tema de",
        "que paso",
        "qué pasó",
        "que pasa",
        "qué pasa",
        "resumen de la conversacion",
        "resumen de la charla",
        "de que hablaro",
        "de qué hablaron",
        "de que estaban hablando",
        "de qué estaban hablando",
        "de que estaban discutiendo",
        "contexto de la conversacion",
        "contexto de la charla",
        "tema de la conversacion",
        "tema de la charla",
        "sobre de que",
        "sobre qué",
        "what are you talking about",
        "what are they talking about",
        "whats the topic",
        "what is the topic",
        "what's the topic",
        "what happened",
        "whats going on",
        "what's going on",
        "summary of the conversation",
        "conversation summary",
    ]

    for trig in strong_triggers:
        if trig in t:
            return True

    flexible_patterns = [
        "de que va",
        "de qué va",
        "que se esta hablando",
        "qué se está hablando",
        "el tema de",
        "cual es el",
    ]
    for pat in flexible_patterns:
        if pat in t:
            return True

    return False


def is_pure_image_generation_request(text: str) -> bool:
    """
    Stricter + broader detector for explicit *pure text-to-image generation* requests
    on first turn (no attached images, no reply-to-image).

    Catches many more natural phrasings in Spanish/English while aggressively
    excluding anything that smells like edit, analysis, or search.

    Returns True only for direct "create a new image from description" intent.
    """
    if not text or len(text.strip()) < 5:
        return False

    t = _strip_accents(text.lower())

    # Broad set of generation verbs (direct commands to create)
    gen_verbs = (
        "genera",
        "crea",
        "haz",
        "dibuja",
        "pinta",
        "ilustra",
        "imagina",
        "generame",
        "creame",
        "hazme",
        "dibujame",
        "pintame",
        "ilustrame",
        "generate",
        "create",
        "draw",
        "make",
        "paint",
        "render",
        "produce",
        "quiero una imagen",
        "necesito una imagen",
        "una imagen de",
        "quiero que generes",
        "hazme una",
        "creame una",
    )

    # Image target nouns / phrases
    img_nouns = (
        "imagen",
        "image",
        "foto",
        "photo",
        "dibujo",
        "ilustracion",
        "ilustración",
        "picture",
        "pic",
        "drawing",
        "ilustración de",
        "render de",
        "arte de",
        "retrato de",
        "paisaje de",
        "escena de",
        # Common fast-typo variants for "imagen"
        "iamgen",
        "imajen",
        "imagne",
        "inagen",
        "imagenn",
        "iamagen",
    )

    has_gen = any(v in t for v in gen_verbs)
    has_img = any(n in t for n in img_nouns)
    if not (has_gen and has_img):
        return False

    # Very strong exclusion list for anything involving existing visual references or analysis
    ref_or_edit_signals = (
        "esta imagen",
        "esta foto",
        "la imagen",
        "la foto",
        "esa imagen",
        "esa foto",
        "las imágenes",
        "las fotos",
        "la que",
        "las que",
        "con esta",
        "basado en",
        "usando esta",
        "de esta",
        "sobre esta",
        "referencia",
        "edita",
        "editá",
        "transforma",
        "convierte esta",
        "redibuja",
        "modifica",
        "meme con",
        "estilo con esta",
        "haz un estilo",
        "en estilo de esta",
        "que ves",
        "qué ves",
        "describe",
        "analiza esta",
        "analiza la",
        "explica esta",
        "cuéntame de esta",
        "qué hay en",
        "qué ves en",
        "dime qué hay",
        "adjunta",
        "subida",
        "en el mensaje",
        "en el reply",
    )
    if any(sig in t for sig in ref_or_edit_signals):
        return False

    # Pure search (not generation)
    search_only = (
        "busca imagen",
        "busca imágenes",
        "busca fotos",
        "imágenes de",
        "fotos de",
        "pictures of",
        "muéstrame imágenes",
        "busca una imagen",
    )
    if any(s in t for s in search_only) and not any(
        v in t for v in ("genera", "crea", "haz una", "dibuja", "quiero una imagen de")
    ):
        return False

    # Capability questions or meta about the feature itself (not a direct generation command)
    if any(
        x in t
        for x in (
            "puedes generar imagen",
            "puedes crear imagen",
            "como genero imagen",
            "cómo genero una imagen",
            "funciona la imagen",
        )
    ):
        return False

    # Very short + no real subject after the verb → probably not a serious gen request
    words = t.split()
    if len(words) <= 3:
        return False

    return True


# =============================================================================
# 4. Visual and Creation Intent Detectors (llm_utils.py)
# =============================================================================
# These decide tool offering and vision behavior in the LLM layer.
#
# _detect_visual_intent:
#   - Broader detector.
#   - Used to set image understanding/search flags on native tools,
#     to decide recent vision harvesting, etc.
#   - Explicitly NOT used to offer the heavy generate/edit tools.
#
# _detect_image_creation_intent:
#   - STRICT detector.
#   - The signal that causes generate_image, edit_image, and generate_video
#     (when explicit) to be offered in the custom tool set.
#   - Must remain narrow to avoid polluting normal chat with expensive media tools.
# =============================================================================


def _detect_visual_intent(text: str) -> bool:
    """Lightweight visual/media intent detector (broader: used for vision context, image understanding in search, analysis, etc).
    NOT used to decide offering of generate/edit tools (see _detect_image_creation_intent for that).
    """
    if not text:
        return False
    t = text.lower()
    visual_keywords = [
        "imagen",
        "image",
        "foto",
        "photo",
        "dibuja",
        "draw",
        "genera",
        "crea",
        "haz",
        "video",
        "anima",
        "animación",
        "meme",
        "estilo",
        "transforma",
        "edita",
        "cyberpunk",
        "blanco y negro",
        "grok-imagine",
        "imágenes de",
        "fotos de",
        "pictures of",
        "muéstrame imágenes",
        "show images",
    ]
    return any(kw in t for kw in visual_keywords)


def _detect_image_creation_intent(text: str) -> bool:
    """
    STRICT detector for clear user intent to GENERATE a new image or EDIT/TRANSFORM/ANIMATE an existing image/video.
    This is the signal used to offer the expensive custom media tools (generate_image, edit_image, generate_video).

    Key goal: do NOT offer image gen/edit tools just because:
    - an image is attached or referenced in a reply
    - user is asking analysis/description questions ("qué ves en esta", "analiza la foto")
    - user is asking a factual/current-event question in a thread that happens to have images

    Only creation/edit/transform commands should trigger the heavy tool schemas.
    The model still receives images for vision when present (separate from tool offering).
    Native search tools are offered broadly (for normal/rich first turns) based on context_need;
    the model decides actual usage via SYSTEM_PROMPT + tool descriptions (no keyword heuristic
    is involved in offering decisions).
    """
    if not text:
        return False
    t = text.lower()

    # Strong explicit creation / edit / video-from-image phrases (Spanish + English)
    creation_phrases = [
        # Pure generation (first-turn or follow-up) - images
        "genera una imagen",
        "genera imagen",
        "generame una imagen",
        "generame imagen",
        "generá una imagen",
        "crea una imagen",
        "crea imagen",
        "creame una imagen",
        "creá una imagen",
        "haz una imagen",
        "hazme una imagen",
        "haz imagen",
        "hazme imagen",
        "dibuja una",
        "dibujame",
        "dibuja imagen",
        "pinta una",
        "ilustra una",
        "quiero una imagen",
        "necesito una imagen",
        "una imagen de",
        "quiero que generes una",
        "make an image",
        "generate an image",
        "create an image",
        "draw a",
        "i want an image of",
        # Pure text-to-video (T2V) standalone requests — must set creation intent so generate_video tool is offered
        "genera un video",
        "genera video",
        "generame un video",
        "generame video",
        "generá un video",
        "crea un video",
        "crea video",
        "creame un video",
        "creá un video",
        "haz un video",
        "hazme un video",
        "haz video",
        "hazme video",
        "dibuja un video",
        "anima",
        "crea una animacion",
        "crea una animación",
        "quiero un video",
        "necesito un video",
        "un video de",
        "un video con",
        "make a video",
        "generate a video",
        "create a video",
        "i want a video of",
        # Edit / transform / style on reference (critical for reply-to-image + edit cases)
        "edita esta",
        "edita la",
        "editá esta",
        "editá la",
        "edita la imagen",
        "edita esta foto",
        "transforma esta",
        "transforma la",
        "convierte esta",
        "convierte la",
        "pasa esta a",
        "pasa la a",
        "cambia esta a",
        "cambia la a",
        "redibuja esta",
        "redibuja la",
        "meme con esta",
        "meme con la",
        "haz un meme con esta",
        "haz un meme con la",
        "genera un estilo con esta",
        "haz un estilo con esta",
        "en estilo de esta",
        "estilo cyberpunk con",
        "make this",
        "edit this",
        "turn this into",
        "style this as",
        # Video from image reference (I2V, explicit only)
        "video de esta",
        "video de la",
        "haz un video de esta",
        "haz un video de la",
        "genera un video de esta",
        "genera un video de la",
        "crea un video de esta",
        "anima esta",
        "anima la",
        "convierte esta en video",
        "convierte la en video",
        "video con esta foto",
        "haz video de la que",
        "animate this",
        "make a video of this",
        # Follow-up creation from a previous bot-generated image (in reply context)
        "basado en esta",
        "usando esta",
        "con esta imagen",
        "con esta foto",
        "otra version de la",
        "variante de la",
        "haz otra",
        "genera otra",
        "la que generaste",
        "la imagen anterior",
        "la foto anterior",
        "la generada",
        "la del bot",
    ]

    analysis_signals = (
        "qué ves",
        "que ves",
        "analiza",
        "describe",
        "cuéntame de",
        "qué hay en",
        "qué ves en",
        "dime qué hay",
        "explica esta",
        "qué es esta",
        "que es esta",
        "resumen de esta",
        "de qué trata esta",
    )
    is_pure_analysis = any(a in t for a in analysis_signals)

    for phrase in creation_phrases:
        if phrase in t:
            if not is_pure_analysis:
                return True
            # If analysis verb present, still allow if there's explicit creation action too (e.g. "edita la que generaste")
            # Use longer phrases to avoid substring false positives like 'genera' in 'generaste'
            if any(
                cv in t
                for cv in (
                    "edita",
                    "editá",
                    "transforma",
                    "convierte",
                    "hazme",
                    "haz una",
                    "haz otra",
                    "genera una",
                    "genera otra",
                    "crea una",
                    "crea otra",
                    "otra version",
                    "variante de",
                    "haz un estilo",
                )
            ):
                return True

    # Fallback: strong gen verb + clear creation noun (image or video), but NOT pure analysis
    strong_gen_verbs = (
        "genera",
        "crea",
        "haz",
        "dibuja",
        "quiero una imagen de",
        "quiero un video",
    )
    creation_nouns = (
        "imagen",
        "image",
        "foto",
        "photo",
        "dibujo",
        "ilustracion",
        "ilustración",
        "picture",
        "drawing",
        "arte de",
        "retrato",
        "escena",
        # Common typos for "imagen" (users type fast)
        "iamgen",
        "imajen",
        "imagne",
        "inagen",
        "imagenn",
        "iamagen",
        # Video / animation nouns for pure T2V fallback
        "video",
        "videos",
        "animacion",
        "animación",
        "animacion de",
        "video de",
    )
    has_verb = any(v in t for v in strong_gen_verbs)
    has_noun = any(n in t for n in creation_nouns)

    if has_verb and has_noun:
        if not is_pure_analysis:
            return True

    return False


# Internal helper used by the detectors above (and by context/core.py functions).
# Not part of the public API but kept here for co-location with the lists it depends on.
def _strip_accents(text: str) -> str:
    if not text:
        return ""
    import unicodedata

    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(c for c in nfkd if not unicodedata.combining(c))
