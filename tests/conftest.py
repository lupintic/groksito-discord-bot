"""
Pytest configuration and shared fixtures for Groksito tests.

Keeps the test suite lightweight: no external test libs beyond stdlib + pytest.
"""

import pytest

import groksito_discord.llm.media_tools as media_tools_mod
import groksito_discord.llm.tools as tools_mod


@pytest.fixture
def patch_video_enabled(monkeypatch):
    """Temporarily force the video feature flag for tests that need deterministic behavior."""
    def _set(enabled: bool):
        monkeypatch.setattr(media_tools_mod, "ENABLE_VIDEO_GENERATION", enabled)
        monkeypatch.setattr(tools_mod, "ENABLE_VIDEO_GENERATION", enabled)
        return enabled
    return _set
