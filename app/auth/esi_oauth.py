"""
EVE Online ESI OAuth2 PKCE flow pro native/CLI aplikace.
Nevyžaduje client_secret — používá PKCE (code_challenge).

Flow:
  1. Vygeneruj code_verifier + code_challenge
  2. Otevři prohlížeč → EVE login
  3. Spusť lokální server na :5173 pro callback
  4. Vyměň code + verifier za tokeny
  5. Ulož tokeny
"""
import os
import secrets
import hashlib
import base64
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
    return sqlite3.connect(os.path.join(app_dir, "eve_cache.db"))

_login_lock = threading.Lock()

console = Console()

AUTH_URL      = "https://login.eveonline.com/v2/oauth/authorize"
TOKEN_URL     = "https://login.eveonline.com/v2/oauth/token"
CALLBACK_PORT = 5173
CALLBACK_URL  = f"http://localhost:{CALLBACK_PORT}/callback"

SCOPES = [
    # --- Blueprinty a výroba ---
    "esi-characters.read_blueprints.v1",       # blueprinty postavy (ME/TE, BPO/BPC)
    "esi-corporations.read_blueprints.v1",     # blueprinty korporace
    "esi-industry.read_character_jobs.v1",     # aktivní výrobní joby postavy
    "esi-industry.read_corporation_jobs.v1",   # aktivní výrobní joby korporace
    "esi-industry.read_character_mining.v1",   # mining ledger postavy

    # --- Assety a inventář ---
    "esi-assets.read_assets.v1",               # assety postavy (materiály na stanicích)
    "esi-assets.read_corporation_assets.v1",   # assety korporace

    # --- Vesmírné struktury ---
    "esi-universe.read_structures.v1",         # jména player struktur (citadely)
    "esi-search.search_structures.v1",         # hledání struktur jménem

    # --- Trh a finance ---
    "esi-wallet.read_character_wallet.v1",     # ISK balance postavy
    "esi-wallet.read_corporation_wallets.v1",  # ISK balance korporace
    "esi-markets.read_character_orders.v1",    # vlastní market ordery
    "esi-markets.read_corporation_orders.v1",  # market ordery korporace
    "esi-markets.structure_markets.v1",        # trh v player strukturách (citadely)
    "esi-contracts.read_character_contracts.v1",   # kontrakty postavy

    # --- Dovednosti ---
    "esi-skills.read_skills.v1",               # natrénované skilly (vliv na výrobu)
    "esi-skills.read_skillqueue.v1",           # fronta skillů

    # --- Lokace ---
    "esi-location.read_location.v1",           # aktuální poloha postavy
    "esi-location.read_ship_type.v1",          # aktuální loď

    # --- Planetární interakce (PI materiály) ---
    "esi-planets.manage_planets.v1",           # PI kolonie a extrakce

    # --- Korporace ---
    "esi-corporations.read_facilities.v1",     # výrobní zařízení korporace
    "esi-characters.read_corporation_roles.v1", # role v korporaci
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
# Lokální callback server
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
        # Browser tab zůstane na téhle stránce — žádný redirect na
        # localhost:8000/auth/sync, který by uživateli otevřel naši appku
        # v externím browseru místo toho aby zůstal v původním okně.
        # Webview mezitím polluje /api/auth/status a samo se přesměruje
        # na /auth/sync, jakmile je character uložen.
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
        # serve_forever() neukončí thread sám — spustíme shutdown z jiného
        # threadu, jinak by deadlock (shutdown čeká na ukončení serve_forever
        # smyčky, která čeká na nás).
        threading.Thread(target=self.server.shutdown, daemon=True).start()

    def log_message(self, *args):
        pass  # potlač HTTP logy


def _wait_for_callback(timeout: int = 120) -> str | None:
    server = HTTPServer(("localhost", CALLBACK_PORT), _CallbackHandler)
    server.timeout = timeout
    server.handle_request()
    return _CallbackHandler.code


# ---------------------------------------------------------------------------
# Hlavní login funkce
# ---------------------------------------------------------------------------

def login(client_id: str | None = None) -> bool:
    """
    Spustí OAuth2 PKCE flow.
    Vrátí True při úspěchu.
    """
    if client_id:
        save_client_id(client_id)
    else:
        client_id = get_client_id()

    if not client_id:
        console.print("[red]Chybí client_id. Spusť: python login.py --client-id <ID>[/]")
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

    console.print(f"\n[bold]Otevírám EVE Online login v prohlížeči...[/]")
    console.print(f"[dim]Pokud se prohlížeč neotevře, přejdi ručně na:[/]")
    console.print(f"[cyan]{auth_url}[/]\n")
    webbrowser.open(auth_url)

    console.print("[dim]Čekám na callback (max 120s)...[/]")
    code = _wait_for_callback()

    if not code:
        console.print("[red]Login vypršel nebo selhal.[/]")
        return False

    # Výměna code za tokeny
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

    # Dekóduj JWT pro character info (bez ověření signatury — důvěřujeme HTTPS)
    try:
        payload = jwt.decode(access_token, options={"verify_signature": False})
        sub = payload.get("sub", "")           # "CHARACTER:EVE:12345678"
        character_id   = int(sub.split(":")[-1])
        character_name = payload.get("name", "Unknown")
    except Exception:
        console.print("[red]Nepodařilo se dekódovat JWT token.[/]")
        return False

    conn = _open_conn()
    try:
        ensure_characters_table(conn)
        save_tokens(conn, access_token, refresh_token, expires_in, character_id, character_name)
    finally:
        conn.close()
    console.print(f"[bold green]Přihlášen jako: {character_name} (ID: {character_id})[/]")
    return True


# Reference na aktivní callback server (HTTPServer) — None když login není rozjet.
# Drží se proto, aby ho /auth/cancel mohl shutdown-nout.
_active_server: HTTPServer | None = None
_cancelled: bool = False


def cancel_web_login() -> bool:
    """Zruší probíhající login flow. Vrátí True pokud byl něco k rušení.

    Shutdown lokálního callback HTTP serveru → thread v `_run_callback`
    skončí, lock se uvolní, user může okamžitě zkusit login znovu.
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
    Spustí OAuth2 PKCE flow pro web UI.
    Vrátí auth URL pro redirect, nebo None pokud chybí client_id.
    Callback server běží na pozadí — po úspěchu uloží tokeny a přesměruje na appku.
    """
    global _active_server, _cancelled
    if not _login_lock.acquire(blocking=False):
        return None  # login už probíhá

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
            server = HTTPServer(("localhost", CALLBACK_PORT), _CallbackHandler)
            # serve_forever() místo handle_request() aby šel přerušit přes shutdown()
            # z `cancel_web_login()`. Handler nastaví code a po jeho zpracování
            # shutdown-uje server.
            _active_server = server
            # Watchdog — pokud user nepřijde zpět do 15 min, shutdown a uvolni lock.
            def _watchdog():
                import time
                time.sleep(15 * 60)
                if _active_server is server:
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

            if _cancelled or not _CallbackHandler.code:
                return  # cancelled or no code received

            code = _CallbackHandler.code
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
                return
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
        finally:
            _active_server = None
            _login_lock.release()

    threading.Thread(target=_run_callback, daemon=True).start()
    return auth_url
