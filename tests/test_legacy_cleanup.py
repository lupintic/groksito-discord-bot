"""Regression tests for structure modernization (#72 / #73)."""

from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LEGACY_INTEGRATIONS = PROJECT_ROOT / "src" / "groksito_discord" / "integrations"
CANONICAL_STEAM = PROJECT_ROOT / "src" / "groksito_discord" / "discord" / "integrations" / "steam.py"


def test_legacy_top_level_integrations_directory_removed():
    """Stale top-level integrations/ must not exist after Phase 1 refactor."""
    assert not LEGACY_INTEGRATIONS.exists(), (
        "Remove stale src/groksito_discord/integrations/; "
        "Steam/Twitch live under discord/integrations/."
    )


def test_canonical_steam_module_present():
    assert CANONICAL_STEAM.is_file()


def test_steam_integration_imports_from_discord_subpackage():
    from groksito_discord.discord.integrations import steam

    assert hasattr(steam, "_resolve_steam_game_local")