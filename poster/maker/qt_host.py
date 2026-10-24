"""Owned Qt browser window. No Tk imports; launched by the provider over private pipes."""

import json
import os
from pathlib import Path
import sys
import threading


def main():
    if sys.platform == "win32" and sys.stdout is None:
        # A --windowed executable has no Python standard streams. The parent
        # explicitly supplies private pipe handles, never a console window.
        import ctypes
        import msvcrt
        from ctypes import wintypes

        get_handle = ctypes.windll.kernel32.GetStdHandle
        get_handle.argtypes = [wintypes.DWORD]
        get_handle.restype = wintypes.HANDLE
        sys.stdin = os.fdopen(msvcrt.open_osfhandle(get_handle(-10), os.O_RDONLY), "r", encoding="utf-8")
        sys.stdout = os.fdopen(msvcrt.open_osfhandle(get_handle(-11), os.O_WRONLY), "w", encoding="utf-8")
    port = int(os.environ["POSTER_QT_PORT"])
    temporary = Path(os.environ["POSTER_QT_TEMP"])
    os.environ["QTWEBENGINE_REMOTE_DEBUGGING"] = f"127.0.0.1:{port}"
    os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = (
        "--remote-debugging-address=127.0.0.1 --remote-allow-origins=http://127.0.0.1"
    )
    # Sandbox remains enabled in desktop releases. Root-only CI needs an explicit opt-in.
    if os.environ.get("POSTER_QT_TEST_NO_SANDBOX") == "1":
        os.environ["QTWEBENGINE_DISABLE_SANDBOX"] = "1"
    os.environ["XDG_CACHE_HOME"] = str(temporary / "cache")
    from PySide6.QtCore import QObject, Signal, QTimer, QUrl, QPoint, Qt
    from PySide6.QtWidgets import QApplication, QLineEdit, QMainWindow, QTabWidget, QToolBar
    from PySide6.QtWebEngineWidgets import QWebEngineView
    from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile

    app = QApplication([])

    def send(message):
        sys.stdout.write(json.dumps(message, ensure_ascii=True) + "\n")
        sys.stdout.flush()

    class Bridge(QObject):
        received = Signal(dict)

    class View(QWebEngineView):
        def createWindow(self, kind):
            return window.add_tab(opener=self.page().devToolsId())

    class Window(QMainWindow):
        def __init__(self):
            super().__init__()
            self.setWindowTitle("Poster Maker — браузер")
            self.resize(1050, 730)
            self.profile = QWebEngineProfile(self)
            self.profile.setHttpCacheType(QWebEngineProfile.HttpCacheType.MemoryHttpCache)
            self.tabs = QTabWidget()
            self.tabs.setTabsClosable(True)
            self.setCentralWidget(self.tabs)
            toolbar = QToolBar("Навигация")
            self.addToolBar(toolbar)
            for title, operation in (("Назад", "back"), ("Вперёд", "forward"), ("Обновить", "reload")):
                toolbar.addAction(title, lambda op=operation: getattr(self.tabs.currentWidget(), op)())
            self.address = QLineEdit()
            self.address.setPlaceholderText("https://…")
            toolbar.addWidget(self.address)
            self.address.returnPressed.connect(self.navigate)
            toolbar.addAction("+ Вкладка", self.add_tab)
            toolbar.addAction("−", lambda: self.zoom(-0.1))
            toolbar.addAction("100%", lambda: self.tabs.currentWidget().setZoomFactor(1))
            toolbar.addAction("+", lambda: self.zoom(0.1))
            self.tabs.currentChanged.connect(self.changed)
            self.tabs.tabCloseRequested.connect(self.close_tab)
            self.add_tab()

        def add_tab(self, checked=False, *, opener=None):
            view = View(self)
            view.opener_target = opener
            view.setPage(QWebEnginePage(self.profile, view))
            index = self.tabs.addTab(view, "Новая вкладка")
            view.urlChanged.connect(self.changed)
            view.titleChanged.connect(lambda title, v=view: self.title(v, title))
            self.tabs.setCurrentIndex(index)
            view.setUrl(QUrl("about:blank"))
            view.setFocus(Qt.FocusReason.OtherFocusReason)
            return view

        def title(self, view, title):
            self.tabs.setTabText(self.tabs.indexOf(view), title[:60] or "Новая вкладка")
            self.changed()

        def navigate(self):
            url = QUrl.fromUserInput(self.address.text())
            if url.scheme() in {"http", "https"} and not url.userName() and not url.password():
                self.tabs.currentWidget().setUrl(url)

        def zoom(self, delta):
            view = self.tabs.currentWidget()
            view.setZoomFactor(max(0.25, min(3, view.zoomFactor() + delta)))

        def close_tab(self, index):
            view = self.tabs.widget(index)
            self.tabs.removeTab(index)
            view.page().deleteLater()
            view.deleteLater()
            if not self.tabs.count():
                self.add_tab()
            self.changed()

        def state(self):
            current = self.tabs.currentWidget()
            return {
                "active": current.page().devToolsId() if current else None,
                "tabs": [
                    {
                        "target": self.tabs.widget(i).page().devToolsId(),
                        "opener": self.tabs.widget(i).opener_target,
                        "url": self.tabs.widget(i).url().toString(),
                        "title": self.tabs.widget(i).title(),
                    }
                    for i in range(self.tabs.count())
                ],
                "private_profile": self.profile.isOffTheRecord(),
                "view_rect": [
                    current.mapToGlobal(QPoint(0, 0)).x(),
                    current.mapToGlobal(QPoint(0, 0)).y(),
                    current.width(),
                    current.height(),
                ]
                if current
                else None,
            }

        def changed(self, *args):
            current = self.tabs.currentWidget()
            if current:
                self.address.setText(current.url().toString())
            send({"state": self.state()})

        def command(self, message):
            request_id = message.get("id")
            try:
                command, params = message["command"], message.get("params", {})
                if command == "state":
                    result = self.state()
                elif command == "new_tab":
                    result = self.add_tab().page().devToolsId()
                elif command in {"activate", "close_tab"}:
                    index = next(
                        i
                        for i in range(self.tabs.count())
                        if self.tabs.widget(i).page().devToolsId() == params["target"]
                    )
                    if command == "activate":
                        self.tabs.setCurrentIndex(index)
                        self.show()
                        self.raise_()
                        self.activateWindow()
                        self.tabs.currentWidget().setFocus(Qt.FocusReason.OtherFocusReason)
                    else:
                        self.close_tab(index)
                    result = True
                elif command == "quit":
                    result = True
                    QTimer.singleShot(0, app.quit)
                else:
                    raise ValueError("Unsupported command")
                send({"id": request_id, "result": result})
            except Exception:
                send({"id": request_id, "error": "BROWSER_HOST_COMMAND_FAILED"})

    window = Window()
    bridge = Bridge()
    bridge.received.connect(window.command)

    def read():
        stream = sys.stdin
        if stream is None:  # PyInstaller windowed mode uses the inherited binary descriptors below.
            stream = os.fdopen(0, "r", encoding="utf-8")
        for line in stream:
            if len(line) > 1_000_000:
                break
            try:
                message = json.loads(line)
                if isinstance(message, dict):
                    bridge.received.emit(message)
            except ValueError:
                break
        bridge.received.emit({"command": "quit"})

    threading.Thread(target=read, daemon=True).start()
    window.show()
    app.exec()
    # Destroy views before their shared profile to avoid Qt teardown use-after-free.
    for i in range(window.tabs.count()):
        window.tabs.widget(i).page().deleteLater()
    app.processEvents()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
