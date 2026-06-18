"""Tests for CHANGELOG section extraction used by the release workflow (#97)."""

from __future__ import annotations

from scripts.extract_changelog import extract_version_section


SAMPLE_CHANGELOG = """# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Pending work lands here.

## [0.2.0] - 2026-06-17

### Added
- Message splitting for long Discord replies.

### Fixed
- Video generation parity improvements.

## [0.1.0] - 2026-06-01

### Added
- Initial standalone bot release.
"""


def test_extract_version_section_returns_matching_block():
    section = extract_version_section(SAMPLE_CHANGELOG, "0.2.0")
    assert "### Added" in section
    assert "Message splitting" in section
    assert "### Fixed" in section
    assert "Video generation" in section
    assert "## [0.1.0]" not in section


def test_extract_version_section_missing_version_returns_empty():
    assert extract_version_section(SAMPLE_CHANGELOG, "9.9.9") == ""