# Implementation Plan: Ticket #9 - Define Target Agentic Architecture for Groksito

**One-sentence goal:** Create a clear, living target architecture document (as a major new section in the existing ARCHITECTURE.md) that defines the desired future agentic state for Groksito, serving as the reference for the migration phases outlined in related tickets #10–#16.

**Ticket requirements (mapped to concrete changes):**
- Produce the document called for in #9: "a clear target architecture document that defines how Groksito should work in the future".
- Focus areas (verbatim): Letting Grok reason more natively; Better use of MCP + Skills; Reducing manual classification and decision heuristics; Keeping Discord constraints in mind (replying in the correct channel, safety, etc.).
- Result must be a *living document* in the repo (use/extend ARCHITECTURE.md which already hosts current arch + phase notes from #5/#7).
- Align with and reference the concrete follow-on tickets that break down the work (#10 reduce pre-filtering, #11 recent context tool-driven, #12 tool descs, #13 expose Discord actions as native tools, #14 deprecate heavy classification/heuristics, #15 observability for tool decisions, #16 cleanup).
- Document must be high-level target (end-state vision + principles + constraints) rather than a detailed phased spec (leave tactics to the phase tickets).
- No code changes required or in scope for this ticket; purely documentation artifact + minor consistency edits to surrounding ARCHITECTURE.md text if helpful for "current vs target" framing.
- Safety: strictly limited to versioned source (src/, tests/, root *.md, tmp/ plan+summary artifacts). Zero touch of data/, oauth/, .env*, secrets/*, runtime state.

**Exploration findings (key):**
- ARCHITECTURE.md already exists as the canonical doc; it has "Overview...", "High-Level Components", "Core Request Flow" (with embedded **Performance notes (Ticket #5)** and **Agentic Phase 1 (Ticket #7)**), "Key Design Principles", etc. Perfect home for the target section (append or insert as forward-looking part).
- Current state (post #7 on this branch): heavy keyword lists + classify_query_context_need (intents.py + context/core.py) still drive tiers (casual/minimal/normal/rich/image_gen) that gate native search + custom tools; decision.py is now almost pure heuristic (prior tiny LLM decision call removed); light decision tools (respond_directly + get_recent_context) offered on plain addressed non-extreme turns via should_offer_light_decision_tools + offer_light_decision_tools path; heavy skill meta tools gated on strong signals; native search offered on normal/rich but model decides usage via SYSTEM_PROMPT + improved tool descs; ultra-minimal SYSTEM_PROMPT; activation/safety boundaries in conversation.py + client.py + response_safety + explicit intent gates + sandbox.
- Skills system is deliberately lightweight/non-agentic (instructions + allowed_tools subset, opt-in via create/use tools or auto-create, injected only when active); sandbox only for power tools when explicitly allowed.
- No AGENTS.md/Claude.md at root (prior plans noted this); branch naming convention feat/ticket-N-...; commits reference tickets; tests via `python -m pytest -q --tb=short` (pyproject.toml); build is setuptools + pip -r requirements.
- Related open issues (from list_issues) exactly decompose the migration: #10–#16. "MCP + Skills" in ticket #9 interpreted in context of the Grok ecosystem (MCP tool interfaces for native reasoning, plus the existing skills/ + skill_tools for reusable behaviors); target should describe how exposing clean, well-described Discord primitives + on-demand tools + MCP-style extensibility lets Grok drive more behavior.
- Safety history from #7 plan/summary: every edit path-verified; only permitted files; relative paths preferred; tmp/ used for plans/summaries; final git porcelain + name-only checks.
- No direct MCP server usage inside the bot today (the MCPs in the session are for the Grok Build TUI: github, docker, notion); for the doc we describe "MCP-style" clean tool interfaces and future potential for the bot to consume/register MCP tools for extended capabilities (e.g., admin, integrations) while Grok reasons natively.
- Current philosophy ("Let Grok be Grok", maximum nativeness, tiered/lazy tools, minimal injection, prompt-cache friendly) is the foundation to evolve from, not replace.

**Ordered implementation steps (high-level):**
1. Update todo tracking; write this plan to tmp/implement-ticket-plan-9.md (persist for auditability, following #7 convention).
2. Before any write/search_replace on any path: explicitly verify against forbidden list (no .env* anywhere, no data/ oauth/ secrets/ credential/ token/ private/ key/ etc., no runtime non-source state). Use relative paths for file ops where possible.
3. Re-read ARCHITECTURE.md (full) + README.md top + key excerpts of current arch flow to ensure the new section integrates stylistically (concise, structured, uses **bold** for emphasis, code blocks sparingly, references existing files/paths).
4. Draft the target section content (high-level, ~self-contained):
   - Header + vision statement tying to Grok web agentic model + this ticket's bullets.
   - Target principles (native reasoning first; tools over heuristics; skills + MCP-style interfaces for consistency/extensibility; safety/activation as non-negotiable thin layer).
   - Target runtime flow (activation/safety gates remain; inside activated turn: Grok sees rich but clean set of well-described native + custom Discord action tools + on-demand context tools + search + skill tools; model decides direct vs tool use vs skill vs recent; classify/keyword heuristics largely deprecated except for hard safety/activation/spam prevention and ultra-light extremes like pure image_gen).
   - Discord constraints preserved (correct channel/thread via client.py Gateway ownership + reply/mention paths in conversation.py; no cross-channel actions without explicit safe design; rate-limit awareness implicit in tool design).
   - Safety preserved/enhanced (response_safety, guild allow-lists, explicit-intent video/gen, sandbox-only for privileged, no auto-offer of power tools, length guards, observability for audit).
   - Skills evolution: more native via tools; better descriptions; MCP + skills synergy (skills as user-defined "agents" with restricted surface; future MCP registration for bot extensibility).
   - Phased realization: brief mapping to #10–#16 (e.g. #11 moves recent to pure tool, #13 exposes reply/send as tools, #14 deprecates classify for decision paths, #16 final cleanup).
   - Observability, metrics, and living-doc maintenance notes.
   - Non-goals / boundaries (do not remove activation heuristics; do not introduce autonomous background agents; keep token/latency discipline).
5. Insert the new section into ARCHITECTURE.md at a logical location (after current "Agentic Phase 1" notes or as a top-level "## Target Agentic Architecture (Roadmap)" after the overview, with forward refs from Core Request Flow and Principles). Keep surrounding text minimal (e.g., one sentence in current flow: "See Target section for the end-state direction.").
6. (Optional minimal) Add a short "Target direction" sentence to the Key Design Principles or Overview if it improves navigability.
7. Write final summary artifact to tmp/grok-ticket9-impl-summary.md (following exact naming/style of prior).
8. Verification steps (detailed below).
9. Git: since current branch is feat/ticket-7-..., create appropriately named branch per convention (e.g. `git checkout -b feat/ticket-9-define-target-agentic-architecture`) if not already clean dedicated; commit with message referencing #9; push; discover MCP create_pull_request tool via search_tool then use with exact schema (owner=lupintic, repo=groksito-discord-bot, title including (#9), body "Implements #9. ...", head=branch, base=main or default, draft=true).
10. If any ambiguity during doc drafting, ask_user_question for scope (but ticket is clear).

**Files expected to be created or modified (with one-line rationale for each):**
- tmp/implement-ticket-plan-9.md — required process artifact (plan).
- ARCHITECTURE.md — primary deliverable: add the living target architecture section (and tiny integration notes); this fulfills the "result in a living document" requirement.
- tmp/grok-ticket9-impl-summary.md — final implementation summary artifact (changes, verification evidence, commands, rationale), following #5/#7 precedent.
- (No src/ or tests/ changes expected or desired for this documentation ticket.)

**Testing/verification approach:**
- `python -m pytest -q --tb=short` (full suite, per pyproject; accept any pre-existing unrelated failures as in prior tickets).
- Targeted: no specific new tests (doc-only); optionally run `python -m pytest -q --tb=short tests/test_classification.py tests/test_tool_selection.py tests/test_skills.py` to confirm doc-unrelated code still healthy.
- `git status --porcelain`; `git diff --name-only`; `git diff --name-only --cached` — must show *only* the three expected files (tmp/plan, ARCHITECTURE.md, tmp/summary); zero forbidden paths.
- Re-run `python -m src.groksito_discord --status` (or equivalent health) to ensure no accidental side effects (should be none).
- Before/after: read_file on ARCHITECTURE.md (relevant sections) to confirm style match (matches current tone: clear, bullet-heavy, references to code paths, safety emphasis).
- Explicit safety scan of final diff: confirm no .env, data/, oauth/, secrets, tokens, runtime state, etc.
- If using subagent for the doc write: full review loop until 0 issues.
- Manual: ensure the target section is actionable as reference (someone reading #10–#16 would understand the north star) and balanced (visionary but grounded in current + Discord realities).

**Risks, follow-ups, scope boundaries:**
- Risk: doc too vague or too prescriptive — mitigate by keeping target high-level (principles + end-state flow + constraints + phase map), not dictating exact code in future phases.
- Risk: "MCP" term is ambiguous in bot context (TUI MCPs vs. tool-calling style) — interpret explicitly in doc as "MCP-style clean, self-describing tool interfaces" + skills + future extensibility; note current implementation uses hybrid custom tools + native xAI tools.
- Scope boundary: This ticket is *only* the definition document. All implementation, deprecation, new tools, etc. are follow-on tickets (#10+). Do not edit code, do not add feature flags, do not change heuristics here.
- Follow-ups: After merge, the open phase tickets become the execution track; update the target doc as phases land (living).
- Maintenance: Add a note that significant future changes to the realized architecture should update both current description and this target section.
- Strictly obey mandatory safety on every step (path verification before write/search_replace; only versioned application + doc + tmp artifacts). If the ticket scope felt like it required code, would have escalated, but it is explicitly "define ... document".
- Branch/PR: use discovered grok_com_github__create_pull_request (search first); draft PR recommended; title/body reference #9 and "Closes #9" or "Implements #9"; include testing note ("Doc-only change; pytest green; safety verified").

**Status:** Plan complete and persisted. Ready to proceed to implementation (direct targeted edit to ARCHITECTURE.md since doc change is low-risk/non-logic, or delegate via implement skill subagent with persona for review if desired for extra rigor). All rules of the implement-ticket skill followed to this point. Use todo_write to advance phases.

(End of plan.)