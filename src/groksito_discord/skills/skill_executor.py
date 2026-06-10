"""
Skill Executor / Injector (lightweight).

Responsibilities:
- Take a Decision (from the decision layer) and the current conversation input.
- If the decision says "use_skill" and the skill is approved, produce a
  SkillInjection that contains:
    * A high-priority system block with the skill's natural language instructions
    * The subset of tools the skill is allowed to use
- Provide helpers to filter native + custom tool lists accordingly.
- Never bypass the user-approval gate.

This module does NOT create new agent loops. It only prepares data that the
existing llm.py + Responses API flow consumes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from ..correlation import cid_prefix
from .skill_registry import get_skill_registry, Skill

logger = logging.getLogger("groksito.skills.executor")


@dataclass
class SkillInjection:
    """What the main LLM flow needs to do when a skill is active for the turn."""
    skill: Skill
    system_block: str                 # Text to inject as an additional system message (high priority)
    allowed_native: set[str]          # e.g. {"web_search", "x_search"}
    allowed_custom: set[str]          # e.g. future narrow tools like "get_steam_players"
    rationale: str = ""


def prepare_skill_injection(
    *,
    decision_skill_id: str | None,
    user_message: str = "",
) -> SkillInjection | None:
    """
    Main entry point used by the LLM orchestrator.

    Returns None if:
    - No skill was selected by the decision
    - The skill does not exist
    - The skill is not approved (hard requirement)

    The returned SkillInjection tells the caller exactly what to inject
    into the prompt and which tools to keep in the schema list.
    """
    cid_p = cid_prefix()

    if not decision_skill_id:
        return None

    registry = get_skill_registry()
    skill = registry.get(decision_skill_id)

    if not skill:
        logger.debug(f"{cid_p}[SKILLS] Decision wanted skill_id={decision_skill_id} but it does not exist")
        return None

    if not skill.approved:
        logger.info(f"{cid_p}[SKILLS] Decision wanted skill_id={decision_skill_id} but it is NOT approved — refusing to activate")
        return None

    # Build the instruction block. We present it as a high-signal system message
    # so it participates well in prompt caching (stable content per skill).
    # We keep it clearly delineated so the model knows this is "specialized mode".
    instructions = skill.instructions.strip()
    block = (
        f"[SKILL ACTIVE: {skill.name}]\n"
        f"{instructions}\n"
        f"Only use the tools explicitly allowed for this skill. Stay focused on the skill's purpose. "
        f"Be concise and follow the skill instructions exactly."
    )

    # Partition allowed_tools into native vs custom (extensible)
    native = {"web_search", "x_search", "image_search"}  # image_* are flags on web_search
    allowed_native: set[str] = set()
    allowed_custom: set[str] = set()

    for t in (skill.allowed_tools or []):
        t = t.strip().lower()
        if not t:
            continue
        if t in native or t.startswith("web_search") or t.startswith("x_search"):
            if "web" in t:
                allowed_native.add("web_search")
            if "x" in t:
                allowed_native.add("x_search")
        else:
            allowed_custom.add(t)

    inj = SkillInjection(
        skill=skill,
        system_block=block,
        allowed_native=allowed_native,
        allowed_custom=allowed_custom,
        rationale=f"skill:{skill.id}",
    )

    logger.info(f"{cid_p}[SKILLS] Prepared injection for approved skill '{skill.name}' (id={skill.id})")
    return inj


def inject_skill_into_responses_input(
    initial_input: list[dict],
    injection: SkillInjection,
) -> list[dict]:
    """
    Inject the skill's instruction block as a high-priority system message.

    We insert it right after the base SYSTEM_PROMPT so it has strong influence
    but does not fight with dynamic context blocks that come later.
    """
    if not injection or not injection.system_block:
        return initial_input

    # Find a good insertion point: after the very first system message (the main SYSTEM_PROMPT)
    new_input = list(initial_input)
    inserted = False
    for i, msg in enumerate(new_input):
        if msg.get("role") == "system":
            # Insert immediately after the first system message
            new_input.insert(i + 1, {"role": "system", "content": injection.system_block})
            inserted = True
            break

    if not inserted:
        # Fallback: put it as the second message overall
        new_input.insert(1, {"role": "system", "content": injection.system_block})

    return new_input


def filter_native_search_tools(
    native_tools: list[dict],
    injection: SkillInjection | None,
) -> list[dict]:
    """
    When a skill is active, restrict the native search tools offered to exactly
    what the skill declared.

    If injection is None we return the original list unchanged (normal behavior).
    """
    if not injection or not injection.allowed_native:
        return native_tools

    allowed = injection.allowed_native
    filtered: list[dict] = []

    for t in native_tools:
        ttype = t.get("type", "")
        if ttype == "web_search" and "web_search" in allowed:
            filtered.append(t)
        elif ttype == "x_search" and "x_search" in allowed:
            filtered.append(t)
        # Other future native types are passed through only if explicitly allowed

    return filtered


def filter_custom_tools(
    custom_tools: list[dict],
    injection: SkillInjection | None,
) -> list[dict]:
    """
    Restrict custom tools to the intersection of what the skill allows and
    what was going to be offered anyway.

    For the base implementation most skills will only use native web/x.
    This function keeps the door open for future narrow custom tools per skill
    (e.g. a "get_steam_players" tool that a Steam skill could request).
    """
    if not injection or not injection.allowed_custom:
        # No custom restrictions from the skill — return as planned
        return custom_tools

    allowed = injection.allowed_custom
    return [t for t in custom_tools if t.get("name") in allowed]


def should_use_recent_context_from_decision(
    decision_needs_recent: bool,
    is_reply_to_bot: bool,
    is_mentioned: bool,
) -> bool:
    """
    Helper so the main flow can honor the decision layer's opinion about
    recent context without duplicating logic.
    """
    return bool(decision_needs_recent or is_reply_to_bot or is_mentioned)
