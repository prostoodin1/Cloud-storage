# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path
import sys

project_root = Path(SPECPATH).parent
icon = str(project_root / "assets" / "cloud-storage.ico")

client = Analysis(
    [str(project_root / "cloud_storage" / "client" / "main.py")],
    pathex=[str(project_root)],
    binaries=[],
    datas=[],
    hiddenimports=["win32crypt", "win32com.client"] if sys.platform == "win32" else [],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["fastapi", "uvicorn"],
    noarchive=False,
)
client_pyz = PYZ(client.pure)
client_exe = EXE(
    client_pyz,
    client.scripts,
    [],
    exclude_binaries=True,
    name="CloudStorageClient",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon=icon,
)
coll = COLLECT(
    client_exe,
    client.binaries,
    client.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="CloudStorageClient",
)
if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="Cloud Storage Client.app",
        bundle_identifier="com.cloudstorage.desktop-client",
        info_plist={
            "CFBundleDisplayName": "Cloud Storage Client",
            "NSHighResolutionCapable": True,
        },
    )
