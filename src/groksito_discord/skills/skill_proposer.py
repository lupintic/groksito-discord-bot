"""
Skill Proposer / Auto-Creator — conservative automatic skill creation for recurring needs.

New behavior (no user proposals):
- When the *same type* of request happens multiple times in a short recent window
  (default 3+ in 48h, now more proactive), Groksito directly creates the skill with approved=True.
- Explicit user requests like "create a skill for steam charts / player counts..." or
  "crea una skill para consultar jugadores..." trigger at count=1 for strong lookup patterns.
- Creation remains conservative: 48h time window + semantic filters (game builds vs meta) +
  only for useful recurring *external data* patterns (steam players, prices, live results...).
- After creation, a short natural confirmation is returned for the caller to surface
  conversationally (e.g. "Listo, creé una skill para consultar jugadores de Steam.").

Key safety:
- Heavy filtering for "skill/habilidad" as in *game character abilities* (RPGs, PoE builds, etc.).
  We only create meta-skills for things Groksito can repeatedly *do for the user*.
- Creation-intent phrases bypass the blocker when combined with clean lookup fingerprints.
- One-offs, casual chat, and game-internal "skills" never trigger creation.
- Reuses existing (larger) channel history (with timestamps) + registry for persistence.
- Expanded steam-players fingerprint + relaxed but safe guards to catch "steam charts",
  "player counts", "pico de jugadores", "how many players in X steam" etc.

The decision layer (using existing approved skills) and execution/injection paths are unchanged.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Any

from ..context import get_recent_channel_messages
from ..utils.correlation import cid_prefix
from .skill_registry import get_skill_registry, Skill

logger = logging.getLogger("groksito.skills.proposer")

# =============================================================================
# Fingerprints (very small, extensible set of common recurring patterns)
# =============================================================================

_FINGERPRINT_RULES: list[tuple[str, list[str]]] = [
    ("steam-players", [
        "jugadores", "players", "player count", "player counts", "steam", "concurrentes", "concurrent players",
        "pico", "peak", "stmchr", "steamchart", "steam chart", "steam charts", "charts", "chart",
        "black desert", "path of exile", "poe2", "crimson desert", "lost ark", "throne and liberty",
        "cuántos jugadores", "cuantos jugadores", "cuántos en steam", "how many players", "jugadores steam"
    ]),
    ("price-check", ["precio", "price", "cuesta", "dólar", "blue", "oficial", "cotización", "cuánto sale"]),
    ("crypto-price", ["btc", "eth", "bitcoin", "ethereum", "crypto", "cripto", "precio btc"]),
    ("match-result", ["partido", "resultado", "goles", "live", "en vivo", "score", "boca", "river", "vs"]),
    ("x-sentiment", ["qué dicen", "opiniones en x", "en twitter", "reacciones en x", "trending", "en tendencia"]),
]

# Strong signals that the user is talking about *game character abilities / builds*
# (PoE, Last Epoch, Diablo, etc.). We must NOT create a meta-skill for Groksito in these cases.
_GAME_ABILITY_SIGNALS = [
    "skill de", "skills de", "habilidad", "habilidades", "mi skill", "la skill", "tus skills",
    "skill tree", "árbol de habilidades", "árbol de skill", "pasiva", "pasivas", "keystone",
    "nodo", "nodos", "ascendancy", "gem", "gema", "build", "build de", "league starter",
    "leveling", "endgame", "poe build", "poe2 build", "diablo build", "grim dawn", "última epoch",
    "last epoch", "skill gem", "puntos de habilidad", "árbol de talentos", "mi personaje",
    "en el juego", "para el boss", "para mapping", "para bossing",
]

# Explicit user requests to *create a (meta) skill/capability* for Groksito to handle a recurring task.
# When present + a clean data-lookup fingerprint, we lower (or drop) the occurrence threshold so
# "create a skill for steam charts..." works immediately instead of requiring 3-5 prior data queries.
_CREATION_INTENT_SIGNALS = [
    "crea una skill", "crear una skill", "haz una skill", "creame una skill", "quiero una skill",
    "create a skill", "make a skill", "crea skill para", "crear skill para", "haz skill para",
    "agrega una skill", "nueva skill para", "quiero que", "para que", "de ahora en más",
    "la próxima vez", "repetidamente", "siempre que", "automáticamente", "crea una habilidad",
]

# Explicit user requests to *edit/update an existing skill* (improve instructions, change behavior, etc.).
# Used to decide when to offer the edit_skill tool instead of (or in addition to) creating a new one.
_EDIT_INTENT_SIGNALS = [
    "mejora la skill", "mejora las instrucciones", "edita la skill", "edita las instrucciones",
    "actualiza la skill", "actualiza las instrucciones", "mejora skill", "edita skill",
    "update the skill", "edit the skill", "improve the skill", "mejora la habilidad",
    "cambia las instrucciones de", "modifica la skill", "update instructions of the skill",
    "edit instructions", "mejora las de la skill",
]

def _fingerprint(text: str) -> str | None:
    if not text:
        return None
    t = text.lower()
    for name, kws in _FINGERPRINT_RULES:
        if any(kw in t for kw in kws):
            return name
    # Very loose generic "lookup something current" (last resort)
    if any(k in t for k in ("busca", "dime", "cuántos", "cuantos", "cuál es el")) and len(text) < 120:
        return "general-lookup"
    return None


def _is_game_ability_context(text: str) -> bool:
    """
    Returns True if the message is very likely talking about in-game character skills/abilities/builds
    rather than something Groksito can do repeatedly for the user (player counts, prices, etc.).

    This is the key semantic guard to avoid creating nonsense skills like "PoE Skill Builds"
    when the user is theorycrafting their character.

    Explicit "create a skill for me to do X lookup" requests are allowed through (return False).
    """
    if not text:
        return False
    t = text.lower()

    # Explicit requests to create a meta-skill for Groksito (data lookup, steam charts etc.)
    # must bypass the game-ability blocker even if they contain "skill" + game names.
    if _has_explicit_creation_intent(text):
        return False

    # Direct strong signals
    if any(sig in t for sig in _GAME_ABILITY_SIGNALS):
        return True

    # "skill" or "habilidad" appearing together with game names but *without* clear player-count / price / lookup signals
    has_skill_word = "skill" in t or "habilidad" in t or "build" in t
    game_names = ("poe", "poe2", "path of exile", "diablo", "last epoch", "grim dawn", "última epoch", "warframe", "destiny")
    has_game = any(g in t for g in game_names)

    if has_skill_word and has_game:
        # Counter-signals that this is actually a *player count / external* query about the game
        # (or explicit creation request for a lookup skill). Expanded to catch "player count", "steam chart" etc.
        player_or_lookup = any(x in t for x in (
            "jugadores", "players", "player count", "player counts", "concurrentes", "concurrent",
            "pico", "peak", "steam", "cuántos", "precio", "price", "cuesta", "cuantos jugadores",
            "chart", "charts", "count", "counts", "steam chart", "steam charts"
        ))
        if not player_or_lookup:
            return True

    # "build con skills", "mis skills", standalone in RPG context
    if ("build" in t or "mis skills" in t or "mis habilidades" in t) and not any(x in t for x in (
        "jugadores", "players", "player count", "steam", "concurrent", "precio de", "dólar",
        "chart", "charts", "steam chart"
    )):
        return True

    return False


def _has_explicit_creation_intent(text: str) -> bool:
    """True if the user is explicitly telling Groksito to create a reusable skill for a task."""
    if not text:
        return False
    t = text.lower()
    return any(sig in t for sig in _CREATION_INTENT_SIGNALS)


def _has_explicit_edit_intent(text: str) -> bool:
    """True if the user is explicitly asking to edit/update an existing skill's instructions or behavior."""
    if not text:
        return False
    t = text.lower()
    return any(sig in t for sig in _EDIT_INTENT_SIGNALS)


