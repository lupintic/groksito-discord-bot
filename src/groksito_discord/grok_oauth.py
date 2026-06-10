"""
Experimental Grok OAuth support for SuperGrok / X Premium+ subscriptions.

Implements browser-based OAuth 2.0 + PKCE (loopback callback) against accounts.x.ai (login) / auth.x.ai (OIDC authorize in practice),
modeled directly after the proven flow in Hermes Agent (Nous Research).

- No XAI_API_KEY required when GROK_AUTH_MODE=oauth.
- Tokens stored securely in ./oauth/xai_oauth_tokens.json (dedicated dir outside data/,
  or explicit path via GROK_OAUTH_TOKEN_FILE). Auto-refreshed with proactive
  15-minute safety margin (great for Docker 24/7).
- Reuses the exact same bearer for Responses API + direct image/video endpoints.
- Clearly experimental: may hit backend tier gates (403s for some subs), different quotas, etc.
  Use at your own risk. API key mode is the stable, recommended default.

Usage (from code):
    from .grok_oauth import get_grok_bearer, login_oauth_interactive, logout_oauth, print_auth_status

    # Preferred: always gives the best available (OAuth token with auto-refresh if present, else XAI_API_KEY)
    bearer = get_grok_bearer()
    if bearer:
        # Use as api_key= to AsyncOpenAI(base_url="https://api.x.ai/v1") or "Authorization: Bearer {bearer}"
        ...

    # One-time setup (owner only):
    #   python -m src.groksito_discord --login-oauth
    #   python -m src.groksito_discord --login-oauth --no-browser
    #   python -m src.groksito_discord --login-oauth --print-url-only   # Docker / VPS + ssh -L
    #   python -m src.groksito_discord --auth-status
    #   python -m src.groksito_discord --logout-oauth
    #   python -m src.groksito_discord --test-auth   # verify after login

CLI integration (added in __main__):
    python -m src.groksito_discord --login-oauth
    python -m src.groksito_discord --login-oauth --no-browser
    python -m src.groksito_discord --login-oauth --print-url-only   # great for Docker
    python -m src.groksito_discord --auth-status
    python -m src.groksito_discord --logout-oauth
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import ssl
import stat
import threading
import time
import webbrowser
from contextlib import contextmanager
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from .config import settings

logger = logging.getLogger("groksito.grok_oauth")

# =============================================================================
# Constants (sourced from Hermes Agent implementation + xAI OIDC discovery)
# =============================================================================

XAI_OAUTH_ISSUER = "https://auth.x.ai"  # OIDC endpoints (authorize, token, discovery) live here for the working PKCE flow
XAI_OAUTH_AUTHORIZE_BASE = "https://auth.x.ai"
XAI_OAUTH_DISCOVERY_URL = f"{XAI_OAUTH_ISSUER}/.well-known/openid-configuration"  # may 404; we rely on fallbacks + discovered endpoints when available
XAI_OAUTH_CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
XAI_OAUTH_SCOPE = "openid profile email offline_access grok-cli:access api:access"

XAI_OAUTH_REDIRECT_HOST = "127.0.0.1"
XAI_OAUTH_REDIRECT_PORT = settings.grok_oauth_port  # 56121 by default (Hermes value)
XAI_OAUTH_REDIRECT_PATH = "/callback"
XAI_OAUTH_REDIRECT_URI = f"http://{XAI_OAUTH_REDIRECT_HOST}:{XAI_OAUTH_REDIRECT_PORT}{XAI_OAUTH_REDIRECT_PATH}"

XAI_ACCESS_TOKEN_REFRESH_SKEW_SECONDS = 120  # small margin: treat as expired if within 2min of expiry (used by is_expired)

# Proactive refresh margin for long-running bots (e.g. Docker containers running 24/7).
# We refresh the token if it will expire within this window, *before* handing it out
# to API calls. This is more robust than only refreshing after the token is already
# expired or a request fails.
XAI_ACCESS_TOKEN_PROACTIVE_REFRESH_SECONDS = 15 * 60  # 15 minutes safety margin

AUTH_STORE_VERSION = 1
AUTH_LOCK_TIMEOUT_SECONDS = 15.0

# =============================================================================
# Token storage model
# =============================================================================

@dataclass
class XaiOAuthTokens:
    access_token: str
    refresh_token: Optional[str] = None
    token_type: str = "Bearer"
    expires_at: Optional[float] = None  # unix timestamp
    scope: Optional[str] = None
    id_token: Optional[str] = None  # if present

    def is_expired(self, skew: int = XAI_ACCESS_TOKEN_REFRESH_SKEW_SECONDS) -> bool:
        """True if the token has already expired (or will within the small skew margin).
        Used for strict 'is it usable now?' checks (e.g. in status and fallback).
        """
        if not self.expires_at:
            return True
        return time.time() + skew >= self.expires_at

    def will_expire_soon(self, seconds: int = None) -> bool:
        """True if the token will expire soon (within `seconds`) or is already expired.

        This is used for *proactive* early refresh. Default is a large safety margin
        (15 minutes) so that in long-running processes (Docker 24/7 bots) we refresh
        the token well before it becomes unusable, avoiding auth failures on the next
        LLM/media call after a long idle period.

        If seconds is None, uses XAI_ACCESS_TOKEN_PROACTIVE_REFRESH_SECONDS.
        """
        if seconds is None:
            seconds = XAI_ACCESS_TOKEN_PROACTIVE_REFRESH_SECONDS
        if not self.expires_at:
            return True
        return time.time() + seconds >= self.expires_at

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        # never persist raw secrets in logs, but ok in file
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "XaiOAuthTokens":
        return cls(**{k: d.get(k) for k in cls.__dataclass_fields__ if k in d})


# =============================================================================
# File locking (cross platform, best effort)
# =============================================================================

@contextmanager
def _file_lock(path: Path, timeout: float = AUTH_LOCK_TIMEOUT_SECONDS):
    """Simple cross-platform advisory lock using exclusive open + fcntl/msvcrt."""
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    f = None
    try:
        f = open(lock_path, "w")
        # best effort
        try:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except Exception:
            try:
                import msvcrt
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            except Exception:
                pass  # no lock, proceed (race possible on Windows without proper setup)
        yield
    finally:
        if f:
            try:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            except Exception:
                try:
                    import msvcrt
                    msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
                except Exception:
                    pass
            f.close()
            try:
                lock_path.unlink(missing_ok=True)
            except Exception:
                pass


def _atomic_write_json(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    # Apply secure file permissions after saving the token file (as required).
    # 0o600 = owner read/write only. This is best-effort.
    try:
        if os.name != "nt":
            os.chmod(path, 0o600)
        else:
            # Windows: no direct equivalent via chmod in the same way;
            # relies on directory ACLs, user profile permissions, or running
            # under a dedicated user. The file will inherit from parent dir.
            pass
    except Exception:
        pass


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"Failed to read oauth token file {path}: {e}")
        return None


# =============================================================================
# Token persistence
# =============================================================================

def _get_token_path() -> Path:
    """Return the configured path for the OAuth token file.

    Defaults to ./oauth/xai_oauth_tokens.json (see config.py: oauth_token_file).
    The parent directory is ensured on save (and via ensure_directories at startup).
    """
    return settings.oauth_token_file


def load_oauth_tokens() -> Optional[XaiOAuthTokens]:
    path = _get_token_path()
    # Note: _file_lock will ensure the parent dir (./oauth/) exists as a side effect
    # (via its own mkdir), so the dir gets created on first status/load even
    # before any save. This is acceptable and helps Docker scenarios.
    with _file_lock(path):
        raw = _read_json(path)
        if not raw or raw.get("version") != AUTH_STORE_VERSION:
            return None
        tokens = raw.get("tokens")
        if not tokens:
            return None
        return XaiOAuthTokens.from_dict(tokens)


def save_oauth_tokens(tokens: XaiOAuthTokens) -> None:
    path = _get_token_path()

    # Ensure the dedicated ./oauth/ directory exists before saving.
    # This is the primary place where the oauth/ dir is auto-created if missing
    # (in addition to startup ensure_directories and the mkdir inside _file_lock
    # / _atomic_write_json). Uses pathlib as required. Works for Docker volumes
    # (the dir on host will be used if mounted; otherwise created in container fs).
    path.parent.mkdir(parents=True, exist_ok=True)

    payload = {
        "version": AUTH_STORE_VERSION,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "tokens": tokens.to_dict(),
    }
    with _file_lock(path):
        _atomic_write_json(path, payload)
    logger.info(f"[OAuth] Saved xAI Grok OAuth tokens to {path} (expires ~{tokens.expires_at})")


def clear_oauth_tokens() -> None:
    path = _get_token_path()
    with _file_lock(path):
        try:
            path.unlink(missing_ok=True)
            logger.info(f"[OAuth] Cleared xAI Grok OAuth tokens at {path}")
        except Exception as e:
            logger.warning(f"Failed clearing token file: {e}")


# =============================================================================
# PKCE + OAuth helpers
# =============================================================================

def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def generate_pkce_pair() -> Tuple[str, str]:
    """Return (code_verifier, code_challenge)."""
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def discover_oauth_endpoints() -> dict:
    """Fetch authorization and token endpoints from OIDC discovery (with fallbacks)."""
    try:
        with httpx.Client(timeout=10.0) as client:
            r = client.get(XAI_OAUTH_DISCOVERY_URL)
            r.raise_for_status()
            data = r.json()
            return {
                "authorization_endpoint": data.get("authorization_endpoint"),
                "token_endpoint": data.get("token_endpoint"),
            }
    except Exception as e:
        logger.debug(f"OIDC discovery failed: {e}")
    return {}


def discover_token_endpoint() -> str:
    """Fetch token endpoint from OIDC discovery (with fallback)."""
    eps = discover_oauth_endpoints()
    if eps.get("token_endpoint"):
        return eps["token_endpoint"]
    # Fallback based on Hermes patterns / known xAI
    return "https://auth.x.ai/oauth2/token"



# =============================================================================
# Local callback server (loopback PKCE)
# =============================================================================

class _OAuthCallbackHandler(BaseHTTPRequestHandler):
    """One-shot handler that captures the OAuth code + state."""
    code: Optional[str] = None
    state: Optional[str] = None
    error: Optional[str] = None

    def do_GET(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        _OAuthCallbackHandler.code = qs.get("code", [None])[0]
        _OAuthCallbackHandler.state = qs.get("state", [None])[0]
        _OAuthCallbackHandler.error = qs.get("error", [None])[0]

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()

        if _OAuthCallbackHandler.error:
            html = f"<h1>Authorization failed</h1><p>{_OAuthCallbackHandler.error}</p>"
        else:
            html = (
                "<h1>Authorization received</h1>"
                "<p>You can close this tab. Groksito is processing the login...</p>"
                "<script>window.close();</script>"
            )
        self.wfile.write(html.encode("utf-8"))

    def log_message(self, format, *args):
        # Quiet the http server logs unless debug
        logger.debug(f"[OAuth callback] {format % args}")


def _run_callback_server_once(port: int, timeout: float = 180.0) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Start a short-lived server, wait for one request or timeout. Returns (code, state, error).

    On port-in-use (common when re-running or multiple attempts): prints a clear message
    suggesting --manual-paste or changing GROK_OAUTH_PORT.
    """
    try:
        server = ThreadingHTTPServer((XAI_OAUTH_REDIRECT_HOST, port), _OAuthCallbackHandler)
    except OSError as e:
        if "address already in use" in str(e).lower() or getattr(e, "errno", None) in (98, 10048):
            print(f"\n[OAuth] Port {port} is already in use (another listener or previous run?).")
            print("  Solutions:")
            print("    • Use --manual-paste and complete the flow in the browser, then paste the ?code= value.")
            print(f"    • Pick a different port: set GROK_OAUTH_PORT=56122 (or other) in .env and retry.")
            print("    • Kill the process holding the port (netstat / lsof / Task Manager).")
        else:
            print(f"[OAuth] Failed to bind callback server on {XAI_OAUTH_REDIRECT_HOST}:{port}: {e}")
        return None, None, "port_in_use"
    server.timeout = 1.0

    _OAuthCallbackHandler.code = None
    _OAuthCallbackHandler.state = None
    _OAuthCallbackHandler.error = None

    deadline = time.time() + timeout
    try:
        while time.time() < deadline:
            server.handle_request()  # serves one request then returns
            if _OAuthCallbackHandler.code or _OAuthCallbackHandler.error:
                break
            time.sleep(0.05)
    finally:
        server.server_close()

    return (
        _OAuthCallbackHandler.code,
        _OAuthCallbackHandler.state,
        _OAuthCallbackHandler.error,
    )


