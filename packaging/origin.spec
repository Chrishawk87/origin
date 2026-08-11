# PyInstaller spec — builds Origin into a double-click desktop app.
# Build on the OS you want to ship for (macOS build => .app, Windows => .exe).
#
#   pip install pyinstaller
#   pyinstaller packaging/origin.spec
#
# Output lands in dist/Origin (folder) or dist/Origin.app on macOS.

# -*- mode: python ; coding: utf-8 -*-
import os
block_cipher = None

# uvicorn/fastapi load parts dynamically; PyInstaller needs them named.
hidden = [
    "uvicorn", "uvicorn.logging", "uvicorn.loops", "uvicorn.loops.auto",
    "uvicorn.protocols", "uvicorn.protocols.http", "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets", "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan", "uvicorn.lifespan.on", "uvicorn.lifespan.off",
    "fastapi", "anyio", "click",
]

a = Analysis(
    ["../run_app.py"],
    pathex=[".."],
    binaries=[],
    datas=[("../origin/webui/index.html", "origin/webui")],
    hiddenimports=hidden,
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    cipher=block_cipher,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="Origin",
    console=False,          # no terminal window; it's an app
)
coll = COLLECT(exe, a.binaries, a.zipfiles, a.datas, name="Origin")

# macOS: wrap into a proper .app bundle
import sys
if sys.platform == "darwin":
    app = BUNDLE(coll, name="Origin.app", bundle_identifier="com.origin.app")