@dataclass
class SkillProposal:
    """Legacy structure kept for any external references / tests. Not used in the auto-create flow."""
    fingerprint: str
    suggested_name: str
    reason: str
    instructions: str
    allowed_tools: list[str]
    example_query: str | None = None


@dataclass
class SkillCreationResult:
    """Result of automatic skill creation. Contains the live skill and a short natural confirmation to surface."""
    skill: Skill
    confirmation_message: str
    fingerprint: str
    count_in_window: int


# =============================================================================
# Public API (new auto-create flow)
# =============================================================================

async def detect_and_create_skill(
    *,
    channel_id: int,
    user_id: int,
    current_message: str,
    min_occurrences: int | None = None,
    window_hours: int | None = None,
) -> SkillCreationResult | None:
    """
    Conservative automatic skill creation.

    Scans recent channel messages (with timestamps) for the user.
    - Only counts messages inside a recent time window (default 48h).
    - Completely ignores messages that look like game-character "skills"/builds (semantic filter).
    - Requires a higher threshold (default 5) of the *same* fingerprint.
    - If threshold crossed and no covering approved skill exists yet → creates one with approved=True
      via the registry and returns a result with a short conversational confirmation.

    Returns None if nothing should be created this turn (the common case).
    """
    cid_p = cid_prefix()

    # Resolve conservative defaults (can be overridden by caller or settings)
    try:
        from ..config import settings as _cfg
        eff_min = min_occurrences or int(getattr(_cfg, "skill_auto_create_min_occurrences", 5))
        eff_window_h = window_hours or int(getattr(_cfg, "skill_auto_create_window_hours", 48))
        auto_enabled = bool(getattr(_cfg, "enable_skill_auto_creation", True))
    except Exception:
        eff_min = min_occurrences or 5
        eff_window_h = window_hours or 48
        auto_enabled = True

    if not auto_enabled:
        return None

    window_seconds = eff_window_h * 3600
    now = time.time()

    # Pull recent history (already has ts). Use larger limit now that channel buffer is bigger (150)
    # so we can actually see recurring patterns over a 48h window even with normal chat volume.
    try:
        msgs = get_recent_channel_messages(channel_id, limit=100)
    except Exception:
        msgs = []

    # Filter to this user (strong preference) + non-bot messages as fallback + time window.
    # This focuses recurring detection on the current user's requests (as documented) while
    # still allowing channel-wide signals if author_id is missing.
    user_msgs = []
    for m in msgs:
        aid = int(m.get("author_id") or 0)
        is_b = bool(m.get("is_bot", False))
        ts = float(m.get("ts", 0) or 0)
        if now - ts > window_seconds:
            continue
        if aid == user_id or not is_b:
            user_msgs.append(m)

    # Count fingerprints, applying the game-ability semantic filter aggressively
    counts: dict[str, int] = {}
    examples: dict[str, str] = {}
    latest_ts: dict[str, float] = {}

    def _count_if_clean(text: str, ts_val: float) -> None:
        if not text or _is_game_ability_context(text):
            return
        fp = _fingerprint(text)
        if not fp:
            return
        # Extra guard for steam-players: the message must actually be about player counts / charts,
        # not just mentioning a game. Relaxed to catch "player count", "steam charts", "chart", etc.
        # Also allows "steam" + known game name even without exact player word.
        if fp == "steam-players":
            tlow = text.lower()
            player_chart_terms = (
                "jugadores", "players", "player count", "player counts", "concurrentes", "concurrent",
                "pico", "peak", "steamchart", "stmchr", "cuántos jugadores", "cuantos jugadores",
                "chart", "charts", "count", "counts", "steam chart", "steam charts"
            )
            if not any(x in tlow for x in player_chart_terms):
                if not ("steam" in tlow and any(g in tlow for g in ("path of exile", "poe", "black desert", "lost ark", "throne", "crimson"))):
                    return
        counts[fp] = counts.get(fp, 0) + 1
        if fp not in examples:
            examples[fp] = text[:140]
        latest_ts[fp] = max(latest_ts.get(fp, 0), ts_val)

    for m in user_msgs[-25:]:
        _count_if_clean(m.get("content", "") or "", float(m.get("ts", 0) or 0))

    # Always consider the current turn
    _count_if_clean(current_message or "", now)

    if not counts:
        return None

    registry = get_skill_registry()

    for fp, cnt in sorted(counts.items(), key=lambda x: -x[1]):
        # Support more proactive creation:
        # - For strong lookup patterns (steam players etc.), explicit "create a skill for..." requests
        #   can trigger at count=1 (the current message) or very low count.
        # - Otherwise use the (now default 3) configured threshold.
        # This makes "user asked me to create a skill for steam charts..." work immediately
        # while still being conservative for pure implicit repeats.
        is_explicit_create = _has_explicit_creation_intent(current_message or "") or _has_explicit_creation_intent(examples.get(fp, ""))
        strong_lookup_fp = fp in ("steam-players", "price-check", "crypto-price", "match-result")

        eff_min_fp = eff_min
        if strong_lookup_fp and is_explicit_create:
            eff_min_fp = 1
        elif strong_lookup_fp:
            eff_min_fp = max(2, eff_min)  # still easier than generic for recurring data lookups

        if cnt < eff_min_fp:
            continue

        # Already have something that covers this pattern?
        if registry.has_approved_for_pattern(fp, current_message or examples.get(fp, "")):
            continue

        # Build creation data from the existing high-quality base builders
        base = _base_proposal_for_fingerprint(fp, examples.get(fp, current_message or ""))
        if not base:
            continue

        # Directly create + approve (no proposal step)
        try:
            created = registry.create_approved_skill(
                name=base.suggested_name,
                description=base.reason[:300],
                instructions=base.instructions,
                allowed_tools=base.allowed_tools,
                created_from_pattern=fp,
            )
        except Exception as create_err:
            logger.warning(f"{cid_p}[SKILLS] Failed to auto-create skill for {fp}: {create_err}")
            continue

        # Short, natural, conversational confirmation (never a big proposal block)
        confirmation = _natural_confirmation_for(created, fp, cnt)

        logger.info(f"{cid_p}[SKILLS] AUTO-CREATED skill id={created.id} name={created.name} fp={fp} count_in_window={cnt}")
        return SkillCreationResult(
            skill=created,
            confirmation_message=confirmation,
            fingerprint=fp,
            count_in_window=cnt,
        )

    return None


