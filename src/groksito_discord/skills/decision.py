"""
Decision Layer (Light) for Groksito.

#22 DEPRECATION NOTE:
This module (the pre-decision heuristic + fast-path) is legacy "heavy custom decision
logic" being progressively relaxed per the goal of relying on Grok native reasoning.
The previous tiny LLM decision call was already removed; the remaining heuristic is
kept ONLY for:
  - extreme fast-path token savings on obvious timeless "direct" (skip search schemas)
  - cheap signals for *when* to offer the light decision tools on addressed turns
    (the real native choice mechanism now lives in the main tool-calling flow)

Long term these heuristics shrink further; normal mentions should let the model decide.

(The DECISION_PROMPT below is retained for reference but no longer drives an LLM call.)
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any

from ..correlation import cid_prefix

logger = logging.getLogger("groksito.skills.decision")

# =============================================================================
# THE DECISION PROMPT (stable prefix — excellent for prompt caching)
# =============================================================================
# Refined for higher decision quality + token efficiency:
# - Much stricter "search" criteria (only real time-sensitive/live data).
# - Clear timeless vs fresh distinction with concrete examples.
# - Strong preference for "direct" (saves tool schema tokens when we can skip web/x_search).
# - Good handling of recent_context on addressed turns.
# - Compact but example-driven so the model makes consistent high-quality calls.
# =============================================================================

DECISION_PROMPT = """You are a lightweight decision router for Groksito (normal Discord chat with Grok).

