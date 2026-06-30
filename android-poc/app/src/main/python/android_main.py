"""
Android entry point. Spouští reálnou FastAPI appku (app.web.main) přes uvicorn
na 127.0.0.1, stejně jako launcher.py na desktopu — ale bez pywebview/PyQt.
Java (MainActivity) zavolá start_server(files_dir) na pozadí; UI vlákno pak
počká na port a načte WebView na http://127.0.0.1:<port>.
"""
import os
import socket

PORT = 8000

# Reference na Android Activity — předaná z Javy přes set_context().
# Potřebná pro otevření systémového browseru (ESI SSO login) přes Intent.
_activity = None


def _log(msg):
    # Jde do logcat (python.stdout) — užitečné při ladění na zařízení.
    print(f"[android_main] {msg}", flush=True)


def set_context(activity):
    """Java MainActivity sem předá `this` po startu Pythonu."""
    global _activity
    _activity = activity


def _open_url_intent(url):
    """Otevře URL v systémovém browseru přes Android Intent (ACTION_VIEW).
    Náhrada za webbrowser/xdg-open, které na Chaquopy nefungují.
    EVE SSO pak po loginu přesměruje na http://localhost:5173/callback —
    loopback je na zařízení sdílený, takže callback server appky to chytne.
    """
    from android.content import Intent
    from android.net import Uri
    intent = Intent(Intent.ACTION_VIEW, Uri.parse(url))
    intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
    _activity.startActivity(intent)
    _log("opened SSO url via Intent")


def start_server(files_dir, port=PORT):
    """Blokující — běží v Java background vlákně po celou dobu života appky.

    files_dir = app-private úložiště (Context.getFilesDir()). Sem MainActivity
    předtím rozbalil sde_base.db + app/web/templates. Slouží zároveň jako
    writable adresář pro eve_cache.db (EVE_APP_DIR) i jako read zdroj
    bundlovaných dat (EVE_BUNDLE_DIR).
    """
    os.environ.setdefault("EVE_APP_DIR", files_dir)
    os.environ.setdefault("EVE_BUNDLE_DIR", files_dir)
    os.environ["EVE_ANDROID"] = "1"   # UI: nativní updater místo desktopového
    _log(f"EVE_APP_DIR=EVE_BUNDLE_DIR={files_dir}")

    # Import až po nastavení env (app.web.main čte cesty při importu —
    # SDE bootstrap z EVE_BUNDLE_DIR/sde_base.db do EVE_APP_DIR/eve_cache.db).
    from app.web import main as webmain
    # Zaregistruj Android Intent-opener pro SSO login (místo xdg-open/webbrowser).
    webmain.set_browser_opener(_open_url_intent)
    app = webmain.app
    import uvicorn

    _log(f"starting uvicorn on 127.0.0.1:{port}")
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        # uvicorn instaluje signal handlery jen na main threadu — na background
        # threadu je sám přeskočí, takže běh ve vlákně je v pořádku.
    )
    server = uvicorn.Server(config)

    import asyncio
    asyncio.run(server.serve())
    _log("uvicorn stopped")


def is_up(port=PORT):
    """Pomocná: vrátí True, když server na portu přijímá spojení."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False
