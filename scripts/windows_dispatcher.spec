# Shared-runtime multi-executable bundle. Invoked by build_windows_app.py.
import os
from pathlib import Path
from PyInstaller.utils.hooks import collect_all, collect_data_files, copy_metadata

root = Path(os.environ["POSTER_BUILD_ROOT"])
embedded = os.environ["POSTER_BUILD_EMBEDDED"] == "1"
hook = os.environ["POSTER_BUILD_HOOK"]
pw_data, pw_binaries, pw_hidden = collect_all("playwright")
data = collect_data_files("poster") + collect_data_files("tzdata") + pw_data
for distribution in ("playwright", "httpx", "pydantic", "cryptography", "filelock", "psutil"):
    data += copy_metadata(distribution)

gui = Analysis(
    [str(root / "poster/desktop_entry.py")], pathex=[str(root)],
    binaries=pw_binaries, datas=data,
    hiddenimports=pw_hidden + (["poster.maker.qt_app", "poster.maker.qt_host", "poster.dispatcher.qt_app", "poster.product.qt"] if embedded else []),
    excludes=[] if embedded else ["poster.product.qt", "PySide6", "poster.maker.qt_app", "poster.maker.qt_host", "poster.dispatcher.qt_app"],
    runtime_hooks=[hook],
)
headless = Analysis(
    [str(root / "poster/dispatcher/runtime_entry.py")], pathex=[str(root)],
    binaries=pw_binaries, datas=data, hiddenimports=pw_hidden,
    excludes=["poster.product.qt", "PySide6", "tkinter", "_tkinter", "poster.maker.qt_app", "poster.maker.qt_host",
              "poster.dispatcher.qt_app", "poster.recorder.app"], runtime_hooks=[hook],
)
gui_pyz = PYZ(gui.pure)
headless_pyz = PYZ(headless.pure)
options = [("X utf8", None, "OPTION")]
maker = EXE(gui_pyz, gui.scripts, options, exclude_binaries=True, name="Poster", console=False, upx=False)
service = EXE(headless_pyz, headless.scripts, options, exclude_binaries=True, name="PosterService", console=True, upx=False)
worker = EXE(headless_pyz, headless.scripts, options, exclude_binaries=True, name="PosterWorker", console=True, upx=False)
executables = [maker, service, worker]
if embedded:
    executables.append(EXE(gui_pyz, gui.scripts, options, exclude_binaries=True, name="PosterDispatcher", console=False, upx=False))
COLLECT(*executables, gui.binaries, gui.datas, headless.binaries, headless.datas,
        strip=False, upx=False, name="Poster")
