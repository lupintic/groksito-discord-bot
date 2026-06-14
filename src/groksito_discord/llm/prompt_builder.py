"""
Prompt builder surface for the LLM layer.

The authoritative prompt text lives in prompt.py (single source of truth).
This module re-exports it for the llm/ package layout used by the refactor.
"""

from ..prompt import SUMMARIZATION_PROMPT, SYSTEM_PROMPT

__all__ = ["SYSTEM_PROMPT", "SUMMARIZATION_PROMPT"]