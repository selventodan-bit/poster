"""Real Linux-side harness control and HTTP/TLS fixture test; not Windows/Linux evidence."""

import hashlib
import json
from pathlib import Path
import socket
import subprocess
import sys
import uuid

import httpx

from poster.remote.relay import Client
from poster.dispatcher.models import DispatcherError
from test_dispatcher_service import wait


def test_linux_peer_real_processes_and_private_ssh_pipe_contract(tmp_path):
    helper = Path(__file__).resolve().parents[1] / "scripts/crosshost/linux_peer.py"
    root = tmp_path / "fresh-peer"
    session = uuid.uuid4().hex

    def call(action, **payload):
        value = subprocess.run(
            [sys.executable, str(helper), "--root", str(root), action],
            input=json.dumps(dict(payload, session=session)).encode(),
            capture_output=True,
            timeout=60,
        )
        assert value.returncode == 0, (action, value.stderr)
        return json.loads(value.stdout)

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    try:
        profile = call("prepare", hostname="127.0.0.1", url=f"https://127.0.0.1:{port}", port=port)
        client = Client(profile["controller"], "controller")
        try:

            def ready():
                try:
                    return client.probe()["tls_verified"]
                except DispatcherError:
                    return False

            wait(ready)
        finally:
            client.close()
        state = wait(lambda: call("observe").get("url"))
        call("release", label="typed")
        private = dict(n=17, ok=True, protected="PRIVATE_PEER_CANARY")
        with httpx.Client(trust_env=False) as http:
            assert http.post(state + "/typed", json=private).status_code == 200
        value = call("observe")
        assert value["platform"] == "linux" and value["uid"] != 0
        assert (
            value["calls"][0]["sha256"]
            == hashlib.sha256(json.dumps(private, sort_keys=True).encode()).hexdigest()
        )
        assert value["calls"][0]["types"]["n"] == "int"
        assert b"PRIVATE_PEER_CANARY" not in (root / "calls.jsonl").read_bytes()
        denied = subprocess.run(
            [sys.executable, str(helper), "--root", str(root), "stop"],
            input=json.dumps(dict(role="relay", session="another-session")).encode(),
            capture_output=True,
        )
        assert denied.returncode != 0
        client = Client(profile["controller"], "controller")
        try:
            assert client.probe()["tls_verified"]
        finally:
            client.close()
    finally:
        if root.exists():
            call("cleanup")