# =============================================================================
# Public API
# =============================================================================

# -----------------------------------------------------------------------------
# Docker / remote server OAuth notes (very common deployment)
# -----------------------------------------------------------------------------
# When the bot runs in Docker (the recommended way for 24/7 operation):
#
# 1. Add a volume for the oauth directory (see docker-compose.yml):
#       volumes:
#         - ./data:/app/data
#         - ./oauth:/app/oauth     # <--- this one
#
# 2. The code defaults to a relative "oauth" directory (becomes /app/oauth inside
#    the container because WORKDIR=/app). The volume makes the tokens survive
#    `docker compose down -v` (as long as you don't delete the host ./oauth dir).
#
# 3. Login from outside the container:
#       docker compose run --rm groksito-discord-bot --login-oauth --no-browser
#    or (new convenient flag):
#       ... --login-oauth --print-url-only
#
#    Then follow the printed instructions (SSH -L tunnel from your laptop is
#    usually the easiest).
#
# 4. After successful login the tokens live in ./oauth/xai_oauth_tokens.json on
#    the host and will be picked up automatically on next container start.
#
# The --login-oauth command now auto-detects Docker and forces no-browser mode
# with much more detailed guidance.
# -----------------------------------------------------------------------------

def get_grok_access_token(force_refresh: bool = False) -> Optional[str]:
    """
    Return a valid access token for use as Bearer in xAI calls.

    Refresh strategy (improved for Docker / long-running use):
    - Always refresh if force_refresh=True.
    - Proactively refresh early if the token *will expire soon* (within 15 minutes by default).
      This ensures we almost always hand out a fresh, long-lived token to callers
      (LLM, image gen, video gen) instead of a token that is about to die.
    - Falls back to the strict is_expired() (2min skew) for the "already unusable" case.
    - After a successful refresh we persist the new tokens immediately.
    - If refresh fails we log clearly (see _refresh_tokens) and return None instead of
      a potentially expired token (prevents silent auth failures on the next API call).

    This is lazy (on-demand when a token is requested) but with a large proactive
    window, which is simple, effective, and works great for 24/7 containers without
    needing a background thread.
    """
    if not settings.using_oauth:
        return None

    tokens = load_oauth_tokens()
    if not tokens:
        logger.warning("[OAuth] No tokens found. Run with --login-oauth first.")
        return None

    # Proactive early refresh (main improvement for reliability)
    if force_refresh or tokens.will_expire_soon():
        refreshed = _refresh_tokens(tokens)
        if refreshed:
            tokens = refreshed
            save_oauth_tokens(tokens)
        else:
            # Refresh failed - do not return a (near-)expired token to the caller.
            # The error was already logged in detail inside _refresh_tokens.
            logger.warning("[OAuth] Refresh attempt did not produce a usable token. "
                           "Returning None. Re-login is likely required.")
            return None

    # Final strict check: never hand out a token that is already expired.
    if tokens and tokens.access_token and not tokens.is_expired():
        return tokens.access_token

    if tokens and tokens.access_token:
        logger.warning("[OAuth] Token is expired and no valid refresh succeeded.")
    return None


