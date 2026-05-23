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

block_cipher = None

a = Analysis(
    ["launcher.py"],
    pathex=["."],
    binaries=[],
    datas=[
        # Jinja2 templates
        ("app/web/templates", "app/web/templates"),
        # App icon (tray + taskbar)
        ("assets/icon.ico", "assets"),
    ],
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
        # Tray icon — Windows + Linux backends
        "pystray",
        "pystray._base",
        "pystray._win32",
        "pystray._gtk",
        "pystray._appindicator",
        "pystray._xorg",
        # python-xlib needed by pystray._xorg on Linux (KDE/X11)
        "Xlib",
        "Xlib.display",
        "Xlib.ext",
        "Xlib.ext.shape",
        "Xlib.protocol",
        "Xlib.xobject",
        "PIL",
        "PIL.Image",
        "PIL.ImageDraw",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Exclude unused heavy libs
        "tkinter",
        "matplotlib",
        "numpy",
        "pandas",
        "PyQt5",
        "PyQt6",
        "wx",
        "gi",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

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
    console=False,          # No terminal window — app lives in system tray
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
