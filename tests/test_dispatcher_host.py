"""Independent service/client lifetime, real workers and Maker-key compatibility."""

import subprocess
import sys
import time

from cryptography.fernet import Fernet
import psutil
import pytest

from poster.dispatcher.host import ensure_service, request
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
