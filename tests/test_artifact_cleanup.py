"""Regression tests for unused artifact cleanup (#72 / #78)."""

from __future__ import annotations

import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = PROJECT_ROOT / "src" / "groksito_discord"


def _git_tracked_files() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def test_no_skill_tools_module_in_source():
    assert not (SRC_ROOT / "llm" / "skill_tools.py").exists()


def test_no_skills_json_loading_in_active_source():
    """Legacy dynamic-skill experiment must not be wired into runtime paths."""
    for py_file in SRC_ROOT.rglob("*.py"):
        text = py_file.read_text(encoding="utf-8")
        assert "skills.json" not in text, f"{py_file.relative_to(PROJECT_ROOT)} references skills.json"
        assert "skill_tools" not in text, f"{py_file.relative_to(PROJECT_ROOT)} references skill_tools"


def test_session_artifact_dirs_not_tracked_in_git():
    tracked = _git_tracked_files()
    forbidden_prefixes = (
        "mcps/",
        "agent-tools/",
        "mcp-tool-schemas/",
        "terminals/",
        ".grok/",
        "docs/",
    )
    forbidden_exact = {
        "AGENTS.md",
        "agents.md",
        "Agents.md",
    }
    for path in tracked:
        for prefix in forbidden_prefixes:
            assert not path.startswith(prefix), f"Session artifact tracked in git: {path}"
        assert path not in forbidden_exact, f"Agent guidance file tracked in git: {path}"


def test_runtime_data_json_not_tracked_except_gitkeep():
    tracked = _git_tracked_files()
    data_json = [p for p in tracked if p.startswith("data/") and p.endswith(".json")]
    assert data_json == [], f"Runtime data JSON must not be committed: {data_json}"
    assert "data/.gitkeep" in tracked


def test_gitignore_covers_grok_session_artifacts():
    gitignore = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")
    for pattern in (
        ".grok/",
        "mcps/",
        "agent-tools/",
        "terminals/",
        "mcp-tool-schemas/",
        "docs/",
        "AGENTS.md",
        "tests/data/",
    ):
        assert pattern in gitignore