from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import sys
import threading
import time

import psutil
import pytest

from poster.dispatcher.models import DispatcherError
from poster.dispatcher.service import Dispatcher, enqueue
from poster.dispatcher.store import Store
from poster.maker.example_projects import chain
from poster.maker.model import Variable
from poster.maker.storage import Vault, save_graph

ROOT = Path(__file__).resolve().parents[1]
SERVICE = """import sys
from pathlib import Path
from poster.dispatcher.store import Store
from poster.dispatcher.service import Dispatcher
s=Store(Path(sys.argv[1]))
try: Dispatcher(s,max_workers=int(sys.argv[2]),max_per_project=int(sys.argv[3])).run(once=sys.argv[4]=='1')
finally:s.close()
"""


def wait(predicate, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.025)
    raise AssertionError("Dispatcher condition was not reached")


def launch(store, workers=2, per_project=1, once=True):
    return subprocess.Popen(
        [
            sys.executable,
            "-c",
            SERVICE,
            str(store.path),
            str(workers),
            str(per_project),
            "1" if once else "0",
        ],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def finish(process):
    out, err = process.communicate(timeout=25)
    assert process.returncode == 0, (out.decode(errors="replace"), err.decode(errors="replace"))


def graph_file(tmp_path, actions, name="main", same_id=None, variables=None):
    directory = tmp_path / name
    directory.mkdir(exist_ok=True)
    path = directory / (name + ".postergraph.json")
    graph = chain(name, actions)
    if same_id:
        graph.id = same_id
    graph.variables.update(variables or {})
    save_graph(path, graph, Vault())
    return path


@contextmanager
def controlled_server():
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def request(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            with self.server.condition:
                self.server.calls.append(
                    {"method": self.command, "path": self.path, "cookie": self.headers.get("Cookie", "")}
                )
                self.server.condition.notify_all()
            raw = json.dumps({"value": 3, "cookie": self.headers.get("Cookie", "")}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            if self.path.startswith("/hold/"):
                self.server.gates[self.path.rsplit("/", 1)[-1]].wait(20)
            try:
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                pass

        do_GET = do_POST = request

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    server.calls = []
    server.condition = threading.Condition()
    server.gates = {str(i): threading.Event() for i in range(5)}
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, f"http://127.0.0.1:{server.server_port}"
    finally:
        for gate in server.gates.values():
            gate.set()
        server.shutdown()
        server.server_close()
        thread.join(5)


@pytest.mark.parametrize(
    "workers,per_project,same_project,parallel",
    [(1, 2, False, False), (2, 2, False, True), (2, 1, True, False), (2, 2, True, True)],
)
def test_real_process_concurrency_limits_and_isolation(
    tmp_path, workers, per_project, same_project, parallel
):
    with controlled_server() as (server, base):
        store = Store(tmp_path / "q.sqlite")
        jobs = []
        for i in range(2):
            path = graph_file(
                tmp_path,
                [
                    (
                        "HTTP.SetCookie",
                        {"name": "sid", "value": "{{job_label}}", "url": base, "secure": False},
                    ),
                    ("HTTP.Get", {"url": base + "/hold/{{job_label}}", "protect_response": False}),
                    ("Files.WriteText", {"path": "same.txt", "text": "{{job_label}}"}),
                ],
                name=str(i),
                same_id="same" if same_project else None,
                variables={"job_label": Variable(value=None, input=True, required=True)},
            )
            jobs.append(enqueue(store, path, inputs={"job_label": str(i)}))
        process = launch(store, workers, per_project)
        try:
            wait(lambda: len(server.calls) >= 1)
            if parallel:
                wait(lambda: len(server.calls) == 2)
                assert len({store.history(job)[0]["pid"] for job in jobs}) == 2
            else:
                assert sorted(store.show(job)["status"] for job in jobs) == ["PENDING", "RUNNING"]
                assert len(server.calls) == 1
            server.gates["0"].set()
            wait(lambda: len(server.calls) == 2)
            server.gates["1"].set()
            finish(process)
            assert [store.show(job)["status"] for job in jobs] == ["SUCCEEDED", "SUCCEEDED"]
            reports = [json.loads(Path(store.history(job)[0]["report"]).read_text()) for job in jobs]
            assert reports[0]["workdir"] != reports[1]["workdir"]
            for i, r in enumerate(reports):
                assert (Path(r["workdir"]) / "same.txt").read_text() == str(i)
            assert {c["path"]: c["cookie"] for c in server.calls} == {"/hold/0": "sid=0", "/hold/1": "sid=1"}
            assert all(r["browser_imports_forbidden"] for r in reports)
        finally:
            for gate in server.gates.values():
                gate.set()
            if process.poll() is None:
                process.kill()
                process.wait()
            store.close()


def test_stop_drains_active_preserves_pending_and_restarts(tmp_path):
    with controlled_server() as (server, base):
        store = Store(tmp_path / "q.sqlite")
        jobs = []
        for i in range(2):
            source = graph_file(tmp_path, [("HTTP.Get", {"url": base + "/hold/" + str(i)})], name=str(i))
            jobs.append(enqueue(store, source))
        process = launch(store, workers=1, once=False)
        try:
            wait(lambda: len(server.calls) == 1)
            store.request_stop()
            server.gates["0"].set()
            finish(process)
            assert store.show(jobs[0])["status"] == "SUCCEEDED" and store.show(jobs[1])["status"] == "PENDING"
            process = launch(store, workers=1)
            wait(lambda: len(server.calls) == 2)
            server.gates["1"].set()
            finish(process)
            assert store.show(jobs[1])["status"] == "SUCCEEDED"
        finally:
            for gate in server.gates.values():
                gate.set()
            if process.poll() is None:
                process.kill()
                process.wait()
            store.close()


@pytest.mark.parametrize("operation", ["cancel", "worker_crash", "dispatcher_crash"])
def test_post_admitted_without_ack_never_repeats_after_recovery(tmp_path, operation):
    with controlled_server() as (server, base):
        source = graph_file(
            tmp_path, [("HTTP.Post", {"url": base + "/hold/0", "body_type": "json", "body": {"write": True}})]
        )
        store = Store(tmp_path / "q.sqlite")
        job = enqueue(store, source, key="single-write")
        process = launch(store)
        try:
            wait(lambda: len(server.calls) == 1)
            execution = store.history(job)[0]
            pid, birth = execution["pid"], execution["birth"]
            if operation == "cancel":
                store.cancel(job)
                finish(process)
            elif operation == "worker_crash":
                psutil.Process(pid).kill()
                finish(process)
            else:
                process.kill()
                process.wait(timeout=10)
                Dispatcher(store).reconcile()
            wait(lambda: store.show(job)["status"] in {"UNKNOWN", "REVIEW_REQUIRED"})
            Dispatcher(store).run(once=True)
            assert enqueue(store, source, key="single-write") == job
            assert len(server.calls) == 1 and store.show(job)["attempt"] == 1
            process_obj = None
            try:
                process_obj = psutil.Process(pid)
            except psutil.NoSuchProcess:
                pass
            assert (
                process_obj is None
                or abs(process_obj.create_time() - birth) > 0.01
                or process_obj.status() == psutil.STATUS_ZOMBIE
            )
        finally:
            server.gates["0"].set()
            if process.poll() is None:
                process.kill()
                process.wait()
            store.close()


def test_timeout_and_cancel_without_external_effects(tmp_path):
    source = graph_file(
        tmp_path,
        [
            ("Files.WriteText", {"path": "kept.txt", "text": "already written"}),
            ("Logic.Delay", {"milliseconds": 10000}),
        ],
    )
    store = Store(tmp_path / "q.sqlite")
    job = enqueue(store, source, timeout=0.6)
    Dispatcher(store).run(once=True)
    assert store.show(job)["status"] == "TIMED_OUT"
    job = enqueue(store, source, timeout=15)
    process = launch(store)
    try:
        wait(lambda: bool(store.history(job)) and store.history(job)[0]["last_confirmed_step"] == "n0")
        store.cancel(job)
        finish(process)
        assert store.show(job)["status"] == "CANCELLED"
        report = json.loads(Path(store.history(job)[0]["report"]).read_text())
        assert report["outputs_may_be_partial"] and not report["external_effects_may_have_occurred"]
        assert (Path(report["workdir"]) / "kept.txt").read_text() == "already written"
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        store.close()


def test_snapshot_execution_ignores_modified_source_and_rejects_tamper(tmp_path):
    source = graph_file(tmp_path, [("Files.WriteText", {"path": "result.txt", "text": "original"})])
    store = Store(tmp_path / "q.sqlite")
    job = enqueue(store, source)
    graph = chain("changed", [("Files.WriteText", {"path": "result.txt", "text": "CHANGED"})])
    save_graph(source, graph, Vault())
    Dispatcher(store).run(once=True)
    report = json.loads(Path(store.history(job)[0]["report"]).read_text())
    assert (Path(report["workdir"]) / "result.txt").read_text() == "original"
    second = enqueue(store, source)
    manifest = store.root / "snapshots" / store.show(second)["version"] / "manifest.json"
    manifest.write_text("{}")
    Dispatcher(store).run(once=True)
    assert store.show(second)["status"] == "FAILED"
    assert store.history(second)[0]["code"] == "SNAPSHOT_INVALID"
    store.close()


def test_second_dispatcher_cannot_recover_a_live_dispatcher(tmp_path):
    source = graph_file(tmp_path, [("Logic.Delay", {"milliseconds": 10000})])
    store = Store(tmp_path / "q.sqlite")
    job = enqueue(store, source)
    process = launch(store)
    try:
        wait(lambda: store.show(job)["status"] == "RUNNING")
        with pytest.raises(DispatcherError, match="уже обслуживает"):
            Dispatcher(store).run(once=True)
        with pytest.raises(DispatcherError, match="уже обслуживает"):
            Dispatcher(store).reconcile()
        assert store.show(job)["attempt"] == 1
        store.cancel(job)
        finish(process)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        store.close()


def test_repeated_stop_start_cycles_keep_history(tmp_path):
    source = graph_file(tmp_path, [("Variables.Set", {"name": "answer", "value": 42})])
    path = tmp_path / "q.sqlite"
    for _ in range(3):
        store = Store(path)
        job = enqueue(store, source)
        process = launch(store, once=False)
        try:
            wait(lambda store=store, job=job: store.show(job)["status"] == "SUCCEEDED")
            store.request_stop()
            finish(process)
            assert len(store.history(job)) == 1
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            store.close()
    store = Store(path)
    assert store.counts() == {"SUCCEEDED": 3}
    store.close()


def test_explicit_profile_is_serialized_across_independent_queues(tmp_path):
    from poster.maker.profiles import Profiles

    profiles = Profiles(tmp_path / "profiles")
    profile_id = profiles.create("Controlled profile")
    with controlled_server() as (server, base):
        stores = [Store(tmp_path / f"{i}.sqlite") for i in range(2)]
        sources = [
            graph_file(tmp_path, [("Browser.OpenURL", {"url": base + "/hold/" + str(i)})], name=str(i))
            for i in range(2)
        ]
        jobs = [
            enqueue(store, source, profile_root=profiles.root, profile_id=profile_id)
            for store, source in zip(stores, sources, strict=True)
        ]
        processes = []
        try:
            processes.append(launch(stores[0]))
            wait(lambda: any(c["path"] == "/hold/0" for c in server.calls))
            processes.append(launch(stores[1]))
            wait(lambda: stores[1].show(jobs[1])["status"] == "RUNNING")
            assert not any(c["path"] == "/hold/1" for c in server.calls)
            server.gates["0"].set()
            finish(processes[0])
            wait(lambda: any(c["path"] == "/hold/1" for c in server.calls))
            server.gates["1"].set()
            finish(processes[1])
            assert all(
                store.show(job)["status"] == "SUCCEEDED" for store, job in zip(stores, jobs, strict=True)
            )
            assert (
                profiles.open(profile_id).data["revision"] == 0
            )  # Dispatcher never implicitly saves a session.
        finally:
            for gate in server.gates.values():
                gate.set()
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.wait()
            for store in stores:
                store.close()


def test_worker_browser_crash_reaps_owned_browser_descendants(tmp_path):
    with controlled_server() as (server, base):
        source = graph_file(tmp_path, [("Browser.OpenURL", {"url": base + "/hold/0"})])
        store = Store(tmp_path / "q.sqlite")
        job = enqueue(store, source)
        process = launch(store)
        try:
            wait(lambda: any(c["path"] == "/hold/0" for c in server.calls))
            worker = psutil.Process(store.history(job)[0]["pid"])
            descendants = [(child.pid, child.create_time()) for child in worker.children(recursive=True)]
            assert descendants  # Real Chromium / Playwright processes, no mock provider.
            worker.kill()
            finish(process)
            assert store.show(job)["status"] == "UNKNOWN"

            def gone():
                for pid, birth in descendants:
                    try:
                        candidate = psutil.Process(pid)
                        if (
                            abs(candidate.create_time() - birth) < 0.01
                            and candidate.status() != psutil.STATUS_ZOMBIE
                        ):
                            return False
                    except psutil.NoSuchProcess:
                        pass
                return True

            wait(gone)
        finally:
            server.gates["0"].set()
            if process.poll() is None:
                process.kill()
                process.wait()
            store.close()


def test_snapshot_secret_sidecars_and_typed_inputs_survive_project_run(tmp_path):
    from poster.maker.model import Variable

    directory = tmp_path / "source"
    directory.mkdir()
    child = chain(
        "child",
        [
            ("Variables.Compare", {"left": "{{child_secret}}", "right": {"flag": True}}),
            (
                "JSON.Get",
                {"value": "{{argument}}", "path": "/number"},
                {"save_as": "result", "result_path": "value"},
            ),
        ],
    )
    child_vault = Vault()
    child.variables["child_secret"] = Variable(secret=True, value=child_vault.put_value({"flag": True}))
    child.variables["argument"] = Variable(secret=True, input=True, required=True, value=None)
    child.extensions["outputs"] = ["result"]
    save_graph(directory / "child.postergraph.json", child, child_vault)
    parent = chain(
        "parent",
        [
            (
                "Project.Run",
                {"path": "child.postergraph.json", "inputs": {"argument": "{{payload}}"}},
                {"save_as": "child", "result_path": "outputs"},
            ),
            (
                "JSON.Get",
                {"value": "{{child}}", "path": "/result"},
                {"save_as": "number", "result_path": "value"},
            ),
            ("Variables.Compare", {"left": "{{number}}", "right": 7}),
        ],
    )
    parent_vault = Vault()
    parent.variables["payload"] = Variable(
        secret=True, value=parent_vault.put_value({"number": 7, "flag": True})
    )
    source = directory / "parent.postergraph.json"
    save_graph(source, parent, parent_vault)
    store = Store(tmp_path / "q.sqlite")
    try:
        job = enqueue(store, source)
        Dispatcher(store).run(once=True)
        assert store.show(job)["status"] == "SUCCEEDED"
        assert store.history(job)[0]["last_confirmed_step"] == "end"
    finally:
        store.close()
