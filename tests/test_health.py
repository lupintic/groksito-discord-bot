"""
Tests for health reporting.
"""

from groksito_discord.core.health import get_health_status


def test_health_reports_core_fields():
    health = get_health_status()
    assert "status" in health
    assert "video_generation_enabled" in health
    assert isinstance(health["status"], str)