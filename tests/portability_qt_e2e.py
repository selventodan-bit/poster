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


def test_legacy_typed_input_form_does_not_require_source_or_profile_bindings(app, tmp_path, monkeypatch):  # noqa: F811
    from PySide6.QtWidgets import QSpinBox
    from poster.dispatcher.qt_app import InputsDialog

    monkeypatch.setenv("POSTER_CLOUD_HOME", str(tmp_path / "private-cloud"))
    parent = QWidget()
    parent.agent_items = []
    parent.timeout = QSpinBox()
    parent.timeout.setRange(1, 86400)
    parent.timeout.setValue(300)
    dialog = InputsDialog(
        dict(name="Legacy form", inputs=[dict(name="flag", type="bool", secret=False, required=True)]), parent
    )
    try:
        dialog.show()
        app.processEvents()
        dialog.fields[0][4].setCurrentIndex(1)
        assert dialog.values() == ({"flag": True}, {})
        assert not dialog.profile_path.text()
    finally:
        dialog.close()
        parent.close()
        app.processEvents()


def test_real_consent_dialog_shows_selected_scope_and_decline_does_not_grant(app, tmp_path, monkeypatch):  # noqa: F811
    from PySide6.QtWidgets import QDialog, QMessageBox

    monkeypatch.setenv("POSTER_CLOUD_HOME", str(tmp_path / "private-cloud"))
    source = tmp_path / "project.postergraph.json"
    save_graph(
        source, chain("Consent scope", [("Files.WriteText", dict(path="out.txt", text="ok"))]), Vault()
    )
    profiles = Profiles(tmp_path / "profiles")
    profile_id = profiles.create("Synthetic local profile")
    parent = QWidget()
    version = "b" * 64
    dialog = PreparationDialog(
        parent,
        source=source,
        profiles=profiles,
        dispatcher_root=tmp_path / "native",
        item=dict(project_id="project", version=version),
    )
    dialog.profile_choice.setCurrentIndex(dialog.profile_choice.findData(profile_id))
    dialog.origins.setText("https://fixture.example")
    dialog.ttl.setValue(90)
    dialog.permitted_copies.setValue(5)
    observed, operations = {}, []

    # This unit isolates the consent controls from transport: no permission or
    # runtime is fabricated. Other jobs exercise the actual pinned Agent.
    def metadata(name, coroutine, callback=None):
        coroutine.close()
        operations.append(name)
        assert name == "agents"
        callback([dict(name="Pinned test Agent", id="a" * 64)])

    monkeypatch.setattr(dialog, "submit", metadata)
    remaining = [100]

    def respond():
        remaining[0] -= 1
        modal = app.activeModalWidget()
        if isinstance(modal, QMessageBox):
            observed["text"] = modal.text()
            QTest.mouseClick(modal.button(QMessageBox.StandardButton.No), Qt.MouseButton.LeftButton)
            return
        if isinstance(modal, QInputDialog):
            modal.accept()
        if remaining[0] > 0:
            QTimer.singleShot(30, respond)
        elif isinstance(modal, QDialog):
            modal.reject()

    dialog.show()
    QTimer.singleShot(50, respond)
    try:
        QTest.mouseClick(dialog.findChild(QPushButton, "prepare_agent_consent"), Qt.MouseButton.LeftButton)
        text = observed["text"]
        assert "Разрешённых копий: 5" in text and "Срок разрешения, секунд: 90" in text
        assert "a" * 64 in text and version in text and "https://fixture.example" in text
        assert operations == ["agents"]
    finally:
        dialog.close()
        parent.close()
        app.processEvents()
