"""Frozen 4B.2 acceptance through OS mouse/keyboard; SQLite is read-only evidence.

The harness uses Python; application PATH contains only Windows directories.
No IPC mutations, clock patching, synthetic worker or database writes are used.
"""

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time

from windows_gui_launch import open_portable


def check(executable, output):
    import psutil
    import win32clipboard
    import win32con
    from pywinauto import Desktop, keyboard, mouse
    from poster.maker.example_projects import chain
    from poster.maker.model import Variable
    from poster.maker.storage import Vault, save_graph
    from poster.dispatcher.processes import terminate_tree

    output.mkdir(parents=True, exist_ok=False)
    root, observer = output / "Private state Ж", output / "observer-private.json"
    os.environ["POSTER_DISPATCHER_WORKSPACE"] = str(root)
    os.environ["POSTER_DISPATCHER_OBSERVER"] = str(observer)
    os.environ.pop("POSTER_SESSION_KEY", None)
    build = json.loads((executable.parent / "BUILD_INFO.json").read_bytes())
    assert build["commit"] == os.environ["EXPECTED_POSTER_COMMIT"]
    for record in build["launchers"].values():
        assert (
            hashlib.sha256((executable.parent / record["file"]).read_bytes()).hexdigest() == record["sha256"]
        )
    phase, process, window, service = "start", None, None, None
    owned, reports = {}, []
    canary = "PRIVATE_SCHEDULE_4B2_CANARY"

    def snapshot():
        try:
            return json.loads(observer.read_bytes())
        except (OSError, ValueError):
            return {}

    def wait(predicate, label, timeout=50):
        nonlocal phase
        phase = label
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = snapshot()
            if predicate(value):
                return value
            time.sleep(0.08)
        raise AssertionError("Schedule GUI: " + label)

    def click(name):
        state = wait(lambda s: s.get("widgets", {}).get(name, {}).get("enabled"), name)
        window.set_focus()
        point = state["widgets"][name]
        mouse.click(coords=(point["x"], point["y"]))

    def paste(text):
        win32clipboard.OpenClipboard()
        try:
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardText(str(text), win32con.CF_UNICODETEXT)
        finally:
            win32clipboard.CloseClipboard()
        keyboard.send_keys("^v", pause=0.05)

    def fill(name, text):
        click(name)
        keyboard.send_keys("^a")
        paste(text)

    def tab(index):
        state = wait(lambda s: s.get("connected") and not s.get("busy"), "idle")
        window.set_focus()
        point = state["tabs"][index]
        mouse.click(coords=(point["x"], point["y"]))

    def open_gui():
        nonlocal process, window, service
        observer.unlink(missing_ok=True)
        open_portable(executable)
        window = Desktop(backend="win32").window(title="Poster Dispatcher — очередь проектов")
        window.wait("visible", timeout=40)
        window.set_focus()
        process = psutil.Process(window.process_id())
        owned[process.pid] = process.create_time()
        state = wait(lambda s: s.get("connected") and not s.get("busy"), "connected")
        service = state["service"]
        owned[service["pid"]] = service["birth"]
        assert (
            Path(psutil.Process(service["pid"]).exe()).resolve()
            == (executable.parent / "PosterService.exe").resolve()
        )

    def close_gui():
        window.set_focus()
        keyboard.send_keys("%{F4}")
        process.wait(20)

    def rows(table):
        if not (root / "dispatcher.sqlite").exists():
            return []
        assert table in {"jobs", "executions", "schedule_events", "schedules"}
        with sqlite3.connect((root / "dispatcher.sqlite").as_uri() + "?mode=ro", uri=True) as db:
            db.row_factory = sqlite3.Row
            return [dict(r) for r in db.execute("SELECT * FROM " + table)]

    def complete(count):
        wait(
            lambda _: len(rows("jobs")) == count and all(r["status"] == "SUCCEEDED" for r in rows("jobs")),
            "worker completion",
        )
        for execution in rows("executions"):
            raw = Path(execution["report"]).read_bytes()
            assert hashlib.sha256(raw).hexdigest() == execution["report_sha256"]
            report = json.loads(raw)
            assert report["worker_frozen"] and report["gui_imports_forbidden"]
            assert (
                Path(report["worker_executable"]).resolve()
                == (executable.parent / "PosterWorker.exe").resolve()
            )
            assert (Path(report["workdir"]) / "output.txt").read_text(
                encoding="utf-8"
            ) == "scheduled frozen output"
            assert (
                report["worker_pid"] != service["pid"]
                if "worker_pid" in report
                else execution["pid"] != service["pid"]
            )
            assert canary not in raw.decode()
            reports.append(dict(pid=execution["pid"], run_id=execution["run_id"], frozen=True))

    def select_schedule():
        tab(2)
        state = wait(lambda s: len(s.get("schedules", [])) == 1, "schedule visible")
        row = state["schedules"][0]
        mouse.click(coords=(row["x"], row["y"]))
        wait(lambda s: s["schedules"][0]["selected"], "schedule selected")

    try:
        graph = chain(
            "Schedule frozen Ж",
            [
                ("Logic.Delay", {"milliseconds": 1500}),
                ("JSON.Stringify", {"value": {"variable": "password"}}),
                ("Files.WriteText", {"path": "output.txt", "text": "scheduled frozen output"}),
            ],
        )
        graph.variables["password"] = Variable(input=True, secret=True, required=True)
        source = output / "project.postergraph.json"
        save_graph(source, graph, Vault())
        open_gui()
        click("add_project")
        dialog = Desktop(backend="win32").window(title="Добавить сохранённый проект Maker")
        dialog.wait("visible", timeout=15)
        dialog.set_focus()
        keyboard.send_keys("%n")
        paste(source)
        keyboard.send_keys("{ENTER}")
        dialog.wait_not("visible", timeout=15)
        wait(lambda s: len(s.get("projects", [])) == 1 and not s.get("busy"), "project added")
        tab(2)
        click("create_schedule")
        wait(lambda s: "save_schedule" in s.get("widgets", {}), "schedule form")
        # Once in UTC, using a short genuine future instant and the production timing rules.
        due = datetime.now(timezone.utc) + timedelta(seconds=15)
        fill("schedule_start", due.strftime("%Y-%m-%d %H:%M:%S"))
        window.capture_as_image().save(output / "01-schedule-form.png")
        click("save_schedule")
        wait(lambda s: "confirm_enqueue" in s.get("widgets", {}), "protected inputs")
        fill("input_password", canary)
        click("confirm_enqueue")
        wait(lambda s: len(s.get("schedules", [])) == 1 and not s.get("busy"), "schedule saved")
        assert len(rows("jobs")) == 0, "future schedule must not run early"
        close_gui()
        assert psutil.Process(service["pid"]).is_running()
        complete(1)
        assert len(rows("schedule_events")) == 1
        open_gui()
        select_schedule()
        click("schedule_history")
        wait(lambda s: not s.get("busy"), "history")
        window.capture_as_image().save(output / "02-history-after-gui-close.png")
        click("shutdown_service")
        psutil.Process(service["pid"]).wait(30)
        close_gui()
        open_gui()
        assert len(rows("jobs")) == len(rows("schedule_events")) == 1
        select_schedule()
        click("pause_schedule")
        wait(lambda _: not rows("schedules")[0]["enabled"], "disabled")
        # Change to a short interval through the same production GUI.
        click("edit_schedule")
        click("schedule_mode")
        keyboard.send_keys("{HOME}{DOWN}{ENTER}")
        fill("schedule_interval", "2")
        fill(
            "schedule_start",
            (datetime.now(timezone.utc) + timedelta(seconds=2)).strftime("%Y-%m-%d %H:%M:%S"),
        )
        click("save_schedule")
        wait(
            lambda s: not s.get("busy") and rows("schedules")[0]["enabled"] == 0, "disabled interval retained"
        )
        # Readiness barrier: wait across at least two due intervals, no new admission.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            assert len(rows("jobs")) == len(rows("schedule_events")) == 1
            time.sleep(0.1)
        tab(0)
        state = wait(lambda s: bool(s.get("projects")), "manual project")
        row = state["projects"][0]
        mouse.click(coords=(row["x"], row["y"]))
        click("enqueue_projects")
        fill("input_password", canary)
        click("confirm_enqueue")
        complete(2)
        assert len(rows("schedule_events")) == 1
        # Public diagnostics and encrypted-at-rest state must not expose the protected input.
        for path in root.rglob("*"):
            if path.is_file() and path.suffix not in {".lock", ".key"}:
                assert canary.encode() not in path.read_bytes(), path.name
        assert canary not in observer.read_text(encoding="utf-8")
        report = dict(
            status="PASSED",
            commit=build["commit"],
            closed_gui_schedule=True,
            restart_duplicate_jobs=0,
            disabled_no_enqueue=True,
            manual_queue=True,
            secret_leaks=0,
            workers=reports,
            harness_has_python=True,
            application_python_on_path=False,
        )
    except Exception as exc:
        report = dict(status="FAILED", phase=phase, exception_type=type(exc).__name__, commit=build["commit"])
        report.update(
            service_code=snapshot().get("error"),
            visible_controls=sorted(snapshot().get("widgets", {})),
            jobs=[{"status": r["status"], "attempt": r["attempt"]} for r in rows("jobs")],
        )
        print("::error title=Scheduler frozen GUI::" + json.dumps(report), flush=True)
        if window:
            try:
                window.capture_as_image().save(output / "failure.png")
            except Exception:
                pass
        raise
    finally:
        # No Task Scheduler settings are modified by this GUI scenario.
        for pid, birth in reversed(list(owned.items())):
            terminate_tree(pid, birth)
        observer.unlink(missing_ok=True)
        if report["status"] == "PASSED":
            print("::notice title=Scheduler frozen GUI::" + json.dumps(report), flush=True)
        (output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--exe", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    check(args.exe.resolve(), args.output.resolve())