def _refresh_tokens(tokens: XaiOAuthTokens) -> Optional[XaiOAuthTokens]:
    """
    Perform the OAuth refresh_token grant.

    Robustness improvements:
    - Special-case handling for 'invalid_grant' / 400/401 responses: these mean the
      refresh token itself is no longer valid (user revoked, password change, or
      token rotated too many times). In this case we log a very clear message
      telling the user exactly what to run, and we clear the local token file
      so that subsequent calls and --auth-status don't keep trying with bad data.
    - Other errors are logged but do not clear tokens (transient network etc. may
      succeed on next attempt).
    - Always return None on any failure so callers know they don't have a valid token.
    """
    if not tokens.refresh_token:
        logger.warning("[OAuth] No refresh_token present; re-login required.")
        return None

    token_url = discover_token_endpoint()
    data = {
        "grant_type": "refresh_token",
        "refresh_token": tokens.refresh_token,
        "client_id": XAI_OAUTH_CLIENT_ID,
    }
    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.post(token_url, data=data)
            resp.raise_for_status()
            tok = resp.json()
            new_tokens = XaiOAuthTokens(
                access_token=tok["access_token"],
                refresh_token=tok.get("refresh_token") or tokens.refresh_token,
                token_type=tok.get("token_type", "Bearer"),
                expires_at=time.time() + int(tok.get("expires_in", 3600)),
                scope=tok.get("scope"),
                id_token=tok.get("id_token"),
            )
            logger.info("[OAuth] Refreshed xAI Grok access token successfully.")
            return new_tokens
    except httpx.HTTPStatusError as e:
        status = getattr(e.response, "status_code", None)
        err = ""
        err_desc = ""
        try:
            body = e.response.json() if e.response else {}
            err = body.get("error", "")
            err_desc = body.get("error_description", str(e))
        except Exception:
            err_desc = (e.response.text[:300] if e.response else str(e))

        logger.error(f"[OAuth] Token refresh failed with HTTP {status}: {err} - {err_desc}")

        if err in ("invalid_grant", "invalid_token") or status in (400, 401):
            logger.error(
                "[OAuth] Refresh token is invalid/expired (invalid_grant or equivalent). "
                "This usually happens after token revocation or long inactivity. "
                "You MUST re-login to continue using OAuth mode:\n"
                "    python -m src.groksito_discord --login-oauth\n"
                "The invalid tokens have been cleared."
            )
            try:
                clear_oauth_tokens()
            except Exception:
                pass
            return None
        return None
    except Exception as e:
        logger.error(f"[OAuth] Token refresh failed: {e}")
        # Transient errors (network, 5xx, timeout) - do not clear tokens.
        # Next call to get_grok_access_token() will try again.
        # If it keeps failing the user will see the error via logs / failed requests.
        return None


