"""Real Qt event loop and separate service/worker processes; no mock scheduler."""

import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.skipif(os.environ.get("POSTER_TEST_QT") != "1", reason="Explicit native Qt CI job")
def test_dispatcher_native_client_lifetime_tables_inputs_and_real_workers(tmp_path):
    process = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=100,
    )
    assert process.returncode == 0, process.stdout + process.stderr


def scenario(root):
    import json
    import time
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QFileDialog, QDialogButtonBox
    from poster.dispatcher.qt_app import DispatcherWindow, InputsDialog
    from poster.dispatcher.host import request
    from poster.maker.example_projects import chain
    from poster.maker.storage import save_graph, Vault
    from test_dispatcher_service import controlled_server

    os.environ["POSTER_DISPATCHER_WORKSPACE"] = str(root / "Dispatcher Ж")
    os.environ["POSTER_DISPATCHER_OBSERVER"] = str(root / "observer.json")
    app = QApplication([])
    window = DispatcherWindow()
    window.show()

    def wait(predicate):
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            app.processEvents()
            if predicate():
                return
            QTest.qWait(20)
        raise AssertionError("Qt condition: " + str(window.last_error))

    def click(name):
        wait(lambda: not window.busy and window.buttons[name].isEnabled())
        QTest.mouseClick(window.buttons[name], Qt.MouseButton.LeftButton)

    wait(lambda: window.connected and not window.busy)
    try:
        with controlled_server() as (server, base):
            for number in range(2):
                path = root / (str(number) + ".postergraph.json")
                save_graph(
                    path,
                    chain(
                        "GUI " + str(number),
                        [
                            ("HTTP.Post", {"url": base + "/hold/" + str(number)}),
                            ("Files.WriteText", {"path": "output.txt", "text": "Qt worker"}),
                        ],
                    ),
                    Vault(),
                )
                QFileDialog.getOpenFileName = staticmethod(lambda *args, path=path: (str(path), ""))
                click("add_project")
                wait(lambda number=number: len(window.projects_model.rows) == number + 1 and not window.busy)
            # Exercise the protected and typed input form without exposing values.
            dialog = InputsDialog(
                {
                    "name": "Typed",
                    "inputs": [
                        {"name": "flag", "type": "bool", "secret": False, "required": True},
                        {"name": "password", "type": "str", "secret": True, "required": True},
                    ],
                },
                window,
            )
            dialog.fields[0][4].setCurrentIndex(1)
            dialog.fields[1][2].setText("PRIVATE_QT_FORM_CANARY")
            assert dialog.values() == ({"flag": True}, {"password": "PRIVATE_QT_FORM_CANARY"})
            dialog.close()
            dialog.deleteLater()
            for number in range(2):
                window.projects.selectRow(number)
                from PySide6.QtCore import QTimer

                def accept():
                    active = app.activeModalWidget()
                    if isinstance(active, InputsDialog):
                        buttons = active.findChild(QDialogButtonBox)
                        QTest.mouseClick(
                            buttons.button(QDialogButtonBox.StandardButton.Ok), Qt.MouseButton.LeftButton
                        )
                    else:
                        QTimer.singleShot(20, accept)

                QTimer.singleShot(20, accept)
                click("enqueue_projects")
                wait(lambda number=number: len(window.jobs_model.rows) == number + 1 and not window.busy)
                window.tabs.setCurrentIndex(0)
            click("start_service")
            wait(lambda: len(server.calls) == 2)
            wait(lambda: len([r for r in window.jobs_model.rows if r["status"] == "RUNNING"]) == 2)
            pids = [r["pid"] for r in window.jobs_model.rows]
            assert len(set(pids)) == 2 and all(pids)
            window.close()
            assert request(window.root, "ping")["running"]
            assert all(r["status"] == "RUNNING" for r in request(window.root, "jobs")["rows"])
            for gate in server.gates.values():
                gate.set()
            window = DispatcherWindow()
            window.show()
            wait(
                lambda: (
                    len(window.jobs_model.rows) == 2
                    and all(r["status"] == "SUCCEEDED" for r in window.jobs_model.rows)
                )
            )
            window.tabs.setCurrentIndex(1)
            window.jobs.selectRow(0)
            click("job_details")
            wait(lambda: "SHA256" in window.details.toPlainText())
            assert (Path(window.selected_report["workdir"]) / "output.txt").read_text() == "Qt worker"
            assert "PRIVATE_QT_FORM_CANARY" not in (root / "observer.json").read_text()
            window.grab().save(str(root / "dispatcher-native.png"))
            click("stop_service")
            wait(lambda: not window.service.get("running") and not window.busy)
            window.max_workers.setValue(1)
            click("save_settings")
            wait(lambda: window.service["settings"]["max_workers"] == 1 and not window.busy)
            assert len(server.calls) == 2
            print(json.dumps({"qt_dispatcher": "passed", "workers": pids, "closed_gui_survives": True}))
    finally:
        request(window.root, "shutdown")
        window.close()
        app.processEvents()


if __name__ == "__main__":
    scenario(Path(sys.argv[1]))
