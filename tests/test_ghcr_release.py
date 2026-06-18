"""GHCR Docker publishing regression tests (#99)."""

from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _read(path: str) -> str:
    return (PROJECT_ROOT / path).read_text(encoding="utf-8")


def test_release_workflow_publishes_bot_and_web_to_ghcr():
    workflow = _read(".github/workflows/release.yml")
    assert "docker/login-action" in workflow
    assert "ghcr.io" in workflow
    assert "target: bot" in workflow
    assert "target: web" in workflow


def test_release_workflow_tags_version_latest_and_sha():
    workflow = _read(".github/workflows/release.yml")
    assert "docker/metadata-action" in workflow
    assert "type=semver" in workflow
    assert "type=sha" in workflow
    assert "latest" in workflow


def test_release_workflow_passes_oci_build_args():
    workflow = _read(".github/workflows/release.yml")
    assert "org.opencontainers.image" in workflow
    assert "build-args" in workflow


def test_dockerfile_declares_oci_image_labels():
    dockerfile = _read("Dockerfile")
    for label in (
        "org.opencontainers.image.title",
        "org.opencontainers.image.version",
        "org.opencontainers.image.revision",
        "org.opencontainers.image.source",
    ):
        assert label in dockerfile, f"missing OCI label: {label}"


def test_readme_documents_prebuilt_ghcr_images():
    readme = _read("README.md")
    assert "ghcr.io/lupintic/groksito-discord-bot" in readme
    assert "ghcr.io/lupintic/groksito-discord-bot-web" in readme
    assert "docker pull" in readme


def test_docker_compose_allows_prebuilt_image_override():
    compose = _read("docker-compose.yml")
    assert "ghcr.io/lupintic/groksito-discord-bot" in compose
    assert "ghcr.io/lupintic/groksito-discord-bot-web" in compose