"""
Pytest configuration and shared fixtures for Groksito tests.

Keeps the test suite lightweight: no external test libs beyond stdlib + pytest.
"""

import pytest
from datetime import date, datetime
from unittest.mock import patch

# Import the modules under test so we can reset internal state safely.
# Using the package import (src on pythonpath via pytest config).
import groksito_discord.context as context_mod
import groksito_discord.media_tools as media_tools_mod
import groksito_discord.tools as tools_mod


@pytest.fixture(autouse=True)
def reset_global_state():
    """Reset mutable module globals between tests to keep tests isolated and repeatable."""
    # Video quotas (in-memory daily counters)
    if hasattr(context_mod, "_video_quotas"):
        context_mod._video_quotas.clear()

    yield

    if hasattr(context_mod, "_video_quotas"):
        context_mod._video_quotas.clear()


@pytest.fixture
def frozen_today(monkeypatch):
    """Fixture to freeze 'today' for quota day-reset tests.

    Usage:
        def test_something(frozen_today):
            frozen_today("2026-01-15")
            ...
    """
    def _freeze(iso_date: str):
        fixed = date.fromisoformat(iso_date)
        # Patch both the date.today used inside context and the date class if referenced directly.
        monkeypatch.setattr(context_mod, "date", type("date", (), {"today": staticmethod(lambda: fixed), "fromisoformat": staticmethod(date.fromisoformat)}))
        # Also patch in case other modules imported date.
        try:
            import groksito_discord.context as cm  # ensure
            monkeypatch.setattr("groksito_discord.context.date", type("d", (), {"today": staticmethod(lambda: fixed)}))
        except Exception:
            pass
        return fixed

    return _freeze


@pytest.fixture
def patch_video_enabled(monkeypatch):
    """Temporarily force the video feature flag for tests that need deterministic behavior."""
    def _set(enabled: bool):
        monkeypatch.setattr(media_tools_mod, "ENABLE_VIDEO_GENERATION", enabled)
        monkeypatch.setattr(tools_mod, "ENABLE_VIDEO_GENERATION", enabled)
        return enabled
    return _set
