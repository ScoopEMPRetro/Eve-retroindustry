"""
EVE Retroindustry — launcher with system tray icon.

Entry point for both development mode and PyInstaller frozen bundle.

Usage (dev):   python launcher.py
Usage (build): pyinstaller eve_retroindustry.spec
"""
from __future__ import annotations

import multiprocessing
import os
import sys
import threading
import time
import webbrowser

# Windows: suppress harmless ConnectionResetError noise from ProactorEventLoop
if sys.platform == "win32":
    import asyncio
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------

if getattr(sys, "frozen", False):
    _BUNDLE_DIR: str = sys._MEIPASS          # type: ignore[attr-defined]
    _APP_DIR: str = os.path.dirname(sys.executable)
    sys.path.insert(0, _BUNDLE_DIR)
else:
    _BUNDLE_DIR = os.path.dirname(os.path.abspath(__file__))
    _APP_DIR = _BUNDLE_DIR

os.environ.setdefault("EVE_APP_DIR", _APP_DIR)
os.environ.setdefault("EVE_BUNDLE_DIR", _BUNDLE_DIR)


# ---------------------------------------------------------------------------
# Tray icon image — hexagonal factory (matches SVG logo, 100×100 viewbox)
# ---------------------------------------------------------------------------

def _make_tray_image(size: int = 64):
    from PIL import Image, ImageDraw

    img  = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    s    = size / 100.0

    bg   = (13,  17,  23,  255)  # #0d1117
    gold = (227, 179, 65,  255)  # #e3b341

    def pt(x: float, y: float) -> tuple[float, float]:
        return (x * s, y * s)

    hex_pts = [
        pt(50,  3),
        pt(95, 26.5),
        pt(95, 73.5),
        pt(50, 97),
        pt( 5, 73.5),
        pt( 5, 26.5),
    ]

    draw.polygon(hex_pts, fill=bg)

    stroke = max(2, int(s * 5))
    draw.line(hex_pts + [hex_pts[0]], fill=gold, width=stroke)

    # Factory left tower
    draw.rectangle([pt(26, 32), pt(39, 64)], fill=gold)
    # Factory right tower
    draw.rectangle([pt(62, 40), pt(74, 64)], fill=gold)
    # Factory base
    draw.rectangle([pt(18, 62), pt(82, 85)], fill=gold)
    # Door cutout
    draw.rectangle([pt(43, 68), pt(57, 85)], fill=bg)

    return img


# ---------------------------------------------------------------------------
# Uvicorn server thread
# ---------------------------------------------------------------------------

class _ServerThread(threading.Thread):
    def __init__(self) -> None:
        super().__init__(daemon=True)
        from app.web.main import app as _app
        import uvicorn
        self._server = uvicorn.Server(
            uvicorn.Config(_app, host="127.0.0.1", port=8000, log_level="warning")
        )

    def run(self) -> None:
        import asyncio
        asyncio.run(self._server.serve())

    def stop(self) -> None:
        self._server.should_exit = True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    import pystray

    srv = _ServerThread()
    srv.start()

    def _open_browser() -> None:
        time.sleep(2.5)
        webbrowser.open("http://127.0.0.1:8000")

    threading.Thread(target=_open_browser, daemon=True).start()

    image = _make_tray_image(64)

    def on_open(icon: pystray.Icon, item: pystray.MenuItem) -> None:
        webbrowser.open("http://127.0.0.1:8000")

    def on_quit(icon: pystray.Icon, item: pystray.MenuItem) -> None:
        icon.stop()
        srv.stop()

    menu = pystray.Menu(
        pystray.MenuItem("Open App", on_open, default=True),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", on_quit),
    )

    icon = pystray.Icon("EVE Retroindustry", image, "EVE Retroindustry", menu)
    icon.run()

    srv.join(timeout=3)
    os._exit(0)


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
