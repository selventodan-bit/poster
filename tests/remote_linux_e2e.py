"""Real isolated Controller, TLS relay, Linux Agent and GraphRunner child processes."""

import json
from pathlib import Path
import socket
import subprocess
import sys

import pytest

from poster.dispatcher.host import ensure_service, request
from poster.dispatcher.models import DispatcherError
from poster.dispatcher.processes import terminate_tree
from poster.remote.registry import read_json
from poster.remote.relay import Client
from poster.remote.setup import tls_files, relay_profiles, agent_init
from poster.maker.example_projects import chain
from poster.maker.storage import Vault, save_graph
from test_dispatcher_service import wait


@pytest.fixture
def remote(tmp_path):
    if sys.platform != "linux":
        pytest.fail("Remote Linux E2E belongs in the Linux-only CI target")
    controller = tmp_path / "Controller"
    info = ensure_service(controller)
    root = tmp_path / "transport"
    cert, key = tls_files(root, "127.0.0.1")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    relay_profiles(root, f"https://127.0.0.1:{port}", cert)
    relay = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "poster.remote",
            "relay",
            "--config",
            str(root / "relay.json"),
            "--database",
            str(root / "relay.sqlite"),
            "--cert",
            str(cert),
            "--key-file",
            str(key),
            "--port",
            str(port),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    client = Client(read_json(root / "controller-relay.json"), "controller")

    def ready():
        try:
            client.exchange()
            return True
        except DispatcherError:
            return False

    wait(ready)
    client.close()
    public = tmp_path / "controller-public.json"
    request(controller, "remote_identity", path=str(public))
    agent_root = tmp_path / "Linux Agent"
    agent_init(agent_root, "Linux fixture", public, root / "agent-relay.json")
    agent_id = request(
        controller,
        "remote_register",
        registration=str(agent_root / "registration.json"),
        relay_profile=str(root / "controller-relay.json"),
    )
    assert request(controller, "remote_agents")[0]["state"] == "PENDING"
    agent = subprocess.Popen(
        [sys.executable, "-m", "poster.remote", "agent", "--root", str(agent_root)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    context = dict(
        controller=controller,
        info=info,
        agent=agent,
        agent_id=agent_id,
        agent_root=agent_root,
        relay=relay,
        transport=root,
        cert=cert,
        key=key,
        port=port,
    )
    try:
        request(controller, "remote_approve", id=agent_id)
        wait(lambda: request(controller, "remote_agents")[0]["online"])
        yield context
    finally:
        try:
            request(controller, "shutdown")
        except DispatcherError:
            pass
        for process in [context["agent"], context["relay"]]:
            if process.poll() is None:
                terminate_tree(
                    process.pid, __import__("psutil").Process(process.pid).create_time(), group=False
                )
                process.wait(10)
        try:
            terminate_tree(context["info"]["pid"], context["info"]["birth"])
        except DispatcherError:
            pass


def enqueue(remote, tmp_path, actions, *, variables=None, secrets=None, outputs=None, timeout=300):
    graph = chain("Remote graph", actions)
    graph.variables.update(variables or {})
    source = tmp_path / (graph.id + ".postergraph.json")
    save_graph(source, graph, Vault())
    item = request(remote["controller"], "add", source=str(source))
    value = request(
        remote["controller"],
        "enqueue_batch",
        items=[
            dict(
                id=item["id"],
                agent_id=remote["agent_id"],
                remote_outputs=outputs or [],
                secrets=secrets or {},
                timeout=timeout,
            )
        ],
        request_id=graph.id,
    )
    assert not value["errors"], value
    return value["jobs"][0], item


def terminal(remote, job):
    def finished():
        value = request(remote["controller"], "show", id=job)
        return value if value["status"] not in {"PENDING", "LEASED", "RUNNING"} else None

    return wait(finished, timeout=45)


def test_real_remote_graph_and_output_are_returned_to_authoritative_queue(remote, tmp_path):
    job, item = enqueue(
        remote,
        tmp_path,
        [("Files.WriteText", {"path": "nested/output.txt", "text": "Linux GraphRunner output"})],
        outputs=["nested/output.txt"],
    )
    value = terminal(remote, job)
    assert value["status"] == "SUCCEEDED", value
    assert (Path(value["workdir"]) / "nested/output.txt").read_text() == "Linux GraphRunner output"
    assert value["report"]["remote_agent"] == remote["agent_id"]
    assert value["report"]["project_version"] == item["version"]
    assert value["report"]["gui_imports_forbidden"]
    assert type(value["history"][0]["pid"]) is int
    assert value["history"][0]["pid"] != remote["info"]["pid"]


def test_completed_workers_keep_agent_online_and_deliver_every_report(remote, tmp_path):
    root = remote["controller"]
    job, item = enqueue(
        remote,
        tmp_path,
        [("Files.WriteText", {"path": "out.txt", "text": "fast completion"})],
        outputs=["out.txt"],
    )
    jobs = []
    for index in range(20):
        if index:
            queued = request(
                root,
                "enqueue_batch",
                items=[dict(id=item["id"], agent_id=remote["agent_id"], remote_outputs=["out.txt"])],
                request_id="sequential-" + str(index),
            )
            assert not queued["errors"]
            job = queued["jobs"][0]
        jobs.append(job)
        assert terminal(remote, job)["status"] == "SUCCEEDED"
        assert remote["agent"].poll() is None
        assert (Path(request(root, "show", id=job)["workdir"]) / "out.txt").read_text() == "fast completion"
    assert len(set(jobs)) == 20


def test_protected_types_parallel_limits_and_cancellation(remote, tmp_path):
    from poster.maker.model import Variable
    from test_dispatcher_service import controlled_server

    root = remote["controller"]
    with controlled_server() as (server, base):
        variables = {"protected": Variable(input=True, secret=True, required=True)}
        secret = {"n": 17, "ok": True, "token": "REMOTE_PRIVATE_CANARY"}
        first, _ = enqueue(
            remote,
            tmp_path,
            [
                ("JSON.Stringify", {"value": {"variable": "protected"}}),
                ("HTTP.Post", {"url": base + "/hold/0"}),
                ("Files.WriteText", {"path": "out.txt", "text": "result"}),
            ],
            variables=variables,
            secrets={"protected": secret},
            outputs=["out.txt"],
        )
        second, _ = enqueue(remote, tmp_path, [("HTTP.Post", {"url": base + "/hold/1"})])
        wait(lambda: len(server.calls) == 2)
        wait(
            lambda: len({r["pid"] for r in request(root, "jobs", status="RUNNING")["rows"] if r["pid"]}) == 2
        )
        active = request(root, "jobs", status="RUNNING")["rows"]
        assert len(active) == 2 and len({r["pid"] for r in active}) == 2
        third, _ = enqueue(
            remote,
            tmp_path,
            [("Files.WriteText", {"path": "third.txt", "text": "third"})],
            outputs=["third.txt"],
        )
        assert request(root, "show", id=third)["status"] == "PENDING"
        server.gates["0"].set()
        server.gates["1"].set()
        assert terminal(remote, first)["status"] == "SUCCEEDED"
        assert terminal(remote, second)["status"] == "SUCCEEDED"
        assert terminal(remote, third)["status"] == "SUCCEEDED"
        # Decrypt only in this private test to prove ORIGINAL JSON types, never in public monitoring.
        import cryptography.fernet

        first_run = request(root, "show", id=first)["history"][0]["run_id"]
        result_path = root / "dispatcher.sqlite.data/runs" / first_run / "results.enc"
        value = json.loads(
            cryptography.fernet.Fernet((root / "queue.key").read_bytes().strip()).decrypt(
                result_path.read_bytes()
            )
        )
        assert value["variables"]["protected"] == "••••"
        assert b"REMOTE_PRIVATE_CANARY" not in (root / "dispatcher.sqlite").read_bytes()
        assert "REMOTE_PRIVATE_CANARY" not in json.dumps(request(root, "remote_agents"))
        assert "REMOTE_PRIVATE_CANARY" not in json.dumps(request(root, "show", id=first))
    delayed, _ = enqueue(remote, tmp_path, [("Logic.Delay", {"milliseconds": 120000})])
    wait(lambda: request(root, "show", id=delayed)["status"] == "RUNNING")
    request(root, "cancel", ids=[delayed])
    assert terminal(remote, delayed)["status"] in {"CANCELLED", "UNKNOWN"}


def test_actual_protected_json_types_arrive_at_real_http_endpoint(remote, tmp_path):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading
    from poster.maker.model import Variable

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.server.value = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        secret = {"n": 17, "ok": True, "nested": [None, 42, False], "token": "TYPED_REMOTE_CANARY"}
        job, _ = enqueue(
            remote,
            tmp_path,
            [
                (
                    "HTTP.Post",
                    {
                        "url": f"http://127.0.0.1:{server.server_port}/json",
                        "body_type": "json",
                        "body": {"variable": "protected"},
                    },
                )
            ],
            variables={"protected": Variable(input=True, secret=True, required=True)},
            secrets={"protected": secret},
        )
        assert terminal(remote, job)["status"] == "SUCCEEDED"
        assert server.value == secret and type(server.value["n"]) is int and type(server.value["ok"]) is bool
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


def restart_relay(remote):
    config = remote["transport"]
    remote["relay"] = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "poster.remote",
            "relay",
            "--config",
            str(config / "relay.json"),
            "--database",
            str(config / "relay.sqlite"),
            "--cert",
            str(remote["cert"]),
            "--key-file",
            str(remote["key"]),
            "--port",
            str(remote["port"]),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


@pytest.mark.parametrize("victim", ["agent", "controller", "relay", "worker"])
def test_crash_or_partition_never_repeats_accepted_post(remote, tmp_path, victim):
    import psutil
    from test_dispatcher_service import controlled_server

    root = remote["controller"]
    with controlled_server() as (server, base):
        job, _ = enqueue(remote, tmp_path, [("HTTP.Post", {"url": base + "/hold/0"})])
        wait(lambda: server.calls)
        wait(lambda: request(root, "show", id=job)["history"][0]["pid"])
        execution = request(root, "show", id=job)["history"][0]
        if victim == "worker":
            psutil.Process(execution["pid"]).kill()
        elif victim == "agent":
            remote["agent"].kill()
            remote["agent"].wait(10)
            remote["agent"] = subprocess.Popen(
                [sys.executable, "-m", "poster.remote", "agent", "--root", str(remote["agent_root"])],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        elif victim == "controller":
            psutil.Process(remote["info"]["pid"]).kill()
            from test_dispatcher_host import stopped

            wait(lambda: stopped(remote["info"]))
            remote["info"] = ensure_service(root)
            assert request(root, "ping")["running"]  # Trusted transport resumes on Service restart.
        else:
            remote["relay"].kill()
            remote["relay"].wait(10)
        value = terminal(remote, job)
        assert value["status"] == "UNKNOWN", value
        assert len(server.calls) == 1
        if victim == "relay":
            restart_relay(remote)
            wait(lambda: request(root, "remote_agents")[0]["online"])
        assert request(root, "show", id=job)["status"] == "UNKNOWN"
        assert len(request(root, "show", id=job)["history"]) == 1
        assert len(server.calls) == 1
        server.gates["0"].set()


def test_explicit_schedule_remote_target_and_revoke_without_local_fallback(remote, tmp_path):
    import time

    root = remote["controller"]
    job, item = enqueue(
        remote,
        tmp_path,
        [("Files.WriteText", {"path": "out.txt", "text": "remote schedule"})],
        outputs=["out.txt"],
    )
    assert terminal(remote, job)["status"] == "SUCCEEDED"
    schedule = request(
        root,
        "schedule_create",
        item_id=item["id"],
        spec={"mode": "once", "start": int(time.time()) + 2},
        agent_id=remote["agent_id"],
        remote_outputs=["out.txt"],
    )
    wait(lambda: request(root, "schedule_history", id=schedule)["total"] == 1)
    scheduled = request(root, "schedule_history", id=schedule)["rows"][0]["job_id"]
    assert terminal(remote, scheduled)["status"] == "SUCCEEDED"
    assert request(root, "show", id=scheduled)["agent_id"] == remote["agent_id"]
    assert (Path(request(root, "show", id=scheduled)["workdir"]) / "out.txt").read_text() == "remote schedule"
    request(root, "remote_revoke", id=remote["agent_id"])
    assert not request(root, "remote_agents")[0]["online"]
    value = request(
        root,
        "enqueue_batch",
        items=[dict(id=item["id"], agent_id=remote["agent_id"])],
        request_id="must-not-fallback",
    )
    assert not value["jobs"] and value["errors"][0]["code"] == "REMOTE_REVOKED"


def test_duplicate_transport_and_fresh_grant_cannot_repeat_a_run(remote, tmp_path):
    import sqlite3
    import time
    from cryptography.fernet import Fernet
    from poster.remote.protocol import Identity, pack
    from test_dispatcher_service import controlled_server

    root = remote["controller"]
    cipher = Fernet((root / "queue.key").read_bytes().strip())
    controller = Identity(json.loads(cipher.decrypt((root / "remote-identity.enc").read_bytes())))
    peer = read_json(remote["agent_root"] / "registration.json")["peer"]
    client = Client(read_json(remote["transport"] / "controller-relay.json"), "controller")
    try:
        with controlled_server() as (server, base):
            job, _ = enqueue(remote, tmp_path, [("HTTP.Post", {"url": base + "/hold/0"})])
            wait(lambda: server.calls)
            run_id = request(root, "show", id=job)["history"][0]["run_id"]
            with sqlite3.connect(root / "dispatcher.sqlite") as db:
                encrypted = db.execute(
                    "SELECT key_cipher FROM remote_runs WHERE run_id=?", (run_id,)
                ).fetchone()[0]
            grant = dict(
                kind="grant",
                run_id=run_id,
                key=json.loads(cipher.decrypt(encrypted))["key"],
                deadline=time.time() + 60,
            )
            first = pack(controller, peer, grant)
            second = pack(controller, peer, grant)
            client.exchange([first, first, second])

            def received():
                with sqlite3.connect(remote["agent_root"] / "agent-journal.sqlite") as db:
                    return (
                        db.execute(
                            "SELECT count(*) FROM seen WHERE id IN (?,?)", (first["id"], second["id"])
                        ).fetchone()[0]
                        == 2
                    )

            wait(received)
            assert len(server.calls) == 1 and len(request(root, "show", id=job)["history"]) == 1
            server.gates["0"].set()
            assert terminal(remote, job)["status"] == "SUCCEEDED"
            # A new signed permit for an old run cannot override its durable tombstone.
            late = pack(controller, peer, grant)
            client.exchange([pack(controller, peer, dict(kind="pulse", runs=[run_id], revoked=False)), late])

            def late_received():
                with sqlite3.connect(remote["agent_root"] / "agent-journal.sqlite") as db:
                    return db.execute("SELECT 1 FROM seen WHERE id=?", (late["id"],)).fetchone()

            wait(late_received)
            wait(lambda: read_json(remote["agent_root"] / "status.json")["active"] == 0)
            assert len(server.calls) == 1
    finally:
        client.close()


def test_browser_timeout_cleans_actual_browser_processes(remote, tmp_path):
    import psutil
    from poster.fixtures import serve_fixture

    with serve_fixture() as (_, base):
        job, _ = enqueue(
            remote,
            tmp_path,
            [("Browser.OpenURL", {"url": base + "/classic/new"}), ("Logic.Delay", {"milliseconds": 120000})],
            timeout=4,
        )
        wait(
            lambda: (
                request(remote["controller"], "show", id=job)["history"]
                and request(remote["controller"], "show", id=job)["history"][0]["pid"]
            )
        )
        pid = request(remote["controller"], "show", id=job)["history"][0]["pid"]
        descendants = wait(lambda: psutil.Process(pid).children(recursive=True))
        result = terminal(remote, job)
        assert result["status"] == "UNKNOWN"

        def gone():
            for process in descendants:
                try:
                    if process.is_running() and process.status() != psutil.STATUS_ZOMBIE:
                        return False
                except psutil.NoSuchProcess:
                    pass
            return True

        wait(gone)


def test_wrong_relay_credentials_and_untrusted_tls_are_rejected(remote):
    profile = read_json(remote["transport"] / "agent-relay.json")
    bad = Client({**profile, "token": "bad" * 20}, "agent")
    try:
        with pytest.raises(DispatcherError):
            bad.exchange()
    finally:
        bad.close()
    unknown_ca = Client({**profile, "ca": None, "ca_pem": None}, "agent")
    try:
        with pytest.raises(DispatcherError):
            unknown_ca.exchange()
    finally:
        unknown_ca.close()


@pytest.mark.parametrize("line_ending", ["LF", "CRLF"])
def test_committed_loop_csv_excel_and_nested_projects_run_unchanged(remote, tmp_path, line_ending):
    from poster.maker.example_projects import source_directory

    directory = tmp_path / "saved"
    directory.mkdir()
    for path in source_directory().glob("*.postergraph.json"):
        raw = path.read_bytes()
        if line_ending == "CRLF":
            raw = raw.replace(b"\n", b"\r\n")
        (directory / path.name).write_bytes(raw)
    for name, outputs in [
        ("02-lists-loop.postergraph.json", ["lists/result.json"]),
        ("03-csv-excel.postergraph.json", ["tables/output.csv", "tables/output.xlsx"]),
        ("04-parent.postergraph.json", ["nested/result.txt"]),
    ]:
        item = request(remote["controller"], "add", source=str(directory / name))
        value = request(
            remote["controller"],
            "enqueue_batch",
            items=[dict(id=item["id"], agent_id=remote["agent_id"], remote_outputs=outputs)],
            request_id=name,
        )
        assert not value["errors"]
        result = terminal(remote, value["jobs"][0])
        assert result["status"] == "SUCCEEDED", result
        assert all((Path(result["workdir"]) / output).is_file() for output in outputs)
        if name.startswith("02"):
            assert json.loads((Path(result["workdir"]) / outputs[0]).read_text()) == [
                "ALPHA",
                "BETA",
                "ГАММА",
            ]
        if name.startswith("04"):
            assert (Path(result["workdir"]) / outputs[0]).read_text() == "POSTER Ж"


def test_secretref_snapshot_bytes_and_master_key_boundary(remote, tmp_path, monkeypatch):
    import base64
    import sqlite3
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from cryptography.fernet import Fernet
    from poster.maker.model import Variable

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.server.value = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            self.server.gate.wait(20)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.gate = threading.Event()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        maker_key = Fernet.generate_key()
        keyfile = tmp_path / "maker.key"
        keyfile.write_bytes(maker_key)
        monkeypatch.setenv("POSTER_SESSION_KEY", maker_key.decode())
        secret = {"n": 23, "ok": True, "token": "SECRETREF_REMOTE_CANARY"}
        vault = Vault()
        graph = chain(
            "Maker SecretRef",
            [
                (
                    "HTTP.Post",
                    {
                        "url": f"http://127.0.0.1:{server.server_port}/post",
                        "body_type": "json",
                        "body": {"variable": "saved"},
                    },
                )
            ],
        )
        graph.variables["saved"] = Variable(secret=True, value=vault.put_value(secret))
        source = tmp_path / "secret.postergraph.json"
        save_graph(source, graph, vault)
        original = source.read_bytes()
        root = remote["controller"]
        item = request(root, "add", source=str(source), key_file=str(keyfile))
        value = request(
            root,
            "enqueue_batch",
            items=[dict(id=item["id"], agent_id=remote["agent_id"])],
            request_id="secret-ref",
        )
        assert not value["errors"]
        job = value["jobs"][0]
        wait(lambda: hasattr(server, "value"))
        assert server.value == secret
        run = request(root, "show", id=job)["history"][0]["run_id"]
        master = (root / "queue.key").read_bytes().strip()
        with sqlite3.connect(root / "dispatcher.sqlite") as db:
            encrypted = db.execute("SELECT key_cipher FROM remote_runs WHERE run_id=?", (run,)).fetchone()[0]
        temporary = json.loads(Fernet(master).decrypt(encrypted))["key"].encode()
        bundle_path = remote["agent_root"] / "runs" / run / "snapshots" / item["version"] / "bundle.enc"
        bundle = json.loads(Fernet(temporary).decrypt(bundle_path.read_bytes()))
        assert base64.b64decode(bundle["files"][source.name]) == original
        for file in remote["agent_root"].rglob("*"):
            if file.is_file() and file.suffix != ".lock":
                try:
                    content = file.read_bytes()
                except FileNotFoundError:
                    continue
                assert master not in content and maker_key not in content
        server.gate.set()
        assert terminal(remote, job)["status"] == "SUCCEEDED"
    finally:
        server.gate.set()
        server.shutdown()
        server.server_close()
        thread.join(5)
