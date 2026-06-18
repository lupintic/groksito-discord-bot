"""Extract a Keep a Changelog version section for GitHub Releases (#97)."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


def normalize_version(version: str) -> str:
    """Strip leading ``v`` and pre-release suffix for section lookup."""
    version = version.lstrip("v")
    return re.split(r"-(?:pre|rc|alpha|beta)", version, maxsplit=1)[0]


def extract_version_section(changelog_text: str, version: str) -> str:
    """Return the markdown block for ``## [<version>]`` including its header."""
    version = normalize_version(version)
    pattern = rf"^## \[{re.escape(version)}\][^\n]*\n(.*?)(?=^## \[|\Z)"
    match = re.search(pattern, changelog_text, flags=re.MULTILINE | re.DOTALL)
    if not match:
        return ""

    header = f"## [{version}]"
    for line in changelog_text.splitlines():
        if line.startswith(f"## [{version}]"):
            header = line
            break

    return f"{header}\n{match.group(1).rstrip()}\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True, help="Release version (e.g. 0.2.0 or v0.2.0-pre.1)")
    parser.add_argument("--file", type=Path, default=Path("CHANGELOG.md"), help="Path to CHANGELOG.md")
    args = parser.parse_args(argv)

    text = args.file.read_text(encoding="utf-8")
    section = extract_version_section(text, args.version)
    if section:
        sys.stdout.write(section)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())