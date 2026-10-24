"""Windowed executable entry point. It never invokes a terminal or an installer."""

import multiprocessing
import os
from pathlib import Path
import sys


def main():
    multiprocessing.freeze_support()
    if getattr(sys, "frozen", False):
        os.chdir(Path(sys.executable).resolve().parent)
    if "--qt-browser-host" in sys.argv:
        from poster.maker.qt_host import main as host_main

        return host_main()
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(Path(".poster/browsers").absolute()))
    try:
        from poster.recorder.app import launch

        launch()
    except Exception:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(
            "Poster — запуск",
            "Не удалось открыть Poster. Распакуйте весь архив в доступную для записи папку. "
            "Сохраните существующую папку .poster: в ней проекты, знания и ключи.",
            parent=root,
        )
        root.destroy()
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
