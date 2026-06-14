"""LLM client (Grok Responses), prompt building, input assembly, tools, helpers, sandbox.

Reexports for `from groksito_discord.llm import call_grok...` compat (used by core.conversation etc).
"""

from .client import (
    call_grok_for_groksito,
    call_grok_with_tools,
)

