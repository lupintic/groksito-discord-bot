"""Documentation accuracy regression tests (#72 / #77)."""

from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _read(path: str) -> str:
    return (PROJECT_ROOT / path).read_text(encoding="utf-8")


def test_readme_maps_to_current_module_layout():
    readme = _read("README.md")
    assert "discord/client.py" in readme
    assert "discord/integrations/steam.py" in readme
    assert "core/grok_oauth.py" in readme
    assert "llm/client.py" in readme
    assert "bot.py" not in readme


def test_architecture_maps_to_current_module_layout():
    arch = _read("ARCHITECTURE.md")
    assert "discord/client.py" in arch
    assert "discord/integrations/steam.py" in arch
    assert "core/grok_oauth.py" in arch
    assert "media/delivery.py" in arch
    assert "integrations/steam.py" not in arch.replace("discord/integrations/steam.py", "")
    assert "bot.py" not in arch
    assert "video_generation.py" not in arch


def test_architecture_documents_pantsu_context_compat_once():
    arch = _read("ARCHITECTURE.md")
    assert arch.count("pantsu_context.json") == 1