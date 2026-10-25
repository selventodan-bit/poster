import json
import os
from pathlib import Path

import pytest

from poster.dispatcher.processes import WorkerProcess
from poster.dispatcher.security import key_setup, seal, unseal
from poster.dispatcher.snapshots import Snapshots, digest
from poster.dispatcher.store import Store
from poster.maker.example_projects import chain
from poster.maker.model import Variable
from poster.maker.storage import Vault, save_graph


def start_worker(tmp_path, graph, *, inputs=None, secrets=None):
    source = tmp_path / "source" / "project.postergraph.json"
    source.parent.mkdir(exist_ok=True)
    save_graph(source, graph, Vault())
    store = Store(tmp_path / "q.sqlite")
    key_setup(store.root)
    pid, version = Snapshots(store.root).create(source)
    job = store.enqueue(pid, version, source, seal({"inputs": inputs or {}, "secrets": secrets or {}}))
    execution = store.claim("test-owner", 2, 2)
    worker = WorkerProcess(store, execution)
    worker.permit()
    return store, job, execution, worker


def test_actual_worker_reuses_graph_runner_without_gui_or_browser(tmp_path):
    graph = chain(
        "files", [("Files.WriteText", {"path": "данные с пробелами/result.txt", "text": "real worker"})]
    )
    store, job, execution, worker = start_worker(tmp_path, graph)
    try:
        assert worker.process.wait(timeout=15) == 0
        saved = store.show(job)
        assert saved["status"] == "SUCCEEDED" and saved["attempt"] == 1
        history = saved["history"][0]
        report = json.loads(Path(history["report"]).read_text())
        assert (
            report["worker_id"] != os.getpid()
            and report["gui_imports_forbidden"]
            and report["browser_imports_forbidden"]
        )
        assert digest(Path(history["report"]).read_bytes()) == history["report_sha256"]
        assert (Path(report["workdir"]) / "данные с пробелами/result.txt").read_text() == "real worker"
        assert not (Path(report["workdir"]).parent / "project").exists()
        encrypted = unseal((Path(report["workdir"]).parent / "results.enc").read_bytes())
        assert encrypted["results"][1]["action"] == "Files.WriteText"
    finally:
        worker.close(force=True)
        store.close()


def test_worker_input_secret_is_not_in_sqlite_reports_or_argv(tmp_path):
    secret = "PRIVATE_CREDENTIAL_CANARY_7639"
    graph = chain(
        "secrets",
        [
            (
                "Strings.Transform",
                {"operation": "upper", "value": "{{password}}"},
                {"save_as": "result", "result_path": "value"},
            )
        ],
    )
    graph.variables["password"] = Variable(value=None, input=True, required=True, secret=True)
    store, job, execution, worker = start_worker(tmp_path, graph, secrets={"password": secret})
    try:
        import psutil

        assert secret not in " ".join(psutil.Process(worker.process.pid).cmdline())
        assert worker.process.wait(timeout=15) == 0
        assert store.show(job)["status"] == "SUCCEEDED"
        raw = b"".join(p.read_bytes() for p in tmp_path.rglob("*") if p.is_file())
        assert secret.encode() not in raw
        report = store.show(job)["history"][0]["report"]
        assert secret not in Path(report).read_text()
    finally:
        worker.close(force=True)
        store.close()


@pytest.mark.parametrize(
    "name", ["02-lists-loop.postergraph.json", "03-csv-excel.postergraph.json", "04-parent.postergraph.json"]
)
def test_real_saved_examples_in_a_fresh_worker(tmp_path, name):
    from poster.maker.example_projects import source_directory

    source = source_directory() / name
    store = Store(tmp_path / "queue.sqlite")
    key_setup(store.root)
    pid, version = Snapshots(store.root).create(source)
    job = store.enqueue(pid, version, source, seal({"inputs": {}}))
    execution = store.claim("owner", 1, 1)
    worker = WorkerProcess(store, execution)
    worker.permit()
    try:
        assert worker.process.wait(timeout=20) == 0
        assert store.show(job)["status"] == "SUCCEEDED"
    finally:
        worker.close(force=True)
        store.close()


def test_unrelated_browser_dependency_never_weakens_data_only_worker(tmp_path):
    directory = tmp_path / "source"
    directory.mkdir()
    browser = chain("unused browser", [("Browser.OpenURL", {"url": "https://never-open.invalid/"})])
    save_graph(directory / "unrelated.postergraph.json", browser, Vault())
    graph = chain("data only", [("Files.WriteText", {"path": "result.txt", "text": "data"})])
    store, job, execution, worker = start_worker(tmp_path, graph)
    try:
        assert worker.process.wait(timeout=15) == 0
        report = json.loads(Path(store.history(job)[0]["report"]).read_text())
        assert report["browser_imports_forbidden"] and report["gui_imports_forbidden"]
    finally:
        worker.close(force=True)
        store.close()
