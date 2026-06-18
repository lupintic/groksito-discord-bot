# Releasing Groksito

This document describes how maintainers cut pre-releases and stable releases for **groksito-discord-bot**. It complements the contributor workflow in [CONTRIBUTING.md](./CONTRIBUTING.md) and the automated pipeline in [`.github/workflows/release.yml`](./.github/workflows/release.yml).

## Who releases

Release tagging is a **maintainer** responsibility. Contributors should land changes on `main` via PR; maintainers cut tags when `main` is ready.

## Versioning policy

This project follows [Semantic Versioning](https://semver.org/) (`MAJOR.MINOR.PATCH`):

| Phase | Version examples | Meaning |
|-------|------------------|---------|
| Early development | `0.2.0`, `0.3.0` | Public API and behavior may still evolve. Breaking changes are allowed while `0.x`. |
| Stable | `1.0.0+` | Semver guarantees apply to user-facing behavior and packaging. |

**Source of truth:** `version` in [`pyproject.toml`](./pyproject.toml). The installed package exposes the same value via `groksito_discord.__version__`.

### Pre-release vs stable

| Kind | When to use | Example tags |
|------|-------------|--------------|
| **Pre-release** | Validate packaging, GHCR images, and release notes before calling a version stable. Use for beta testers and Docker early adopters. | `v0.3.0-pre.1`, `v0.3.0-rc.1` |
| **Stable** | `main` is green, changelog is finalized, and you are ready to publish `:latest` Docker tags and recommend the version broadly. | `v0.3.0`, `v1.0.0` |

**Tag format:** always prefix with `v` (e.g. `v0.3.0`, not `0.3.0`). Pushing a `v*` tag triggers the Release workflow.

Pre-release detection in CI: tags containing `-pre`, `-rc`, `-alpha`, or `-beta` are marked as GitHub **pre-releases** and do **not** receive the `:latest` GHCR tag.

## Prerequisites (must pass before tagging)

Run these on the commit you intend to tag (usually the tip of `main` after merging release-prep PRs):

```bash
# Fast local gate (recommended before every tag)
python scripts/check.py --skip-docker

# Full gate including Docker image builds (recommended before stable releases)
python scripts/check.py
```

`scripts/check.py` runs:

1. `pytest -q`
2. `python -m groksito_discord --check`
3. `python -m groksito_discord --status`
4. (unless `--skip-docker`) `docker build --target bot` and `docker build --target web`

CI on `main` also runs pytest (Python 3.11 and 3.12), import/version checks, and packaging builds. **Do not tag** if `main` CI is failing.

Optional manual packaging smoke test:

```bash
python -m pip install build
python -m build --sdist --wheel
```

## Release checklist

### 1. Prepare the changelog

[`CHANGELOG.md`](./CHANGELOG.md) follows [Keep a Changelog](https://keepachangelog.com/).

1. Move entries from `## [Unreleased]` into a new dated section, e.g. `## [0.3.0] - 2026-06-18`.
2. Leave an empty `## [Unreleased]` section at the top for the next cycle.
3. Use categories: `Added`, `Changed`, `Deprecated`, `Removed`, `Fixed`, `Security`.
4. Write user-facing bullets; link PRs/issues when helpful.

The release workflow extracts the matching section with `scripts/extract_changelog.py` and uses it as the GitHub Release body (pre-release tags still use the **base** version section — e.g. `v0.3.0-rc.1` reads `## [0.3.0]`).

Verify locally:

```bash
python scripts/extract_changelog.py --version 0.3.0 --file CHANGELOG.md
```

### 2. Bump the package version

Update `version` in `pyproject.toml` to match the release (without the `v` prefix):

```toml
version = "0.3.0"
```

Commit changelog + version bump on `main` (or a short-lived release-prep branch merged to `main`).

### 3. Run verification

```bash
python scripts/check.py --skip-docker   # minimum before any tag
python scripts/check.py                 # before stable tags
```

### 4. Tag and push

```bash
git checkout main
git pull origin main

# Pre-release example
git tag -a v0.3.0-pre.1 -m "v0.3.0-pre.1"
git push origin v0.3.0-pre.1

# Stable example
git tag -a v0.3.0 -m "v0.3.0"
git push origin v0.3.0
```

Use annotated tags (`-a`) so the release notes and history stay clear.

### 5. Monitor the Release workflow

Pushing `v*` starts [`.github/workflows/release.yml`](./.github/workflows/release.yml), which:

1. Builds Python **sdist** and **wheel** artifacts.
2. Creates a **GitHub Release** (pre-release flag set automatically for `-pre`/`-rc`/`-alpha`/`-beta` tags).
3. Publishes **GHCR** images:
   - `ghcr.io/lupintic/groksito-discord-bot:<version>` (bot)
   - `ghcr.io/lupintic/groksito-discord-bot-web:<version>` (web dashboard)
   - `:latest` only on **stable** tags (no hyphen suffix in the tag)
   - `sha-<short>` tags for pinning to a commit

Watch the Actions tab until all jobs succeed. If the workflow fails, fix `main`, cut a new patch/pre tag — do not move or force-push published tags.

## Post-release tasks

1. **SECURITY.md** — update the supported versions table when a new stable line ships.
2. **README** — confirm GHCR examples and version references still match (if the stable version changed materially).
3. **Communicate** — publish the GitHub Release link; for stable releases, note new Docker pull tags (`:latest` updates only on stable).
4. **Pre-releases** — tell early adopters to pin explicit version tags (not `:latest`).

## Docker consumers

Pre-built images are published to [GitHub Container Registry](https://github.com/lupintic/groksito-discord-bot/pkgs/container/groksito-discord-bot):

```bash
docker pull ghcr.io/lupintic/groksito-discord-bot:0.3.0
docker pull ghcr.io/lupintic/groksito-discord-bot:latest        # stable only
docker pull ghcr.io/lupintic/groksito-discord-bot:sha-d645292   # commit pin
```

Pre-release tags publish version-specific tags but **not** `:latest`. See [README.md](./README.md#pre-built-images-ghcr).

## Quick reference

| Step | Command / action |
|------|------------------|
| Verify (fast) | `python scripts/check.py --skip-docker` |
| Verify (full) | `python scripts/check.py` |
| Changelog excerpt | `python scripts/extract_changelog.py --version 0.3.0` |
| Version bump | `pyproject.toml` → `version = "0.3.0"` |
| Tag | `git tag -a v0.3.0 -m "v0.3.0" && git push origin v0.3.0` |
| CI trigger | Push tag matching `v*` |

## Related docs

- [CONTRIBUTING.md](./CONTRIBUTING.md) — day-to-day development and PR changelog expectations
- [CHANGELOG.md](./CHANGELOG.md) — user-visible history
- [SECURITY.md](./SECURITY.md) — supported versions and vulnerability reporting
- [README.md](./README.md) — installation and GHCR usage