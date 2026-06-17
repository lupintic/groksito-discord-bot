"""Packaging and entry-point regression tests (#72 / #76)."""

from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = PROJECT_ROOT / "src" / "groksito_discord"


def test_configure_env_script_replaces_root_setup_py():
    assert (PROJECT_ROOT / "scripts" / "configure_env.py").is_file()
    assert not (PROJECT_ROOT / "setup.py").exists()


def test_dockerfile_uses_canonical_entrypoint():
    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "src.groksito_discord" not in dockerfile
    assert "groksito" in dockerfile or "groksito_discord" in dockerfile


def test_no_src_groksito_run_path_in_python_sources():
    offenders: list[str] = []
    for path in SRC_ROOT.rglob("*.py"):
        if "src.groksito_discord" in path.read_text(encoding="utf-8"):
            offenders.append(str(path.relative_to(PROJECT_ROOT)))
    assert offenders == []


def test_groksito_main_entry_importable():
    from groksito_discord.main import run

    assert callable(run)


def test_module_main_entrypoint():
    import groksito_discord.__main__ as main_mod

    assert hasattr(main_mod, "run")