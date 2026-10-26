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
    try:
        request(controller, "remote_approve", id=agent_id)
        wait(lambda: request(controller, "remote_agents")[0]["online"])
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


def enqueue(remote, tmp_path, actions, *, variables=None, secrets=None, outputs=None):
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

    return wait(finished, timeout=30)


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
    assert value["history"][0]["pid"] != remote["info"]["pid"]


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
            request(root, "start")
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
