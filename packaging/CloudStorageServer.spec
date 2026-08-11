# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path
import sys

project_root = Path(SPECPATH).parent

a = Analysis(
    [str(project_root / "cloud_storage" / "main.py")],
    pathex=[str(project_root)],
    binaries=[],
    datas=[(str(project_root / "assets" / "cloud-storage.ico"), "assets")],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="CloudStorageServerManager",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    uac_admin=sys.platform == "win32",
    icon=str(project_root / "assets" / "cloud-storage.ico"),
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="CloudStorageServerManager",
)
if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="Cloud Storage Server Manager.app",
        bundle_identifier="com.cloudstorage.server-manager",
        info_plist={
            "CFBundleDisplayName": "Cloud Storage Server Manager",
            "NSHighResolutionCapable": True,
        },
    )
