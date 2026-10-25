"""Explicit runtime/resource allowlist: never collect user config or artworks."""
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

root = Path(SPECPATH).parent
datas = collect_data_files("webview")
datas += [(str(root / "pelican" / "preview.html"), "pelican")]

analysis = Analysis(
    [str(root / "desktop_entry.py")],
    pathex=[str(root)],
    binaries=collect_dynamic_libs("webview"),
    datas=datas,
    hiddenimports=["webview.platforms.winforms", "webview.platforms.edgechromium",
                   "clr", "pythonnet", "clr_loader", "_cffi_backend"],
    excludes=["PyQt5", "PyQt6", "PySide2", "PySide6", "gi", "webview.platforms.gtk",
              "webview.platforms.qt", "webview.platforms.cocoa", "webview.platforms.android"],
    noarchive=False,
)
pyz = PYZ(analysis.pure)
exe = EXE(
    pyz, analysis.scripts, analysis.binaries, analysis.datas, [],
    name="API-Tester", debug=False, bootloader_ignore_signals=False,
    strip=False, upx=False, console=False,
    version=str(root / "build_tools" / "version.txt"),
)
