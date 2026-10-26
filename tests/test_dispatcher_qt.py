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
    from PySide6.QtCore import Qt, QPoint
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
            header = window.jobs.horizontalHeader()
            QTest.mouseClick(
                header.viewport(), Qt.MouseButton.LeftButton, pos=QPoint(20, header.height() // 2)
            )
            wait(lambda: window.jobs_model.rows[0]["name"] == "GUI 0" and not window.busy)
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
            # Create a real persisted schedule using the native form, then close the client.
            from datetime import datetime, timedelta
            from poster.dispatcher.schedule_dialog import ScheduleDialog

            window.tabs.setCurrentIndex(2)

            def accept_schedule():
                active = app.activeModalWidget()
                if isinstance(active, ScheduleDialog):
                    active.start.setText(
                        (datetime.now() + timedelta(seconds=3)).strftime("%Y-%m-%d %H:%M:%S")
                    )
                    active.validate()
                    QTimer.singleShot(30, accept_schedule)
                elif isinstance(active, InputsDialog):
                    active.validate()
                else:
                    QTimer.singleShot(20, accept_schedule)

            QTimer.singleShot(20, accept_schedule)
            click("create_schedule")
            wait(lambda: len(window.schedules_model.rows) == 1 and not window.busy)
            schedule_id = window.schedules_model.rows[0]["id"]
            window.close()
            wait(lambda: request(window.root, "schedule_history", id=schedule_id)["total"] == 1)
            job_id = request(window.root, "schedule_history", id=schedule_id)["rows"][0]["job_id"]
            wait(lambda: request(window.root, "show", id=job_id)["status"] == "SUCCEEDED")
            assert (
                Path(request(window.root, "show", id=job_id)["workdir"]) / "output.txt"
            ).read_text() == "Qt worker"
            window = DispatcherWindow()
            window.show()
            wait(lambda: len(window.schedules_model.rows) == 1 and not window.busy)
            window.tabs.setCurrentIndex(2)
            window.schedules.selectRow(0)
            click("schedule_history")
            wait(lambda: "SUCCEEDED" in window.details.toPlainText() and not window.busy)
            click("pause_schedule")
            wait(lambda: not window.schedules_model.rows[0]["enabled"] and not window.busy)
            window.grab().save(str(root / "scheduler-native.png"))
            # Enroll through the real UI/IPC. No agent runs here: OFFLINE must stay real.
            from poster.remote.protocol import Identity

            registration = root / "registration.json"
            registration.write_text(
                json.dumps(
                    dict(
                        peer=Identity().public(), name="Offline Linux", os="linux", version="5A.1", protocol=1
                    )
                )
            )
            relay_profile = root / "controller-relay.json"
            relay_profile.write_text(
                json.dumps(dict(url="https://127.0.0.1:9", channel="b" * 32, token="test-only-token-" * 4))
            )
            files = iter([registration, relay_profile])
            QFileDialog.getOpenFileName = staticmethod(lambda *args: (str(next(files)), ""))
            window.tabs.setCurrentIndex(3)
            click("remote_register")
            wait(lambda: len(window.remote_agents_model.rows) == 1 and not window.busy)
            assert window.remote_agents_model.rows[0]["state"] == "PENDING"
            window.remote_agents.selectRow(0)
            from PySide6.QtWidgets import QMessageBox

            def consent():
                active = app.activeModalWidget()
                if isinstance(active, QMessageBox):
                    QTest.mouseClick(active.button(QMessageBox.StandardButton.Yes), Qt.MouseButton.LeftButton)
                else:
                    QTimer.singleShot(20, consent)

            QTimer.singleShot(20, consent)
            click("remote_approve")
            wait(lambda: window.remote_agents_model.rows[0]["state"] == "TRUSTED" and not window.busy)
            assert window.remote_agents_model.rows[0]["online"] is False
            agent_id = window.remote_agents_model.rows[0]["id"]
            window.tabs.setCurrentIndex(0)
            window.projects.selectRow(0)

            def route():
                active = app.activeModalWidget()
                if isinstance(active, InputsDialog):
                    active.remote_target.agent.setCurrentIndex(active.remote_target.agent.findData(agent_id))
                    active.validate()
                else:
                    QTimer.singleShot(20, route)

            QTimer.singleShot(20, route)
            click("enqueue_projects")
            wait(
                lambda: any(r.get("agent_id") == agent_id for r in window.jobs_model.rows) and not window.busy
            )
            assert (
                next(r for r in window.jobs_model.rows if r.get("agent_id") == agent_id)["status"]
                == "PENDING"
            )
            window.tabs.setCurrentIndex(3)
            window.remote_agents.selectRow(0)
            QTimer.singleShot(20, consent)
            click("remote_revoke")
            wait(lambda: window.remote_agents_model.rows[0]["state"] == "REVOKED" and not window.busy)
            window.grab().save(str(root / "remote-workers-native.png"))
            # Stage 5B wizard: real TLS probe + file pairing + wrong fingerprint rejection.
            import socket
            import threading
            from poster.remote.setup import tls_files, relay_profiles
            from poster.remote.relay import server as relay_server
            from poster.remote.registry import read_json
            from poster.remote.pairing import agent_pair

            relay_root = root / "wizard-relay"
            cert, key = tls_files(relay_root, "127.0.0.1")
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            url = f"https://127.0.0.1:{port}"
            relay_profiles(relay_root, url, cert)
            relay = relay_server(
                read_json(relay_root / "relay.json"), relay_root / "mailbox.sqlite", cert, key, port=port
            )
            relay_thread = threading.Thread(target=relay.serve_forever, daemon=True)
            relay_thread.start()
            try:
                click("remote_pairing")
                wizard = window.pairing_dialog
                wait(lambda: len(wizard.controller.text()) == 64 and not window.busy)
                wizard.url.setText(url)
                QFileDialog.getOpenFileName = staticmethod(
                    lambda *args: (str(relay_root / "pairing-relay.json"), "")
                )
                offer = root / "wizard-offer.json"
                QFileDialog.getSaveFileName = staticmethod(lambda *args: (str(offer), ""))
                QTimer.singleShot(20, consent)
                QTest.mouseClick(wizard.actions["export"], Qt.MouseButton.LeftButton)
                wait(lambda: offer.exists() and not window.busy)
                peer = agent_pair(root / "wizard-agent", "Wizard offline", offer, wizard.controller.text())
                QFileDialog.getOpenFileName = staticmethod(
                    lambda *args: (str(root / "wizard-agent/registration.json"), "")
                )
                wizard.fingerprint.setText("0" * 64)
                QTest.mouseClick(wizard.actions["import"], Qt.MouseButton.LeftButton)
                wait(lambda: window.last_error == "REMOTE_FINGERPRINT" and not window.busy)
                assert wizard.agent_id is None
                wizard.fingerprint.setText(peer)
                QTest.mouseClick(wizard.actions["import"], Qt.MouseButton.LeftButton)
                wait(lambda: wizard.agent_id == peer and not window.busy)
                QTimer.singleShot(20, consent)
                QTest.mouseClick(wizard.actions["approve"], Qt.MouseButton.LeftButton)
                wait(
                    lambda: (
                        any(
                            r["id"] == peer and r["state"] == "TRUSTED"
                            for r in window.remote_agents_model.rows
                        )
                        and not window.busy
                    )
                )
                QTest.mouseClick(wizard.actions["diagnose"], Qt.MouseButton.LeftButton)
                wait(lambda: "OFFLINE" in wizard.result.toPlainText() and not window.busy)
                assert "TLS" in wizard.result.toPlainText()
                wizard.grab().save(str(root / "pairing-native.png"))
                QTimer.singleShot(20, consent)
                QTest.mouseClick(wizard.actions["revoke"], Qt.MouseButton.LeftButton)
                wait(
                    lambda: (
                        any(
                            r["id"] == peer and r["state"] == "REVOKED"
                            for r in window.remote_agents_model.rows
                        )
                        and not window.busy
                    )
                )
                wizard.close()
            finally:
                relay.shutdown()
                relay.server_close()
                relay_thread.join(5)
            print(json.dumps({"qt_dispatcher": "passed", "workers": pids, "closed_gui_survives": True}))
    finally:
        request(window.root, "shutdown")
        window.close()
        app.processEvents()


if __name__ == "__main__":
    scenario(Path(sys.argv[1]))
