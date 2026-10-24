"""Windowed executable entry point. It never invokes a terminal or an installer."""

import json
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
        try:
            info = json.loads(Path("BUILD_INFO.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            info = {}
        if info.get("ui") == "qt" or "--maker-qt" in sys.argv:
            from poster.maker.qt_app import launch
        else:
            from poster.recorder.app import launch

        return launch() or 0
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
