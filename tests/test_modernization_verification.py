"""Final modernization roadmap verification (#72 / #79)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = PROJECT_ROOT / "src" / "groksito_discord"

LEGACY_PATHS = (
    SRC_ROOT / "integrations",
    SRC_ROOT / "media" / "generator.py",
    SRC_ROOT / "media" / "editor.py",
    SRC_ROOT / "media" / "video_generation.py",
    SRC_ROOT / "llm" / "skill_tools.py",
    PROJECT_ROOT / "setup.py",
)

def test_legacy_paths_removed():
    for path in LEGACY_PATHS:
        assert not path.exists(), f"Legacy path still present: {path.relative_to(PROJECT_ROOT)}"


def test_canonical_layout_present():
    required = [
        SRC_ROOT / "discord" / "client.py",
        SRC_ROOT / "discord" / "integrations" / "steam.py",
        SRC_ROOT / "core" / "grok_oauth.py",
        SRC_ROOT / "llm" / "tools.py",
        SRC_ROOT / "media" / "delivery.py",
        PROJECT_ROOT / "scripts" / "configure_env.py",
        PROJECT_ROOT / "data" / ".gitkeep",
    ]
    for path in required:
        assert path.is_file(), f"Expected file missing: {path.relative_to(PROJECT_ROOT)}"


def test_docs_avoid_stale_run_and_import_paths():
    for doc in ("README.md", "ARCHITECTURE.md", "GROK_OAUTH.md"):
        text = (PROJECT_ROOT / doc).read_text(encoding="utf-8")
        assert "python -m src.groksito_discord" not in text, doc
        if doc == "ARCHITECTURE.md":
            stale = text.replace("discord/integrations/steam.py", "")
            assert "integrations/steam.py" not in stale, doc
            assert "video_generation.py" not in text, doc


def test_groksito_cli_check_exits_zero():
    env = os.environ.copy()
    env.setdefault("DISCORD_BOT_TOKEN", "ci-test-discord-token")
    env.setdefault("XAI_API_KEY", "ci-test-xai-api-key")
    result = subprocess.run(
        [sys.executable, "-m", "groksito_discord", "--check"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_check_script_exists():
    assert (PROJECT_ROOT / "scripts" / "check.py").is_file()