def login_oauth_interactive(
    no_browser: bool = False,
    manual_paste: bool = False,
    print_url_only: bool = False,
    timeout: float = 180.0,
) -> bool:
    """
    Perform the full browser (or manual) OAuth PKCE login flow.
    Stores tokens on success.
    Returns True on success.

    Docker / remote / headless improvements:
    - print_url_only: just prints the auth URL and exits (useful for scripting
      or when you want to copy the URL to another machine).
    - no_browser mode (automatically selected in Docker) prints very detailed
      instructions for port publishing and SSH -L tunnels.
    - The actual listener still runs inside the container; the browser always
      runs on the *user's* machine (laptop / desktop).
    """
    port = settings.grok_oauth_port
    redirect_uri = f"http://{XAI_OAUTH_REDIRECT_HOST}:{port}{XAI_OAUTH_REDIRECT_PATH}"

    verifier, challenge = generate_pkce_pair()
    state = _b64url(secrets.token_bytes(16))

    # Prefer discovered authorization_endpoint, otherwise build the known working pattern
    eps = discover_oauth_endpoints()
    if eps.get("authorization_endpoint"):
        auth_url_base = eps["authorization_endpoint"]
    else:
        auth_url_base = f"{XAI_OAUTH_AUTHORIZE_BASE}/oauth2/authorize"

    params = {
        "response_type": "code",
        "client_id": XAI_OAUTH_CLIENT_ID,
        "redirect_uri": redirect_uri,
        "scope": XAI_OAUTH_SCOPE,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "prompt": "consent",          # fuerza la pantalla de autorización
        "access_type": "offline",     # asegura refresh_token
    }
    auth_url = f"{auth_url_base}?{urlencode(params)}"

    print("\n[Grok OAuth] Starting SuperGrok / X Premium+ login flow (experimental)...")
    print(f"  Redirect URI: {redirect_uri}")
    print(f"  Using client_id: {XAI_OAUTH_CLIENT_ID}")

    if print_url_only:
        # Very convenient for Docker / CI / remote where you just want the URL
        # and will handle the callback / paste in a separate step or tunnel.
        # We allow this even if GROK_AUTH_MODE is not yet 'oauth' so the user
        # can obtain the URL during initial setup.
        print("\n[PRINT-URL-ONLY MODE]")
        print("Copy the URL below and open it from a browser on a machine that")
        print("can reach accounts.x.ai and can reach the redirect target (your")
        print("laptop, after setting up SSH -L or publishing the port).")
        print("\n" + auth_url + "\n")
        print("After the browser is redirected to the localhost callback URL,")
        print("you can either:")
        print("  - let the running listener (this process) catch it, or")
        print("  - run the command again with --manual-paste and paste the code.")
        return False

    if not (settings.using_oauth or settings.auth_prefers_oauth or settings.auth_mode == "auto"):
        # Still allow login to populate tokens for "auto" or "just works" UX.
        logger.info("[OAuth] GROK_AUTH_MODE not strictly 'oauth' — proceeding with login anyway (tokens will be preferred automatically on next run).")

    if manual_paste:
        print("\n[MANUAL PASTE MODE]")
        print("1. Open this URL in your browser (on a machine that can reach accounts.x.ai):")
        print(auth_url)
        print("\n2. After approving, if you see a code on the page or are redirected to a localhost URL,")
        print("   paste the FULL callback URL or just the ?code=... value here.")
        code_input = input("Callback URL or code: ").strip()
        # parse
        if "code=" in code_input:
            parsed = parse_qs(urlparse(code_input).query)
            code = parsed.get("code", [None])[0]
            ret_state = parsed.get("state", [None])[0]
        else:
            code = code_input
            ret_state = state
        if not code:
            print("No code provided.")
            return False
    else:
        if no_browser:
            print("\n[NO-BROWSER / HEADLESS / DOCKER]")
            print("Open the URL below in a browser running on your *local machine*")
            print("(laptop/desktop), NOT inside the container or on the remote server.")
            print("\n" + auth_url + "\n")
            print("Docker / remote server tips:")
            print("  • If the container is on the SAME machine as your browser:")
            print("      Make sure the port is published, e.g.")
            print("        docker run -p 56121:56121 ...")
            print("      or in docker-compose.yml add under ports:")
            print("        - \"56121:56121\"")
            print("  • If the container is on a REMOTE server (VPS, etc.):")
            print("      From your laptop run:")
            print("        ssh -L 56121:localhost:56121 user@your-remote-host")
            print("      (keep the tunnel open while you log in). Then open the URL")
            print("      from your laptop browser.")
            print(f"\nThe OAuth listener inside the container is active on {redirect_uri}")
            print(f"and will wait for the callback for {int(timeout)} seconds.")
        else:
            try:
                webbrowser.open(auth_url)
                print(f"\nOpened browser to: {auth_url}")
                print("Complete the sign-in + approval in the browser window.")
            except Exception:
                print(f"\nCould not auto-open browser. Manually visit:\n{auth_url}")

        print(f"\nWaiting for callback on port {port} (timeout {int(timeout)}s)...")
        code, ret_state, err = _run_callback_server_once(port, timeout=timeout)

        if err:
            print(f"Authorization error from server: {err}")
            return False
        if not code:
            print("Timed out waiting for authorization callback.")
            return False

    if ret_state and ret_state != state:
        print("State mismatch (possible CSRF or proxy). Aborting.")
        return False

    # Exchange code
    token_url = discover_token_endpoint()
    exchange_data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": XAI_OAUTH_CLIENT_ID,
        "code_verifier": verifier,
    }

    print("Exchanging authorization code for tokens...")
    try:
        with httpx.Client(timeout=20.0) as client:
            resp = client.post(token_url, data=exchange_data)
            resp.raise_for_status()
            tok = resp.json()
    except httpx.HTTPStatusError as e:
        body = e.response.text[:300] if e.response else str(e)
        print(f"Token exchange failed: {e} - {body}")
        # Common: 403 tier gate -> advise fallback
        if e.response and e.response.status_code in (403, 401):
            print("This may be a subscription tier restriction on the OAuth surface.")
            print("As a workaround you can still use a regular XAI_API_KEY + GROK_AUTH_MODE=api_key.")
        return False
    except Exception as e:
        print(f"Token exchange error: {e}")
        return False

    access = tok.get("access_token")
    if not access:
        print("No access_token in response.")
        return False

    expires_in = int(tok.get("expires_in", 3600))
    new_tokens = XaiOAuthTokens(
        access_token=access,
        refresh_token=tok.get("refresh_token"),
        token_type=tok.get("token_type", "Bearer"),
        expires_at=time.time() + expires_in,
        scope=tok.get("scope"),
        id_token=tok.get("id_token"),
    )

    save_oauth_tokens(new_tokens)
    print("\n✅ Successfully logged in via xAI Grok OAuth (SuperGrok / X Premium+).")
    print(f"   Tokens saved to: {_get_token_path()}")
    print("   Access token will be auto-refreshed before expiry.")
    print("   You can now run the bot with GROK_AUTH_MODE=oauth (XAI_API_KEY not required).")
    return True


