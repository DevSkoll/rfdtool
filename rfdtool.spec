# PyInstaller spec for rfdtool — produces a standalone Linux binary.
#
# Build:  .venv/bin/pyinstaller rfdtool.spec
# Output: dist/rfdtool/

# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

block_cipher = None

# pymavlink ships dialect XML files at runtime; pyqtgraph has its own Qt resources;
# pyserial has nothing data-side but include for safety.
datas = []
datas += collect_data_files("pymavlink")
datas += collect_data_files("pyqtgraph")

hiddenimports = []
hiddenimports += collect_submodules("pymavlink.dialects.v20")
hiddenimports += collect_submodules("pymavlink.dialects.v10")

a = Analysis(
    ["rfdtool.py"],
    pathex=["."],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "matplotlib",
        "scipy",
        "numpy.distutils",
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
    name="rfdtool",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
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
    name="rfdtool",
)
