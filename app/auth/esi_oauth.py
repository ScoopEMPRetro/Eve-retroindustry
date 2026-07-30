"""
EVE Online ESI OAuth2 PKCE flow for native/CLI applications.
Does not require a client_secret — uses PKCE (code_challenge).

Flow:
  1. Generate code_verifier + code_challenge
  2. Open browser → EVE login
  3. Start a local server on :5173 for the callback
  4. Exchange code + verifier for tokens
  5. Save tokens
"""
import os
import secrets
import hashlib
import base64
import socket
import sqlite3
import webbrowser
import threading
import urllib.parse
import json
import jwt
import httpx
from http.server import HTTPServer, BaseHTTPRequestHandler
from rich.console import Console

from app.auth.token_store import (
    save_tokens, save_client_id, get_client_id, ensure_characters_table,
)


def _open_conn() -> sqlite3.Connection:
    """Open a fresh SQLite connection to the app DB (used from OAuth callback thread)."""
    app_dir = os.environ.get("EVE_APP_DIR") or os.path.join(
        os.path.dirname(__file__), "..", ".."
    )
    conn = sqlite3.connect(os.path.join(app_dir, "eve_cache.db"), timeout=30.0)
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA journal_mode=WAL")
    except Exception:
        pass
    return conn

_login_lock = threading.Lock()

console = Console()

AUTH_URL      = "https://login.eveonline.com/v2/oauth/authorize"
TOKEN_URL     = "https://login.eveonline.com/v2/oauth/token"
CALLBACK_PORT = 5173
CALLBACK_URL  = f"http://localhost:{CALLBACK_PORT}/callback"

SCOPES = [
    # --- Blueprints and manufacturing ---
    "esi-characters.read_blueprints.v1",       # character blueprints (ME/TE, BPO/BPC)
    "esi-corporations.read_blueprints.v1",     # corporation blueprints
    "esi-industry.read_character_jobs.v1",     # active character industry jobs
    "esi-industry.read_corporation_jobs.v1",   # active corporation industry jobs
    "esi-industry.read_character_mining.v1",   # character mining ledger

    # --- Assets and inventory ---
    "esi-assets.read_assets.v1",               # character assets (materials at stations)
    "esi-assets.read_corporation_assets.v1",   # corporation assets

    # --- Space structures ---
    "esi-universe.read_structures.v1",         # player structure names (citadels)
    "esi-search.search_structures.v1",         # search structures by name

    # --- Market and finance ---
    "esi-wallet.read_character_wallet.v1",     # character ISK balance
    "esi-wallet.read_corporation_wallets.v1",  # corporation ISK balance
    "esi-markets.read_character_orders.v1",    # own market orders
    "esi-markets.read_corporation_orders.v1",  # corporation market orders
    "esi-markets.structure_markets.v1",        # markets in player structures (citadels)
    "esi-contracts.read_character_contracts.v1",   # character contracts
    "esi-contracts.read_corporation_contracts.v1", # corporation contracts

    # --- Skills ---
    "esi-skills.read_skills.v1",               # trained skills (affect manufacturing)
    "esi-skills.read_skillqueue.v1",           # skill queue

    # --- Location ---
    "esi-location.read_location.v1",           # current character location
    "esi-location.read_ship_type.v1",          # current ship

    # --- Planetary interaction (PI materials) ---
    "esi-planets.manage_planets.v1",           # PI colonies and extraction

    # --- Corporation ---
    "esi-corporations.read_facilities.v1",     # corporation industry facilities
    "esi-characters.read_corporation_roles.v1", # corporation roles
]


# ---------------------------------------------------------------------------
# PKCE helpers
# ---------------------------------------------------------------------------

def _pkce_pair() -> tuple[str, str]:
    verifier  = secrets.token_urlsafe(43)
    digest    = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


# ---------------------------------------------------------------------------
# Local callback server
# ---------------------------------------------------------------------------

