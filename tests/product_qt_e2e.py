"""Actual Qt parameter editing for every action and encrypted maintenance workflow."""

import json
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QLineEdit, QPushButton

from cloud_qt_e2e import app, wait  # noqa: F401
from poster.product.qt import MaintenanceDialog
from poster.maker.model import Document
from poster.maker.storage import Vault, load_graph
from test_product_crypto import PASSWORD


def test_real_qt_maintenance_preview_restore_and_password_does_not_enter_report(app, tmp_path):  # noqa: F811
    root = tmp_path / "maker"
    root.mkdir()
    (root / "result.txt").write_text("saved-result")
    dialog = MaintenanceDialog(dict(maker=root))
    archive = tmp_path / "product.pbackup"
    dialog.archive.setText(str(archive))
    dialog.password.setText(PASSWORD)
    dialog.repeat.setText(PASSWORD)
    dialog.consent.setChecked(True)
    dialog.show()
    try:
        assert not dialog.go.visibleRegion().isEmpty()
        QTest.mouseClick(dialog.go, Qt.MouseButton.LeftButton)
        wait(app, lambda: dialog.thread is None)
        assert archive.exists(), dialog.output.toPlainText()
        assert PASSWORD not in dialog.output.toPlainText() and not dialog.password.text()
        (root / "result.txt").write_text("current-result")
        dialog.operation.setCurrentIndex(2)
        QTest.mouseClick(dialog.go, Qt.MouseButton.LeftButton)
        assert "Сначала" in dialog.output.toPlainText()
        dialog.operation.setCurrentIndex(1)
        dialog.password.setText(PASSWORD)
        QTest.mouseClick(dialog.go, Qt.MouseButton.LeftButton)
        wait(app, lambda: dialog.thread is None)
        assert dialog.preview, dialog.output.toPlainText()
        dialog.operation.setCurrentIndex(2)
        dialog.password.setText(PASSWORD)
        QTest.mouseClick(dialog.go, Qt.MouseButton.LeftButton)
        wait(app, lambda: dialog.thread is None)
        assert (root / "result.txt").read_text() == "saved-result", dialog.output.toPlainText()
        assert PASSWORD not in dialog.output.toPlainText()
        output = Path(".poster/product-qt-public")
        output.mkdir(parents=True, exist_ok=True)
        # No password fields or private paths in a shareable screenshot.
        dialog.paths.hide()
        dialog.archive.clear()
        dialog.output.setPlainText(
            "Восстановление PASSED; задания требуют ручной проверки; расписания отключены; remote revocation UNCONFIRMED."
        )
        app.processEvents()
        dialog.grab().save(str(output / "maintenance.png"))
    finally:
        wait(app, lambda: dialog.thread is None)
        dialog.close()
        app.processEvents()


def test_all_78_original_action_parameter_forms_save_and_reopen(app, tmp_path, monkeypatch):  # noqa: F811
    from poster.maker.qt_app import MakerWindow
    from poster.maker.qt_browser import configure_browser
    from poster.recorder.workspace import Workspace

    monkeypatch.setenv("POSTER_CLOUD_HOME", str(tmp_path / "cloud"))
    port = configure_browser(tmp_path / "browser")
    window = MakerWindow(port, workspace=Workspace(tmp_path / "maker"))
    window.show()
    evidence = []
    try:
        for spec in window.registry.actions.values():
            window.replace(Document().graph, vault=Vault())
            params = spec.params.model_construct().model_dump(mode="json")
            if spec.name == "Browser.WaitPopup":
                cause = window.add_action("Browser.Click", dict(selector="#popup"))
                params["after_node"] = cause.id
                params["tab_id"] = "popup"
            node = window.add_action(spec.name, params)
            app.processEvents()
            assert set(window.property_widgets) == {p.name for p in spec.properties}, spec.name
            # Exercise every real editor and apply through its actual button.
            # Defaults include tables, file options, lists, JSON and secret refs.
            for _prop, widget in window.property_widgets.values():
                if isinstance(widget, QLineEdit):
                    widget.setText(widget.text())
            title = "Stage 6 / " + spec.name
            window.node_title.setText(title)
            apply_button = window.property_area.widget().findChild(QPushButton, "apply")
            window.property_area.ensureWidgetVisible(apply_button)
            app.processEvents()
            QTest.mouseClick(apply_button, Qt.MouseButton.LeftButton)
            assert window.document.graph.node(node.id).title == title, (spec.name, window.last_error)
            path = tmp_path / (spec.name + ".postergraph.json")
            window.path = path
            window.save()
            saved, vault = load_graph(path)
            assert saved.node(node.id).title == title
            window.replace(saved, vault=vault)
            window.canvas.choose([node.id])
            app.processEvents()
            assert window.node_title.text() == title
            from poster.maker.variables import Variables

            values = saved.node(node.id).params
            if spec.reveal_parameters:
                values = Variables({}, vault).reveal(values)
            window.registry.parameters(saved.node(node.id), values)
            evidence.append(dict(action=spec.name, properties=len(spec.properties), status="PASSED"))
        assert len(evidence) == 78
        output = Path(".poster/product-qt-public")
        output.mkdir(parents=True, exist_ok=True)
        (output / "action-forms.json").write_text(json.dumps(evidence, indent=2))
        window.path = None
        window.document.dirty = False
        window.grab().save(str(output / "maker.png"))
        from poster.product.qt import HelpDialog

        help_dialog = HelpDialog(window)
        help_dialog.show()
        assert help_dialog.findChild(QPushButton, "help_diagnostics")
        help_dialog.close()
    finally:
        window.document.dirty = False
        window.close()
        wait(app, lambda: window.close_ready)
        app.processEvents()


def test_maintenance_remains_reachable_after_genuine_restore_crash(app, tmp_path, monkeypatch):  # noqa: F811
    import subprocess
    import sys
    from poster.product import backup, restore
    from poster.product.locking import application_lease
    from poster.dispatcher.models import DispatcherError
    import pytest

    root = tmp_path / "maker"
    root.mkdir()
    (root / "kept.txt").write_text("saved")
    archive = tmp_path / "recovery.pbackup"
    proof = backup.create(dict(maker=root), archive, PASSWORD, confirmed=True)
    code = """
import json,sys,os
from poster.product import restore
p=json.load(sys.stdin)
def crash(phase):
 if phase=='OLD_MOVED_0':os._exit(77)
restore.checkpoint=crash
restore.restore(p['archive'],p['password'],p['roots'],expected_sha256=p['sha'],confirmed=True)
"""
    process = subprocess.run(
        [sys.executable, "-c", code],
        input=json.dumps(
            dict(archive=str(archive), password=PASSWORD, roots=dict(maker=str(root)), sha=proof["sha256"])
        ),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert process.returncode == 77 and not root.exists()
    with pytest.raises(DispatcherError):
        with application_lease(root, "maker"):
            pass
    monkeypatch.setenv("POSTER_MAKER_WORKSPACE", str(root))
    dialog = MaintenanceDialog()
    assert dialog.roots == dict(maker=root)
    dialog.operation.setCurrentIndex(3)
    dialog.consent.setChecked(True)
    dialog.resize(700, 480)
    dialog.show()
    app.processEvents()
    try:
        QTest.mouseClick(dialog.go, Qt.MouseButton.LeftButton)
        wait(app, lambda: dialog.thread is None)
        assert (root / "kept.txt").read_text() == "saved", dialog.output.toPlainText()
        assert not restore.discover(dict(maker=root))
        with application_lease(root, "maker"):
            pass
    finally:
        dialog.close()
