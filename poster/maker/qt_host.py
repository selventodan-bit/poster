"""Standalone provider host reusing the same native browser pane as Full Maker."""

import json
import os
from pathlib import Path
import sys
import threading


def streams():
    if sys.platform == "win32" and sys.stdout is None:
        import ctypes
        import msvcrt
        from ctypes import wintypes

        get_handle = ctypes.windll.kernel32.GetStdHandle
        get_handle.argtypes = [wintypes.DWORD]
        get_handle.restype = wintypes.HANDLE
        sys.stdin = os.fdopen(msvcrt.open_osfhandle(get_handle(-10), os.O_RDONLY), "r", encoding="utf-8")
        sys.stdout = os.fdopen(msvcrt.open_osfhandle(get_handle(-11), os.O_WRONLY), "w", encoding="utf-8")


def main():
    streams()
    from PySide6.QtCore import QObject, Signal, QTimer, QUrl
    from PySide6.QtWidgets import QApplication, QMainWindow
    from .qt_browser import BrowserPane, configure_browser

    configure_browser(Path(os.environ["POSTER_QT_TEMP"]), int(os.environ["POSTER_QT_PORT"]))
    app = QApplication([])
    pane = BrowserPane()
    pane.navigate_requested.connect(lambda url: pane.current().setUrl(QUrl(url)))
    window = QMainWindow()
    window.setWindowTitle("Poster Maker — браузер")
    window.resize(1050, 730)
    window.setCentralWidget(pane)
    app.setQuitOnLastWindowClosed(False)
    stopping = False

    def send(message):
        try:
            sys.stdout.write(json.dumps(message, ensure_ascii=True) + "\n")
            sys.stdout.flush()
        except (BrokenPipeError, OSError):
            QTimer.singleShot(0, shutdown)

    def shutdown():
        nonlocal stopping
        if not stopping:
            stopping = True
            pane.dispose()
            window.hide()
            send({"event": "closing"})
            QTimer.singleShot(0, app.quit)

    class Bridge(QObject):
        received = Signal(dict)

    bridge = Bridge()

    def command(message):
        try:
            name = message.get("command")
            if name == "quit":
                result = True
                QTimer.singleShot(0, shutdown)
            elif name in {"state", "new_tab", "activate", "close_tab"}:
                result = pane.command(name, message.get("params", {}))
            else:
                raise ValueError("Unsupported command")
            send({"id": message.get("id"), "result": result})
        except Exception:
            send({"id": message.get("id"), "error": "BROWSER_HOST_COMMAND_FAILED"})

    bridge.received.connect(command)

    def read():
        for line in sys.stdin:
            if len(line) > 1_000_000:
                break
            try:
                value = json.loads(line)
                if isinstance(value, dict):
                    bridge.received.emit(value)
            except ValueError:
                break
        bridge.received.emit({"command": "quit"})

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    window.show()
    app.exec()
    shutdown()
    reader.join(timeout=5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
