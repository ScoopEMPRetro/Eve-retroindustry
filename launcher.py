"""
EVE Retroindustry — launcher.

Entry point for both development mode and PyInstaller frozen bundle.

Usage (dev):   python launcher.py
Usage (build): pyinstaller eve_retroindustry.spec

On first run, the app will prompt the user to download game data (~5 MB)
via the browser before the main UI becomes available.
"""
from __future__ import annotations

import multiprocessing
import os
import sys
import threading
import time
import webbrowser


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------

if getattr(sys, "frozen", False):
    # Running inside a PyInstaller bundle.
    # sys._MEIPASS  = read-only extraction dir (code + bundled assets)
    # sys.executable = path to the .exe
    _BUNDLE_DIR: str = sys._MEIPASS          # type: ignore[attr-defined]
    _APP_DIR: str = os.path.dirname(sys.executable)
    sys.path.insert(0, _BUNDLE_DIR)
else:
    _BUNDLE_DIR = os.path.dirname(os.path.abspath(__file__))
    _APP_DIR = _BUNDLE_DIR

# Expose to app.web.main via env vars so it can resolve paths independently.
os.environ.setdefault("EVE_APP_DIR", _APP_DIR)
os.environ.setdefault("EVE_BUNDLE_DIR", _BUNDLE_DIR)


# ---------------------------------------------------------------------------
# Browser auto-open
# ---------------------------------------------------------------------------

def _open_browser() -> None:
    time.sleep(2.5)
    webbrowser.open("http://127.0.0.1:8000")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    threading.Thread(target=_open_browser, daemon=True).start()

    print("=" * 56)
    print("  EVE Retroindustry")
    print("  Server: http://127.0.0.1:8000")
    print("  Press Ctrl+C to quit.")
    print("=" * 56)

    # Import the app object directly — string-based import fails in frozen mode.
    from app.web.main import app as _app  # noqa: PLC0415
    import uvicorn
    uvicorn.run(
        _app,
        host="127.0.0.1",
        port=8000,
        log_level="warning",
    )


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
