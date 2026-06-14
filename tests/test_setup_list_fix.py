import tempfile
import json
from pathlib import Path
import sys

# Prefer the real shared module (the single source of truth after the rework).
# Fall back to exec'ing setup.py only if needed (for its local fallback path).
SRC = Path(__file__).resolve().parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

try:
    from groksito_discord.utils.env_utils import (
        safe_write_env,
        _format_env_value,
        _format_list_for_display,
        parse_env_file,
    )
    print('Helpers loaded from src/groksito_discord/env_utils.py (unified)')
except Exception:
    # Legacy fallback for the test: pull names from setup.py's local definitions
    code = open('setup.py', encoding='utf-8').read()
    namespace = {}
    exec(code.split('def main() -> None:')[0], namespace)
    globals().update(namespace)
    print('Helpers loaded from setup.py (fallback path)')

# Test _format for list -> must be compact json for bare capture + pydantic
formatted_list = _format_env_value([463553669477171200, 316109851640201217])
print('json list compact:', repr(formatted_list))
assert formatted_list == '[463553669477171200,316109851640201217]', 'must be compact json array'

print('str normal:', repr(_format_env_value('foo bar')))

# Test tolerant display read (for prompt defaults from old or new .env)
print('display from json str:', repr(_format_list_for_display('[123,456]')))
print('display from comma:', repr(_format_list_for_display('123,456')))
print('display from quoted comma:', repr(_format_list_for_display('"123,456"')))
print('display from broken:', repr(_format_list_for_display('"[123, 456"')))

# Full sim of safe write including list + preserve secrets + upgrade format
with tempfile.TemporaryDirectory() as td:
    envp = Path(td) / '.env'
    original = '# test config\nALLOWED_GUILD_IDS=old,format\nXAI_API_KEY=sk-secret-123\nlog_level=INFO\n'
    envp.write_text(original, encoding='utf-8')

    # Simulate what setup collects: list for guilds
    updates = {
        'allowed_guild_ids': [111, 222],  # will be json dumped by format
        'log_level': 'DEBUG',
    }
    ok, msg, bak = safe_write_env(envp, updates)
    print('write ok:', ok, 'msg:', msg, 'bak:', bak and bak.name)

    content = envp.read_text(encoding='utf-8')
    print('--- resulting .env ---')
    print(repr(content))
    print('---')

    assert 'XAI_API_KEY=sk-secret-123' in content, 'secret not preserved!'
    # The writer preserves the *file's original key casing* on update.
    # The test file started with ALLOWED_GUILD_IDS (upper), so we expect that casing.
    assert 'ALLOWED_GUILD_IDS=[111,222]' in content or 'allowed_guild_ids=[111,222]' in content, 'guilds not written as compact json!'
    assert 'old,format' not in content, 'old guild value should have been replaced'
    assert 'log_level=DEBUG' in content
    assert '# test config' in content, 'comments not preserved'
    print('Simulated safe write with list field: SUCCESS')

print('All tests passed. The JSON error should be fixed.')
