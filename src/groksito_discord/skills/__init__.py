"""
Lightweight Skills + Decision system for Groksito (normal chat only).

Design goals (per spec):
- Decision layer runs before final response using a stable, cache-friendly prompt.
- Skills are natural-language instructions + a declared set of allowed tools.
- User approval is mandatory before any skill can be used.
- Recurring needs trigger clear, optional proposals (never auto-activated).
- Execution reuses the existing Responses API + tool loop (no new heavy agent).
- Everything stays small (< ~300 LOC per file typical).

Public surface (import from here for convenience):
- get_skill_registry
- make_decision
- detect_and_propose_skill
- prepare_skill_injection
"""

from __future__ import annotations

from .skill_registry import (
    get_skill_registry,
    SkillRegistry,
    Skill,
)
from .decision import (
    make_decision,
    Decision,
    DecisionAction,
)
from .skill_proposer import (
    detect_and_create_skill,
    detect_and_propose_skill,  # legacy shim (returns None, may side-effect create)
    should_offer_create_skill_tool,  # light trigger for offering the native create_skill tool
    SkillCreationResult,
    SkillProposal,  # legacy
)
from .skill_executor import (
    prepare_skill_injection,
    SkillInjection,
)

__all__ = [
    "get_skill_registry",
    "SkillRegistry",
    "Skill",
    "make_decision",
    "Decision",
    "DecisionAction",
    "detect_and_create_skill",
    "detect_and_propose_skill",  # legacy shim
    "should_offer_create_skill_tool",  # light trigger for offering the native create_skill tool to the model
    "SkillCreationResult",
    "SkillProposal",  # legacy
    "prepare_skill_injection",
    "SkillInjection",
]
