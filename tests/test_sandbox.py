"""
Tests for sandbox execution helpers (code_execution / playwright_browser).

These tools are intentionally high-power and only reachable via approved skills
that declare them in allowed_tools. Tests focus on:

- Graceful fallback behavior when Docker is unavailable (the common case in CI / dev).
- That the public schemas are only offered through the skill augmentation path
  (never in normal get_tools_for_request without a skill active).
"""

from unittest.mock import patch

import pytest

from groksito_discord.sandbox import (
    run_code_execution,
    run_playwright_browser,
    _run_docker_command,
)
from groksito_discord.tools import (
    get_tools_for_request,
    get_skill_specific_custom_schemas,
)


def test_sandbox_falls_back_when_docker_missing():
    """When docker binary is not present we get a clear simulation message."""
    # Force the "not found" path
    with patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError):
        result = run_code_execution("print(1+1)")
        assert "docker not available" in result.lower() or "simulation" in result.lower()

    with patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError):
        result = run_playwright_browser(url="https://example.com", action="extract_text")
        assert "docker not available" in result.lower() or "simulation" in result.lower()


@pytest.mark.asyncio
async def test_sandbox_permission_error_message_is_helpful():
    """Permission errors should mention docker group or socket mount."""
    with patch("asyncio.create_subprocess_exec", side_effect=PermissionError):
        result = await _run_docker_command(["docker", "version"])
        assert "permission" in result.lower()
        assert "docker group" in result.lower() or "socket" in result.lower()


def test_sandbox_schemas_not_offered_in_normal_chat():
    """code_execution and playwright_browser must not appear in normal tool offering."""
    tools = get_tools_for_request(
        query_need="normal",
        has_visual_intent=False,
        offer_decision_tools=False,
    )
    names = {t.get("name") for t in tools if isinstance(t, dict)}
    assert "code_execution" not in names
    assert "playwright_browser" not in names


def test_sandbox_schemas_only_via_skill_custom_path():
    """They only appear when an approved skill explicitly allows them."""
    # No skill -> nothing
    assert get_skill_specific_custom_schemas(None) == []
    assert get_skill_specific_custom_schemas([]) == []

    # Skill that declares them -> schemas returned
    schemas = get_skill_specific_custom_schemas(["code_execution", "playwright_browser"])
    names = {s.get("name") for s in schemas if isinstance(s, dict)}
    assert "code_execution" in names
    assert "playwright_browser" in names

    # Unknown custom tool is ignored
    schemas = get_skill_specific_custom_schemas(["code_execution", "nonexistent_tool"])
    names = {s.get("name") for s in schemas if isinstance(s, dict)}
    assert "nonexistent_tool" not in names
    assert "code_execution" in names
