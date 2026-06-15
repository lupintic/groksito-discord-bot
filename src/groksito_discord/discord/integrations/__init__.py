"""
External service integrations for Groksito.

Currently contains:
- steam: Steam player count + image integration (powers /steamchart, /stmchr, /topgames).
- twitch: Twitch Helix viewer/stream stats (powers /versus).

This package was introduced in Phase 1 of the refactoring to remove unrelated
third-party data fetching logic from the Discord client module (client.py).
All behavior is preserved exactly.
"""

from . import steam, twitch

__all__ = ["steam", "twitch"]
