# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

project_root = Path(SPECPATH).parent
icon = str(project_root / "assets" / "cloud-storage.ico")

service = Analysis(
    [str(project_root / "cloud_storage" / "core" / "win_service.py")],
    pathex=[str(project_root)],
    binaries=[],
    datas=[],
    hiddenimports=[
        "servicemanager",
        "win32service",
        "win32serviceutil",
        "win32timezone",
        "uvicorn.logging",
        "uvicorn.loops.auto",
        "uvicorn.protocols.http.auto",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["PySide6"],
    noarchive=False,
)
service_pyz = PYZ(service.pure)
service_exe = EXE(
    service_pyz,
    service.scripts,
    [],
    exclude_binaries=True,
    name="CloudStorageServerService",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon=icon,
)
coll = COLLECT(
    service_exe,
    service.binaries,
    service.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="CloudStorageServerService",
)
