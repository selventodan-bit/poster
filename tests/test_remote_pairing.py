"""Real TLS enrollment/probes and signed messages; not a cross-host GraphRunner E2E."""

import json
import socket
import threading
import time

import pytest

from poster.dispatcher.models import DispatcherError
from poster.dispatcher.security import key_setup
from poster.dispatcher.store import Store
from poster.remote.controller import Controller
from poster.remote.diagnostics import connection
from poster.remote.pairing import export_offer, agent_pair, finish_offer
from poster.remote.protocol import Identity, pack
from poster.remote.registry import Registry, read_json
from poster.remote.relay import Client, server
from poster.remote.setup import tls_files, relay_profiles


@pytest.fixture
def pairing(tmp_path):
    store = Store(tmp_path / "Controller" / "dispatcher.sqlite")
    key_setup(store.root)
    relay = tmp_path / "relay"
    cert, key = tls_files(relay, "127.0.0.1")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    url = f"https://127.0.0.1:{port}"
    relay_profiles(relay, url, cert)
    instance = server(read_json(relay / "relay.json"), relay / "mailbox.sqlite", cert, key, port=port)
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    offer = tmp_path / "pairing.json"
    info = export_offer(store, relay / "pairing-relay.json", url, offer)
    try:
        yield dict(store=store, relay=relay, offer=offer, info=info, root=tmp_path / "Agent", url=url)
    finally:
        instance.shutdown()
        instance.server_close()
        thread.join(5)
        store.close()


def test_pairing_binds_both_fingerprints_and_consumes_nonce_once(pairing):
    p = pairing
    with pytest.raises(DispatcherError) as error:
        agent_pair(p["root"], "Linux", p["offer"], "0" * 64)
    assert error.value.code == "REMOTE_FINGERPRINT" and not p["root"].exists()
    agent_id = agent_pair(p["root"], "Linux", p["offer"], p["info"]["controller"])
    with pytest.raises(DispatcherError) as error:
        finish_offer(p["store"], p["root"] / "registration.json", "0" * 64)
    assert error.value.code == "REMOTE_FINGERPRINT"
    assert finish_offer(p["store"], p["root"] / "registration.json", agent_id) == agent_id
    registry = Registry(p["store"])
    assert registry.rows()[0]["state"] == "PENDING"
    with pytest.raises(DispatcherError):
        registry.approve(agent_id, "0" * 64)
    assert registry.rows()[0]["state"] == "PENDING"
    registry.approve(agent_id, agent_id)
    with pytest.raises(DispatcherError):
        finish_offer(p["store"], p["root"] / "registration.json", agent_id)
    original = (p["root"] / "agent.json").read_bytes()
    with pytest.raises(FileExistsError):
        agent_pair(p["root"], "Linux", p["offer"], p["info"]["controller"])
    assert (p["root"] / "agent.json").read_bytes() == original


@pytest.mark.parametrize("damage", ["expired", "signature", "controller", "nonce"])
def test_pairing_tamper_and_expiry_do_not_register_or_approve(pairing, damage):
    p = pairing
    agent_id = agent_pair(p["root"], "Linux", p["offer"], p["info"]["controller"])
    path = p["root"] / "registration.json"
    value = read_json(path)
    if damage == "expired":
        p["store"].db.execute("UPDATE remote_pairings SET expires=?", (time.time() - 1,))
    elif damage == "signature":
        value["pairing"]["signature"] = "broken"
    else:
        value["pairing"][damage if damage != "nonce" else "id"] = "a" * 32
    path.write_text(json.dumps(value))
    with pytest.raises(DispatcherError):
        finish_offer(p["store"], path, agent_id)
    assert not Registry(p["store"]).rows()


def test_relay_probe_does_not_consume_messages_or_create_online_status(pairing):
    p = pairing
    agent_id = agent_pair(p["root"], "Linux", p["offer"], p["info"]["controller"])
    finish_offer(p["store"], p["root"] / "registration.json", agent_id)
    registry = Registry(p["store"])
    registry.approve(agent_id)
    controller = Client(read_json(p["relay"] / "controller-relay.json"), "controller")
    agent = Client(read_json(p["relay"] / "agent-relay.json"), "agent")
    identity = Identity(read_json(p["root"] / "agent.json")["identity"])
    packet = pack(registry.identity(), identity.public(), dict(kind="pulse", runs=[], revoked=False))
    try:
        controller.exchange([packet])
        assert agent.probe()["tls_verified"]
        assert agent.exchange()[0]["id"] == packet["id"]
        public = connection(p["store"], agent_id, {"max_workers": 2})
        assert public["relay"] and not public["online"] and "REMOTE_OFFLINE" in public["codes"]
        assert "token" not in json.dumps(public) and "cipher" not in json.dumps(public)
        network = Controller(p["store"].path)
        network.store, network.identity = p["store"], registry.identity()
        row = p["store"].db.execute("SELECT * FROM remote_agents").fetchone()
        body = dict(
            kind="heartbeat",
            version="5A.1",
            os="linux",
            protocol=1,
            runs=[],
            capabilities=dict(phase="STOPPED", worker=True, browser=False, disk_mb=1000, memory_mb=1000),
        )
        network.handle(row, pack(identity, registry.identity().public(), body))
        public = connection(p["store"], agent_id, {"max_workers": 2})
        assert not public["online"] and "REMOTE_STOPPED" in public["codes"]
    finally:
        controller.close()
        agent.close()


@pytest.mark.parametrize(
    "failure,code",
    [
        ("auth", "REMOTE_AUTH"),
        ("ca", "REMOTE_TLS"),
        ("hostname", "REMOTE_TLS"),
        ("network", "REMOTE_NETWORK"),
    ],
)
def test_real_tls_diagnostics_classify_without_exposing_credentials(pairing, failure, code):
    p = pairing
    config = read_json(p["relay"] / "controller-relay.json")
    if failure == "auth":
        config["token"] = "wrong-credential-" * 4
    elif failure == "ca":
        config["ca_pem"] = None
    elif failure == "hostname":
        config["url"] = config["url"].replace("127.0.0.1", "localhost")
    else:
        config["url"] = "https://127.0.0.1:1"
    client = Client(config, "controller")
    try:
        with pytest.raises(DispatcherError) as error:
            client.probe()
        assert error.value.code == code
        assert config["token"] not in str(error.value)
    finally:
        client.close()
