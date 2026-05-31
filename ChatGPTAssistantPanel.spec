# -*- mode: python ; coding: utf-8 -*-
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

hiddenimports = ["_tkinter", "tkinter", "tkinter.ttk", "tkinter.filedialog", "tkinter.messagebox"]
hiddenimports += collect_submodules("modules")
hiddenimports += collect_submodules("mail_adapters")
hiddenimports += collect_submodules("control_panel")

block_cipher = None

tcl_root = Path(sys.base_prefix) / "tcl"
datas = []
if (tcl_root / "tcl8.6" / "init.tcl").exists():
    # PyInstaller 6 + Python 3.13 runtime hook expects _tcl_data under dist/_internal
    datas.append((str(tcl_root / "tcl8.6"), "_tcl_data"))
if (tcl_root / "tk8.6" / "tk.tcl").exists():
    # PyInstaller 6 + Python 3.13 runtime hook expects _tk_data under dist/_internal
    datas.append((str(tcl_root / "tk8.6"), "_tk_data"))


a = Analysis(
    ["control_panel_app.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
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
    name="ChatGPTAssistantPanel",
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
    icon=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="ChatGPTAssistantPanel",
)