def _natural_confirmation_for(skill: Skill, fp: str, observed_count: int) -> str:
    """Generate a short, friendly, non-robotic confirmation. Varies a little by pattern."""
    name_lower = skill.name.lower()

    if fp == "steam-players":
        variants = [
            "Listo, creé una skill para consultar jugadores de Steam.",
            "Hecho. A partir de ahora respondo más rápido y consistente cuando preguntes por jugadores en Steam.",
            "Creé una skill para conteos de Steam. La próxima vez va a ser más directo.",
            "Perfecto — ahora tengo una skill dedicada para Steam Charts. Preguntame por cualquier juego.",
        ]
        return variants[observed_count % len(variants)]

    if fp == "price-check":
        return "Listo, creé una skill para buscar precios rápido."

    if fp == "crypto-price":
        return "Perfecto, ahora tengo una skill dedicada para precios de crypto."

    if fp == "match-result":
        return "Listo, creé una skill para resultados de partidos."

    if fp == "x-sentiment":
        return "Creé una skill para ver el pulso en X de forma más limpia."

    # Generic but still natural
    return f"Listo, creé una skill para {name_lower}."


# Legacy helper kept for any old call sites / tests (now points at the new behavior for creation)
async def detect_and_propose_skill(
    *,
    channel_id: int,
    user_id: int,
    current_message: str,
    min_occurrences: int = 3,
) -> SkillProposal | None:
    """
    Legacy shim. The system no longer proposes — it auto-creates when thresholds are met.
    This function now returns None in normal operation (no proposal objects are built for the chat flow).
    Kept only to avoid import breakage in tests or other modules.
    """
    # We intentionally do not return old-style proposals anymore.
    # If someone calls this, we can trigger the creation path but still return None
    # so that old proposal-sending code does not accidentally send big proposal text.
    try:
        # Fire-and-forget creation (the real work happens in the new function)
        await detect_and_create_skill(
            channel_id=channel_id,
            user_id=user_id,
            current_message=current_message,
            min_occurrences=max(min_occurrences, 5),  # force conservative
        )
    except Exception:
        pass
    return None  # Never surface a proposal object


