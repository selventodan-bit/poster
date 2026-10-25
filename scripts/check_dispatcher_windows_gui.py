"""Extracted Windows release, native OS input, real service and frozen workers.

The harness has Python; the application sees only Windows system directories
on PATH. All mutations go through the UI, except intentional OS crash tests.
Observer, SQLite and reports are read-only evidence, never a command channel.
"""

import argparse
from contextlib import contextmanager
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sqlite3
import threading
import time

from windows_gui_launch import open_portable


@contextmanager
def controlled():
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            label = self.path.strip("/")
            call = {"label": label, "start": time.monotonic(), "end": None}
            with self.server.lock:
                self.server.calls.append(call)
                if label == "secret":
                    self.server.secret_value = json.loads(raw)
                gate = self.server.gates.setdefault(label, threading.Event())
            if label == "secret":
                gate.set()
            gate.wait(115)
            call["end"] = time.monotonic()
            try:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"ok":true}')
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    server.calls, server.gates, server.lock = [], {}, threading.Lock()
    server.secret_value = None
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, "http://127.0.0.1:" + str(server.server_port)
    finally:
        for gate in server.gates.values():
            gate.set()
        server.shutdown()
        server.server_close()
        thread.join(5)


def check(executable, output):
    import psutil
    import win32clipboard
    import win32con
    from pywinauto import Desktop, keyboard, mouse
    from poster.maker.example_projects import chain
    from poster.maker.storage import Vault, save_graph
    from poster.maker.model import Variable

    output.mkdir(parents=True, exist_ok=False)
    observer, maker_observer = output / "observer-private.json", output / "maker-observer-private.json"
    root = output / "Постоянные данные Dispatcher"
    os.environ["POSTER_DISPATCHER_WORKSPACE"] = str(root)
    os.environ["POSTER_DISPATCHER_OBSERVER"] = str(observer)
    os.environ["POSTER_GUI_TEST_OUTPUT"] = str(maker_observer)
    os.environ.pop("POSTER_SESSION_KEY", None)
    info = json.loads((executable.parent / "BUILD_INFO.json").read_bytes())
    assert info["commit"] == os.environ["EXPECTED_POSTER_COMMIT"]
    for role in ["worker", "service", "gui"]:
        record = info["launchers"][role]
        assert (
            hashlib.sha256((executable.parent / record["file"]).read_bytes()).hexdigest() == record["sha256"]
        )
    marker = executable.parent / "user-preservation-canary.txt"
    marker.write_text("keep user files", encoding="utf-8")
    windows = Path(os.environ["SystemRoot"])
    process = window = service = None
    owned, workers_seen, scenarios, proofs = {}, {}, [], {}
    phase, safe_error = "start", None
    canary = "PRIVATE_MAKER_DISPATCHER_4B1_CANARY"

    def snapshot(path=observer):
        try:
            return json.loads(path.read_bytes())
        except (OSError, ValueError):
            return {}

    def wait(predicate, label, timeout=65, path=observer):
        nonlocal phase
        phase = label
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = snapshot(path)
            if predicate(state):
                return state
            time.sleep(0.08)
        raise AssertionError("GUI condition: " + label)

    def paste(value):
        win32clipboard.OpenClipboard()
        try:
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardText(value, win32con.CF_UNICODETEXT)
        finally:
            win32clipboard.CloseClipboard()
        keyboard.send_keys("^v", pause=0.05)

    def click(name):
        state = wait(
            lambda s: name in s.get("widgets", {}) and s["widgets"][name]["enabled"], "control " + name
        )
        window.set_focus()
        p = state["widgets"][name]
        mouse.click(coords=(p["x"], p["y"]))

    def fill(name, value):
        click(name)
        keyboard.send_keys("^a", pause=0.03)
        paste(str(value))

    def idle():
        return wait(lambda s: s.get("connected") and not s.get("busy"), "GUI idle")

    def tab(index):
        state = idle()
        p = next(p for p in state["tabs"] if p["index"] == index)
        mouse.click(coords=(p["x"], p["y"]))
        time.sleep(0.15)

    def native_file(title, path):
        dialog = Desktop(backend="win32").window(title=title)
        dialog.wait("visible", timeout=20)
        dialog.set_focus()
        keyboard.send_keys("%n", pause=0.1)
        paste(str(path))
        keyboard.send_keys("{ENTER}", pause=0.1)
        dialog.wait_not("visible", timeout=20)

    def open_gui():
        nonlocal window, process, service
        observer.unlink(missing_ok=True)
        open_portable(executable)
        window = Desktop(backend="win32").window(title="Poster Dispatcher — очередь проектов")
        window.wait("visible", timeout=50)
        process = psutil.Process(window.process_id())
        owned[process.pid] = process.create_time()
        state = idle()
        service = state["service"]
        assert (
            Path(psutil.Process(service["pid"]).exe()).resolve()
            == (executable.parent / "PosterService.exe").resolve()
        )
        owned[service["pid"]] = service["birth"]
        return state

    def close_gui(crash=False):
        nonlocal process
        if crash:
            process.kill()
        else:
            window.set_focus()
            keyboard.send_keys("%{F4}")
        process.wait(timeout=25)
        process = None

    def add(path, name):
        tab(0)
        click("add_project")
        native_file("Добавить сохранённый проект Maker", path)
        wait(lambda s: any(r["name"] == name for r in s.get("projects", [])), "registered " + name)

    def select_project(name):
        tab(0)
        fill("dispatcher_search", name)
        state = wait(
            lambda s: len(s.get("projects", [])) == 1 and s["projects"][0]["name"] == name, "project filtered"
        )
        row = state["projects"][0]
        mouse.click(coords=(row["x"], row["y"]))
        wait(lambda s: s.get("projects") and s["projects"][0]["selected"], "project selected")

    def enqueue(name, timeout=300):
        select_project(name)
        before = set(all_jobs())
        click("enqueue_projects")
        wait(lambda s: "confirm_enqueue" in s.get("widgets", {}), "enqueue dialog")
        fill("job_timeout", timeout)
        click("confirm_enqueue")
        wait(lambda s: not s.get("busy") and bool(set(all_jobs()) - before), "admitted")
        fill("dispatcher_search", "")
        return (set(all_jobs()) - before).pop()

    def all_jobs():
        if not (root / "dispatcher.sqlite").is_file():
            return {}
        with sqlite3.connect((root / "dispatcher.sqlite").as_uri() + "?mode=ro", uri=True) as db:
            db.row_factory = sqlite3.Row
            return {r["id"]: dict(r) for r in db.execute("SELECT * FROM jobs")}

    def state_job(job, terminal):
        wait(lambda _: all_jobs()[job]["status"] in terminal, "job terminal " + "/".join(terminal))
        return all_jobs()[job]["status"]

    def evidence(job):
        with sqlite3.connect((root / "dispatcher.sqlite").as_uri() + "?mode=ro", uri=True) as db:
            db.row_factory = sqlite3.Row
            row = dict(
                db.execute("SELECT * FROM executions WHERE job_id=? ORDER BY attempt DESC", (job,)).fetchone()
            )
        if row["pid"]:
            workers_seen[row["pid"]] = row["birth"]
        if row["report"]:
            raw = Path(row["report"]).read_bytes()
            assert hashlib.sha256(raw).hexdigest() == row["report_sha256"]
            row["public_report"] = json.loads(raw)
        return row

    def select_job(job):
        tab(1)
        fill("dispatcher_search", job)
        state = wait(lambda s: len(s.get("jobs", [])) == 1 and s["jobs"][0]["id"] == job, "job filtered")
        row = state["jobs"][0]
        mouse.click(coords=(row["x"], row["y"]))
        wait(lambda s: s.get("jobs") and s["jobs"][0]["selected"], "job selected")

    def start():
        click("start_service")
        wait(lambda s: s.get("service", {}).get("running"), "scheduler running")

    def stop():
        click("stop_service")
        wait(lambda s: s.get("connected") and not s["service"]["running"], "scheduler drained")

    def settings(workers):
        fill("max_workers", workers)
        click("save_settings")
        wait(
            lambda s: s.get("service", {}).get("settings", {}).get("max_workers") == workers, "limits applied"
        )

    def assert_worker(job):
        wait(lambda _: evidence(job).get("pid"), "worker PID recorded")
        row = evidence(job)
        child = psutil.Process(row["pid"])
        assert Path(child.exe()).resolve() == (executable.parent / "PosterWorker.exe").resolve()
        assert child.pid != service["pid"] and child.pid != process.pid
        assert not any("python.exe" in arg.lower() or "pythonw.exe" in arg.lower() for arg in child.cmdline())
        assert canary not in " ".join(child.cmdline())
        return child

    try:
        with controlled() as (server, base):
            sources = output / "Проекты Ж"
            sources.mkdir()
            for label in ["a", "b"]:
                save_graph(
                    sources / (label + ".postergraph.json"),
                    chain(
                        "Frozen " + label,
                        [
                            ("HTTP.Post", {"url": base + "/" + label, "timeout_ms": 120000}),
                            (
                                "Files.WriteText",
                                {"path": "Результат Ж/output.txt", "text": "Frozen " + label},
                            ),
                        ],
                    ),
                    Vault(),
                )
            save_graph(
                sources / "delay.postergraph.json",
                chain(
                    "Frozen delay",
                    [
                        ("Logic.Delay", {"milliseconds": 120000}),
                    ],
                ),
                Vault(),
            )
            open_gui()
            for label in ["a", "b"]:
                add(sources / (label + ".postergraph.json"), "Frozen " + label)
            settings(2)
            a, b = enqueue("Frozen a"), enqueue("Frozen b")
            start()
            wait(lambda _: len(server.calls) == 2, "two HTTP requests actually received")
            first, second = assert_worker(a), assert_worker(b)
            assert first.pid != second.pid
            window.capture_as_image().save(output / "01-parallel-workers.png")
            close_gui()
            assert psutil.Process(service["pid"]).is_running() and first.is_running() and second.is_running()
            old_service = service["pid"]
            open_gui()
            assert service["pid"] == old_service
            server.gates["a"].set()
            server.gates["b"].set()
            for job, label in [(a, "a"), (b, "b")]:
                assert state_job(job, {"SUCCEEDED", "FAILED", "UNKNOWN"}) == "SUCCEEDED"
                report = evidence(job)["public_report"]
                assert (
                    report["worker_frozen"]
                    and report["browser_imports_forbidden"]
                    and report["gui_imports_forbidden"]
                )
                assert (Path(report["workdir"]) / "Результат Ж/output.txt").read_text(
                    encoding="utf-8"
                ) == "Frozen " + label
            assert max(c["start"] for c in server.calls) < min(c["end"] for c in server.calls)
            proofs["parallel"] = {"max_workers": 2, "pids": [first.pid, second.pid], "overlap": True}
            select_job(a)
            click("job_details")
            wait(lambda s: s.get("details_sha_verified"), "verified result shown")
            window.capture_as_image().save(output / "02-history-result.png")
            scenarios.extend(
                [
                    "two_frozen_workers",
                    "parallel_http_overlap",
                    "outputs",
                    "gui_close_service_survives",
                    "reconnect_history",
                    "verified_report",
                ]
            )
            stop()
            settings(1)
            server.gates["a"], server.gates["b"] = threading.Event(), threading.Event()
            c, d = enqueue("Frozen a"), enqueue("Frozen b")
            start()
            wait(lambda _: len(server.calls) == 3, "serial first request")
            assert_worker(c)
            # Hold a confirmed accepted request, and observe the real queue/PIDs.
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline:
                assert len(server.calls) == 3 and all_jobs()[d]["status"] == "PENDING"
                time.sleep(0.05)
            server.gates["a"].set()
            assert state_job(c, {"SUCCEEDED", "FAILED", "UNKNOWN"}) == "SUCCEEDED"
            wait(lambda _: len(server.calls) == 4, "serial second request")
            assert_worker(d)
            assert server.calls[3]["start"] >= server.calls[2]["end"]
            server.gates["b"].set()
            assert state_job(d, {"SUCCEEDED", "FAILED", "UNKNOWN"}) == "SUCCEEDED"
            proofs["serial"] = {"max_workers": 1, "overlap": False}
            stop()
            pending = enqueue("Frozen a")
            select_job(pending)
            click("cancel_jobs")
            assert state_job(pending, {"CANCELLED"}) == "CANCELLED"
            add(sources / "delay.postergraph.json", "Frozen delay")
            running = enqueue("Frozen delay")
            start()
            state_job(running, {"RUNNING"})
            assert_worker(running)
            select_job(running)
            click("cancel_jobs")
            assert state_job(running, {"CANCELLED", "FAILED", "UNKNOWN"}) == "CANCELLED"
            stop()
            timed = enqueue("Frozen delay", timeout=1)
            start()
            assert state_job(timed, {"TIMED_OUT", "FAILED", "UNKNOWN"}) == "TIMED_OUT"
            evidence(timed)
            stop()
            scenarios.extend(
                ["max_workers_one_no_overlap", "cancel_pending", "cancel_running", "individual_timeout"]
            )
            # The GUI process crashes while a write is accepted. The service is
            # intentionally NOT its child Job Object, so execution continues.
            server.gates["a"] = threading.Event()
            crash = enqueue("Frozen a")
            start()
            wait(lambda _: len(server.calls) == 5, "write accepted before GUI crash")
            child = assert_worker(crash)
            close_gui(crash=True)
            assert child.is_running() and psutil.Process(service["pid"]).is_running()
            open_gui()
            assert service["pid"] == old_service
            child.kill()
            assert state_job(crash, {"UNKNOWN"}) == "UNKNOWN"
            server.gates["a"].set()
            stop()
            click("reconcile_jobs")
            state_job(crash, {"REVIEW_REQUIRED"})
            start()
            assert len(server.calls) == 5
            stop()
            scenarios.extend(
                ["gui_crash_service_survives", "worker_crash_unknown", "reconcile_no_post_retry"]
            )
            window.capture_as_image().save(output / "03-review-required.png")
            # Service crash: workers lose their gate/control owner, no resend.
            server.gates["b"] = threading.Event()
            lost = enqueue("Frozen b")
            start()
            wait(lambda _: len(server.calls) == 6, "write accepted before service crash")
            assert_worker(lost)
            psutil.Process(service["pid"]).kill()
            close_gui()
            server.gates["b"].set()
            open_gui()
            click("reconcile_jobs")
            state_job(lost, {"REVIEW_REQUIRED"})
            start()
            assert len(server.calls) == 6
            stop()
            scenarios.append("service_crash_no_retry")
            # Actual Maker save of a protected typed variable using native input.
            close_gui()
            secret_path = executable.parent / ".poster/maker-projects/dispatcher-secret.postergraph.json"
            graph = chain(
                "Maker protected to frozen",
                [
                    (
                        "HTTP.Post",
                        {
                            "url": base + "/secret",
                            "body_type": "json",
                            "body": {"variable": "credential"},
                        },
                    )
                ],
            )
            graph.variables["credential"] = Variable(secret=True)
            save_graph(secret_path, graph, Vault())
            open_portable(executable.parent / "Poster.exe")
            maker_window = Desktop(backend="win32").window(title="Poster 3.0 — ProjectMaker")
            maker_window.wait("visible", timeout=50)
            maker_process = psutil.Process(maker_window.process_id())
            owned[maker_process.pid] = maker_process.create_time()
            wait(lambda s: s.get("ui") == "qt" and not s.get("busy"), "Maker ready", path=maker_observer)

            def maker_click(name):
                state = wait(lambda s: name in s.get("positions", {}), "Maker " + name, path=maker_observer)
                maker_window.set_focus()
                mouse.click(coords=tuple(state["positions"][name]))

            maker_click("open")
            native_file("Открыть граф", secret_path)
            wait(lambda s: s.get("graph", {}).get("name") == graph.name, "Maker opened", path=maker_observer)
            for _ in range(12):
                state = snapshot(maker_observer)
                if any(t[0] == "Переменные" for t in state.get("tools_tabs", [])):
                    break
                maker_click("tools_scroll_right")
                time.sleep(0.15)
            state = snapshot(maker_observer)
            mouse.click(coords=tuple(next(t[1] for t in state["tools_tabs"] if t[0] == "Переменные")))
            wait(lambda s: s.get("tools_page") == "Переменные", "Maker variable tab", path=maker_observer)
            for name, value in [
                ("variable_name", "credential"),
                ("variable_value", json.dumps({"credential": canary, "n": 23, "ok": True})),
            ]:
                maker_click(name)
                keyboard.send_keys("^a")
                paste(value)
            maker_click("variable_secret")
            maker_click("var_save")
            wait(
                lambda s: bool(
                    s.get("graph", {})
                    .get("variables", {})
                    .get("credential", {})
                    .get("value", {})
                    .get("secret_ref")
                ),
                "Maker protected",
                path=maker_observer,
            )
            maker_click("save")
            wait(
                lambda s: not s.get("dirty") and not s.get("busy"),
                "Maker saved protected",
                path=maker_observer,
            )
            assert canary not in secret_path.read_text(encoding="utf-8")
            maker_window.set_focus()
            keyboard.send_keys("%{F4}")
            assert maker_process.wait(timeout=35) == 0
            paste("")  # Clear the test credential from the clipboard.
            open_gui()
            add(secret_path, graph.name)
            secret_job = enqueue(graph.name)
            start()
            assert state_job(secret_job, {"SUCCEEDED", "FAILED", "UNKNOWN"}) == "SUCCEEDED"
            secret_evidence = evidence(secret_job)
            assert secret_evidence["public_report"]["worker_frozen"]
            assert server.secret_value == {"credential": canary, "n": 23, "ok": True}
            assert (root / "queue.key").read_bytes().strip() != (
                executable.parent / ".poster/private/session.key"
            ).read_bytes().strip()
            stop()
            scenarios.append("maker_gui_protected_typed_value_to_frozen_worker")
            window.capture_as_image().save(output / "04-maker-secret-succeeded.png")
            # Move the app directory after explicit service shutdown. User data
            # and the original Maker folder remain intact, with no migration.
            click("shutdown_service")
            wait(lambda _: not psutil.pid_exists(service["pid"]), "service explicit shutdown")
            close_gui()
            previous = executable.parent
            moved = previous.with_name(previous.name + " Перенесён")
            previous.rename(moved)
            executable = moved / executable.name
            open_gui()
            assert all_jobs()[secret_job]["status"] == "SUCCEEDED"
            assert (moved / marker.name).read_text(encoding="utf-8") == "keep user files"
            assert (moved / ".poster/private/session.key").exists()
            click("shutdown_service")
            wait(lambda _: not psutil.pid_exists(service["pid"]), "final service shutdown")
            close_gui()
            # Restore only the test-owned distribution's location for CI steps.
            moved.rename(previous)
            executable = previous / executable.name
            scenarios.append("move_bundle_stable_queue_and_key")
            for pid, birth in workers_seen.items():
                try:
                    p = psutil.Process(pid)
                    assert p.create_time() != birth or not p.is_running()
                except psutil.NoSuchProcess:
                    pass
            public = [observer, maker_observer, root / "settings.json", root / "dispatcher.sqlite"]
            public += list(root.rglob("report.json")) + list(root.glob("dispatcher.sqlite-*"))
            for path in public:
                if path.is_file():
                    assert canary.encode() not in path.read_bytes()
            report = {
                "commit": info["commit"],
                "status": "SUCCEEDED",
                "scenarios": scenarios,
                "launcher": "shared-runtime separate PosterService.exe / PosterWorker.exe",
                "worker_pids": sorted(workers_seen),
                "worker_count": len(workers_seen),
                "proofs": proofs,
                "path_restricted": [str(windows / "System32"), str(windows)],
                "harness_python_installed": True,
                "application_python_dependency": False,
                "secrets_not_in_public_state": True,
                "processes_closed": True,
            }
            (output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
            print("\n::notice title=Frozen Dispatcher GUI evidence::" + json.dumps(report), flush=True)
    except Exception as exc:
        safe_error = type(exc).__name__
        print(
            "\n::error title=Frozen Dispatcher GUI failure::"
            + json.dumps(
                {
                    "phase": phase,
                    "type": safe_error,
                    "state_error": snapshot().get("error"),
                    "action": snapshot().get("action"),
                    "connected": snapshot().get("connected"),
                    "service": snapshot().get("service"),
                    "job_states": [r["status"] for r in all_jobs().values()],
                }
            ),
            flush=True,
        )
        if window is not None and not phase.startswith("Maker"):
            try:
                window.capture_as_image().save(output / "failure.png")
            except Exception:
                pass
        raise AssertionError("Frozen Dispatcher failure: " + phase) from None
    finally:
        # Never upload private queue/snapshot/key files or observer files.
        for path in [observer, maker_observer]:
            path.unlink(missing_ok=True)
        for pid, birth in {**owned, **workers_seen}.items():
            try:
                p = psutil.Process(pid)
                if p.create_time() == birth:
                    for child in p.children(recursive=True):
                        child.kill()
                    p.kill()
                    p.wait(5)
            except psutil.Error:
                pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--exe", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    check(args.exe.resolve(), args.output.resolve())
