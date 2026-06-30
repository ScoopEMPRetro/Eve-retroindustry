"""
Android entry point. Spouští reálnou FastAPI appku (app.web.main) přes uvicorn
na 127.0.0.1, stejně jako launcher.py na desktopu — ale bez pywebview/PyQt.
Java (MainActivity) zavolá start_server(files_dir) na pozadí; UI vlákno pak
počká na port a načte WebView na http://127.0.0.1:<port>.
"""
import os
import socket

PORT = 8000


def _log(msg):
    # Jde do logcat (python.stdout) — užitečné při ladění na zařízení.
    print(f"[android_main] {msg}", flush=True)


def start_server(files_dir, port=PORT):
    """Blokující — běží v Java background vlákně po celou dobu života appky.

    files_dir = app-private úložiště (Context.getFilesDir()). Sem MainActivity
    předtím rozbalil sde_base.db + app/web/templates. Slouží zároveň jako
    writable adresář pro eve_cache.db (EVE_APP_DIR) i jako read zdroj
    bundlovaných dat (EVE_BUNDLE_DIR).
    """
    os.environ.setdefault("EVE_APP_DIR", files_dir)
    os.environ.setdefault("EVE_BUNDLE_DIR", files_dir)
    _log(f"EVE_APP_DIR=EVE_BUNDLE_DIR={files_dir}")

    # Import až po nastavení env (app.web.main čte cesty při importu —
    # SDE bootstrap z EVE_BUNDLE_DIR/sde_base.db do EVE_APP_DIR/eve_cache.db).
    from app.web.main import app
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
