"""Independent service/client lifetime, real workers and Maker-key compatibility."""

import subprocess
import sys
import time
import json
from pathlib import Path

from cryptography.fernet import Fernet
import psutil
import pytest

from poster.dispatcher.host import ensure_service, request, endpoint
from poster.dispatcher.models import DispatcherError
from poster.dispatcher.processes import same_process, terminate_tree
from poster.dispatcher.workspace import queue_key
from poster.maker.example_projects import chain
from poster.maker.model import Variable
from poster.maker.storage import Vault, save_graph
from test_dispatcher_service import controlled_server, wait


def stopped(info):
    try:
        process = same_process(info["pid"], info["birth"])
        return process is None or process.status() == psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return True  # Exit between create_time() and status() is confirmed exit.


@pytest.fixture
def host(tmp_path):
    root = tmp_path / "Служба с пробелом"
    info = ensure_service(root)
    try:
        yield root, info
    finally:
        try:
            request(root, "shutdown")
            wait(lambda: stopped(info))
        except (DispatcherError, AssertionError):
            terminate_tree(info["pid"], info["birth"])


def register(root, path, actions, name="Рабочий проект", variables=None, vault=None):
    graph = chain(name, actions)
    graph.variables.update(variables or {})
    save_graph(path, graph, vault or Vault())
    return request(root, "add", source=str(path))


def test_service_survives_client_exit_and_preserves_catalog_and_history(host, tmp_path):
    root, info = host
    with controlled_server() as (server, base):
        item = register(root, tmp_path / "main.postergraph.json", [("HTTP.Post", {"url": base + "/hold/0"})])
        job = request(root, "enqueue", ids=[item["id"]], request_id="stable-admission")[0]
        request(root, "start")
        wait(lambda: server.calls)
        # A short-lived independent client exits while the real worker is blocked.
        script = "from poster.dispatcher.host import request; import sys; request(__import__('pathlib').Path(sys.argv[1]),'ping')"
        subprocess.run([sys.executable, "-c", script, str(root)], check=True, timeout=15)
        from multiprocessing import connection

        address, family = endpoint(root)
        raw = connection.PipeClient(address) if family == "AF_PIPE" else connection.SocketClient(address)
        raw.close()  # GUI crash in the middle of authentication, before JSON.
        assert request(root, "ping")["pid"] == info["pid"]
        assert request(root, "show", id=job)["status"] == "RUNNING"
        execution = request(root, "show", id=job)["history"][0]
        assert execution["pid"] != info["pid"]
        server.gates["0"].set()
        wait(lambda: request(root, "show", id=job)["status"] == "SUCCEEDED")
        assert request(root, "show", id=job)["report"]["status"] == "SUCCEEDED"
        request(root, "stop")
        wait(lambda: not request(root, "ping")["running"])
        request(root, "remove", ids=[item["id"]])
        assert not request(root, "projects")
        assert request(root, "jobs")["rows"][0]["name"] == "Рабочий проект"
        assert request(root, "enqueue", ids=[], request_id="empty") == []
        assert len(server.calls) == 1


def test_maker_distinct_key_secret_types_through_service_and_worker(host, tmp_path, monkeypatch):
    root, _ = host
    maker = tmp_path / "Maker Ж" / ".poster"
    private = maker / "private"
    private.mkdir(parents=True)
    key = Fernet.generate_key()
    (private / "session.key").write_bytes(key)
    monkeypatch.setenv("POSTER_SESSION_KEY", key.decode())
    vault = Vault()
    secret = vault.put_value({"credential": "CANARY_MAKER_KEY_4B1", "n": 23, "ok": True})
    item = register(
        root,
        maker / "projects" / "secret.postergraph.json",
        [("JSON.Stringify", {"value": {"variable": "protected"}})],
        variables={"protected": Variable(secret=True, value=secret)},
        vault=vault,
    )
    assert "project_key" not in item
    job = request(root, "enqueue", ids=[item["id"]], request_id="secret-job")[0]
    request(root, "start")
    wait(lambda: request(root, "show", id=job)["status"] in {"SUCCEEDED", "FAILED", "UNKNOWN"})
    value = request(root, "show", id=job)
    assert value["status"] == "SUCCEEDED", value
    assert value["report"]["gui_imports_forbidden"] and value["report"]["browser_imports_forbidden"]
    for path in root.rglob("*"):
        if path.is_file() and path.suffix not in {".key"}:
            if path.suffix == ".lock":
                # Windows byte-range locks deliberately deny concurrent reads;
                # their single marker byte cannot contain credential data.
                assert path.stat().st_size <= 1
                continue
            assert b"CANARY_MAKER_KEY_4B1" not in path.read_bytes()
            assert key not in path.read_bytes()


def test_service_crash_recovers_without_post_retry(host, tmp_path):
    root, info = host
    with controlled_server() as (server, base):
        item = register(root, tmp_path / "main.postergraph.json", [("HTTP.Post", {"url": base + "/hold/0"})])
        job = request(root, "enqueue", ids=[item["id"]], request_id="write-once")[0]
        request(root, "start")
        wait(lambda: server.calls)
        # Kill only the host: Windows Job Object / control-pipe EOF owns cleanup.
        psutil.Process(info["pid"]).kill()
        wait(lambda: stopped(info))
        time.sleep(0.2)
        replacement = ensure_service(root)
        try:
            request(root, "reconcile")
            assert request(root, "show", id=job)["status"] == "REVIEW_REQUIRED"
            request(root, "start")
            assert request(root, "enqueue", ids=[item["id"]], request_id="write-once") == [job]
            assert len(server.calls) == 1
        finally:
            server.gates["0"].set()
            request(root, "shutdown")
            wait(lambda: stopped(replacement))


