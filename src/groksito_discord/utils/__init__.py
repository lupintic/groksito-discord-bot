"""
Shared utilities for Groksito.

This package centralizes common helper functions to reduce duplication
and improve maintainability.

Phase 2 (text utilities): URL / link extraction helpers were moved here
from conversation.py (primary) and the inline logic in client.py.

All functions preserve exact previous behavior and output.
"""

from . import text

__all__ = ["text"]
