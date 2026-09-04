# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

project_root = Path(SPECPATH).parent
icon = str(project_root / "assets" / "cloud-storage.ico")

core = Analysis(
    [str(project_root / "cloud_storage" / "core" / "main.py")],
    pathex=[str(project_root)],
    binaries=[],
    datas=[(str(project_root / "cloud_storage" / "core" / "web_assets"), "cloud_storage/core/web_assets")],
    hiddenimports=["uvicorn.logging", "uvicorn.loops.auto", "uvicorn.protocols.http.auto"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["PySide6"],
    noarchive=False,
)
core_pyz = PYZ(core.pure)
core_exe = EXE(
    core_pyz,
    core.scripts,
    [],
    exclude_binaries=True,
    name="CloudStorageLegacyCore",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    icon=icon,
)
coll = COLLECT(
    core_exe,
    core.binaries,
    core.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="CloudStorageLegacyCore",
)