def test_missing_existing_queue_key_never_replaced(tmp_path, monkeypatch):
    root = tmp_path / "state"
    queue_key(root)
    (root / "dispatcher.sqlite").touch()
    (root / "queue.key").unlink()
    monkeypatch.delenv("POSTER_SESSION_KEY", raising=False)
    with pytest.raises(DispatcherError, match="Ключ"):
        queue_key(root)
    assert not (root / "queue.key").exists()


def test_incorrect_maker_key_does_not_register_or_replace_it(host, tmp_path, monkeypatch):
    root, _ = host
    monkeypatch.setenv("POSTER_SESSION_KEY", Fernet.generate_key().decode())
    vault = Vault()
    graph = chain("Secret", [])
    graph.variables["password"] = Variable(secret=True, value=vault.put("CANARY_NO_MATCH"))
    path = tmp_path / "export" / "unknown.postergraph.json"
    save_graph(path, graph, vault)
    with pytest.raises(DispatcherError) as error:
        request(root, "add", source=str(path))
    assert error.value.code == "PROJECT_KEY_REQUIRED"
    assert request(root, "projects") == []


def test_registered_snapshot_survives_source_removal_and_report_tampering_is_rejected(host, tmp_path):
    root, _ = host
    source = tmp_path / "immutable.postergraph.json"
    item = register(root, source, [("Files.WriteText", {"path": "out.txt", "text": "original version"})])
    source.unlink()  # No live-file fallback when queueing an inspected version.
    job = request(root, "enqueue", ids=[item["id"]], request_id="immutable")[0]
    request(root, "start")
    wait(lambda: request(root, "show", id=job)["status"] == "SUCCEEDED")
    request(root, "stop")
    wait(lambda: not request(root, "ping")["running"])
    report = request(root, "show", id=job)
    assert (Path(report["workdir"]) / "out.txt").read_text() == "original version"
    path = Path(report["history"][0]["report"])
    raw = path.read_bytes()
    value = json.loads(raw)
    value["status"] = "FAILED"
    path.write_text(json.dumps(value))
    with pytest.raises(DispatcherError) as error:
        request(root, "show", id=job)
    assert error.value.code == "REPORT_INVALID"
    assert request(root, "jobs")["rows"][0]["attempt"] == 1
    path.write_bytes(raw)


def test_unrelated_foreign_secret_graph_does_not_require_its_key_and_input_defaults_are_kept(
    host, tmp_path, monkeypatch
):
    root, _ = host
    monkeypatch.setenv("POSTER_SESSION_KEY", Fernet.generate_key().decode())
    vault = Vault()
    foreign = chain("Foreign key", [])
    foreign.variables["private"] = Variable(secret=True, value=vault.put("FOREIGN_PRIVATE_CANARY"))
    save_graph(tmp_path / "foreign.postergraph.json", foreign, vault)
    item = register(
        root,
        tmp_path / "default.postergraph.json",
        [
            ("Files.WriteText", {"path": "default.txt", "text": "{{note}}"}),
        ],
        variables={"note": Variable(input=True, required=True, value="Saved Maker default")},
    )
    assert item["inputs"] == [
        {"name": "note", "secret": False, "required": True, "has_default": True, "type": "str"}
    ]
    job = request(root, "enqueue", ids=[item["id"]], request_id="use-default")[0]
    request(root, "start")
    wait(lambda: request(root, "show", id=job)["status"] == "SUCCEEDED")
    assert (
        Path(request(root, "show", id=job)["workdir"]) / "default.txt"
    ).read_text() == "Saved Maker default"


def test_service_rejects_live_settings_change_and_bounded_pagination(host, tmp_path):
    from poster.dispatcher.store import Store

    root, _ = host
    source = tmp_path / "page.postergraph.json"
    item = register(root, source, [])
    store = Store(root / "dispatcher.sqlite")
    cipher = Fernet((root / "queue.key").read_bytes())
    try:
        for _ in range(430):
            store.enqueue(
                item["project_id"], item["version"], source, cipher.encrypt(b'{"inputs":{},"secrets":{}}')
            )
    finally:
        store.close()
    first = request(root, "jobs", search="Рабочий", order="created", descending=False)
    second = request(root, "jobs", offset=200, status="PENDING", order="created", descending=False)
    last = request(root, "jobs", offset=400, order="created", descending=False)
    assert first["total"] == second["total"] == 430
    assert len(first["rows"]) == len(second["rows"]) == 200 and len(last["rows"]) == 30
    assert not {r["id"] for r in first["rows"]} & {r["id"] for r in second["rows"]}
    request(root, "cancel", ids=[r["id"] for page in [first, second, last] for r in page["rows"]])
    assert request(root, "ping")["counts"] == {"CANCELLED": 430}
    request(root, "start")
    with pytest.raises(DispatcherError) as error:
        request(root, "settings", max_workers=4, max_per_project=2, timeout=13)
    assert error.value.code == "DISPATCHER_BUSY"
    request(root, "stop")
    wait(lambda: not request(root, "ping")["running"])
    assert request(root, "settings", max_workers=4, max_per_project=2, timeout=13)["max_workers"] == 4
