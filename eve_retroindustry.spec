# -*- mode: python ; coding: utf-8 -*-
#
# EVE Retroindustry — PyInstaller spec
#
# Build:
#   1. python scripts/build_sde_base.py   (creates sde_base.db)
#   2. pyinstaller eve_retroindustry.spec
#
# Output: dist/EVE_Retroindustry/
#   ├── EVE_Retroindustry.exe   (Windows) / EVE_Retroindustry (Linux)
#   ├── sde_base.db             (bundled, used as first-run template)
#   └── ... (Python runtime + deps)
#
# Distribute the entire dist/EVE_Retroindustry/ folder as a ZIP.
# eve_cache.db is created next to the .exe on first run.

import sys
from PyInstaller.utils.hooks import collect_all

block_cipher = None

# pywebview backend differs per platform. On Linux we ship PyQt6-WebEngine
# (self-contained, no system webkit2gtk dependency); on Windows we use the
# default Edge WebView2 backend (preinstalled on Win10+).
#
# We rely on PyInstaller's built-in PyQt6 hook (it sees the imports in
# webview.platforms.qt and pulls only the Qt modules we actually use:
# QtCore, QtGui, QtWidgets, QtNetwork, QtWebEngineCore, QtWebEngineWidgets).
# Collecting all of PyQt6 unconditionally bloats the build by ~400 MB with
# Qt6Quick/QML/3D/Designer modules that we never touch.
_qt_hiddenimports = []
if sys.platform.startswith("linux"):
    _qt_hiddenimports = [
        "PyQt6.QtCore",
        "PyQt6.QtGui",
        "PyQt6.QtWidgets",
        "PyQt6.QtNetwork",
        "PyQt6.QtWebEngineCore",
        "PyQt6.QtWebEngineWidgets",
    ]

_wv_datas, _wv_binaries, _wv_hiddenimports = collect_all("webview")

a = Analysis(
    ["launcher.py"],
    pathex=["."],
    binaries=_wv_binaries,
    datas=[
        ("app/web/templates", "app/web/templates"),
        ("assets/icon.ico", "assets"),
    ] + _wv_datas,
    hiddenimports=[
        # JWT / auth
        "jwt",
        "jwt.algorithms",
        # uvicorn internals not auto-detected
        "uvicorn.logging",
        "uvicorn.loops",
        "uvicorn.loops.auto",
        "uvicorn.loops.asyncio",
        "uvicorn.protocols",
        "uvicorn.protocols.http",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.http.h11_impl",
        "uvicorn.protocols.websockets",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.lifespan",
        "uvicorn.lifespan.on",
        "uvicorn.lifespan.off",
        # anyio backend
        "anyio",
        "anyio._backends._asyncio",
        # HTTP
        "httpx",
        "httpcore",
        "httpcore._backends.asyncio",
        "h11",
        # FastAPI / starlette
        "starlette.middleware.cors",
        "multipart",
        "python_multipart",
        # email (used by httpx internally)
        "email.mime.text",
        "email.mime.multipart",
        # PIL kept for any image processing (formerly used by tray icon)
        "PIL",
        "PIL.Image",
    ] + _qt_hiddenimports + _wv_hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "matplotlib",
        "numpy",
        "pandas",
        "PyQt5",
        "wx",
        "gi",
        # pystray (replaced by pywebview)
        "pystray",
        "Xlib",
        # Qt modules pulled in transitively but unused by our QWebEngineView
        "PyQt6.QtQml",
        "PyQt6.QtQuick",
        "PyQt6.QtQuickWidgets",
        "PyQt6.Qt3DCore",
        "PyQt6.Qt3DRender",
        "PyQt6.Qt3DInput",
        "PyQt6.Qt3DLogic",
        "PyQt6.Qt3DExtras",
        "PyQt6.QtMultimedia",
        "PyQt6.QtMultimediaWidgets",
        "PyQt6.QtPdf",
        "PyQt6.QtPdfWidgets",
        "PyQt6.QtCharts",
        "PyQt6.QtDataVisualization",
        "PyQt6.QtSensors",
        "PyQt6.QtBluetooth",
        "PyQt6.QtPositioning",
        "PyQt6.QtSerialPort",
        "PyQt6.QtSql",
        "PyQt6.QtTest",
        "PyQt6.QtDesigner",
        "PyQt6.QtHelp",
        "PyQt6.QtSvg",
        "PyQt6.QtXml",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

# Post-filter Qt6 binaries/datas: the PyQt6 PyInstaller hook bulk-collects
# the entire Qt6 lib/, plugins/, qml/, translations/ trees. We only need
# QtCore, QtGui, QtWidgets, QtNetwork, QtWebEngine* and a small set of
# platform/imageformat/tls plugins. Strip everything else to keep the
# bundle close to ~250 MB instead of ~700 MB.
if sys.platform.startswith("linux"):
    import re

    _qt_lib_keep = re.compile(
        r"(libQt6(Core|DBus|Gui|Network|OpenGL|Widgets|"
        r"WebEngineCore|WebEngineWidgets|WebChannel|Positioning|Quick|Qml|QmlMeta|QmlModels|QmlWorkerScript)"
        r"|libicu|libavcodec|libavformat|libavutil|libswresample|libwebp|"
        r"libminizip|libxslt|libxml2|liblcms2|libssl|libcrypto)"
    )
    _qt_plugin_keep = re.compile(
        r"PyQt6/Qt6/plugins/(platforms|imageformats|tls|iconengines|"
        r"xcbglintegrations|egldeviceintegrations|"
        r"wayland-shell-integration|wayland-decoration-client|"
        r"wayland-graphics-integration-client|platforminputcontexts|"
        r"platformthemes|networkinformation)/"
    )
    _qt_resources_keep = re.compile(
        r"PyQt6/Qt6/resources/(qtwebengine_resources|qtwebengine_devtools|"
        r"icudtl|v8_context_snapshot)"
    )

    def _qt_keep(dest: str) -> bool:
        # Only filter PyQt6/Qt6 paths — leave everything else untouched.
        if "PyQt6/Qt6/" not in dest:
            return True
        if "/lib/" in dest:
            base = dest.rsplit("/", 1)[-1]
            return bool(_qt_lib_keep.match(base))
        if "/plugins/" in dest:
            return bool(_qt_plugin_keep.search(dest))
        if "/translations/" in dest:
            return dest.endswith("/qtwebengine_locales/en-US.pak") or "en-US" in dest
        if "/resources/" in dest:
            return bool(_qt_resources_keep.search(dest))
        if "/qml/" in dest:
            return False
        if "/qsci/" in dest:
            return False
        return True

    a.binaries = [b for b in a.binaries if _qt_keep(b[0])]
    a.datas    = [d for d in a.datas    if _qt_keep(d[0])]

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="EVE_Retroindustry",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    icon="assets/icon.ico",
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="EVE_Retroindustry",
)
