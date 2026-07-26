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


def _patch_qt_clipboard() -> None:
    """Fix the clipboard in the pywebview Qt backend (pywebview 6.x + PyQt6 6.11).

    Two problems that made "Copy" in the Shopping List crash the app on Linux:
      1. JS access to the clipboard is disabled by default → execCommand('copy') fails.
      2. onFeaturePermissionRequested calls setFeaturePermission(url, feature, int),
         but PyQt6 6.11 requires the PermissionPolicy enum → TypeError crashes the app
         the moment the page calls navigator.clipboard.writeText() on a secure
         origin (our http://127.0.0.1). That is exactly what broke copying the list.

    We patch the bundled pywebview (running in the AppImage) before webview.start().
    Everything is in try/except — a failed patch must not crash app startup.
    """
    try:
        from webview.platforms.qt import BrowserView
        from qtpy.QtWebEngineWidgets import QWebEnginePage, QWebEngineSettings
    except Exception as exc:  # pragma: no cover
        print(f"[clipboard-patch] skipped: {exc!r}", file=sys.stderr)
        return

    WebPage = getattr(BrowserView, "WebPage", None)
    if WebPage is None:
        return

    attr = QWebEngineSettings.WebAttribute
    _orig_init = WebPage.__init__

    def _init(self, parent=None, profile=None):
        _orig_init(self, parent, profile)
        try:
            self.settings().setAttribute(attr.JavascriptCanAccessClipboard, True)
            self.settings().setAttribute(attr.JavascriptCanPaste, True)
        except Exception:
            pass

    def _on_perm(self, url, feature):
        try:
            policy = QWebEnginePage.PermissionPolicy
            media = (
                QWebEnginePage.Feature.MediaAudioCapture,
                QWebEnginePage.Feature.MediaVideoCapture,
                QWebEnginePage.Feature.MediaAudioVideoCapture,
            )
            granted = policy.PermissionGrantedByUser if feature in media else policy.PermissionDeniedByUser
            self.setFeaturePermission(url, feature, granted)
        except Exception:
            pass

    WebPage.__init__ = _init
    if hasattr(WebPage, "onFeaturePermissionRequested"):
        WebPage.onFeaturePermissionRequested = _on_perm


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _smoke_test() -> int:
    """Headless self-test of the *packaged* binary (no GUI window).

    Boots the bundled server, confirms a page renders, and imports the Qt /
    pywebview backend — catching PyInstaller packaging failures (missing hidden
    imports, data files, DLLs) that source-level tests can't see. Returns a
    process exit code. Triggered by ``--smoke`` or ``EVE_SMOKE=1``.
    """
    import urllib.request

    port = 8000
    srv = _ServerThread(port)
    srv.start()
    if not _wait_for_server(port):
        print("SMOKE FAIL: server did not start within 15 s", file=sys.stderr)
        return 1
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/about", timeout=10) as r:
            code = r.getcode()
        if code != 200:
            print(f"SMOKE FAIL: /about returned {code}", file=sys.stderr)
            return 1
    except Exception as exc:
        print(f"SMOKE FAIL: request failed: {exc!r}", file=sys.stderr)
        return 1

    # Import the GUI backend the way main() does — surfaces bundling gaps
    # without needing a display (import only, no widgets created). Skippable via
    # EVE_SMOKE_NO_GUI, because importing QtWebEngine on a headless Linux CI
    # runner pulls system Qt libs that aren't present there.
    if not os.environ.get("EVE_SMOKE_NO_GUI"):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        try:
            import PyQt6.QtCore  # noqa: F401
            import PyQt6.QtWebEngineWidgets  # noqa: F401
            import webview  # noqa: F401
            import webview.platforms.qt  # noqa: F401
        except Exception as exc:
            print(f"SMOKE FAIL: Qt/pywebview backend import failed: {exc!r}", file=sys.stderr)
            return 1
    else:
        print("SMOKE: skipping GUI-backend import (EVE_SMOKE_NO_GUI)", flush=True)

    print("SMOKE OK", flush=True)
    return 0


def main() -> None:
    port = 8000
    srv = _ServerThread(port)
    srv.start()

    if not _wait_for_server(port):
        print("ERROR: server did not start within 15 s", file=sys.stderr)
        os._exit(1)

    # Pre-import the Qt backend so any missing-Qt issue surfaces here with a
    # readable traceback instead of cascading through pywebview's silent
    # backend-fallback logic.
    try:
        import PyQt6.QtCore  # noqa: F401
        import PyQt6.QtWebEngineWidgets  # noqa: F401
        import webview.platforms.qt  # noqa: F401
    except Exception as exc:  # pragma: no cover — surfaces at startup only
        print(f"ERROR: Qt backend failed to load: {exc!r}", file=sys.stderr)
        raise

    _patch_qt_clipboard()

    import webview

    url = f"http://127.0.0.1:{port}"
    # Open wide enough that the whole UI (the wide navbar especially) fits with
    # no horizontal scrollbar — 1400 was too narrow and clipped the nav row.
    # Not maximized: keep it a normal, movable/resizable window.
    window = webview.create_window(
        title="EVE Retroindustry",
        url=url,
        width=1600,
        height=1000,
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
    #
    # private_mode=False + storage_path: pywebview defaults to an
    # off-the-record browser profile, which wipes localStorage on every
    # exit — the plan page's "recently used stations/blueprints" and saved
    # form state silently vanished between sessions. Persist the profile
    # next to eve_cache.db (writable app dir).
    storage_dir = os.path.join(_APP_DIR, "webview_data")
    try:
        os.makedirs(storage_dir, exist_ok=True)
    except Exception:
        storage_dir = None
    webview.start(
        gui="qt",
        private_mode=False,
        storage_path=storage_dir,
    )

    srv.join(timeout=3)
    os._exit(0)


if __name__ == "__main__":
    multiprocessing.freeze_support()
    if os.environ.get("EVE_SMOKE") or "--smoke" in sys.argv:
        os._exit(_smoke_test())
    main()
