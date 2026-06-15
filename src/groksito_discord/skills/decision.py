"""
Decision Layer (Light) for Groksito.

Post #22/#24: This is the remaining lightweight heuristic layer (no more LLM decision call).
Kept ONLY for fast-path token savings on obvious timeless "direct" cases and cheap signals
for offering light decision tools on addressed turns. The primary native decisions now
live in Grok's tool-calling flow (get_recent_context, respond_directly, native search etc).

DECISION_PROMPT retained for reference (not used for calls).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any

from ..core.intent import is_conversation_meta_question
from ..utils.correlation import cid_prefix

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
    # Post #24: reduced legacy pre-decider kept only for the narrow fast-path cases.
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
    Lightweight fallback — no keyword-based search routing (#48).

    Search and intent decisions live in Grok's tool-calling flow. This helper only
    signals whether recent context may be useful on addressed turns.
    """
    is_addressed = is_mentioned or is_reply_to_bot
    needs_recent = is_addressed or is_conversation_meta_question(user_message)

    action = DecisionAction.RECENT_CONTEXT if needs_recent and is_addressed else DecisionAction.DIRECT

    return Decision(
        action=action,
        needs_recent_context=needs_recent,
        needs_search="none",
        use_skill=None,
        propose_skill=None,
        rationale="heuristic-prompt-driven",
    )
