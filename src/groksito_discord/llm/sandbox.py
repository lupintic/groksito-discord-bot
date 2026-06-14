"""
Sandbox execution helpers for skill-only power tools (code_execution, playwright_browser).

These are intentionally isolated because:
- They require Docker in the runtime environment.
- They are high-privilege (arbitrary code / browser automation) and are *only* made available
  when an approved skill explicitly lists them in its allowed_tools.
- They have special security/timeout/resource concerns.

Deployment:
- In docker-compose.yml, mount the host Docker socket (commented example provided).
- The official Dockerfile now includes the Docker CLI.
- See health check output ("Docker for skill sandboxes") and compose comments for setup.
- Without the socket: graceful fallback to simulation messages (see _run_docker_command).

This module is imported lazily from tools.py only when one of the sandbox tools is actually dispatched.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shlex
from typing import Any

logger = logging.getLogger("groksito.tools.sandbox")


async def _run_docker_command(cmd: list[str], timeout: float = 60.0) -> str:
    """Run a docker command with timeout and capture stdout+stderr.
    
    Returns a string that is safe to feed back to the model (includes [stderr], [exit=N] markers on failure).
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        out = stdout.decode("utf-8", errors="ignore").strip()
        err = stderr.decode("utf-8", errors="ignore").strip()
        result = out
        if err:
            result += f"\n[stderr]{err[:400]}"
        if proc.returncode != 0:
            result += f"\n[exit={proc.returncode}]"
        return result or "[no output from container]"
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        return "[timeout in sandbox]"
    except FileNotFoundError:
        return "[docker not available in this environment - falling back to simulation]"
    except PermissionError:
        return "[docker permission error - the bot process may need to run as a user in the 'docker' group or have the docker socket mounted]"
    except Exception as e:
        return f"[docker run error] {type(e).__name__}: {str(e)[:200]}"


async def run_code_execution(code: str, timeout_seconds: float = 30.0) -> str:
    """Execute a snippet of Python inside a tightly limited docker container.
    
    Only intended to be called from execute_hybrid_tool when a skill has explicitly allowed 'code_execution'.
    """
    code = str(code or "")[:10000]
    timeout = float(timeout_seconds or 30)

    try:
        wrapped = f'''
import sys
import io
old_stdout = sys.stdout
sys.stdout = mystdout = io.StringIO()
code = {repr(code)}
try:
    result = eval(code)
    if result is not None:
        print(repr(result))
except Exception:
    try:
        exec(code)
    except Exception as e:
        print(f"EXEC_ERROR: {{type(e).__name__}}: {{e}}", file=sys.stderr)
sys.stdout = old_stdout
print(mystdout.getvalue().strip())
'''
        install_cmd = "pip install --no-cache-dir --quiet requests beautifulsoup4 lxml 2>/dev/null || true; "
        shell_cmd = install_cmd + f"python -c {shlex.quote(wrapped)}"
        cmd = [
            "docker", "run", "--rm", "-i",
            "--memory=256m", "--cpus=0.5",
            "--pids-limit=64",
            "--read-only",
            "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m",
            "python:3.11-slim",
            "sh", "-c", shell_cmd
        ]
        return await _run_docker_command(cmd, timeout=timeout)
    except Exception as e:
        return f"[code_execution docker error] {type(e).__name__}: {str(e)[:300]}"


async def run_playwright_browser(
    *,
    url: str,
    action: str = "extract_text",
    instructions: str = "",
    selector: str = "",
    timeout_ms: int = 30000,
) -> str:
    """Run a headless browser action inside the official Playwright docker image.
    
    Security model: fresh context every call, limited resources, no host FS, actions driven strictly
    by the (approved) skill's instructions.
    
    This implementation uses an environment variable to pass parameters into the container
    instead of dangerous f-string interpolation into the executed Python source. This fixes
    quoting / injection risks from model-controlled url/selector values.
    """
    url = str(url or "").strip()
    action = str(action or "extract_text").lower()
    instructions = str(instructions or "").strip()[:500]
    selector = str(selector or "").strip()
    timeout_sec = max(5.0, float(timeout_ms or 30000) / 1000.0)

    if not url:
        return "[playwright error] url is required"

    # Safe config passing via env var (JSON). The snippet inside the container reads it.
    config = {
        "url": url,
        "action": action,
        "instructions": instructions,
        "selector": selector,
        "timeout_ms": int(timeout_sec * 1000),
    }
    config_json = json.dumps(config, ensure_ascii=False)

    # The snippet is now static (no host-side interpolation of untrusted values into source).
    # It reads the config from PW_CONFIG env var.
    pw_snippet = '''
import json
import os
import sys
from playwright.sync_api import sync_playwright

cfg = json.loads(os.environ.get("PW_CONFIG", "{}"))
url = cfg.get("url", "")
action = cfg.get("action", "extract_text")
selector = cfg.get("selector", "")
timeout_ms = int(cfg.get("timeout_ms", 30000))

try:
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
        )
        context = browser.new_context(
            viewport={"width": 1280, "height": 720},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = context.new_page()
        page.set_default_timeout(timeout_ms)
        page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass

        content = ""
        if action in ("extract_text", "navigate", "extract"):
            if selector:
                try:
                    content = page.locator(selector).inner_text(timeout=5000)
                except Exception:
                    content = page.inner_text("body") or page.content()
            else:
                content = page.inner_text("body") or page.content()
        elif action == "extract_html":
            if selector:
                try:
                    content = page.locator(selector).inner_html(timeout=5000)
                except Exception:
                    content = page.content()
            else:
                content = page.content()
        else:
            content = page.inner_text("body") or page.content()

        print((content or "")[:15000])
        browser.close()
except Exception as e:
    print(f"PLAYWRIGHT_ERROR: {type(e).__name__}: {str(e)[:300]}", file=sys.stderr)
'''

    cmd = [
        "docker", "run", "--rm", "-i",
        "--memory=512m", "--cpus=1.0",
        "--pids-limit=100",
        "--tmpfs", "/tmp:rw",
        "-e", f"PW_CONFIG={config_json}",
        "mcr.microsoft.com/playwright/python:v1.44.0",
        "python", "-c", pw_snippet
    ]

    return await _run_docker_command(cmd, timeout=timeout_sec + 15)
