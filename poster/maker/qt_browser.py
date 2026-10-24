"""Native browser pane and queued GUI bridge. Python actions stay in GraphRunner."""

import asyncio
import os
from pathlib import Path
import socket

from PySide6.QtCore import QObject, QPoint, Qt, QUrl, Signal
from PySide6.QtWidgets import QLineEdit, QTabWidget, QToolBar, QVBoxLayout, QWidget
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile, qWebEngineChromiumVersion
from PySide6.QtWebEngineWidgets import QWebEngineView

from .model import GraphError
from .providers import QtProvider


def configure_browser(temporary, port=None):
    """Must precede QApplication; no download, no permanent Chrome directory."""
    if port is None:
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
    os.environ["QTWEBENGINE_REMOTE_DEBUGGING"] = f"127.0.0.1:{port}"
    os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = (
        "--remote-debugging-address=127.0.0.1 --remote-allow-origins=http://127.0.0.1"
    )
    os.environ["XDG_CACHE_HOME"] = str(Path(temporary) / "cache")
    if os.environ.get("POSTER_QT_TEST_NO_SANDBOX") == "1":
        os.environ["QTWEBENGINE_DISABLE_SANDBOX"] = "1"
    return port


class BrowserView(QWebEngineView):
    def __init__(self, owner, opener=None):
        super().__init__(owner)
        self.owner, self.opener_target = owner, opener

    def createWindow(self, kind):
        return self.owner.add_tab(opener=self.page().devToolsId())


class BrowserPane(QWidget):
    changed = Signal()
    navigate_requested = Signal(str)

    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.toolbar = QToolBar("Браузер")
        layout.addWidget(self.toolbar)
        for label, operation in (("←", "back"), ("→", "forward"), ("↻", "reload")):
            self.toolbar.addAction(label, lambda op=operation: getattr(self.current(), op)())
        self.address = QLineEdit()
        self.address.setObjectName("browser_url")
        self.address.setPlaceholderText("https://…")
        self.toolbar.addWidget(self.address)
        self.address.returnPressed.connect(self.navigate)
        self.toolbar.addAction("+ Вкладка", lambda: self.add_tab())
        self.toolbar.addAction("−", lambda: self.zoom(-0.1))
        self.toolbar.addAction("100%", lambda: self.current().setZoomFactor(1))
        self.toolbar.addAction("+", lambda: self.zoom(0.1))
        self.tabs = QTabWidget()
        self.tabs.setObjectName("browser_tabs")
        self.tabs.setTabsClosable(True)
        self.tabs.setMovable(True)
        layout.addWidget(self.tabs)
        self.tabs.currentChanged.connect(self.update_address)
        self.tabs.tabCloseRequested.connect(self.close_tab)
        self.profile = None
        self.reset()

    def current(self):
        return self.tabs.currentWidget()

    def reset(self):
        import shiboken6

        while self.tabs.count():
            view = self.tabs.widget(0)
            self.tabs.removeTab(0)
            shiboken6.delete(view.page())
            shiboken6.delete(view)
        if self.profile:
            shiboken6.delete(self.profile)
        self.profile = QWebEngineProfile(self)
        self.profile.setHttpCacheType(QWebEngineProfile.HttpCacheType.MemoryHttpCache)
        return self.add_tab().page().devToolsId()

    def dispose(self):
        import shiboken6

        while self.tabs.count():
            view = self.tabs.widget(0)
            self.tabs.removeTab(0)
            shiboken6.delete(view.page())
            shiboken6.delete(view)
        if self.profile:
            shiboken6.delete(self.profile)
            self.profile = None

    def add_tab(self, *, opener=None):
        view = BrowserView(self, opener)
        view.setPage(QWebEnginePage(self.profile, view))
        view.urlChanged.connect(self.update_address)
        view.titleChanged.connect(lambda title, v=view: self.title(v, title))
        index = self.tabs.addTab(view, "Новая вкладка")
        self.tabs.setCurrentIndex(index)
        view.setUrl(QUrl("about:blank"))
        view.setFocus(Qt.FocusReason.OtherFocusReason)
        return view

    def title(self, view, title):
        self.tabs.setTabText(self.tabs.indexOf(view), title[:60] or "Новая вкладка")
        self.changed.emit()

    def update_address(self, *args):
        if self.current():
            self.address.setText(self.current().url().toString())
        self.changed.emit()

    def navigate(self):
        url = QUrl.fromUserInput(self.address.text())
        if url.scheme() in {"http", "https"} and not url.userName() and not url.password():
            self.navigate_requested.emit(url.toString())

    def zoom(self, delta):
        self.current().setZoomFactor(max(0.25, min(3, self.current().zoomFactor() + delta)))

    def close_tab(self, index):
        view = self.tabs.widget(index)
        self.tabs.removeTab(index)
        view.page().deleteLater()
        view.deleteLater()
        if not self.tabs.count():
            self.add_tab()

    def state(self):
        current = self.current()
        origin = current.mapToGlobal(QPoint(0, 0)) if current else QPoint()
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
            "view_rect": [origin.x(), origin.y(), current.width(), current.height()] if current else None,
            "tab_points": [
                self.tabs.tabBar().mapToGlobal(self.tabs.tabBar().tabRect(i).center()).toTuple()
                for i in range(self.tabs.count())
            ],
            "private_profile": bool(self.profile and self.profile.isOffTheRecord()),
            "chromium": qWebEngineChromiumVersion(),
        }

    def command(self, command, params):
        if command == "state":
            return self.state()
        if command in {"reset", "clear"}:
            return self.reset()
        if command == "new_tab":
            return self.add_tab().page().devToolsId()
        if command in {"activate", "close_tab"}:
            index = next(
                i
                for i in range(self.tabs.count())
                if self.tabs.widget(i).page().devToolsId() == params["target"]
            )
            if command == "activate":
                self.tabs.setCurrentIndex(index)
                self.current().setFocus(Qt.FocusReason.OtherFocusReason)
            else:
                self.close_tab(index)
            return True
        raise GraphError("BROWSER_HOST_COMMAND_FAILED")


class BrowserBridge(QObject):
    requested = Signal(object)

    def __init__(self, pane, port):
        super().__init__()
        self.pane, self.port = pane, port
        self.requested.connect(self.deliver)

    async def request(self, command, **params):
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self.requested.emit((loop, future, command, params))
        return await asyncio.wait_for(future, 15)

    def deliver(self, packet):
        loop, future, command, params = packet
        try:
            result = self.pane.command(command, params)
            error = None
        except Exception:
            result, error = None, GraphError("BROWSER_HOST_COMMAND_FAILED")

        def complete():
            if not future.done():
                future.set_exception(error) if error else future.set_result(result)

        loop.call_soon_threadsafe(complete)


class EmbeddedProvider(QtProvider):
    """The Qt view lives in the Maker window; CDP is driven on the worker thread."""

    def __init__(self, bridge):
        super().__init__()
        self.bridge = bridge

    async def request(self, command, **params):
        return await self.bridge.request(command, **params)

    async def start(self, driver, headless):
        target = await self.request("reset")
        self.browser = await driver.chromium.connect_over_cdp(
            f"http://127.0.0.1:{self.bridge.port}", timeout=15000, no_defaults=True
        )
        self.context = self.browser.contexts[0]
        return self.browser, self.context, await self.page_for(target)

    async def close(self):
        if self.browser:
            await self.browser.close()
            self.browser = None
        await self.request("clear")
