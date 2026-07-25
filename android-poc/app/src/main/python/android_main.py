"""
Android entry point. Runs the real FastAPI app (app.web.main) via uvicorn
on 127.0.0.1, just like launcher.py on desktop — but without pywebview/PyQt.
Java (MainActivity) calls start_server(files_dir) in the background; the UI
thread then waits for the port and loads a WebView at http://127.0.0.1:<port>.
"""
import os
import socket

PORT = 8000

# Reference to the Android Activity — passed in from Java via set_context().
# Needed to open the system browser (ESI SSO login) via an Intent.
_activity = None


def _log(msg):
    # Goes to logcat (python.stdout) — useful when debugging on the device.
    print(f"[android_main] {msg}", flush=True)


def set_context(activity):
    """Java MainActivity passes `this` here after Python starts."""
    global _activity
    _activity = activity


def _open_url_intent(url):
    """Opens a URL in the system browser via an Android Intent (ACTION_VIEW).
    Replacement for webbrowser/xdg-open, which don't work on Chaquopy.
    After login, EVE SSO then redirects to http://localhost:5173/callback —
    loopback is shared on the device, so the app's callback server catches it.
    """
    from android.content import Intent
    from android.net import Uri
    intent = Intent(Intent.ACTION_VIEW, Uri.parse(url))
    intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
    _activity.startActivity(intent)
    _log("opened SSO url via Intent")


def start_server(files_dir, port=PORT):
    """Blocking — runs in a Java background thread for the whole app lifetime.

    files_dir = app-private storage (Context.getFilesDir()). MainActivity has
    unpacked sde_base.db + app/web/templates here beforehand. It serves both as
    the writable directory for eve_cache.db (EVE_APP_DIR) and as the read source
    for bundled data (EVE_BUNDLE_DIR).
    """
    os.environ.setdefault("EVE_APP_DIR", files_dir)
    os.environ.setdefault("EVE_BUNDLE_DIR", files_dir)
    os.environ["EVE_ANDROID"] = "1"   # UI: native updater instead of the desktop one

    # Redirect all Python output (uvicorn logs + app tracebacks) to a file,
    # so an error can be shown in the app even without adb (see get_log / MainActivity).
    try:
        _f = open(os.path.join(files_dir, "server.log"), "w", buffering=1, encoding="utf-8")
        sys.stdout = _f
        sys.stderr = _f
    except Exception:
        pass
    _log(f"EVE_APP_DIR=EVE_BUNDLE_DIR={files_dir}")

    try:
        # Import only after setting env (app.web.main reads paths at import time —
        # SDE bootstrap from EVE_BUNDLE_DIR/sde_base.db into EVE_APP_DIR/eve_cache.db).
        from app.web import main as webmain
        # Register the Android Intent opener for SSO login (instead of xdg-open/webbrowser).
        webmain.set_browser_opener(_open_url_intent)
        app = webmain.app
        import uvicorn

        _log(f"starting uvicorn on 127.0.0.1:{port}")
        config = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="info",
            # uvicorn installs signal handlers only on the main thread — on a background
            # thread it skips them itself, so running in a thread is fine.
        )
        server = uvicorn.Server(config)

        import asyncio
        asyncio.run(server.serve())
        _log("uvicorn stopped")
    except BaseException:
        import traceback
        _log("SERVER CRASHED:\n" + traceback.format_exc())
        raise


def get_log(files_dir, max_chars=6000):
    """Returns the tail of server.log (to show an error in the app without adb)."""
    try:
        with open(os.path.join(files_dir, "server.log"), "r",
                  encoding="utf-8", errors="replace") as f:
            data = f.read()
        return data[-max_chars:] if data else "(server.log is empty)"
    except Exception as exc:
        return f"(cannot read server.log: {exc})"


def is_up(port=PORT):
    """Helper: returns True when the server on the port accepts connections."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False