class _CallbackHandler(BaseHTTPRequestHandler):
    code: str | None = None
    state: str | None = None

    redirect_to: str = "http://localhost:8000/auth/sync"

    def do_GET(self):
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        _CallbackHandler.code  = params.get("code",  [None])[0]
        _CallbackHandler.state = params.get("state", [None])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        # The browser tab stays on this page — no redirect to
        # localhost:8000/auth/sync, which would open our app in an external
        # browser instead of keeping it in the original window.
        # Meanwhile the webview polls /api/auth/status and redirects itself
        # to /auth/sync as soon as the character is saved.
        body = (
            "<!doctype html>"
            "<meta charset='utf-8'>"
            "<title>EVE Retroindustry — login complete</title>"
            "<style>"
            "  body { font-family: system-ui, sans-serif; background:#0d1117; "
            "         color:#c9d1d9; display:flex; align-items:center; "
            "         justify-content:center; height:100vh; margin:0 }"
            "  .card { background:#161b22; border:1px solid #30363d; "
            "          border-radius:8px; padding:2.5rem 3rem; text-align:center; "
            "          max-width:480px }"
            "  h2 { color:#e3b341; margin:0 0 .75rem }"
            "  p  { margin:.4rem 0; line-height:1.5 }"
            "  .small { color:#8b949e; font-size:.875rem }"
            "</style>"
            "<div class='card'>"
            "<h2>Login complete ✓</h2>"
            "<p>You can close this tab and return to the EVE Retroindustry window.</p>"
            "<p class='small'>The app has already received your authorization "
            "and is loading your character data.</p>"
            "<script>"
            "  // try to auto-close the tab (works only if window was opened "
            "  // by script with window.open) — falls back to staying open."
            "  setTimeout(() => { try { window.close(); } catch (e) {} }, 1500);"
            "</script>"
            "</div>"
        )
        self.wfile.write(body.encode())
        # serve_forever() won't stop the thread on its own — trigger shutdown
        # from another thread, otherwise we'd deadlock (shutdown waits for the
        # serve_forever loop to end, which is waiting on us).
        threading.Thread(target=self.server.shutdown, daemon=True).start()

    def log_message(self, *args):
        pass  # suppress HTTP logs


class _DualStackCallbackServer(HTTPServer):
    """Callback server that accepts BOTH IPv4 (127.0.0.1) and IPv6 (::1) loopback.

    EVE SSO redirects the browser to ``http://localhost:5173/callback``. On some
    machines the browser resolves ``localhost`` to ``::1`` (IPv6), while a plain
    ``HTTPServer(("localhost", ...))`` binds IPv4 only (127.0.0.1) — the redirect
    then hits a closed IPv6 port, the code never arrives, and the app waits
    forever. Binding a dual-stack IPv6 socket makes both loopback flavors reach
    us regardless of how the browser resolves ``localhost``.
    """
    address_family = socket.AF_INET6

    def server_bind(self):
        # Turn OFF v6-only so IPv4-mapped addresses (127.0.0.1) are accepted too.
        # Must be set before bind(); ignore if the platform lacks the option.
        try:
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        except (AttributeError, OSError):
            pass
        super().server_bind()


def _is_addr_in_use(exc: OSError) -> bool:
    import errno as _errno
    return exc.errno in (_errno.EADDRINUSE, getattr(_errno, "WSAEADDRINUSE", 10048))


def _make_callback_server() -> HTTPServer:
    """Create the local callback server, preferring a dual-stack IPv6 socket that
    catches both ``::1`` and ``127.0.0.1``. Fall back to IPv4-only if IPv6 is
    disabled on this machine. Raises OSError if the port can't be bound (e.g. it
    is already in use) — the caller logs that."""
    try:
        return _DualStackCallbackServer(("::", CALLBACK_PORT), _CallbackHandler)
    except OSError as exc:
        if _is_addr_in_use(exc):
            raise  # port taken — IPv4 fallback would fail too; let caller report it
        print(f"[auth] IPv6 dual-stack bind failed ({exc!r}); falling back to IPv4-only",
              flush=True)
        return HTTPServer(("127.0.0.1", CALLBACK_PORT), _CallbackHandler)