Output ONE compact JSON object only (no text, no ```):

{
  "action": "direct" | "search" | "use_skill" | "recent_context",
  "needs_recent_context": bool,
  "needs_search": "web" | "x" | "both" | "none",
  "use_skill": "approved-skill-id-or-null",
  "rationale": "short log note"
}

Rules (follow strictly, be conservative):
- "direct" (DEFAULT & PREFERRED): timeless knowledge, definitions, "qué es X", "cómo funciona", math, code, history, general explanations, opinions, roleplay, concepts. Grok should answer from training data. Examples that must be direct: "qué es la fotosíntesis", "capital de Francia", "cómo se dice hello en español", "explica recursión".
- "search": ONLY when the user needs information that is time-sensitive, live, or has changed since training cutoff. Must have explicit recency or fresh angle: "hoy", "ahora", "actual", "en vivo", "breaking", "última hora", "precio del dólar hoy", "latest controversies", "recent issues", "qué pasó con", "what happened", "drama", "scandal", "problemas con", "issues with". 
  - "Qué es el dólar blue" or "explica bitcoin" = direct.
  - "Dólar blue hoy" / "cuánto está el bitcoin ahora" / "pico de jugadores poe2" / "latest controversies about Task Bar Hero" = search.
- "recent_context": the user addressed the bot (mentioned or reply_to_bot) or is asking about the prior conversation ("de qué hablaban antes", "continúa", "resumen de la charla"). Usually also set needs_recent_context=true.
- "use_skill": only when the exact query matches one of the approved_skills in the signals list.
- needs_recent_context: true on any mention/reply to bot, or any explicit reference to "antes", "la conversación anterior", "qué dijimos".
- needs_search: "none" unless the search criteria above are clearly met. Prefer "none" / "direct" to avoid wasting tokens on tool schemas.
- Never search for one-off questions, general knowledge, or timeless topics.

Signals (compact JSON):
{ signals }
"""

# =============================================================================
# Structured Decision Result
# =============================================================================


class DecisionAction(str, Enum):
    DIRECT = "direct"
    SEARCH = "search"
    USE_SKILL = "use_skill"
    RECENT_CONTEXT = "recent_context"


@dataclass
class Decision:
    action: DecisionAction = DecisionAction.DIRECT
    needs_recent_context: bool = False
    needs_search: str = "none"  # "web" | "x" | "both" | "none"
    use_skill: str | None = None  # skill_id if action == USE_SKILL
    propose_skill: dict[str, str] | None = None  # {"name": "...", "reason": "..."}
    rationale: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action.value,
            "needs_recent_context": self.needs_recent_context,
            "needs_search": self.needs_search,
            "use_skill": self.use_skill,
            "propose_skill": self.propose_skill,
            "rationale": self.rationale,
        }


# =============================================================================
# Decision Engine
# =============================================================================


async def make_decision(
    *,
    user_message: str,
    author_name: str = "",
    is_mentioned: bool = False,
    is_reply_to_bot: bool = False,
    recent_signals: str | None = None,
    approved_skill_names: list[str] | None = None,
    context_need: str = "normal",  # from classify (casual/minimal/normal/rich) — helps heuristics
) -> Decision:
    """
    Lightweight decision step (tiny cache-friendly call).

    Improvements for quality + efficiency:
    - Stronger prompt with concrete timeless vs fresh examples.
    - Fast local pre-filter: obvious "direct" cases skip the extra LLM call entirely.
    - Much improved _heuristic_decision that mirrors the prompt (used both as fast path and fallback).
    - The returned Decision is later used in llm.py to completely suppress web_search/x_search
      tool schemas when action="direct" or needs_search="none" (big token saving).
    """
    cid_p = cid_prefix()

    # Build compact signals block (only varying part — keeps prompt stable for caching)
    signals = {
        "user": author_name or "user",
        "message": (user_message or "")[:280],
        "mentioned": is_mentioned,
        "reply_to_bot": is_reply_to_bot,
        "recent": (recent_signals or "")[:400] if recent_signals else "",
        "approved_skills": (approved_skill_names or [])[:6],
        "need": context_need,
    }
    signals_text = json.dumps(signals, ensure_ascii=False)
    prompt = DECISION_PROMPT.replace("{ signals }", signals_text)

    # ------------------------------------------------------------------
    # Efficiency win: fast local pre-filter for obvious direct cases.
    # If this triggers we completely avoid the extra tiny decision LLM call.
    # This is safe because the improved heuristic below is now quite accurate.
    # ------------------------------------------------------------------
    tlow = (user_message or "").lower()
    has_strong_timeless = any(
        k in tlow
        for k in (
            "qué es",
            "que es",
            "quién es",
            "quien es",
            "qué significa",
            "como se define",
            "capital de",
            "fórmula",
            "matemática",
            "explica el concepto",
            "definición de",
            "cómo funciona un",
            "por qué existe",
        )
    )
    has_strong_fresh = any(
        k in tlow
        for k in (
            "hoy",
            "ahora",
            "actual",
            "en este momento",
            "en vivo",
            "live",
            "breaking",
            "última hora",
            "dólar blue",
            "precio hoy",
            "cotización",
            "clima hoy",
            "steam chart",
            "steam charts",
            "player count",
            "player counts",
            "concurrent player",
            "jugadores steam",
            "pico de jugadores",
            "cuántos jugadores",
            "cuantos jugadores",
            "steam player",
            "charts player",
            "latest",
            "reciente",
            "controvers",
            "polémica",
            "problemas",
            "issues",
            "drama",
            "scandal",
            "qué pasó",
            "what happened",
            "recent",
        )
    )
    has_x_signal = any(k in tlow for k in ("x.com", "tweet", "en tendencia", "twitter"))

    if (
        has_strong_timeless
        and not has_strong_fresh
        and not has_x_signal
        and not (is_mentioned or is_reply_to_bot)
        and context_need not in ("rich",)
        and not (approved_skill_names or [])
    ):
        # Very confident direct + no need for model nuance -> skip LLM decision call
        return _heuristic_decision(
            user_message=user_message,
            is_mentioned=is_mentioned,
            is_reply_to_bot=is_reply_to_bot,
            recent_signals=recent_signals,
            context_need=context_need,
        )

    # NOTE (evolution to native tool calling):
    # The primary decision-making for context, skill use/creation, and direct vs tool actions
    # has moved to the model's native tool calling (get_recent_context, use_skill, create_skill,
    # respond_directly + native web/x_search) inside the main Responses API flow.
    # This tiny decision helper is now kept as a *lightweight heuristic only* for:
    # - Fast-path search tool suppression (big token win on timeless queries)
    # - Fast-path recent_context forcing and skill pre-injection (compatibility / efficiency)
    # - Cheap signals to decide *when* to offer the internal decision tools to the model.
    #
    # The previous LLM-based "tiny decision" call (the AsyncOpenAI block) has been removed
    # to reduce extra model calls and let the main Grok reasoning (via tools) drive behavior.
    # This is a deliberate simplification toward more natural, unified tool-calling decisions.

    # Always use the (now quite strong) heuristic.
    # #22: this path itself is the reduced legacy pre-decider. Future passes will
    # further shrink the keyword sets or remove the need entirely for most turns.
    return _heuristic_decision(
        user_message=user_message,
        is_mentioned=is_mentioned,
        is_reply_to_bot=is_reply_to_bot,
        recent_signals=recent_signals,
        context_need=context_need,
    )


def _parse_decision_json(text: str) -> Decision | None:
    if not text:
        return None
    # Be tolerant: strip code fences if the model was naughty
    t = text.strip()
    if t.startswith("```"):
        # remove first and last fence
        lines = t.splitlines()
        if len(lines) >= 3:
            t = "\n".join(lines[1:-1])
    try:
        obj = json.loads(t)
    except Exception:
        # last-ditch: find first { ... }
        try:
            start = t.find("{")
            end = t.rfind("}")
            if start != -1 and end > start:
                obj = json.loads(t[start : end + 1])
            else:
                return None
        except Exception:
            return None

    if not isinstance(obj, dict):
        return None

    action_raw = str(obj.get("action", "direct")).lower().strip()
    action = DecisionAction.DIRECT
    if action_raw in ("search", "use_skill", "recent_context"):
        action = DecisionAction(action_raw)

    return Decision(
        action=action,
        needs_recent_context=bool(obj.get("needs_recent_context", False)),
        needs_search=str(obj.get("needs_search", "none")).lower(),
        use_skill=obj.get("use_skill"),
        propose_skill=obj.get("propose_skill")
        if isinstance(obj.get("propose_skill"), dict)
        else None,
        rationale=str(obj.get("rationale", ""))[:200],
    )


def _heuristic_decision(
    *,
    user_message: str,
    is_mentioned: bool,
    is_reply_to_bot: bool,
    recent_signals: str | None,
    context_need: str = "normal",
) -> Decision:
    """
    Improved local heuristic (used both for fast-path skips and as fallback).

    Tries hard to match the refined prompt:
    - Strong bias toward "direct" for definitional/timeless questions.
    - Only triggers search on clear time-sensitive + recency signals.
    - Good recent_context detection on addressed turns.
    - Respects context_need (lean direct on casual/minimal).
    """
    t = (user_message or "").lower()

    if context_need in ("casual", "minimal", "image_gen"):
        # Ultra-light turns should almost never search or need heavy context
        return Decision(
            action=DecisionAction.DIRECT,
            needs_recent_context=is_mentioned or is_reply_to_bot,
            needs_search="none",
            rationale="heuristic-casual",
        )

    # --- Recent / address signals (very important for coherence) ---
    recent_kw = (
        "antes",
        "dijimos",
        "hablábamos",
        "qué pasó",
        "continúa",
        "de qué",
        "resumen de la",
        "la charla",
        "tema anterior",
    )
    is_addressed = is_mentioned or is_reply_to_bot
    needs_recent = is_addressed or any(k in t for k in recent_kw)

    # --- Timeless / definitional patterns (force direct) ---
    timeless_starters = (
        "qué es",
        "que es",
        "quién es",
        "quien es",
        "qué significa",
        "como se define",
        "capital de",
        "fórmula de",
        "explica qué es",
        "definición",
        "cómo funciona un",
        "por qué existe",
        "qué es la",
        "concepto de",
    )
    has_timeless = any(k in t for k in timeless_starters)

    # --- Strong fresh / live / current-value signals (the only things that justify search) ---
    # #22: trimmed a few marginal terms as part of reducing pre-decision keyword weight.
    # Still sufficient for fast-path; model + native tools handle nuance.
    strong_fresh = (
        "hoy",
        "ahora",
        "actual",
        "en vivo",
        "live",
        "breaking",
        "última hora",
        "dólar blue",
        "cotización",
        "clima hoy",
        "pico jugadores",
        "steam chart",
        "steam charts",
        "player count",
        "jugadores steam",
        "pico de jugadores",
        "cuántos jugadores",
        "latest",
        "reciente",
        "controvers",
        "polémica",
        "problemas",
        "qué pasó",
        "what happened",
        "recent",
    )
    has_strong_fresh = any(k in t for k in strong_fresh)

    # Weaker time words that are often not enough by themselves
    weak_time = (
        "precio",
        "dólar",
        "partido",
        "noticia",
        "resultados",
        "clima",
        "issues",
        "problemas",
        "controvers",
        "drama",
        "scandal",
    )
    has_weak_time = any(k in t for k in weak_time)

    needs_search = "none"
    if has_strong_fresh or (has_weak_time and not has_timeless):
        needs_search = "web"

    # X / Twitter specific
    if any(k in t for k in ("x.com", "twitter", "tweet", "en tendencia", "post en x")):
        needs_search = "x" if needs_search == "none" else "both"

    # Decide primary action
    action = DecisionAction.DIRECT
    if needs_search != "none":
        action = DecisionAction.SEARCH
    if needs_recent and action == DecisionAction.DIRECT:
        action = DecisionAction.RECENT_CONTEXT

    # If strongly addressed + meta, we may want recent_context even if there is some fresh data
    if needs_recent and is_addressed and not has_strong_fresh:
        action = DecisionAction.RECENT_CONTEXT

    return Decision(
        action=action,
        needs_recent_context=needs_recent or is_addressed,
        needs_search=needs_search,
        use_skill=None,
        propose_skill=None,
        rationale="heuristic-improved",
    )
"