def _has_covering_skill(approved: list[Skill], fp: str, query: str) -> bool:
    """Legacy thin wrapper. Prefer registry.has_approved_for_pattern in new code."""
    try:
        reg = get_skill_registry()
        return reg.has_approved_for_pattern(fp, query)
    except Exception:
        pass
    # Fallback to old logic
    q = (query or "").lower()
    for sk in approved:
        if fp == "steam-players" and ("steam" in sk.name.lower() or "player" in sk.name.lower() or "jugador" in sk.name.lower()):
            return True
        if fp == "price-check" and ("price" in sk.name.lower() or "precio" in sk.name.lower()):
            return True
        if fp == "crypto-price" and ("crypto" in sk.name.lower() or "btc" in sk.name.lower() or "bitcoin" in sk.name.lower()):
            return True
        if any(tok in (sk.name + " " + sk.description).lower() for tok in q.split() if len(tok) > 4):
            return True
    return False


async def _build_proposal(fingerprint: str, example: str) -> SkillProposal | None:
    """Create a high-quality proposal. Uses a tiny LLM call when possible for natural wording."""
    base = _base_proposal_for_fingerprint(fingerprint, example)
    if not base:
        return None

    # Optional polish with a tiny stable prompt (still very cheap)
    polished = await _maybe_polish_proposal(base)
    return polished or base


