"""
Skill meta / decision tools for Groksito.

This module contains the tools and schemas that allow the model (via native tool calling)
to manage and activate lightweight approved skills:

- create_skill / edit_skill / use_skill / respond_directly / get_recent_context
- Their (very large, prescriptive) schemas
- The automatic pre-save testing harness (_test_skill_proposal + _derive_test_query)
- Helpers for injecting skill-specific custom tool schemas (code_execution, playwright_browser)

These are only offered on relevant turns (via offer_* flags from llm.py) and are strictly
gated behind user-approved skills (or conservative auto-creation).

Kept separate from core tools.py to reduce the size and cognitive load of the main
hybrid tool dispatcher and tiered schema selection.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from ..utils.correlation import cid_prefix

logger = logging.getLogger("groksito.tools")


# =============================================================================
# Skill Meta Tool Schemas (large prescriptive descriptions)
# =============================================================================

def _create_skill_schema() -> dict:
    """Internal tool schema for model-driven skill creation via native tool calling.

    Offered only on a small number of relevant turns (explicit "create a skill" language
    or strong candidate patterns for reusable capabilities). The model then decides
    via its own reasoning whether (and how) to call it. This is the primary new path
    for skill creation.
    """
    return {
        "type": "function",
        "name": "create_skill",
        "description": (
            "Create a new reusable skill for Groksito that bundles highly detailed, prescriptive instructions together with a minimal curated set of allowed tools. "
            "Once approved, matching future queries can be handled with greater consistency, lower latency, and better specialization than generic reasoning.\n\n"
            "CRITICAL INSTRUCTIONS FOR THE 'instructions' PARAMETER (this is the most important part of the skill and determines whether it will actually be useful later):\n"
            "- The instructions you write will be injected as a high-priority system message. They must be extremely DETAILED, STEP-BY-STEP, and PRESCRIPTIVE.\n"
            "- Use strong directive language: ALWAYS, NEVER, EXACTLY, MUST, ONLY, FORMAT AS, STEP 1:, etc.\n"
            "- Be very specific about data sources, exact tool calls/queries to use, how to resolve names/entities, what to do on missing data, and the precise output format expected (Discord-friendly: short, clean, one line per item, use **bold** or simple lists when it helps readability).\n"
            "- Capture user intent: If the user mentions specific games, items, topics, or examples while asking you to create the skill (e.g. \"for Path of Exile 2, Black Desert, Lost Ark, Throne and Liberty\"), you MUST hardcode or explicitly prioritize those examples in the instructions (e.g. \"Always include at minimum the games the user originally mentioned: Path of Exile 2, Black Desert... plus any additional games the user asks about in the future\").\n"
            "- Make the instructions self-contained so that a future version of you can follow them perfectly without needing to remember the original conversation.\n\n"
            "SENIOR ENGINEER MINDSET — Think autonomously and strategically like a senior software engineer designing a production-grade feature (not just following the prompt literally):\n"
            "Before outputting the create_skill call, perform real analysis:\n"
            "1. **Understand the root need deeply**: What exact data does the user want? How volatile is the source (JS-rendered, anti-bot, frequently changing layout)? How important is accuracy, freshness, and consistency over time? What are the failure modes of naive approaches?\n"
            "2. **Evaluate technical approaches rigorously** (consider tradeoffs in reliability, cost, maintainability):\n"
            "   - Is a simple prompt + web_search reliable enough, or will it produce fragile/inaccurate results on this source?\n"
            "   - Would a more robust architecture be superior long-term: e.g., using playwright_browser to get rendered content + code_execution to run a dedicated, reusable Python extraction/parsing/normalization script?\n"
            "   - Should the skill generate and persist a well-written extraction script (included in the instructions or as executable logic) so that future uses invoke deterministic code rather than repeated LLM extraction (which can hallucinate or vary)?\n"
            "   - Is combining tools or creating a small custom script the right call for accuracy and to avoid token waste on repeated similar queries?\n"
            "3. **Prefer reusable, persistent logic over fragile per-call prompting** when the task involves repeated, precise data extraction from complex/volatile sources. Generating a script that can be executed via code_execution often leads to dramatically better, more consistent results than tweaking prompts endlessly.\n"
            "4. **Diagnose before editing (for future calls too)**: If this is an iteration, first explicitly diagnose *why* previous versions failed (e.g. 'web_search returns stale/incomplete data because the page is heavily JS-rendered and Cloudflare-protected') rather than making superficial wording changes.\n"
            "5. Only propose/create a skill version if it represents a genuine improvement in architecture, tool choice, reliability, or maintainability. Avoid blind prompt-tweaking.\n"
            "Output your autonomous reasoning using the optional 'reasoning_notes' and 'proposed_architecture' parameters so the system (and user) can see the engineering thought process.\n\n"
            "TOOL SELECTION (the 'allowed_tools' you choose are critical — reason carefully as an engineer):\n"
            "You are responsible for deciding the optimal set of tools for this skill. The model (you) must analyze the user's need and pick from the available powerful tools:\n"
            "- \"web_search\": Good for general, fast, cached-ish web results and simple facts. Use when broad search + extraction is sufficient and reliable.\n"
            "- \"code_execution\": Use for post-processing data, calculations, parsing, cleaning lists, math, JSON handling, or running small deterministic scripts (including reusable extraction scripts you generate) on results from other tools. Runs in isolated Docker sandbox.\n"
            "- \"playwright_browser\": Use for JS-heavy sites, dynamic content, precise DOM interaction, scraping pages that block simple scrapers, forms, or when you need to 'render' the page and execute JS to get accurate live data. Runs in sandboxed browser context.\n"
            "You may (and often should) combine them (e.g. [\"playwright_browser\", \"code_execution\"] to fetch rendered data then process it with a custom script). Think like an engineer: Does this need rendered browser content or complex interaction? → playwright. Does this need heavy data transformation, normalization, or a reusable script? → code_execution. Simple public data? → web_search.\n"
            "Never include a tool 'just in case' — be minimal and purposeful. The skill will only be allowed to call the tools you list here.\n\n"
            "SUPPORT FOR GENERATING SCRIPTS: When a reusable Python script (for extraction, parsing, etc.) would be the superior long-term solution, include the full, well-commented script in the 'instructions' (or as part of the design). The skill can later extract and execute it via code_execution tool. This is often much more reliable and efficient than asking the LLM to re-extract data every time.\n\n"
            "AUTOMATIC TESTING: After you provide the skill definition in this call, the system will automatically test it with a representative query (derived from your 'reason' or the optional 'test_query' you can provide). It will simulate execution using the tools you declared and evaluate quality (accuracy, format, reliability, no hallucinations). \n"
            "- If the test passes (high score), the skill is saved and activated immediately.\n"
            "- If the test passes (high score or eval 'passed'), the skill is saved with approved=True and is immediately active. If the auto-approval bar is not met, the skill is STILL created and saved as Pending (approved=False) so it shows up in the /skills dashboard for manual approval/review. You get the test feedback and can iterate with another create_skill call or tell the user to approve it from the web UI. The outer tool-calling loop naturally limits this to a small number of iterations (usually 1-2).\n"
            "This keeps a quality bar for automatic activation while never losing a user-requested creation (it lands in Pending for review if needed).\n\n"
            "EXAMPLE OF HIGH-QUALITY INSTRUCTIONS for a Steam player counts skill (use this style and level of detail, and consider script generation if appropriate):\n"
            "'You are now a specialized Steam Charts expert. ... [same detailed example as before, plus note that a reusable parsing script could be embedded and invoked via code_execution for even higher consistency].'\n\n"
            "You (the model) are fully responsible for writing the highest-quality, most specific and prescriptive instructions possible, and for making autonomous engineering decisions. Vague instructions or superficial changes will make the skill useless later. Act like a thoughtful senior engineer, not a literal prompt follower."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Short descriptive name for the new skill (e.g. 'Steam Player Counts', 'Dólar Blue Quick Lookup')."
                },
                "reason": {
                    "type": "string",
                    "description": "Brief reason why creating this reusable skill is valuable (1-2 sentences)."
                },
                "instructions": {
                    "type": "string",
                    "description": "The MOST IMPORTANT field. Write EXTREMELY DETAILED, STEP-BY-STEP, PRESCRIPTIVE instructions (using ALWAYS/NEVER/EXACTLY/MUST/FORMAT AS etc.) that will be injected verbatim as a system prompt. Include exact data fetching steps, precise search queries, output format for Discord, edge case handling, and any specific games/items the user mentioned when requesting the skill. Make it so detailed that the model can follow it perfectly on future turns without ambiguity."
                },
                "allowed_tools": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of tool names the skill is allowed to call. Choose intelligently: 'web_search' for general search, 'code_execution' for processing/calculations (sandboxed Docker), 'playwright_browser' for JS-heavy or interactive sites (sandboxed browser). Combine when needed. Be minimal."
                },
                "test_query": {
                    "type": "string",
                    "description": "Optional: a specific representative query to use for the automatic post-creation test. If omitted, one is derived from the 'reason' (e.g. a Steam players query for a charts skill)."
                },
                "reasoning_notes": {
                    "type": "string",
                    "description": "Optional but strongly encouraged: your autonomous senior-engineer analysis and reasoning (diagnosis of the problem, approaches considered, why you chose this architecture/tool combination/script generation, tradeoffs, why this is a genuine improvement). This helps the system and user understand the thought process."
                },
                "proposed_architecture": {
                    "type": "string",
                    "description": "Optional but encouraged for complex skills: high-level description of the chosen architecture (e.g. 'playwright_browser to get rendered HTML from JS-heavy Steam pages + code_execution with a custom reusable extraction + normalization script for consistent output')."
                }
            },
            "required": ["name", "reason", "instructions", "allowed_tools"]
        }
    }


def _get_recent_context_schema() -> dict:
    """Schema for the model to explicitly request recent conversation summary via tool calling.

    This is one of the core internal decision tools (light set). Offered on plain addressed turns.
    The model calls it when it needs summarized prior chat for coherence or to answer references.
    """
    return {
        "type": "function",
        "name": "get_recent_context",
        "description": (
            "Fetches a compact, targeted on-demand summary of recent messages in the current Discord channel. "
            "Helpful when the user's question refers to or continues prior discussion, shared context, or earlier turns in the thread, and a synthesized overview would improve coherence or accuracy beyond what the immediate message buffer provides. "
            "After receiving the summary you can reason further or call respond_directly (or other tools) to deliver the final answer. The summary is produced only when explicitly requested."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "max_messages": {"type": "integer", "description": "Optional: how many recent messages to base the summary on (default ~12-15)."}
            }
        }
    }


def _use_skill_schema() -> dict:
    """Schema for the model to explicitly select and activate an existing approved skill.

    Part of the internal decision tool set. This unifies skill usage under the model's tool-calling
    reasoning.
    """
    return {
        "type": "function",
        "name": "use_skill",
        "description": (
            "Activate one of the existing approved skills for the current user query. "
            "When successful, the tool result provides a [SKILL ACTIVE: ...] block containing the skill's precise instructions and allowed tools. Follow those instructions exactly in the final response, using only the declared tools and matching the skill's intended style and output contract. "
            "Best when the current request closely matches the documented purpose of an approved, reusable skill (for consistency, specialized behavior, or efficiency on recurring patterns). Provide the skill name or id."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "skill": {
                    "type": "string",
                    "description": "Name or identifier of the approved skill to activate (e.g. 'Steam Player Counts' or the id)."
                }
            },
            "required": ["skill"]
        }
    }


def _edit_skill_schema() -> dict:
    """Internal tool for editing an existing approved skill (primarily its instructions).

    Use this (instead of create_skill) when the user wants to improve, update, or change
    the behavior of a skill that already exists. This prevents duplicate skills with the
    same name/purpose.
    """
    return {
        "type": "function",
        "name": "edit_skill",
        "description": (
            "Edit an existing approved skill (most commonly to improve or update its instructions, name, or allowed tools). "
            "Identify the skill by name or id. Provide the new instructions (and optionally new name or allowed tools). Editing keeps a single canonical version instead of proliferating duplicates via create_skill.\n\n"
            "SENIOR ENGINEER MINDSET FOR EDITS — Act autonomously like a senior engineer performing a code review and refactor (not literal prompt following):\n"
            "1. **Diagnose first**: Before proposing any change, analyze *why* the current skill is insufficient or failing (e.g. 'web_search returns incomplete/stale data because the target is JS-heavy and frequently blocked — a simple prompt tweak won't fix the root architectural issue').\n"
            "2. **Evaluate if a deeper change is warranted**: Is this a case for better instructions only, or does it require changing the allowed_tools (switching to or adding playwright_browser/code_execution), or generating a reusable extraction script that lives in the instructions and gets executed via code_execution for reliability?\n"
            "3. **Prefer architectural improvements**: Only edit if the change meaningfully improves reliability, maintainability, accuracy, or efficiency (better tool choice, introduction of a persistent script, stricter output contracts, etc.). Avoid superficial wording tweaks.\n"
            "4. **Long-term thinking**: Consider whether the edit makes the skill more robust over time rather than patching symptoms.\n"
            "Use 'reasoning_notes' to document your diagnosis and why the proposed edit is a real improvement.\n\n"
            "When editing, you can also revise the allowed_tools list (via the optional new_allowed_tools parameter) if the user wants the skill to gain or lose capabilities (e.g. add playwright_browser for more reliable scraping, or code_execution for post-processing / running a dedicated script). Reason about the best tools and architecture just like in create_skill. Support generating or improving scripts when they would make the skill superior.\n\n"
            "AUTOMATIC TESTING: The proposed new version will be tested with a representative query before the edit is applied. If the test fails, the edit is NOT committed and you receive detailed feedback + suggestion (including architecture/tool advice). Diagnose root cause, improve, and call edit_skill again. This prevents saving broken versions."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "skill": {
                    "type": "string",
                    "description": "Name or id of the existing approved skill to edit (e.g. 'Steam Player Counts' or 'steam-player-counts-...')."
                },
                "new_instructions": {
                    "type": "string",
                    "description": "The new, improved/prescriptive instructions for the skill. Follow the same high-quality guidelines as create_skill (detailed steps, ALWAYS/NEVER rules, exact output formats, capture user intent). Also reconsider tool choice (web_search / code_execution / playwright_browser) if the user's request for improvement implies different capabilities are now needed."
                },
                "new_name": {
                    "type": "string",
                    "description": "Optional: new name for the skill if the user wants to rename it."
                },
                "new_allowed_tools": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional: revised list of allowed tools. The model should reason whether the existing set is still optimal or if e.g. playwright_browser or code_execution should be added/removed based on the improvement request."
                },
                "reason": {
                    "type": "string",
                    "description": "Optional: short reason for the edit (for logging/confirmation)."
                },
                "test_query": {
                    "type": "string",
                    "description": "Optional: specific test query for the automatic validation of this edit. If omitted, derived from the reason or skill name."
                },
                "reasoning_notes": {
                    "type": "string",
                    "description": "Optional but strongly encouraged for real improvements: your autonomous diagnosis of why the current skill is insufficient (root cause analysis) + why this edit represents a genuine architectural/tool/script improvement rather than a superficial change."
                },
                "proposed_architecture": {
                    "type": "string",
                    "description": "Optional: if the edit involves a better architecture or script, describe it here (e.g. 'Added playwright_browser + embedded reusable parsing script executed via code_execution')."
                }
            },
            "required": ["skill", "new_instructions"]
        }
    }


def _respond_directly_schema() -> dict:
    """Optional tool allowing the model to explicitly decide to answer directly.

    Included in the decision tool set (light or full) on addressed turns. Lets the model cleanly signal after
    considering other tools (context, skills, searches) that it will now give the final response.
    """
    return {
        "type": "function",
        "name": "respond_directly",
        "description": (
            "Explicit signal that you will now produce the final, user-facing reply using your built-in knowledge, the provided conversation context, referenced messages, and any information already gathered from prior tool calls. "
            "A strong choice for definitional, explanatory, historical, mathematical, coding, or general-knowledge questions where external freshness is unlikely to add value. "
            "Also appropriate after evaluating other options (recent context, skills, or searches) and determining that direct response is best. "
            "Calling this ends the current tool-calling phase for the turn and delivers the complete answer."
        ),
        "parameters": {
            "type": "object",
            "properties": {}
        }
    }


def _code_execution_schema() -> dict:
    """Schema for secure Python code execution, intended to be used *only* by skills that explicitly declare 'code_execution' in their allowed_tools.

    Security: This tool MUST run inside a Docker container with strict resource limits (CPU, memory, time), no or very limited network access, and a minimal filesystem. 
    The execution environment should have no access to host secrets, the bot's .env, or persistent state unless explicitly provided via args.
    Use for data processing, calculations, parsing, simple scraping logic, or when web_search/playwright results need post-processing.
    Never use for arbitrary user-provided code outside of a skill's controlled instructions.
    """
    return {
        "type": "function",
        "name": "code_execution",
        "description": (
            "Execute a snippet of Python code in an isolated, sandboxed Docker environment with tight limits on CPU, memory, runtime, and network. "
            "The code has access only to a very small set of safe stdlib modules (math, json, re, datetime, etc.) plus any explicitly allowed by the skill. "
            "No file system writes outside /tmp (ephemeral), no access to the bot's internal state or host. "
            "Well suited for cleaning/calculating/post-processing data returned by search or browser tools, simple parsing, math/stats, list/dict transforms, or executing small deterministic extraction/normalization scripts generated as part of a skill. "
            "The containing skill's instructions must explicitly authorize the patterns used. "
            "Returns stdout, stderr, and any explicit return value or last expression."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "The Python code to execute. Must be self-contained. Use print() for output you want returned. Keep it short and focused."
                },
                "timeout_seconds": {
                    "type": "number",
                    "description": "Optional hard timeout (default 10-30s depending on skill policy)."
                }
            },
            "required": ["code"]
        }
    }


def _playwright_browser_schema() -> dict:
    """Schema for Playwright browser automation tool, only offered/usable when a skill explicitly lists 'playwright_browser' in allowed_tools.

    Security: Runs in a completely isolated browser context (fresh profile, no cookies from previous calls unless the skill explicitly manages state via returned data). 
    Should be executed in a Docker container with --no-sandbox or using a dedicated playwright service/image with resource limits and no access to host network/files except through controlled APIs.
    Use when web_search is insufficient (JS-rendered content, login flows, complex interactions, precise DOM extraction, screenshots for verification, or sites that block simple scrapers).
    The skill instructions must describe the exact navigation + extraction strategy.
    Do not use for general web search; prefer web_search for that.
    """
    return {
        "type": "function",
        "name": "playwright_browser",
        "description": (
            "Control a headless browser (Playwright) to visit pages, wait for JS, click, type, scroll, extract text/HTML/attributes, take screenshots, or perform other browser actions. "
            "Each call starts with a clean browser context (or a context provided by previous calls in the same skill activation if state is passed back). "
            "Best when web_search is insufficient due to heavy JavaScript rendering, dynamic/SPA content, anti-scraper protections, need for login/cookie state (managed by the skill), complex DOM interactions, or visual verification via screenshot. "
            "Parameters are flexible: you can use natural language 'instructions' for common tasks or low-level actions. "
            "Always respect robots.txt / terms in the skill logic. Rate limit yourself. For broad factual lookup, web_search is generally the lighter first choice."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The starting URL to navigate to (required for most actions)."
                },
                "action": {
                    "type": "string",
                    "description": "High-level action: 'navigate', 'extract_text', 'extract_html', 'click', 'type', 'scroll', 'screenshot', 'wait_for_selector', or 'custom'."
                },
                "instructions": {
                    "type": "string",
                    "description": "Natural language description of what to accomplish on the page (e.g. 'Go to the Steam page for Path of Exile 2 and extract the current players number and peaks'). The browser agent will interpret this. For broad information retrieval prefer web_search; use browser actions when rendering, interaction, or precise extraction from JS-heavy pages is required."
                },
                "selector": {
                    "type": "string",
                    "description": "CSS/XPath selector for precise actions or extraction (optional)."
                },
                "timeout_ms": {
                    "type": "integer",
                    "description": "Timeout for actions (default 30000)."
                }
            },
            "required": ["url", "action"]
        }
    }


# =============================================================================
# Skill Custom Schema Helpers (used by augmentation when a skill declares extra tools)
# =============================================================================

def get_skill_specific_custom_schemas(allowed_custom: set[str] | list[str] | None) -> list[dict]:
    """Return full tool schemas for any known skill-declared custom tools.

    This allows skills to 'bring their own' powerful tools (code_execution, playwright_browser, future connectors)
    without them being offered in normal chat. The schemas are only injected into the Responses API call
    when a skill that declares them is active.

    MCP tool schema snapshots in docs/reference/mcp-tool-schemas/ are reference-only and are not loaded here.
    See docs/mcp-integration.md for the current power-tool architecture and future MCP path.
    """
    if not allowed_custom:
        return []
    allowed = set(allowed_custom)
    schemas = []
    known = {
        "code_execution": _code_execution_schema,
        "playwright_browser": _playwright_browser_schema,
        # Add future power tools here (explicit schema + sandbox/handler). See docs/mcp-integration.md.
    }
    for name in allowed:
        if name in known:
            try:
                schemas.append(known[name]())
            except Exception:
                pass
    return schemas


def augment_custom_tools_with_skill_customs(
    custom_tools: list[dict], allowed_custom: set[str] | list[str] | None
) -> list[dict]:
    """Merge base custom tools with any additional schemas required by the active skill's allowed_custom list.

    Called from llm.py when a skill is active. Only schemas for tools the skill actually declared
    are added, and only if we have a definition for them.
    """
    if not allowed_custom:
        return list(custom_tools)
    extra = get_skill_specific_custom_schemas(allowed_custom)
    if not extra:
        return list(custom_tools)
    existing_names = {t.get("name") for t in custom_tools if isinstance(t, dict) and t.get("name")}
    result = list(custom_tools)
    for sch in extra:
        nm = sch.get("name")
        if nm and nm not in existing_names:
            result.append(sch)
    return result


# =============================================================================
# Automatic Testing Harness for Skill Proposals (used by create/edit handlers)
# =============================================================================

async def _test_skill_proposal(
    instructions: str,
    allowed_tools: list[str],
    test_query: str,
    reason: str = "",
) -> dict:
    """Lightweight test + evaluation of a proposed skill before saving.

    Executes (simulates) the skill on a test query using the declared tools (via a controlled LLM call),
    then evaluates the quality. Returns dict with score, passed, test_output, feedback, issues, suggestion.

    This is kept lightweight: one execution call + one eval call, no full multi-round tool loop for the test itself.
    The main model drives any iterations by re-calling create/edit with refinements based on this feedback.
    """
    try:
        from openai import AsyncOpenAI
        from ..config import settings as _settings

        bearer = None
        try:
            from ..core.grok_oauth import get_grok_bearer as _get_grok_bearer
            if _get_grok_bearer:
                bearer = _get_grok_bearer()
        except Exception:
            pass
        if not bearer:
            bearer = getattr(_settings, "xai_api_key", None)
        if not bearer:
            return {
                "score": 6,
                "passed": True,
                "test_output": "[test skipped: no credentials]",
                "feedback": "Automatic test skipped (no API key). Skill will be created (auto-approved in this case).",
                "issues": [],
                "suggestion": "",
            }

        client = AsyncOpenAI(
            api_key=bearer,
            base_url="https://api.x.ai/v1",
            timeout=getattr(_settings, "api_timeout_seconds", 30.0),
        )
        model = getattr(_settings, "grok_model", "grok-4.3")

        # Build a simulation prompt for "executing" the skill
        skill_block = f"[SKILL ACTIVE: Proposed]\n{instructions}\nOnly use the tools explicitly allowed for this skill: {allowed_tools}. Be concise and follow the skill instructions exactly."
        exec_prompt = f"{skill_block}\n\nTest query: {test_query}\n\nProvide the exact response the skill would give to the user (simulate any tool use internally and give the final clean answer)."

        # Execution simulation (single shot for lightness; model "uses" the declared tools in its reasoning)
        exec_resp = await client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": exec_prompt}],
            max_tokens=800,
            temperature=0.2,
        )
        test_output = ""
        if exec_resp.choices and exec_resp.choices[0].message and exec_resp.choices[0].message.content:
            test_output = exec_resp.choices[0].message.content.strip()

        # Evaluation
        eval_prompt = (
            f"Test query: {test_query}\n\n"
            f"Proposed skill instructions (summary): {instructions[:600]}...\n"
            f"Allowed tools: {allowed_tools}\n\n"
            f"Simulated skill output:\n{test_output[:1000]}\n\n"
            "Evaluate the quality of this skill output for the test query. Consider:\n"
            "- Accuracy and relevance (correct data/games, no hallucinations)\n"
            "- Format (clean, Discord-friendly, matches any specified structure)\n"
            "- Completeness and consistency\n"
            "- Whether the allowed tools seem appropriate (e.g. if data looks unreliable, suggest playwright_browser or code_execution)\n\n"
            "Output ONLY valid JSON with no extra text:\n"
            '{ "score": 0-10, "passed": true/false (true if score >= 7 or minor issues only), "issues": ["short list of problems"], "feedback": "one sentence summary", "suggestion": "specific advice like \"Switch to playwright_browser for better JS data\" or \"Make output format stricter with one line per game\" }'
        )

        eval_resp = await client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": eval_prompt}],
            max_tokens=300,
            temperature=0.1,
        )
        eval_text = ""
        if eval_resp.choices and eval_resp.choices[0].message and eval_resp.choices[0].message.content:
            eval_text = eval_resp.choices[0].message.content.strip()

        # Parse JSON
        import json
        import re
        json_match = re.search(r"\{[\s\S]*\}", eval_text)
        if json_match:
            eval_data = json.loads(json_match.group(0))
        else:
            eval_data = {"score": 5, "passed": False, "issues": ["Could not parse eval"], "feedback": eval_text[:200], "suggestion": "Refine instructions for clarity."}

        eval_data.setdefault("test_output", test_output)
        eval_data.setdefault("score", 5)
        eval_data.setdefault("passed", eval_data.get("score", 5) >= 7)
        eval_data.setdefault("issues", [])
        eval_data.setdefault("feedback", "")
        eval_data.setdefault("suggestion", "")

        return eval_data

    except Exception as e:
        logger.exception(f"{cid_prefix()}Error during skill proposal test")
        return {
            "score": 5,
            "passed": False,
            "test_output": "",
            "feedback": f"Test execution failed: {str(e)[:150]}",
            "issues": [str(e)],
            "suggestion": "Save as-is or refine the proposal and try again.",
        }


def _derive_test_query(reason: str, name: str, instructions: str = "") -> str:
    """Cheap derivation of a representative test query from the skill creation reason."""
    r = (reason or "").lower()
    if any(k in r for k in ("jugadores", "players", "steam", "poe", "black desert", "chart")):
        return "Dame los jugadores actuales y picos en Steam para Path of Exile 2, Black Desert, Lost Ark y Throne and Liberty."
    if any(k in r for k in ("precio", "price", "dólar", "cotiz")):
        return "Cuál es el precio del dólar blue hoy y el oficial."
    if any(k in r for k in ("crypto", "btc", "eth", "bitcoin")):
        return "Precio actual de Bitcoin y Ethereum."
    return f"Test representative query for skill '{name}': {reason[:120]}"


# =============================================================================
# Handler Functions (extracted from execute_hybrid_tool for cleanliness)
# =============================================================================

async def handle_create_skill(args: dict[str, Any], original_message: Any = None) -> str:
    """Handler for the create_skill meta tool."""
    try:
        from ..skills.skill_registry import get_skill_registry
        reg = get_skill_registry()

        sk_name = str(args.get("name", "Custom Skill")).strip()[:100] or "Custom Skill"
        sk_reason = str(args.get("reason", "User or model identified recurring need.")).strip()[:400]
        sk_instructions = str(args.get("instructions", "Handle matching queries efficiently and consistently.")).strip()
        raw_allowed = args.get("allowed_tools") or ["web_search"]
        if isinstance(raw_allowed, (str, bytes)):
            raw_allowed = [raw_allowed]
        sk_allowed = [str(t).strip().lower() for t in raw_allowed if str(t).strip()]

        # Capture autonomous engineering reasoning (new params for better skills)
        reasoning_notes = args.get("reasoning_notes", "") or args.get("analysis", "")
        proposed_architecture = args.get("proposed_architecture", "") or args.get("architecture", "")

        # Enhance description with architecture notes if provided (for future reference / web UI)
        enhanced_description = sk_reason
        if proposed_architecture:
            enhanced_description = f"{sk_reason} | Architecture chosen: {proposed_architecture}"
        if reasoning_notes:
            enhanced_description = f"{enhanced_description} | Engineer notes: {reasoning_notes[:300]}"

        # Derive or use provided test query
        provided_test = args.get("test_query")
        test_query = provided_test or _derive_test_query(sk_reason, sk_name, sk_instructions)

        # === Lightweight test + eval before saving ===
        test_eval = await _test_skill_proposal(sk_instructions, sk_allowed, test_query, sk_reason)

        test_passed = test_eval.get("score", 0) >= 7 or test_eval.get("passed", False)
        score_val = test_eval.get("score", 0)

        # Always persist the skill from a create_skill call.
        # - If test passed: create approved (immediately usable).
        # - If test failed: still create it (as Pending / approved=False) so it appears in /skills
        #   for manual approval. This prevents the "model claims created but nothing is there"
        #   problem while keeping the test as a gate for *auto-approval*.
        created = reg.create_approved_skill(
            name=sk_name,
            description=enhanced_description,
            instructions=sk_instructions,
            allowed_tools=sk_allowed or ["web_search"],
            created_from_pattern="tool-call",
        )
        if not test_passed:
            reg.revoke(created.id)

        # Send a natural confirmation to the channel (success vs pending)
        if original_message:
            if test_passed:
                conf_text = f"Listo, creé la skill '{created.name}'."
                if proposed_architecture:
                    conf_text += f" (Arquitectura: {proposed_architecture[:80]}...)"
                if score_val:
                    conf_text += f" (Test score: {score_val}/10 — pasó las pruebas automáticas.)"
                else:
                    conf_text += " A partir de ahora la usaré automáticamente para consultas similares."
                log_note = "tested, auto-approved"
            else:
                conf_text = f"Guardé la propuesta de skill '{created.name}' como pendiente (test score {score_val}/10). Puedes aprobarla desde /skills o pedirme que la mejore y vuelva a intentar el create_skill."
                log_note = "tested, saved as pending"
            async def _send_conf(msg=original_message, text=conf_text):
                try:
                    await asyncio.sleep(0.9)
                    await msg.channel.send(text)
                    logger.info(f"{cid_prefix()}[SKILLS] create_skill tool confirmation sent for '{created.name}' ({log_note})")
                except Exception as send_err:
                    logger.debug(f"{cid_prefix()}Could not send create_skill confirmation: {send_err}")
            try:
                asyncio.create_task(_send_conf())
            except Exception:
                pass

        if test_passed:
            return f"Skill created and tested successfully (score {score_val or 'N/A'}). '{created.name}' (id={created.id}) is now approved and immediately usable."
        else:
            feedback = test_eval.get("feedback", "Test failed.")
            issues = test_eval.get("issues", [])
            suggestion = test_eval.get("suggestion", "Refine instructions or switch tools (e.g. add playwright_browser for more reliable data).")
            return (
                f"Test of proposed skill scored {score_val}/10 (auto-approval criteria not met). "
                f"Issues: {issues}. Feedback: {feedback}. Suggestion: {suggestion} "
                f"Test query: '{test_query}'. The skill was saved as Pending (approved=False) and is visible in the /skills dashboard for review/approval. "
                "Improve the definition and call create_skill again if you want to try for automatic approval, or tell the user it is ready for manual approval."
            )
    except Exception as e:
        logger.exception(f"{cid_prefix()}Error in create_skill tool handler")
        return f"Failed to create skill: {str(e)[:300]}"


async def handle_get_recent_context(args: dict[str, Any], original_message: Any = None) -> str:
    """Handler for the get_recent_context meta tool."""
    try:
        logger.info(f"{cid_prefix()}[DECISION] model chose get_recent_context (explicit tool decision to fetch recent context)")
        ch = getattr(original_message, "channel", None)
        ch_id = getattr(ch, "id", 0) if ch else 0
        from ..context.context_summarizer import summarize_recent_conversation
        summary = await summarize_recent_conversation(ch_id)
        if summary:
            return f"RECENT CHANNEL CONTEXT SUMMARY:\n{summary}\n\nUse this to maintain conversational coherence or answer references to prior messages."
        return "No substantial recent conversation context available for this channel."
    except Exception as e:
        return f"Could not retrieve recent context: {str(e)[:150]}"


async def handle_use_skill(args: dict[str, Any], original_message: Any = None) -> str:
    """Handler for the use_skill meta tool."""
    try:
        logger.info(f"{cid_prefix()}[DECISION] model chose use_skill (delegating to approved skill)")
        from ..skills.skill_registry import get_skill_registry
        reg = get_skill_registry()
        ident = str(args.get("skill", "") or args.get("skill_name", "") or args.get("skill_id", "")).strip()
        skill = None
        if ident:
            skill = reg.get_by_name(ident) or reg.get(ident)
        if not skill:
            for s in reg.list_approved():
                if ident.lower() in (s.name.lower() + " " + s.description.lower()):
                    skill = s
                    break
        if skill and skill.approved:
            instructions = skill.instructions.strip()
            block = (
                f"[SKILL ACTIVE: {skill.name}]\n"
                f"{instructions}\n"
                f"Only use the tools explicitly allowed for this skill. Stay focused on the skill's purpose. "
                f"Be concise and follow the skill instructions exactly."
            )
            return f"__USE_SKILL_ACTIVATED__:{skill.id}\n{block}"
        return f"No approved skill matching '{ident}' was found. Available approved skills: {[s.name for s in reg.list_approved()][:5]}."
    except Exception as e:
        return f"Error activating skill: {str(e)[:200]}"


async def handle_edit_skill(args: dict[str, Any], original_message: Any = None) -> str:
    """Handler for the edit_skill meta tool."""
    try:
        from ..skills.skill_registry import get_skill_registry
        reg = get_skill_registry()

        ident = str(args.get("skill", "") or args.get("skill_name", "") or args.get("skill_id", "")).strip()
        new_instructions = args.get("new_instructions")
        new_name = args.get("new_name")
        new_allowed = args.get("new_allowed_tools")
        edit_reason = args.get("reason")

        if not ident or not new_instructions:
            return "edit_skill requires at minimum 'skill' (name/id) and 'new_instructions'."

        # Resolve skill (by id or name, with loose fallback)
        skill = None
        if ident:
            skill = reg.get(ident) or reg.get_by_name(ident)
        if not skill:
            for s in reg.list_approved():
                if ident.lower() in (s.name.lower() + " " + s.description.lower()):
                    skill = s
                    break

        if not skill:
            return f"No skill found matching '{ident}'. Available approved skills: {[s.name for s in reg.list_approved()][:6]}."

        effective_instructions = new_instructions or skill.instructions
        effective_allowed = new_allowed if new_allowed is not None else skill.allowed_tools
        effective_name = new_name or skill.name

        # Capture autonomous engineering reasoning for edits too
        reasoning_notes = args.get("reasoning_notes", "") or args.get("analysis", "") or edit_reason
        proposed_architecture = args.get("proposed_architecture", "") or args.get("architecture", "")

        enhanced_description = skill.description or ""
        if proposed_architecture:
            enhanced_description = f"{enhanced_description} | Updated architecture: {proposed_architecture}"
        if reasoning_notes and reasoning_notes != edit_reason:
            enhanced_description = f"{enhanced_description} | Edit diagnosis: {reasoning_notes[:250]}"

        test_query = args.get("test_query") or _derive_test_query(edit_reason or f"improved {skill.name}", effective_name, effective_instructions)

        # === Test the *proposed edited version* before applying ===
        test_eval = await _test_skill_proposal(effective_instructions, effective_allowed, test_query, edit_reason or "")

        if test_eval.get("score", 0) >= 7 or test_eval.get("passed", False):
            updated = reg.update_skill(
                skill.id,
                name=new_name,
                description=enhanced_description,
                instructions=new_instructions,
                allowed_tools=new_allowed if new_allowed is not None else None,
            )

            if not updated:
                return f"Failed to update skill '{ident}'."

            if original_message:
                conf_text = f"Listo, actualicé la skill '{updated.name}'."
                if proposed_architecture:
                    conf_text += f" (Nueva arquitectura: {proposed_architecture[:80]}...)"
                if edit_reason:
                    conf_text += f" (Motivo: {edit_reason})"
                if test_eval.get("score"):
                    conf_text += f" (Test score: {test_eval.get('score')}/10 — pasó las pruebas.)"
                async def _send_conf(msg=original_message, text=conf_text):
                    try:
                        await asyncio.sleep(0.9)
                        await msg.channel.send(text)
                        logger.info(f"{cid_prefix()}[SKILLS] edit_skill tool confirmation sent for '{updated.name}' (engineer-driven edit)")
                    except Exception as send_err:
                        logger.debug(f"{cid_prefix()}Could not send edit_skill confirmation: {send_err}")
                try:
                    asyncio.create_task(_send_conf())
                except Exception:
                    pass

            return (
                f"Skill edited and tested successfully (score {test_eval.get('score', 'N/A')}). "
                f"'{updated.name}' (id={updated.id}) is now active with the new instructions."
            )
        else:
            feedback = test_eval.get("feedback", "Test failed.")
            issues = test_eval.get("issues", [])
            suggestion = test_eval.get("suggestion", "Refine the new instructions or adjust allowed_tools.")
            return (
                f"Test of proposed EDIT for skill '{skill.name}' FAILED (score {test_eval.get('score', 0)}). "
                f"Issues: {issues}. Feedback: {feedback}. Suggestion: {suggestion} "
                f"Test query: '{test_query}'. The skill was NOT modified. Please call edit_skill again with improved 'new_instructions' (and optionally new_allowed_tools)."
            )
    except Exception as e:
        logger.exception(f"{cid_prefix()}Error in edit_skill tool handler")
        return f"Failed to edit skill: {str(e)[:300]}"


async def handle_respond_directly(args: dict[str, Any], original_message: Any = None) -> str:
    """Handler for the respond_directly meta tool."""
    logger.info(f"{cid_prefix()}[DECISION] model chose respond_directly (explicit signal: ready for final answer, no more tools)")
    return "DECISION: respond directly. I now have enough information to formulate the final answer to the user without further tool calls or actions."
