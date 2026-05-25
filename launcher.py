"""
EVE Retroindustry — launcher with embedded webview window.

Entry point for both development mode and PyInstaller frozen bundle.

Architecture:
- FastAPI/uvicorn runs in a background thread on 127.0.0.1:8000
- pywebview opens a native window that points at that server
- Closing the window stops the server and exits

Usage (dev):   python launcher.py
Usage (build): pyinstaller eve_retroindustry.spec
"""
from __future__ import annotations

import multiprocessing
import os
import socket
import sys
import threading

# Windows: suppress harmless ConnectionResetError noise from ProactorEventLoop
if sys.platform == "win32":
    import asyncio
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------

if getattr(sys, "frozen", False):
    _BUNDLE_DIR: str = sys._MEIPASS          # type: ignore[attr-defined]
    # When running inside an AppImage, $APPIMAGE points to the .appimage file
    # itself (read-only). Store user data (eve_cache.db) next to that file so
    # it survives AppImage remounts across restarts.
    _appimage = os.environ.get("APPIMAGE")
    _APP_DIR: str = os.path.dirname(_appimage) if _appimage else os.path.dirname(sys.executable)
    sys.path.insert(0, _BUNDLE_DIR)
else:
    _BUNDLE_DIR = os.path.dirname(os.path.abspath(__file__))
    _APP_DIR = _BUNDLE_DIR

os.environ.setdefault("EVE_APP_DIR", _APP_DIR)
os.environ.setdefault("EVE_BUNDLE_DIR", _BUNDLE_DIR)

# console=False in PyInstaller sets sys.stdout/stderr to None. Redirect to a
# rotating log file next to the .exe so tracebacks survive (uvicorn's log
# formatter also calls stream.isatty() which would crash on None).
if getattr(sys, "frozen", False) and sys.stdout is None:
    _log_path = os.path.join(_APP_DIR, "eve_retroindustry.log")
    try:
        _log_file = open(_log_path, "a", buffering=1, encoding="utf-8")
    except Exception:
        _log_file = open(os.devnull, "w")
    sys.stdout = _log_file
    sys.stderr = _log_file
    setattr(_log_file, "isatty", lambda: False)


# ---------------------------------------------------------------------------
# Uvicorn server thread
# ---------------------------------------------------------------------------

class _ServerThread(threading.Thread):
    def __init__(self, port: int) -> None:
        super().__init__(daemon=True)
        from app.web.main import app as _app
        import uvicorn
        self._server = uvicorn.Server(
            uvicorn.Config(_app, host="127.0.0.1", port=port, log_level="warning")
        )

    def run(self) -> None:
        import asyncio
        asyncio.run(self._server.serve())

    def stop(self) -> None:
        self._server.should_exit = True


def _wait_for_server(port: int, timeout: float = 15.0) -> bool:
    """Poll TCP until the server accepts connections (or timeout)."""
    import time
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            import time as _t
            _t.sleep(0.1)
    return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    port = 8000
    srv = _ServerThread(port)
    srv.start()

    if not _wait_for_server(port):
        print("ERROR: server did not start within 15 s", file=sys.stderr)
        os._exit(1)

    import webview

    url = f"http://127.0.0.1:{port}"
    window = webview.create_window(
        title="EVE Retroindustry",
        url=url,
        width=1400,
        height=900,
        min_size=(900, 600),
    )

    def on_closed() -> None:
        srv.stop()

    window.events.closed += on_closed

    # Use PyQt6 + QtWebEngine on both Linux and Windows — self-contained
    # bundled Chromium, no runtime dependency on system webkit2gtk or
    # Edge WebView2 / pythonnet. The default Windows backend tries to
    # load Python.Runtime.dll through pythonnet which silently corrupts
    # under PyInstaller on some user machines ("Failed to resolve
    # Python.Runtime.Loader.Initialize").
    webview.start(gui="qt")

    srv.join(timeout=3)
    os._exit(0)


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
