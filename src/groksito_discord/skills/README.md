# Groksito Skills + Decision Layer (Lightweight, Auto-Create)

This package implements a **non-agentic**, natural "skills for normal chat" system.

## Core Ideas (updated flow)

- **Decision Layer** runs a tiny, stable, prompt-cache-friendly call *before* the main response (decides "use existing skill?", search, direct, recent context, etc.).
- A **Skill** = natural language instructions + declared allowed tools.
- **Automatic creation** (no proposals): when a clear recurring pattern is detected with conservative criteria (multiple similar requests in a recent time window + strong semantic filters), Groksito creates the skill **directly with `approved=True`**.
- After creation, a short natural confirmation is sent conversationally (e.g. "Listo, creé una skill para consultar jugadores de Steam.").
- No "Do you want me to create...?" messages. Creation is discreet and immediate for the user.
- Execution and the decision-to-*use* path remain unchanged and lightweight.

## Files (all deliberately small)

- `skill_registry.py` — persistence, CRUD, approval gate, fast lexical match.
- `decision.py` — the `DECISION_PROMPT` + `make_decision()` (cheap cached call + heuristic fallback).
- `skill_proposer.py` — recurring pattern detection (fingerprints + counts) + nice proposal generation.
- `skill_executor.py` — turns a Decision into prompt injection + tool filtering for the normal flow.
- Integrated in `llm.py` (orchestrator) and lightly configured via `config.py`.

## How it stays lightweight & natural

- Decision prompt is tiny/fixed → excellent prompt cache behavior.
- Auto-creation only triggers on strong, recent, repeated signals (default 3× in 48h window; 1× for explicit "create a skill for..." on strong lookup patterns like steam player counts) and only for "meta" tasks useful to Groksito (player counts, prices, live results, X pulse...). Explicit creation requests now work immediately for data lookups.
- When a skill is used, we inject one extra system message with its instructions and restrict tool schemas. Everything else (typing, direct delivery, tool loop, caching) reuses the normal path.
- Confirmation messages are short and natural, sent as a low-key follow-up after the main answer.
- Zero agent loops. No background autonomy.

## Automatic Creation Flow (conservative)

1. User repeatedly asks the same *kind* of thing in a short period (e.g. player counts for several different games over a couple of days).
2. The proposer scans recent history (with timestamps) and applies:
   - Time window filter (default last 48 hours).
   - Raised threshold (default 5 occurrences of the same fingerprint).
   - Strong semantic exclusion for game-internal "skills / builds / habilidades / pasivas / skill tree" language (especially around PoE, Diablo, etc.).
3. If clean recurring pattern (or explicit "crea una skill para..." / "create a skill for steam charts...") + no existing approved skill covers it → `registry.create_approved_skill(...)` is called (approved=True, approved_by="auto"). Strong patterns + creation phrasing now succeed at low/1 count.
4. Main answer is sent normally.
5. A short confirmation is sent ~1s later: "Listo, creé una skill para consultar jugadores de Steam."
6. From that point on, the decision layer can select the skill on matching turns and the instructions + restricted tools are injected. Future identical data queries will be faster and more consistent.

One-off questions, casual chat, and anything that smells like "la skill de mi personaje" are explicitly ignored for creation.

## Adding a New Built-in Skill Pattern

Edit the `_FINGERPRINT_RULES` + `_base_proposal_for_fingerprint` in `skill_proposer.py`. Keep it small.

For skills that would benefit from a narrow custom tool (e.g. a first-class `get_steam_players`), register the tool in the normal `tools.py` / `execute_hybrid_tool` path and list its name in `allowed_tools`. The executor already partitions native vs custom.

## Prompt Caching Notes

- `DECISION_PROMPT` is the stable prefix for the decision call (uses the same per-user `prompt_cache_key`).
- Skill instruction blocks are injected as separate system messages. Once a user has a stable set of approved skills, those blocks are also highly cacheable.
- The main ultra-minimal `SYSTEM_PROMPT` remains untouched.

## Example Decision Output (what the model is asked to emit)

```json
{
  "action": "use_skill",
  "needs_recent_context": false,
  "needs_search": "web",
  "use_skill": "steam-player-counts",
  "propose_skill": null,
  "rationale": "User asked for concurrent players on another game; matches approved Steam skill."
}
```

(Note: The decision layer is now primarily heuristic-driven with strong local pre-filters for direct/search/recent_context/use_skill. Older proposal-style output examples are retained only for historical context; the active paths use native `create_skill`/`edit_skill`/`use_skill` tool calling or conservative auto-creation with no "do you want me to...?" user proposal dance.)

## Disabling

Set in env / .env:

```
ENABLE_SKILL_DECISION_LAYER=false
ENABLE_SKILL_AUTO_CREATION=false
```

(ENABLE_SKILL_PROPOSALS and older proposal-min-occurrence settings are legacy and no longer drive the main flows.)

Or via the Pydantic settings at runtime.

This system deliberately stays in the spirit of the post-2026 "maximum nativeness" Groksito: efficient, optional, user-controlled, and never in the way of just talking to Grok.