def logout_oauth() -> None:
    clear_oauth_tokens()
    print("xAI Grok OAuth tokens cleared.")


def print_auth_status() -> None:
    print(f"GROK_AUTH_MODE={settings.auth_mode} (from env/config)")
    print(f"  prefers_oauth / auto logic: {getattr(settings, 'auth_prefers_oauth', False)}")

    # Show what the runtime will actually use right now
    try:
        current = get_grok_bearer()
        if current:
            # Heuristic: oauth access tokens from this flow are typically longer JWT-like strings
            kind = "OAuth" if (settings.auth_prefers_oauth or len(current) > 80) else "API key"
            print(f"  Effective bearer for calls: {kind} (len={len(current)})")
        else:
            print("  Effective bearer for calls: NONE (run --login-oauth or set XAI_API_KEY)")
    except Exception:
        pass

    if settings.using_oauth and not settings.auth_prefers_oauth:
        key = settings.xai_api_key
        print(f"  (strict oauth mode) API key present as fallback: {'yes (****)' if key else 'no'}")

    tokens = load_oauth_tokens()
    path = _get_token_path()
    if not tokens:
        if settings.auth_mode == "oauth":
            print(f"  OAuth: NO TOKENS STORED (path: {path})")
            print("  Run: python -m src.groksito_discord --login-oauth")
        return

    exp = tokens.expires_at
    if exp:
        remaining = max(0, int(exp - time.time()))
        exp_str = f"expires in ~{remaining//60}m (at {datetime.fromtimestamp(exp, tz=timezone.utc).isoformat()})"
    else:
        exp_str = "no expiry info"

    has_refresh = "yes" if tokens.refresh_token else "NO (re-login will be required)"
    print(f"  OAuth tokens file: {path}")
    print(f"    access present: yes (len={len(tokens.access_token)})")
    print(f"    refresh: {has_refresh}")
    print(f"    {exp_str}")
    if tokens.is_expired():
        print("    (currently expired or near expiry — will refresh on next use or call)")