def _base_proposal_for_fingerprint(fp: str, example: str) -> SkillProposal | None:
    if fp == "steam-players":
        return SkillProposal(
            fingerprint=fp,
            suggested_name="Steam Player Counts",
            reason="El usuario pregunta repetidamente por la cantidad de jugadores concurrentes de distintos juegos en Steam. Un skill dedicado puede resolver el nombre del juego, usar herramientas para obtener datos actuales y dar un formato consistente y rápido.",
            instructions=(
                "Eres un especialista en Steam Charts. Cuando el usuario pregunte por jugadores de un juego:\n"
                "1. Resuelve el nombre (o abreviatura) al título correcto (Black Desert, Path of Exile 2, etc).\n"
                "2. Usa web_search con queries precisas como 'current players Path of Exile 2 steam' o 'Black Desert concurrent players'.\n"
                "3. Reporta SIEMPRE: jugadores ahora + picos si están disponibles.\n"
                "4. Sé extremadamente conciso. Una línea por juego es ideal.\n"
                "5. Si no hay datos frescos, dilo claramente en una frase."
            ),
            allowed_tools=["web_search"],
            example_query=example,
        )

    if fp == "price-check":
        return SkillProposal(
            fingerprint=fp,
            suggested_name="Quick Price Lookup",
            reason="El usuario consulta precios (dólar, productos, etc.) varias veces. Un skill permite dar respuestas directas y frescas usando la mínima cantidad de búsqueda posible.",
            instructions=(
                "Eres un buscador de precios ultra-eficiente. Para cualquier consulta de precio:\n"
                "- Forma la query de búsqueda MÁS ESTRECHA posible (ej: 'dólar blue hoy Argentina site:ambito.com').\n"
                "- Extrae solo el número o valor actual más relevante.\n"
                "- Responde con una sola frase clara + fuente opcional entre paréntesis.\n"
                "- Nunca listes múltiples resultados ni repitas la búsqueda."
            ),
            allowed_tools=["web_search"],
            example_query=example,
        )

    if fp == "crypto-price":
        return SkillProposal(
            fingerprint=fp,
            suggested_name="Crypto Price Checker",
            reason="Consultas repetidas de precios de cripto (BTC, ETH, etc.). El skill estandariza la respuesta rápida y fresca.",
            instructions=(
                "Responde precios de criptomonedas de forma directa y actualizada.\n"
                "Usa web_search solo cuando sea necesario para datos en tiempo real.\n"
                "Formato preferido: 'Bitcoin: $67,200 USD (última hora)'. Sé breve."
            ),
            allowed_tools=["web_search"],
            example_query=example,
        )

    if fp == "match-result":
        return SkillProposal(
            fingerprint=fp,
            suggested_name="Live Match Results",
            reason="El usuario pide resultados de partidos o scores en vivo con frecuencia. Un skill enfocado da respuestas rápidas y precisas.",
            instructions=(
                "Cuando pregunten por un partido o resultado en vivo:\n"
                "- Busca con la query más específica posible (equipos + 'live' o 'resultado').\n"
                "- Devuelve solo el marcador actual + estado del partido.\n"
                "- Muy conciso."
            ),
            allowed_tools=["web_search"],
            example_query=example,
        )

    if fp == "x-sentiment":
        return SkillProposal(
            fingerprint=fp,
            suggested_name="X / Twitter Pulse",
            reason="El usuario quiere saber qué se dice en X sobre temas actuales. Un skill dedicado prioriza x_search y sintetiza solo lo más relevante.",
            instructions=(
                "Eres un analizador de pulso social en X.\n"
                "Usa x_search (no web_search) cuando la pregunta sea claramente sobre opiniones o reacciones en X.\n"
                "De los resultados, conserva 1-2 posts como máximo y resume el sentimiento predominante en una frase.\n"
                "Nunca pegues timelines enteros."
            ),
            allowed_tools=["x_search", "web_search"],
            example_query=example,
        )

    if fp == "general-lookup":
        return SkillProposal(
            fingerprint=fp,
            suggested_name="Quick Fact Lookup",
            reason="El usuario hace consultas factuales cortas y repetitivas que se benefician de una estrategia consistente de búsqueda mínima + respuesta directa.",
            instructions=(
                "Para consultas factuales breves, usa la herramienta de búsqueda más adecuada con la query más estrecha posible.\n"
                "Extrae solo el dato clave. Responde en una o dos frases máximo.\n"
                "Nunca expandas a menos que el usuario lo pida."
            ),
            allowed_tools=["web_search"],
            example_query=example,
        )

    return None