def _wait_for_callback(timeout: int = 120) -> str | None:
    server = _make_callback_server()
    server.timeout = timeout
    try:
        server.handle_request()
    finally:
        server.server_close()
    return _CallbackHandler.code


# ---------------------------------------------------------------------------
# Main login function
# ---------------------------------------------------------------------------

def login(client_id: str | None = None) -> bool:
    """
    Runs the OAuth2 PKCE flow.
    Returns True on success.
    """
    if client_id:
        save_client_id(client_id)
    else:
        client_id = get_client_id()

    if not client_id:
        console.print("[red]Missing client_id. Run: python login.py --client-id <ID>[/]")
        return False

    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(16)

    params = {
        "response_type":         "code",
        "redirect_uri":          CALLBACK_URL,
        "client_id":             client_id,
        "scope":                 " ".join(SCOPES),
        "state":                 state,
        "code_challenge":        challenge,
        "code_challenge_method": "S256",
    }
    auth_url = f"{AUTH_URL}?{urllib.parse.urlencode(params)}"

    console.print(f"\n[bold]Opening EVE Online login in the browser...[/]")
    console.print(f"[dim]If the browser doesn't open, go manually to:[/]")
    console.print(f"[cyan]{auth_url}[/]\n")
    webbrowser.open(auth_url)

    console.print("[dim]Waiting for callback (max 120s)...[/]")
    code = _wait_for_callback()

    if not code:
        console.print("[red]Login timed out or failed.[/]")
        return False

    # Exchange code for tokens
    r = httpx.post(
        TOKEN_URL,
        data={
            "grant_type":    "authorization_code",
            "code":          code,
            "redirect_uri":  CALLBACK_URL,
            "client_id":     client_id,
            "code_verifier": verifier,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=15,
    )

    if r.status_code != 200:
        console.print(f"[red]Token exchange selhal: {r.status_code} {r.text}[/]")
        return False

    data = r.json()
    access_token  = data["access_token"]
    refresh_token = data["refresh_token"]
    expires_in    = data.get("expires_in", 1200)

    # Decode the JWT for character info (without signature verification — we trust HTTPS)
    try:
        payload = jwt.decode(access_token, options={"verify_signature": False})
        sub = payload.get("sub", "")           # "CHARACTER:EVE:12345678"
        character_id   = int(sub.split(":")[-1])
        character_name = payload.get("name", "Unknown")
    except Exception:
        console.print("[red]Failed to decode the JWT token.[/]")
        return False

    conn = _open_conn()
    try:
        ensure_characters_table(conn)
        save_tokens(conn, access_token, refresh_token, expires_in, character_id, character_name)
    finally:
        conn.close()
    console.print(f"[bold green]Logged in as: {character_name} (ID: {character_id})[/]")
    return True


# Reference to the active callback server (HTTPServer) — None when no login is running.
# Kept so that /auth/cancel can shut it down.
_active_server: HTTPServer | None = None
_cancelled: bool = False


def cancel_web_login() -> bool:
    """Cancel an in-progress login flow. Return True if there was anything to cancel.

    Shutting down the local callback HTTP server → the thread in `_run_callback`
    ends, the lock is released, and the user can immediately try logging in again.
    """
    global _active_server, _cancelled
    if _active_server is None:
        return False
    _cancelled = True
    try:
        _active_server.shutdown()
    except Exception:
        pass
    return True


def start_web_login() -> str | None:
    """
    Start the OAuth2 PKCE flow for the web UI.
    Return the auth URL to redirect to, or None if client_id is missing.
    The callback server runs in the background — on success it stores the tokens and redirects to the app.
    """
    global _active_server, _cancelled
    if not _login_lock.acquire(blocking=False):
        return None  # login already in progress

    client_id = get_client_id()
    if not client_id:
        _login_lock.release()
        return None

    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(16)

    params = {
        "response_type":         "code",
        "redirect_uri":          CALLBACK_URL,
        "client_id":             client_id,
        "scope":                 " ".join(SCOPES),
        "state":                 state,
        "code_challenge":        challenge,
        "code_challenge_method": "S256",
    }
    auth_url = f"{AUTH_URL}?{urllib.parse.urlencode(params)}"

    # Reset cancellation flag for this run.
    _cancelled = False
    _CallbackHandler.code = None
    _CallbackHandler.state = None

    def _run_callback():
        global _active_server
        try:
            # Dual-stack callback server (accepts ::1 and 127.0.0.1) so the SSO
            # redirect reaches us no matter how the browser resolves "localhost".
            try:
                server = _make_callback_server()
            except OSError as exc:
                if _is_addr_in_use(exc):
                    print(f"[auth] callback FAILED: port {CALLBACK_PORT} is already in use "
                          f"— another program is holding it. Close it and try again. ({exc!r})",
                          flush=True)
                else:
                    print(f"[auth] callback FAILED: could not bind port {CALLBACK_PORT}: {exc!r}",
                          flush=True)
                return
            # serve_forever() instead of handle_request() so it can be interrupted via shutdown()
            # from `cancel_web_login()`. The handler sets the code and, after processing it,
            # shuts down the server.
            _active_server = server
            print(f"[auth] callback server listening on {server.server_address} "
                  f"(family={server.address_family.name}); waiting for SSO redirect", flush=True)
            # Watchdog — if the user doesn't come back within 15 min, shut down and release the lock.
            def _watchdog():
                import time
                time.sleep(15 * 60)
                if _active_server is server:
                    print("[auth] callback watchdog: no redirect within 15 min — giving up",
                          flush=True)
                    try:
                        server.shutdown()
                    except Exception:
                        pass
            threading.Thread(target=_watchdog, daemon=True).start()
            try:
                server.serve_forever(poll_interval=0.5)
            finally:
                try:
                    server.server_close()
                except Exception:
                    pass

            if _cancelled:
                print("[auth] login cancelled by user", flush=True)
                return
            if not _CallbackHandler.code:
                print("[auth] callback FAILED: no authorization code received — the browser "
                      "never reached the callback (IPv6/IPv4, firewall, or closed too early)",
                      flush=True)
                return

            code = _CallbackHandler.code
            try:
                r = httpx.post(
                    TOKEN_URL,
                    data={
                        "grant_type":    "authorization_code",
                        "code":          code,
                        "redirect_uri":  CALLBACK_URL,
                        "client_id":     client_id,
                        "code_verifier": verifier,
                    },
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    timeout=15,
                )
            except Exception as exc:
                print(f"[auth] token exchange FAILED: request error {exc!r} "
                      "(network down, or a proxy/AV doing TLS interception?)", flush=True)
                return
            if r.status_code != 200:
                print(f"[auth] token exchange FAILED: HTTP {r.status_code} {r.text[:300]}",
                      flush=True)
                return
            try:
                data = r.json()
                payload = jwt.decode(data["access_token"], options={"verify_signature": False})
                sub = payload.get("sub", "")
                character_id   = int(sub.split(":")[-1])
                character_name = payload.get("name", "Unknown")
                conn = _open_conn()
                try:
                    ensure_characters_table(conn)
                    save_tokens(
                        conn,
                        data["access_token"], data["refresh_token"],
                        data.get("expires_in", 1200), character_id, character_name,
                    )
                finally:
                    conn.close()
            except Exception as exc:
                print(f"[auth] callback FAILED: could not store character after token "
                      f"exchange: {exc!r}", flush=True)
                return
            print(f"[auth] login OK: {character_name} (ID {character_id})", flush=True)
        finally:
            _active_server = None
            _login_lock.release()

    threading.Thread(target=_run_callback, daemon=True).start()
    return auth_url
