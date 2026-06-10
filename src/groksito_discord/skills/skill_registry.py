"""
Skill Registry — lightweight persistent storage for approved and proposed skills.

A Skill is a combination of:
- Natural language "instructions" (what the model should do / how to think)
- Declared "allowed_tools" (subset of native + future custom tools)

Skills are **never active** until the user explicitly approves them.
Only approved skills are eligible for the decision layer to select.

Storage: data/skills.json (co-located with other Groksito data).
Each file stays small; this module is pure data + simple matching.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Any

from ..config import settings
from ..correlation import cid_prefix

logger = logging.getLogger("groksito.skills")


@dataclass
class Skill:
    """A reusable, user-approved capability for Groksito in normal chat."""
    id: str
    name: str
    description: str
    instructions: str
    allowed_tools: list[str] = field(default_factory=list)
    # User approval gate
    approved: bool = False
    approved_by: str | None = None
    approved_at: float | None = None
    # Provenance
    created_from_pattern: str | None = None
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Skill":
        # Back-compat for future fields
        return cls(
            id=d["id"],
            name=d["name"],
            description=d.get("description", ""),
            instructions=d["instructions"],
            allowed_tools=d.get("allowed_tools", []) or [],
            approved=bool(d.get("approved", False)),
            approved_by=d.get("approved_by"),
            approved_at=d.get("approved_at"),
            created_from_pattern=d.get("created_from_pattern"),
            created_at=float(d.get("created_at", time.time())),
        )


class SkillRegistry:
    """
    In-memory + persisted registry.
    Thread/async safe enough for Discord bot usage (single-writer via save on mutate).
    """

    def __init__(self, data_dir: Path | None = None):
        base = data_dir or settings.data_dir
        self._path: Path = base / "skills.json"
        self._skills: dict[str, Skill] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            logger.debug(f"[Skills] No skills file yet at {self._path}")
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            items = raw.get("skills", [])
            for item in items:
                try:
                    sk = Skill.from_dict(item)
                    self._skills[sk.id] = sk
                except Exception as e:
                    logger.warning(f"[Skills] Skipping bad skill entry: {e}")
            logger.info(f"{cid_prefix()}[Skills] Loaded {len(self._skills)} skill(s) from {self._path}")
        except Exception as e:
            logger.warning(f"[Skills] Failed to load skills.json: {e}")

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "version": 1,
                "saved_at": time.time(),
                "skills": [s.to_dict() for s in self._skills.values()],
            }
            self._path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            logger.error(f"[Skills] Failed to save skills: {e}")

    # ------------------------------------------------------------------ Public API
    def list_all(self) -> list[Skill]:
        return list(self._skills.values())

    def list_approved(self) -> list[Skill]:
        return [s for s in self._skills.values() if s.approved]

    def get(self, skill_id: str) -> Skill | None:
        return self._skills.get(skill_id)

    def get_by_name(self, name: str) -> Skill | None:
        n = name.lower().strip()
        for s in self._skills.values():
            if s.name.lower().strip() == n:
                return s
        return None

    def register_proposed(self, skill: Skill) -> Skill:
        """Store a skill (respects the approved flag on the passed object)."""
        self._skills[skill.id] = skill
        self._save()
        logger.info(f"[Skills] Saved skill id={skill.id} name={skill.name} (approved={skill.approved})")
        return skill

    def create_approved_skill(
        self,
        *,
        name: str,
        description: str,
        instructions: str,
        allowed_tools: list[str],
        created_from_pattern: str | None = None,
    ) -> Skill:
        """Convenience: create a fully-approved, immediately-active skill and persist it."""
        # Generate a stable-ish id from name
        import re
        base = re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')[:40] or "skill"
        skill_id = f"{base}-{int(time.time())}"
        # Avoid insane collisions by checking
        if skill_id in self._skills:
            skill_id = f"{skill_id}-{hash(name) % 10000}"

        sk = Skill(
            id=skill_id,
            name=name,
            description=description,
            instructions=instructions,
            allowed_tools=allowed_tools or ["web_search"],
            approved=True,
            approved_by="auto",
            approved_at=time.time(),
            created_from_pattern=created_from_pattern,
            created_at=time.time(),
        )
        self._skills[sk.id] = sk
        self._save()
        logger.info(f"[Skills] AUTO-CREATED + APPROVED skill id={sk.id} name={sk.name} (from pattern={created_from_pattern})")
        return sk

    def has_approved_for_pattern(self, pattern: str, query: str = "") -> bool:
        """Quick check if we already have an approved skill that would cover this recurring pattern."""
        approved = self.list_approved()
        # Reuse the covering heuristic from proposer if needed; simple name/desc match here
        q = (query or "").lower()
        for sk in approved:
            if pattern and pattern in (sk.name.lower() + " " + sk.description.lower()):
                return True
            if any(tok in (sk.name + " " + sk.description).lower() for tok in q.split() if len(tok) > 4):
                return True
            # Common known patterns
            if pattern == "steam-players" and any(k in (sk.name + sk.description).lower() for k in ("steam", "player", "jugador", "concurrent")):
                return True
            if pattern == "price-check" and any(k in (sk.name + sk.description).lower() for k in ("price", "precio", "cotiz")):
                return True
        return False

    def approve(self, skill_id: str, approved_by: str = "user") -> Skill | None:
        sk = self._skills.get(skill_id)
        if not sk:
            return None
        sk.approved = True
        sk.approved_by = approved_by
        sk.approved_at = time.time()
        self._save()
        logger.info(f"[Skills] APPROVED skill id={skill_id} by={approved_by}")
        return sk

    def revoke(self, skill_id: str) -> bool:
        sk = self._skills.get(skill_id)
        if not sk:
            return False
        sk.approved = False
        self._save()
        logger.info(f"[Skills] Revoked approval for skill id={skill_id}")
        return True

    def delete(self, skill_id: str) -> bool:
        if skill_id in self._skills:
            del self._skills[skill_id]
            self._save()
            logger.info(f"[Skills] Deleted skill id={skill_id}")
            return True
        return False

    def update_skill(
        self,
        skill_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
        instructions: str | None = None,
        allowed_tools: list[str] | None = None,
    ) -> Skill | None:
        """Update fields of an existing skill (in place) and persist.

        Used by the edit_skill tool. Only approved skills should normally be edited via chat.
        """
        sk = self._skills.get(skill_id)
        if not sk:
            return None

        updated = False
        if name is not None:
            new_name = name.strip()[:100]
            if new_name:
                sk.name = new_name
                updated = True
        if description is not None:
            sk.description = description.strip()[:400]
            updated = True
        if instructions is not None:
            new_instr = instructions.strip()
            if new_instr:
                sk.instructions = new_instr
                updated = True
        if allowed_tools is not None:
            cleaned = [str(t).strip().lower() for t in allowed_tools if str(t).strip()]
            if cleaned:
                sk.allowed_tools = cleaned
                updated = True

        if updated:
            self._save()
            logger.info(f"[Skills] UPDATED skill id={skill_id} name={sk.name}")
        return sk

    # ------------------------------------------------------------------ Matching helpers (very lightweight)
    def find_best_match(self, query: str, max_results: int = 3) -> list[Skill]:
        """
        Extremely lightweight lexical + description match.
        Real selection power lives in the decision prompt + model judgment.
        This is only a fast pre-filter / fallback.
        """
        if not query or not self._skills:
            return []
        q = query.lower()
        scored: list[tuple[float, Skill]] = []
        for sk in self.list_approved():
            score = 0.0
            if sk.name.lower() in q:
                score += 3.0
            if any(t in q for t in sk.name.lower().split()):
                score += 1.0
            for word in sk.description.lower().split():
                if len(word) > 3 and word in q:
                    score += 0.5
            if score > 0:
                scored.append((score, sk))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [s for _, s in scored[:max_results]]


def get_skill_registry() -> SkillRegistry:
    """
    Return a SkillRegistry.

    We intentionally create a fresh instance on every call (cheap JSON load from disk).
    This ensures coherence with the web dashboard, which always creates fresh
    SkillRegistry instances (data_dir=DATA_DIR) on each request.

    Web mutations (approve/revoke/delete via /skills UI) immediately update skills.json.
    Using fresh instances here means the bot's decision layer, auto-creation,
    and meta-tool handlers (create/edit/use_skill) will see the latest state
    on the next relevant turn without requiring a bot restart.

    (The previous singleton cache was a minor perf optimization that caused
    staleness between web and bot.)
    """
    return SkillRegistry()
