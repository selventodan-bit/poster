"""Actual Qt preparation controls and report, without private user data."""

from pathlib import Path

from PySide6.QtCore import QTimer, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QInputDialog, QPushButton, QWidget

from cloud_qt_e2e import app, wait  # noqa: F401
from poster.maker.example_projects import chain
from poster.maker.storage import Vault, save_graph, load_graph
from poster.maker.profiles import Profiles
from poster.portability.qt_dialog import PreparationDialog


def test_real_preparation_profile_network_and_runtime_report(app, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setenv("POSTER_CLOUD_HOME", str(tmp_path / "private-cloud"))
    source = tmp_path / "project.postergraph.json"
    save_graph(
        source, chain("Подготовка CI", [("Files.WriteText", dict(path="out.txt", text="ok"))]), Vault()
    )
    parent = QWidget()
    dialog = PreparationDialog(parent, source=source, profiles=Profiles(tmp_path / "profiles"))
    dialog.show()
    try:
        wait(app, lambda: dialog.isVisible())

        def name():
            modal = app.activeModalWidget()
            assert isinstance(modal, QInputDialog)
            modal.setTextValue("Сеанс CI")
            modal.accept()

        QTimer.singleShot(100, name)
        QTest.mouseClick(dialog.findChild(QPushButton, "prepare_profile_create"), Qt.MouseButton.LeftButton)
        assert dialog.profile_choice.currentData()
        QTest.mouseClick(dialog.findChild(QPushButton, "prepare_profile_clone"), Qt.MouseButton.LeftButton)
        assert len(dialog.profiles.list()) == 2
        QTest.mouseClick(dialog.findChild(QPushButton, "prepare_profile_select"), Qt.MouseButton.LeftButton)
        assert load_graph(source)[0].metadata["browser_profile"] == dialog.profile_choice.currentData()
        # Select a clean session before actual readiness, preserving saved profiles.
        dialog.profile_choice.setCurrentIndex(0)
        QTest.mouseClick(dialog.findChild(QPushButton, "prepare_profile_select"), Qt.MouseButton.LeftButton)
        dialog.tabs.setCurrentIndex(1)
        dialog.network_name.setText("Прямое CI подключение")
        QTest.mouseClick(dialog.findChild(QPushButton, "prepare_network_save"), Qt.MouseButton.LeftButton)
        assert dialog.network_choice.currentData()
        QTest.mouseClick(dialog.findChild(QPushButton, "prepare_network_select"), Qt.MouseButton.LeftButton)
        assert (
            load_graph(source)[0].metadata["poster.network"]["binding"] == dialog.network_choice.currentData()
        )
        dialog.tabs.setCurrentIndex(3)
        dialog.target.setCurrentIndex(dialog.target.findData("generic"))
        QTest.mouseClick(dialog.findChild(QPushButton, "prepare_readiness"), Qt.MouseButton.LeftButton)
        wait(app, lambda: "Фактическая готовность: подтверждена" in dialog.report.toPlainText())
        output = Path(".poster/portability-qt-public")
        output.mkdir(parents=True, exist_ok=True)
        dialog.grab().save(str(output / "readiness.png"))
        dialog.target.setCurrentIndex(dialog.target.findData("colab"))
        QTest.mouseClick(dialog.findChild(QPushButton, "prepare_readiness"), Qt.MouseButton.LeftButton)
        wait(app, lambda: "Фактическая готовность: не подтверждена" in dialog.report.toPlainText())
        assert "Colab/Molab" in dialog.report.toPlainText()
        dialog.grab().save(str(output / "provider-unverified.png"))
    finally:
        dialog.close()
        parent.close()
        app.processEvents()