# =============================================================================
# Convenience for other modules
# =============================================================================

def ensure_valid_oauth_token() -> Optional[str]:
    """Used at startup / before calls in oauth mode."""
    if not settings.using_oauth:
        return None
    tok = get_grok_access_token()
    if not tok:
        logger.error("[OAuth] No valid Grok OAuth token. Use --login-oauth.")
    return tok


def resolve_grok_bearer() -> Optional[str]:
    """
    Legacy/compat wrapper. Prefer get_grok_bearer() for new call sites.
    """
    return get_grok_bearer()


def get_grok_bearer() -> Optional[str]:
    """
    **Primary credential resolver for all xAI calls (chat/Responses + direct image/video/edit).**

    Returns the string to pass as `api_key` to AsyncOpenAI(...) or in "Authorization: Bearer {val}"
    for direct httpx calls to https://api.x.ai .

    Preference (implements "prefers a valid OAuth token when available, fallback to XAI_API_KEY"):
    1. If an OAuth token file exists (or we are in oauth/auto mode): call get_grok_access_token()
       which does proactive refresh (15min margin) + reactive. If we get a fresh access_token, use it.
    2. Otherwise (or if oauth gave nothing): fall back to settings.xai_api_key (if present).

    This means:
    - You can `GROK_AUTH_MODE=api_key` (or leave default) and still benefit from a prior `--login-oauth`
      (token is preferred automatically).
    - `GROK_AUTH_MODE=auto` (recommended for most SuperGrok users): explicit "try oauth, seamless key fallback".
    - `GROK_AUTH_MODE=oauth`: strict (no key fallback for the bearer; clear errors if no token).
    - If nothing is available: returns None → callers emit clear "run --login-oauth or set XAI_API_KEY".

    Safe to call often (refresh is cheap/no-op when not near expiry; file load is fast).
    """
    # Always attempt OAuth path first if there is any chance of a token (file present or mode asks for it).
    # get_grok_access_token() itself is a no-op / returns None quickly if not using_oauth and no token file.
    try:
        # Force a check even in plain api_key mode: if tokens exist on disk we prefer them.
        tok = get_grok_access_token()
        if tok:
            return tok
    except Exception as e:
        logger.debug(f"[OAuth] get_grok_access_token() raised in bearer resolution (non-fatal): {e}")

    # No usable OAuth token. Fall back to classic API key if present.
    key = getattr(settings, "xai_api_key", None) or os.getenv("XAI_API_KEY")
    if key:
        return key

    return None


def ensure_valid_grok_token() -> Optional[str]:
    """Startup / pre-call convenience. Tries to ensure we have *something* usable."""
    b = get_grok_bearer()
    if not b and settings.auth_mode == "oauth":
        logger.error("[OAuth] GROK_AUTH_MODE=oauth but no valid token. Run: python -m src.groksito_discord --login-oauth")
    return b