# =============================================================================
# Lightweight trigger for offering the create_skill *tool* to the model
# (new native tool-calling path). This is intentionally cheap and does not
# perform the creation itself — the model decides via tool call.
# =============================================================================

def should_offer_create_skill_tool(
    current_message: str,
    channel_id: int = 0,
    user_id: int = 0,
) -> bool:
    """Cheap pre-filter: should we include the create_skill tool schema for this turn?

    We only want to pay the (small) schema + reasoning cost in relevant cases.
    - Explicit creation requests from the user are the primary trigger.
    - Messages that match strong "recurring useful data" fingerprints are secondary
      candidates (the model then uses full conversation context + its judgment to
      actually call the tool or not).

    This greatly reduces reliance on complex counting / hardcoded rules for the
    *creation decision* itself.
    """
    if not current_message or not str(current_message).strip():
        return False

    msg = str(current_message)

    # Strongest signal: user is explicitly telling us to create a skill
    if _has_explicit_creation_intent(msg):
        return True

    # Never offer for game-internal character skills/builds
    if _is_game_ability_context(msg):
        return False

    # Light fingerprint match for known useful recurring patterns (player counts,
    # prices, live results, etc.). The model will do the real "is this worth a skill?"
    # reasoning when it sees the tool + the chat history.
    fp = _fingerprint(msg)
    if fp in ("steam-players", "price-check", "crypto-price", "match-result", "x-sentiment", "general-lookup"):
        return True

    # Very short queries that smell like fresh/recurring data lookups
    t = msg.lower().strip()
    if len(t) < 120 and any(k in t for k in (
        "cuántos", "cuantos", "pico", "actual", "ahora", "hoy", "en vivo", "live",
        "precio", "players", "jugadores", "chart", "charts", "concurrent", "count"
    )):
        return True

    return False


