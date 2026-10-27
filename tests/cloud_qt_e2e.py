"""Genuine Qt + actual HTTP Drive fixture, never live Google Drive E2E."""

import json
from pathlib import Path
import time
import uuid

from PySide6.QtCore import QTimer, Qt, QCoreApplication, QEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
import pytest

from cloud_drive_fixture import serve_drive_fixture
from poster.cloud.qt_panel import CloudDialog
from poster.maker.example_projects import chain
from poster.maker.storage import Vault, save_graph
from poster.maker.qt_browser import configure_browser


@pytest.fixture(scope="module")
def app(tmp_path_factory):
    configure_browser(tmp_path_factory.mktemp("cloud-qt-browser"))
    instance = QApplication.instance() or QApplication(["poster-cloud-tests"])
    yield instance
    for widget in instance.topLevelWidgets():
        widget.close()
        widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    instance.processEvents()
    QTest.qWait(100)


def wait(app, predicate, timeout=25):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return
        # On Windows a tight QTest.qWait loop can retain the GIL and starve
        # Python's Drive/cleanup threads. Production QApplication.exec releases
        # it; explicitly yield here while continuing real Qt event delivery.
        time.sleep(0.01)
    import traceback

    states = [
        {
            "window": w.objectName(),
            "busy": getattr(w, "busy", None),
            "code": getattr(w, "last_error", None),
            "visible": w.isVisible(),
        }
        for w in app.topLevelWidgets()
        if w.objectName() in {"poster_cloud_projects", "maker_window"}
    ]
    print(
        "::error title=Cloud Qt diagnostic::"
        + json.dumps({"frames": [f.lineno for f in traceback.extract_stack()[-3:]], "states": states})
    )
    raise AssertionError("Qt cloud UI deadline")


def test_qt_publish_open_and_select_versions_over_actual_http(app, tmp_path, monkeypatch):
    source = tmp_path / "project.postergraph.json"
    graph = chain("Проект из Qt", [("Files.WriteText", {"path": "out.txt", "text": "result"})])
    save_graph(source, graph, Vault())
    (tmp_path / "data.csv").write_text("n\n17\n")
    with serve_drive_fixture() as (api, url):
        config = tmp_path / "test-config.json"
        config.write_text(json.dumps(dict(mode="drive_fixture", nonce=uuid.uuid4().hex, url=url)))
        monkeypatch.setenv("POSTER_CLOUD_TEST_CONFIG", str(config))
        saved = []
        opened = []
        ticks = []
        timer = QTimer()
        timer.timeout.connect(lambda: ticks.append(1))
        timer.start(5)
        dialog = CloudDialog(
            None,
            source=source,
            resources=["data.csv"],
            outputs=["out.txt"],
            published=saved.append,
            opened=opened.append,
        )
        dialog.show()
        try:
            wait(app, lambda: not dialog.busy)
            assert not api.files
            QTest.mouseClick(dialog.buttons["cloud_connect"], Qt.MouseButton.LeftButton)
            wait(app, lambda: not dialog.busy and "0" in dialog.status.text())
            QTest.mouseClick(dialog.buttons["cloud_publish"], Qt.MouseButton.LeftButton)
            wait(app, lambda: bool(saved) and bool(dialog.rows) and not dialog.busy)
            assert len(dialog.rows) == 1 and dialog.rows[0]["project_id"] == graph.id
            assert dialog.version.currentData() == saved[0]["version"]
            assert len(ticks) > 5  # GUI event loop continued during network/storage work.
            QTest.mouseClick(dialog.buttons["cloud_open"], Qt.MouseButton.LeftButton)
            wait(app, lambda: bool(opened))
            path = Path(opened[0]["source"])
            assert path.is_file() and (path.parent / "data.csv").read_text() == "n\n17\n"
            assert opened[0]["version"] == saved[0]["version"]
            assert not dialog.isVisible()
        finally:
            timer.stop()
            dialog.reject()
            dialog.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            app.processEvents()


def test_maker_cloud_actions_and_existing_window_are_available(app, tmp_path):
    from poster.maker.qt_app import MakerWindow
    from poster.recorder.workspace import Workspace
    from PySide6.QtGui import QAction

    window = MakerWindow(0, workspace=Workspace(tmp_path / "maker"))
    window.show()
    try:
        assert window.findChild(QAction, "cloud_save") is not None
        for name in ["connect", "publish", "open", "update", "folder", "history", "sync"]:
            assert window.findChild(QAction, "maker_cloud_" + name) is not None
        assert window.pages.count() >= 1
        assert window.registry.actions and len(window.registry.actions) == 78
    finally:
        window.document.dirty = False
        window.close()
        wait(app, lambda: window.close_ready)
        window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()


def test_dispatcher_cloud_os_hit_waits_for_post_connect_layout(app, tmp_path, monkeypatch):
    """Actual service, deferred Qt layout, fresh observer frames and QWindow hit test."""
    import os
    from PySide6.QtCore import QPoint
    from poster.dispatcher.qt_app import DispatcherWindow
    from poster.dispatcher.host import request
    import importlib.util

    script = Path(__file__).resolve().parents[1] / "scripts/cloud/check_frozen.py"
    spec = importlib.util.spec_from_file_location("cloud_frozen_driver", script)
    driver = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(driver)
    root = tmp_path / "Dispatcher Ж"
    observer = tmp_path / "dispatcher-observer.json"
    monkeypatch.setenv("POSTER_DISPATCHER_WORKSPACE", str(root))
    monkeypatch.setenv("POSTER_DISPATCHER_OBSERVER", str(observer))
    with serve_drive_fixture() as (_, url):
        config = tmp_path / "fixture.json"
        config.write_text(json.dumps(dict(mode="drive_fixture", nonce=uuid.uuid4().hex, url=url)))
        monkeypatch.setenv("POSTER_CLOUD_TEST_CONFIG", str(config))
        window = DispatcherWindow()
        observed = []
        original = window.observe

        def observe():
            original()
            state = json.loads(observer.read_bytes())
            if state.get("connected"):
                point = state["widgets"].get("cloud_projects")
                if point:
                    observed.append((point["x"], point["y"]))

        window.observe = observe
        window.show()
        stable = driver.StableDispatcherControl("cloud_projects", os.getpid())
        chosen = []

        def ready():
            if not observer.exists():
                return False
            state = json.loads(observer.read_bytes())
            point = stable.observe(state, observer.stat().st_mtime_ns)
            if point:
                chosen.append(point)
            return bool(point)

        try:
            wait(app, ready)
            assert len(stable.frames) >= 2
            current = window.buttons["cloud_projects"].mapToGlobal(
                window.buttons["cloud_projects"].rect().center()
            )
            assert (chosen[0]["x"], chosen[0]["y"]) == (current.x(), current.y())
            # The first connected observer runs inside completed(connect),
            # before Qt's pending banner LayoutRequest. It really moves.
            assert observed[0] != observed[-1], observed
            point = window.mapFromGlobal(QPoint(chosen[0]["x"], chosen[0]["y"]))
            QTest.mouseClick(
                window.windowHandle(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, point
            )
            wait(
                app,
                lambda: (
                    hasattr(window, "cloud_dialog")
                    and window.cloud_dialog.isVisible()
                    and not window.cloud_dialog.busy
                ),
            )
            assert window.cloud_dialog.objectName() == "poster_cloud_projects"
            window.cloud_dialog.reject()
            window.cloud_dialog.deleteLater()
        finally:
            window.close()
            request(root, "shutdown")
            window.pool.shutdown(wait=True, cancel_futures=True)
            window.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            app.processEvents()
