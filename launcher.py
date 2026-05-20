"""
EVE Retroindustry — launcher.

Entry point for both development mode and PyInstaller frozen bundle.

Usage (dev):   python launcher.py
Usage (build): pyinstaller eve_retroindustry.spec
"""
from __future__ import annotations

import multiprocessing
import os
import shutil
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
# First-run: seed eve_cache.db from bundled sde_base.db
# ---------------------------------------------------------------------------

def _ensure_db() -> None:
    db_path = os.path.join(_APP_DIR, "eve_cache.db")
    sde_base = os.path.join(_BUNDLE_DIR, "sde_base.db")
    if not os.path.exists(db_path) and os.path.exists(sde_base):
        print("[setup] Creating eve_cache.db from bundled SDE data…")
        shutil.copy2(sde_base, db_path)
        print("[setup] Done.")


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
    _ensure_db()

    threading.Thread(target=_open_browser, daemon=True).start()

    print("=" * 56)
    print("  EVE Retroindustry")
    print("  Server: http://127.0.0.1:8000")
    print("  Press Ctrl+C to quit.")
    print("=" * 56)

    import uvicorn
    uvicorn.run(
        "app.web.main:app",
        host="127.0.0.1",
        port=8000,
        log_level="warning",
    )


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