async def _maybe_polish_proposal(base: SkillProposal) -> SkillProposal | None:
    """Tiny optional LLM polish for nicer natural language in the proposal shown to the user."""
    try:
        # We only do this if we have credentials; otherwise we keep the solid base text.
        from openai import AsyncOpenAI
        from ..llm.llm_utils import _call_responses_with_retry
        from ..core.grok_oauth import get_grok_bearer as _get_grok_bearer  # type: ignore
        from ..config import settings as _settings

        bearer = None
        try:
            if _get_grok_bearer:
                bearer = _get_grok_bearer()
        except Exception:
            pass
        if not bearer:
            bearer = getattr(_settings, "xai_api_key", None)
        if not bearer:
            return None

        client = AsyncOpenAI(api_key=bearer, base_url="https://api.x.ai/v1", timeout=20.0)

        polish_prompt = (
            "You are helping write a short, friendly skill proposal for a Discord user.\n"
            "Given the raw proposal below, improve ONLY the 'reason' and the first sentence of 'instructions' to be warmer and clearer. "
            "Keep everything extremely concise. Output exactly two lines:\n"
            "REASON: <one improved sentence>\n"
            "INSTRUCTIONS_START: <first 1-2 sentences of instructions>\n\n"
            f"NAME: {base.suggested_name}\n"
            f"REASON: {base.reason}\n"
            f"INSTRUCTIONS: {base.instructions}\n"
        )

        resp = await _call_responses_with_retry(
            client,
            model=getattr(_settings, "grok_model", "grok-4.3"),
            input=[{"role": "user", "content": polish_prompt}],
        )
        text = ""
        if hasattr(resp, "output_text") and resp.output_text:
            text = str(resp.output_text)
        else:
            for item in (getattr(resp, "output", None) or []):
                if getattr(item, "type", None) == "message":
                    for c in (getattr(item, "content", None) or []):
                        if getattr(c, "type", None) == "text":
                            text = str(getattr(c, "text", ""))
                            break

        if "REASON:" in text:
            lines = text.splitlines()
            new_reason = base.reason
            new_instr_start = ""
            for ln in lines:
                if ln.upper().startswith("REASON:"):
                    new_reason = ln.split(":", 1)[1].strip()
                if ln.upper().startswith("INSTRUCTIONS_START:"):
                    new_instr_start = ln.split(":", 1)[1].strip()

            improved = SkillProposal(
                fingerprint=base.fingerprint,
                suggested_name=base.suggested_name,
                reason=new_reason or base.reason,
                instructions=(new_instr_start + "\n" + base.instructions) if new_instr_start else base.instructions,
                allowed_tools=base.allowed_tools,
                example_query=base.example_query,
            )
            return improved
    except Exception:
        pass

    return None


def format_proposal_for_user(proposal: SkillProposal) -> str:
    """Human-friendly message to show in chat when proposing."""
    tools = ", ".join(proposal.allowed_tools) if proposal.allowed_tools else "web_search"
    ex = f"\nEjemplo: \"{proposal.example_query}\"" if proposal.example_query else ""
    return (
        f"**Propuesta de skill: {proposal.suggested_name}**\n"
        f"{proposal.reason}\n\n"
        f"**Qué haría:**\n{proposal.instructions[:420]}{'...' if len(proposal.instructions) > 420 else ''}\n\n"
        f"**Herramientas permitidas:** {tools}{ex}\n\n"
        "Si querés que lo cree, respondé **sí**, **crealo**, **aprobar**, o **yes**. "
        "Si no, ignorá este mensaje o decí **no**."
    )
