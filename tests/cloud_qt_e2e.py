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
        QTest.qWait(10)
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
