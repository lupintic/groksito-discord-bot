#!/usr/bin/env python3
"""
Unified, robust .env file management for Groksito.

This module provides a SINGLE source of truth for all .env read/write operations
used by both:
  - scripts/configure_env.py (interactive safe setup / repair / dedup)
  - web/main.py (dashboard config editor)

Core guarantees (the whole point of the rework):
- NEVER duplicate keys. Updates happen in-place using case-insensitive matching.
- When a key being updated already exists multiple times (from prior bugs), we
  collapse to a single updated line (first position wins for structure, value is updated).
- Preserve original file casing for the key, inline comments, blank lines, and
  overall order as much as humanly possible.
- Only brand-new keys are appended (at the very end, or ideally near a related section
  if we had section markers — for simplicity we append cleanly at EOF).
- List values (ALLOWED_GUILD_IDS etc.) are ALWAYS written as compact JSON arrays
  e.g. [123456,789012] so pydantic-settings list[int] validators parse them cleanly.
- Always create timestamped + rolling ".backup" before any write.
- Atomic write via temp file + os.replace + fsync where possible.
- Post-write verification: if any CRITICAL key that was present before the write
  disappears, we restore from backup automatically.
- Protected keys (secrets + auth mode) are enforced at the call site (web) and
  respected here (setup can still change them because it is the privileged tool).

This replaces the previous duplicated-and-slightly-different implementations
that lived in configure_env.py and web/main.py (and the standalone test copy).
Drift between those copies was a major contributor to the duplication bugs.

Usage (preferred):
    from groksito_discord.utils.env_utils import (
        safe_write_env, parse_env_file, parse_env_lines,
        deduplicate_env_file, backup_env, CRITICAL_KEYS, PROTECTED_KEYS,
    )

    ok, msg, backup = safe_write_env(Path(".env"), {"LOG_LEVEL": "DEBUG"})

The functions are intentionally dependency-light (stdlib only) so scripts/configure_env.py
can import them even in minimal environments.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger("groksito.env")

# =============================================================================
# Constants
# =============================================================================

# Keys whose *presence* (and for secrets, whose value) is absolutely critical
# for the bot to function. The writer will refuse to let these vanish.
CRITICAL_KEYS: set[str] = {
    "DISCORD_BOT_TOKEN",
    "XAI_API_KEY",
}

# Keys that control authentication and must not be tampered with by the
# unprivileged web dashboard. scripts/configure_env.py (the CLI tool) is allowed to manage them.
OAUTH_KEYS: set[str] = {
    "GROK_AUTH_MODE",
    "GROK_OAUTH_PORT",
    "GROK_OAUTH_TOKEN_FILE",
}

PROTECTED_KEYS: set[str] = CRITICAL_KEYS | OAUTH_KEYS

# Regex that robustly parses .env lines.
# Supports:
#   - optional "export " prefix
#   - double quotes, single quotes, or bare (until space or #)
#   - optional inline # comment
# Groups: 1=key (original casing), 2=double-quoted, 3=single-quoted, 4=bare, 5=comment
ENV_LINE_RE = re.compile(
    r'^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?:"([^"]*)"|\'([^\']*)\'|([^\s#]*))?\s*(?:#\s*(.*))?$',
    re.IGNORECASE,
)

BACKUP_SUFFIX = ".backup"


# =============================================================================
# Value formatting (the source of many past "my list broke" bugs)
# =============================================================================

def _format_env_value(val: Any) -> str:
    """
    Format a Python value into a safe .env scalar.

    Rules:
    - list/tuple -> compact JSON array: [123,456,789]  (no spaces)
      This is the ONLY format that reliably round-trips through both:
        * the bare-value arm of ENV_LINE_RE
        * pydantic-settings list[int] / list[str] validators (they accept JSON arrays)
    - None or empty -> ""
    - Simple alphanum + limited safe chars (no spaces, no #, no =) -> bare (no quotes)
    - Everything else -> "double-quoted and escaped"

    This must stay in sync with how config.py validators expect the data.
    """
    if isinstance(val, (list, tuple)):
        # Compact is important: the old regex would stop at the first space
        # inside a pretty-printed array. Also produces valid JSON.
        return json.dumps(val, separators=(",", ":"))
    if val is None:
        val = ""
    val = str(val)
    if not val:
        return '""'
    # Bare-safe: letters, digits, and a few path/symbol chars that are common and unambiguous.
    if re.match(r"^[A-Za-z0-9_./:@%+=-]+$", val) and not val[0] in ("-", "+", "="):
        return val
    # Needs quoting. Escape backslashes first, then inner double-quotes.
    escaped = val.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _format_list_for_display(val: Any) -> str:
    """
    Tolerant converter used by interactive prompts (scripts/configure_env.py).

    Accepts:
      - Python list
      - JSON array string (with or without outer quotes)
      - comma-separated string (possibly with brackets/quotes remnants from old bad writes)
    Returns a clean "123,456" string suitable as a prompt default.
    """
    if val is None:
        return ""
    if isinstance(val, (list, tuple)):
        return ",".join(str(x) for x in val)
    if isinstance(val, str):
        v = val.strip()
        if not v:
            return ""
        # JSON array first
        if v.startswith("[") and v.endswith("]"):
            try:
                lst = json.loads(v)
                if isinstance(lst, (list, tuple)):
                    return ",".join(str(x) for x in lst)
            except Exception:
                pass
        # Fallback: comma soup
        try:
            parts = [p.strip().strip('"').strip("'") for p in v.split(",") if p.strip()]
            return ",".join(p for p in parts if p)
        except Exception:
            return v
    return str(val)


# =============================================================================
# Case-insensitive helpers
# =============================================================================

def _get_ci(d: dict[str, str], key: str, default: str = "") -> str:
    """Case-insensitive lookup in a {key: value} dict whose keys may be mixed case."""
    if not key:
        return default
    klower = key.lower()
    for dk, dv in d.items():
        if dk.lower() == klower:
            return dv
    return default


def _ci_contains(keys: set[str] | list[str] | dict[str, Any], candidate: str) -> bool:
    """True if candidate.lower() is in the collection (case-insensitive membership)."""
    cl = candidate.lower()
    if isinstance(keys, (set, list, tuple)):
        return any(k.lower() == cl for k in keys)
    if isinstance(keys, dict):
        return any(k.lower() == cl for k in keys.keys())
    return False


# =============================================================================
# Parsing (preserve original lines for faithful rewrite)
# =============================================================================

def parse_env_file(path: Path) -> dict[str, str]:
    """
    Parse .env into {original_cased_key: value_string}.

    Last occurrence wins in the returned dict (standard .env semantics).
    Comments, blank lines, and export prefixes are ignored for the dict view.
    Use parse_env_lines() when you need the exact original text for rewriting.
    """
    values: dict[str, str] = {}
    if not path.exists():
        return values
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            m = ENV_LINE_RE.match(line)
            if m:
                key = m.group(1)
                val = m.group(2) or m.group(3) or m.group(4) or ""
                values[key] = val
    except (OSError, UnicodeDecodeError) as e:
        logger.warning(f"Failed to parse .env at {path} (non-fatal): {e}")
    return values


def parse_env_lines(path: Path) -> list[str]:
    """Return the exact original lines (with their original line endings) for safe rewrite."""
    if not path.exists():
        return []
    try:
        return path.read_text(encoding="utf-8").splitlines(keepends=True)
    except OSError as e:
        logger.warning(f"Failed to read .env lines from {path} (non-fatal): {e}")
        return []


# =============================================================================
# Backup (always, before any mutation)
# =============================================================================

def backup_env(path: Path) -> Path | None:
    """
    Create both a timestamped backup and a rolling ".env.backup".

    Returns the timestamped path (or None on any failure / no original file).
    We keep the rolling one for quick "just give me the last good state".
    """
    if not path.exists():
        return None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        ts_bak = path.with_name(f"{path.name}{BACKUP_SUFFIX}-{ts}")
        latest = path.with_name(f"{path.name}{BACKUP_SUFFIX}")
        shutil.copy2(path, ts_bak)
        shutil.copy2(path, latest)
        return ts_bak
    except OSError as e:
        logger.warning(f"Failed to backup .env at {path} (non-fatal): {e}")
        return None


# =============================================================================
# Deduplication (explicit "Clean Duplicates" operation)
# =============================================================================

def deduplicate_env_file(path: Path, *, keep: str = "last", make_backup: bool = True) -> bool:
    """
    Remove duplicate keys (case-insensitive), keeping either the first or last occurrence.

    When keep="last" (recommended): the final value in the file wins and its original
    line content (casing + any inline comment) is preserved.

    This is the tool you run when a previous buggy writer left "KEY=foo" at the top
    and three "key=bar" lines at the bottom. It collapses them without losing the
    "most recent intent".

    Returns True if the file was actually changed (and rewritten).
    """
    if not path.exists():
        return False

    lines = parse_env_lines(path)
    changed = False
    kept_lines: list[str] = []

    if keep == "last":
        last_idx: dict[str, int] = {}
        for i, line in enumerate(lines):
            m = ENV_LINE_RE.match(line)
            if m:
                lk = m.group(1).lower().strip()
                last_idx[lk] = i
        for i, line in enumerate(lines):
            m = ENV_LINE_RE.match(line)
            if m:
                lk = m.group(1).lower().strip()
                if i != last_idx[lk]:
                    changed = True
                    continue  # drop older duplicate
            kept_lines.append(line)
    else:  # keep "first"
        seen: set[str] = set()
        for line in lines:
            m = ENV_LINE_RE.match(line)
            if m:
                lk = m.group(1).lower().strip()
                if lk in seen:
                    changed = True
                    continue
                seen.add(lk)
            kept_lines.append(line)

    if changed:
        if make_backup:
            backup_env(path)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text("".join(kept_lines), encoding="utf-8")
        os.replace(tmp, path)
    return changed


# =============================================================================
# THE CORE: safe_write_env
# =============================================================================

def safe_write_env(
    path: Path,
    updates: dict[str, Any],
    *,
    force_backup: bool = True,
    protected_keys: set[str] | None = None,
) -> tuple[bool, str, Path | None]:
    """
    The single robust writer used by both scripts/configure_env.py and the web dashboard.

    Contract:
    - Creates a backup (timestamped + rolling) before touching the file (when it exists).
    - Case-insensitive key matching: "tts_default_voice", "TTS_DEFAULT_VOICE", and
      "Tts_Default_Voice" are all treated as the same key.
    - If the key already exists anywhere in the file (even with different casing),
      we UPDATE IT IN PLACE using the *original casing from the file* for that key.
      We also preserve any inline comment that was on the original line.
    - If the same key appears multiple times (pre-existing duplication from old bugs),
      we emit exactly ONE updated line (in the position of the first occurrence) and
      drop the later duplicate lines for that key. This is a "clean as you touch" behavior.
    - Brand new keys (never seen, even with different case) are appended at the very end.
      We use the caller's preferred casing for the new key (so web can send lower_snake,
      setup can send UPPER, .env.example uses UPPER).
    - Lists are formatted as compact JSON arrays.
    - Atomic write (temp file + os.replace + best-effort fsync).
    - After write: if any CRITICAL key that existed before the operation is now missing,
      we restore the backup and return a failure. This is defense-in-depth.
    - Idempotent: calling with the same updates multiple times is safe.

    protected_keys: optional extra set of keys that must never be written by this call.
                    (The web layer passes PROTECTED_KEYS here as a hard belt-and-suspenders.)
                    scripts/configure_env.py normally passes None so it can manage auth keys.

    Returns: (success: bool, message: str, backup_path_or_None)
    """
    if not updates:
        return True, "no changes", None

    # Belt-and-suspenders: drop anything the caller explicitly says is off-limits.
    if protected_keys:
        plower = {p.lower() for p in protected_keys}
        safe_updates = {k: v for k, v in updates.items() if k.lower() not in plower}
        if len(safe_updates) != len(updates):
            updates = safe_updates
        if not updates:
            return True, "no (safe) changes — all submitted keys were protected and ignored", None

    backup_path: Path | None = None
    if path.exists() and force_backup:
        backup_path = backup_env(path)

    pre_values = parse_env_file(path) if path.exists() else {}
    pre_critical_present = {ck: bool(_get_ci(pre_values, ck)) for ck in CRITICAL_KEYS}

    lines = parse_env_lines(path)

    # Normalize the requested updates for case-insensitive, deduped lookup.
    # We remember the *first* caller-provided casing only for keys that will be newly appended.
    update_map: dict[str, Any] = {}
    caller_casing: dict[str, str] = {}
    for k, v in updates.items():
        lk = k.lower().strip()
        if lk and lk not in update_map:
            update_map[lk] = v
            caller_casing[lk] = k  # caller's casing preference for a brand-new append

    new_lines: list[str] = []
    updated_lowers: set[str] = set()

    for line in lines:
        m = ENV_LINE_RE.match(line)
        if m:
            orig_key = m.group(1)
            lower = orig_key.lower().strip()
            if lower in update_map:
                # First time we see this key while updating -> emit the new value.
                # Subsequent duplicate lines for the same lower key are dropped (cleaning).
                if lower not in updated_lowers:
                    val = update_map[lower]
                    formatted = _format_env_value(val)
                    comment = m.group(5) or ""
                    new_line = f"{orig_key}={formatted}"
                    if comment:
                        new_line += f"  # {comment}"
                    new_line += "\n"
                    new_lines.append(new_line)
                    updated_lowers.add(lower)
                # else: this is a duplicate occurrence of a key we are updating -> drop it
                continue
        new_lines.append(line)

    # Append only keys that were never present anywhere in the original file.
    for lower_key, val in update_map.items():
        if lower_key not in updated_lowers:
            key_to_use = caller_casing.get(lower_key, lower_key)
            formatted = _format_env_value(val)
            new_lines.append(f"{key_to_use}={formatted}\n")

    # Atomic write
    try:
        if not path.parent.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text("".join(new_lines), encoding="utf-8")
        try:
            with open(tmp, "rb") as f:
                os.fsync(f.fileno())
        except OSError as fsync_err:
            logger.debug(f"fsync after .env write best-effort failed: {fsync_err}")
        os.replace(tmp, path)
    except OSError as e:
        # Attempt to restore on catastrophic write failure
        if backup_path and backup_path.exists():
            try:
                shutil.copy2(backup_path, path)
            except OSError as restore_err:
                logger.error(f"Failed to restore .env from backup after write error: {restore_err}")
        return False, f"Write failed: {e}. Backup restored if possible.", backup_path

    # Post-write safety verification for critical keys
    post = parse_env_file(path)
    for ck in CRITICAL_KEYS:
        had = pre_critical_present.get(ck, False)
        now = bool(_get_ci(post, ck))
        if had and not now:
            if backup_path and backup_path.exists():
                try:
                    shutil.copy2(backup_path, path)
                except OSError as restore_err:
                    logger.error(f"Failed to restore .env after critical key loss: {restore_err}")
            return False, (
                f"SAFETY ABORT: Critical key {ck} disappeared after write. "
                "File restored from backup. No secrets were lost."
            ), backup_path
        # Also verify we didn't somehow duplicate it during the write (belt + suspenders)
        # We don't abort for this, but a future enhancement could warn.

    return True, "", backup_path


# =============================================================================
# Convenience: load a template as starting content (used by setup when fresh)
# =============================================================================

def load_template_as_lines(template_path: Path | None = None) -> list[str]:
    """
    Return the lines of a .env template (usually .env.example) if present.

    This lets scripts/configure_env.py do a "clean fresh start based on the documented template"
    instead of an empty file when the user chooses the nuclear "FRESH" option
    or when no .env exists at all.
    """
    candidates: list[Path] = []
    if template_path:
        candidates.append(template_path)
    # Common locations relative to CWD
    candidates.extend([
        Path(".env.example"),
        Path("env.example"),
        Path(".env.template"),
    ])
    for cand in candidates:
        if cand.exists():
            try:
                return cand.read_text(encoding="utf-8").splitlines(keepends=True)
            except Exception:
                pass
    return []


def create_fresh_env_from_template(
    target: Path,
    template_path: Path | None = None,
    overrides: dict[str, Any] | None = None,
) -> tuple[bool, str, Path | None]:
    """
    Create (or replace) target .env using the template as base, then apply overrides.

    This is used by the "Start FRESH" path in scripts/configure_env.py. It still does a backup
    of any pre-existing .env.
    """
    backup_path = backup_env(target) if target.exists() else None

    template_lines = load_template_as_lines(template_path)
    if not template_lines:
        # Minimal skeleton so we never leave the user with an empty file
        template_lines = [
            "# Groksito Discord Bot Configuration\n",
            "# Generated fresh — fill in the required values below.\n\n",
            "DISCORD_BOT_TOKEN=\n",
            "XAI_API_KEY=\n\n",
            "GROK_AUTH_MODE=api_key\n",
            "ALLOWED_GUILD_IDS=\n",
        ]

    # Write the template first (so the file exists with nice structure)
    try:
        if not target.parent.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text("".join(template_lines), encoding="utf-8")
        os.replace(tmp, target)
    except Exception as e:
        if backup_path and backup_path.exists():
            try:
                shutil.copy2(backup_path, target)
            except Exception:
                pass
        return False, f"Failed to write template: {e}", backup_path

    # Now apply any overrides the caller collected (using the safe writer so we get
    # all the protections, JSON list formatting, verification, etc.).
    if overrides:
        ok, msg, bak2 = safe_write_env(target, overrides, force_backup=False)
        if not ok:
            return False, msg, bak2 or backup_path
        # If the template write created a backup and safe_write also did, prefer the more recent one.
        # The caller only cares that *a* backup exists.
    return True, "", backup_path
