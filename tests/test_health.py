"""
Tests for health reporting, including the new Docker sandbox probe added in Phase 0/2.
"""

from unittest.mock import patch

import pytest

from groksito_discord.health import get_health_status


def test_health_docker_probe_reports_availability():
    """The docker probe should be present and report a boolean."""
    health = get_health_status()
    assert "docker_available_for_sandboxes" in health
    assert isinstance(health["docker_available_for_sandboxes"], bool)


def test_health_docker_probe_graceful_on_no_docker():
    """When docker binary is missing we still get a clean False (no crash)."""
    with patch("shutil.which", return_value=None):
        health = get_health_status()
        assert health["docker_available_for_sandboxes"] is False
        assert health.get("docker_version") is None


def test_health_docker_probe_happy_path(monkeypatch):
    """When docker --version succeeds we report True + a version snippet."""
    fake_result = type("obj", (object,), {
        "returncode": 0,
        "stdout": "Docker version 24.0.0, build 1234",
        "stderr": ""
    })()

    with patch("shutil.which", return_value="/usr/bin/docker"), \
         patch("subprocess.run", return_value=fake_result):
        health = get_health_status()
        assert health["docker_available_for_sandboxes"] is True
        assert "Docker version" in (health.get("docker_version") or "")
