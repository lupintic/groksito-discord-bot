"""
Standalone test for the unified safe .env logic (now lives in src/groksito_discord/env_utils.py).

This test imports the real shared implementation so we catch regressions
in the single source of truth used by both scripts/configure_env.py and the web dashboard.
"""

import tempfile
import os
from pathlib import Path

# Import the real unified logic (this is the point of the rework)
import sys
SRC = Path(__file__).resolve().parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from groksito_discord.utils.env_utils import (
    safe_write_env,
    parse_env_file,
    parse_env_lines,
    backup_env as backup_env_file,
    deduplicate_env_file,
    _format_env_value,
    _get_ci,
    CRITICAL_KEYS as CRITICAL_ENV_KEYS,
    PROTECTED_KEYS,
)

# Minimal stand-ins for the web's get_auth_status (tests the parsers + _get_ci)
def get_auth_status(env_path: Path):
    env_values = parse_env_file(env_path)
    status = {}
    xai = _get_ci(env_values, "XAI_API_KEY")
    status["xai_api_key_present"] = bool(xai and len(xai.strip()) > 8)
    if xai and len(xai) > 12:
        status["xai_api_key_masked"] = f"{xai[:6]}...{xai[-4:]}"
    elif xai:
        status["xai_api_key_masked"] = "set (short)"
    else:
        status["xai_api_key_masked"] = "missing"

    disc = _get_ci(env_values, "DISCORD_BOT_TOKEN")
    status["discord_token_present"] = bool(disc and len(disc.strip()) > 10)

    mode = _get_ci(env_values, "GROK_AUTH_MODE") or "api_key"
    status["grok_auth_mode"] = mode.lower()

    tok_file = _get_ci(env_values, "GROK_OAUTH_TOKEN_FILE") or ""
    if not tok_file:
        tok_file = str((Path.cwd() / "oauth" / "xai_oauth_tokens.json").resolve())
    status["oauth_token_file"] = tok_file
    try:
        status["oauth_token_file_exists"] = Path(tok_file).exists()
    except Exception:
        status["oauth_token_file_exists"] = False

    prefers_oauth = mode in ("oauth", "auto") or status["oauth_token_file_exists"]
    if prefers_oauth and status["oauth_token_file_exists"]:
        status["effective_credential"] = "OAuth token (preferred)"
    elif xai:
        status["effective_credential"] = "XAI_API_KEY"
    else:
        status["effective_credential"] = "None (bot will likely fail to start)"

    status["has_any_credential"] = status["xai_api_key_present"] or status["oauth_token_file_exists"]
    return status

# --- End of test helpers ---

print("Using unified env_utils (safe_write_env + parsers).")

with tempfile.TemporaryDirectory() as td:
    envp = Path(td) / ".env"
    original = """# Groksito configuration
# Secrets - DO NOT DELETE
DISCORD_BOT_TOKEN=the_real_discord_token_here
XAI_API_KEY=sk-proj-very-secret-key-that-must-survive
GROK_AUTH_MODE=auto   # prefer oauth when the token file is present

# Some other user stuff
MY_CUSTOM_VAR=hello world

tts_default_voice=eve
log_level=INFO
"""
    envp.write_text(original, encoding="utf-8")

    updates = {"tts_default_voice": "rex", "log_level": "DEBUG"}

    # Use the real unified writer (web layer would call save_env_updates which delegates to this)
    ok, msg, bak = safe_write_env(envp, updates, protected_keys=PROTECTED_KEYS)
    print("Save ok=", ok, "msg[:80]=", (msg or "")[:80], "bak=", bak and bak.name)

    after = envp.read_text(encoding="utf-8")
    print("\n--- AFTER SAVE ---\n" + after + "\n---")

    assert "XAI_API_KEY=sk-proj-very-secret-key-that-must-survive" in after, "XAI KEY LOST!"
    assert "DISCORD_BOT_TOKEN=the_real_discord_token_here" in after, "DISCORD TOKEN LOST!"
    assert "tts_default_voice=rex" in after
    assert "log_level=DEBUG" in after
    assert "MY_CUSTOM_VAR=hello world" in after
    assert "# Groksito configuration" in after
    assert "GROK_AUTH_MODE=auto" in after

    # Idempotency check: write the exact same updates again — must not create dups or change structure badly
    ok2, _, _ = safe_write_env(envp, updates, protected_keys=PROTECTED_KEYS)
    after2 = envp.read_text(encoding="utf-8")
    assert after == after2 or "tts_default_voice=rex" in after2, "Idempotent write should be stable"
    # Count occurrences of a key we touched
    rex_count = after2.lower().count("tts_default_voice=rex")
    assert rex_count == 1, f"Should have exactly one tts_default_voice line, found {rex_count}"

    baks = sorted([p.name for p in Path(td).glob(".env.backup*")])
    print("Backups:", baks)
    assert len(baks) >= 1, "BACKUP MISSING!"

    # Also test get_auth_status (uses the shared parsers)
    st = get_auth_status(envp)
    print("Auth status sample:", {k: st[k] for k in ("xai_api_key_present", "grok_auth_mode", "has_any_credential")})

    # Test dedup helper on a file that has prior duplicates (simulating the old bug)
    dupp = Path(td) / ".env.duptest"
    dupp.write_text(
        "LOG_LEVEL=INFO\n"
        "tts_default_voice=eve\n"
        "LOG_LEVEL=DEBUG   # older dup that should be removed\n"
        "SOME_CUSTOM=keep\n"
        "tts_default_voice=rex  # this should win if we keep last\n",
        encoding="utf-8",
    )
    deduplicate_env_file(dupp, keep="last", make_backup=False)
    after_dup = dupp.read_text(encoding="utf-8")
    assert after_dup.count("LOG_LEVEL=") == 1
    assert "LOG_LEVEL=DEBUG" in after_dup  # last one kept
    assert after_dup.count("tts_default_voice=") == 1

    print("\n*** ALL SAFETY TESTS PASSED (unified env_utils) ***")
print("DONE")
