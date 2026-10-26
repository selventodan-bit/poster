"""Routing/trust/risk fences without a second execution queue."""

import subprocess
import sys
import time

import psutil
import pytest

from poster.dispatcher.models import DispatcherError
from poster.dispatcher.security import key_setup, seal, unseal
from poster.dispatcher.service import Dispatcher, prepare_snapshot
from poster.dispatcher.store import Store
from poster.remote.protocol import Identity, pack, b64
from poster.remote.registry import Registry, output_names
from poster.remote.controller import Controller
from test_dispatcher_store import project


@pytest.fixture
def admitted(tmp_path):
    store = Store(tmp_path / "dispatcher.sqlite")
    key_setup(store.root)
    registry = Registry(store)
    controller = registry.identity()
    agent = Identity()
    registration = dict(peer=agent.public(), name="Linux", os="linux", version="5A.1", protocol=1)
    relay = dict(url="https://relay.example.org", token="x" * 40, channel="a" * 32)
    agent_id = registry.register(registration, relay)
    source = tmp_path / "source.postergraph.json"
    project(source)
    from poster.dispatcher.snapshots import Snapshots

    project_id, version = Snapshots(store.root).create(source)
    cipher, names = prepare_snapshot(store, project_id, version)
    try:
        yield store, registry, controller, agent, agent_id, source, project_id, version, cipher, names
    finally:
        store.close()


def claim(values):
    store, registry, controller, agent, agent_id, source, project_id, version, cipher, names = values
    registry.approve(agent_id)
    store.db.execute("UPDATE remote_agents SET last_seen=?", (time.time(),))
    job = store.enqueue(project_id, version, source, cipher, names, agent_id=agent_id)
    execution = store.claim("owner", 2, 1)
    network = Controller(store.path)
    network.store, network.identity = store, controller
    network.offer(store.is_remote(execution["run_id"]))
    return job, execution, network


def test_pending_and_revoked_peers_cannot_receive_jobs_and_offline_is_not_local(admitted):
    store, registry, _, _, agent_id, source, project_id, version, cipher, names = admitted
    with pytest.raises(DispatcherError):
        store.enqueue(project_id, version, source, cipher, names, agent_id=agent_id)
    registry.approve(agent_id)
    job = store.enqueue(project_id, version, source, cipher, names, agent_id=agent_id)
    assert store.claim("owner", 2, 1) is None
    assert store.payload(job)["agent_id"] == agent_id and store.payload(job)["status"] == "PENDING"
    registry.revoke(agent_id)
    with pytest.raises(DispatcherError):
        registry.approve(agent_id)
    assert store.claim("owner", 2, 1) is None


def test_durable_risk_precedes_key_release_and_corrupt_report_becomes_unknown(admitted):
    store, registry, controller, agent, agent_id, *_ = admitted
    job, execution, network = claim(admitted)
    agent_row = store.db.execute("SELECT * FROM remote_agents WHERE id=?", (agent_id,)).fetchone()
    assert store.payload(job)["status"] == "LEASED"
    assert not store.db.execute("SELECT 1 FROM remote_outbox WHERE id LIKE '%:grant'").fetchone()
    network.handle(
        agent_row, pack(agent, controller.public(), dict(kind="accepted", run_id=execution["run_id"]))
    )
    assert store.execution(execution["run_id"])["effects_possible"] == 1
    assert store.payload(job)["status"] == "RUNNING"
    assert store.db.execute("SELECT 1 FROM remote_outbox WHERE id LIKE '%:grant'").fetchone()
    broken = dict(
        kind="report", run_id=execution["run_id"], report=b64(b"{}"), sha256="wrong", files={}, results=None
    )
    network.handle(agent_row, pack(agent, controller.public(), broken))
    assert store.payload(job)["status"] == "UNKNOWN"
    assert store.history(job)[0]["code"] == "REPORT_INVALID"
    evidence = store.root / "runs" / execution["run_id"] / "late-result.enc"
    original = evidence.read_bytes()
    assert unseal(original)["key"]["key"]
    # A correctly authenticated duplicate response cannot alter a terminal result or re-execute.
    network.handle(agent_row, pack(agent, controller.public(), broken))
    assert store.payload(job)["status"] == "UNKNOWN" and store.claim("owner", 2, 1) is None
    assert evidence.read_bytes() == original  # Preserve the private review key after receipt loss.


def test_controller_recovery_never_signals_an_agent_pid_or_reissues_grant(admitted):
    store, _, controller, agent, agent_id, *_ = admitted
    job, execution, network = claim(admitted)
    row = store.db.execute("SELECT * FROM remote_agents WHERE id=?", (agent_id,)).fetchone()
    network.handle(row, pack(agent, controller.public(), dict(kind="accepted", run_id=execution["run_id"])))
    sleeper = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(20)"])
    try:
        store.db.execute(
            "UPDATE executions SET pid=?,birth=? WHERE run_id=?",
            (sleeper.pid, psutil.Process(sleeper.pid).create_time(), execution["run_id"]),
        )
        Dispatcher(store).recover()
        assert sleeper.poll() is None
        assert store.payload(job)["status"] == "UNKNOWN"
        assert not store.db.execute("SELECT 1 FROM remote_outbox WHERE id LIKE '%:grant'").fetchone()
        assert len(store.history(job)) == 1
    finally:
        sleeper.terminate()
        sleeper.wait(10)


def test_pregrant_recovery_is_provably_unstarted(admitted):
    store, *_ = admitted
    job, execution, _ = claim(admitted)
    Dispatcher(store).recover()
    assert store.payload(job)["status"] == "PENDING"
    assert store.history(job)[0]["code"] == "NOT_STARTED"
    assert not store.db.execute("SELECT * FROM remote_outbox").fetchall()


def test_never_exports_master_key_even_when_reused_as_maker_key(admitted):
    store, *_ = admitted
    job, execution, network = claim(admitted)
    store.db.execute("DELETE FROM remote_outbox")
    from poster.auth import session_key

    store.db.execute(
        "UPDATE jobs SET inputs_cipher=? WHERE id=?",
        (seal({"inputs": {}, "project_key": session_key().decode()}), job),
    )
    with pytest.raises(DispatcherError) as error:
        network.offer(store.is_remote(execution["run_id"]))
    assert error.value.code == "REMOTE_MASTER_KEY"
    assert not store.db.execute("SELECT * FROM remote_outbox").fetchall()


@pytest.mark.parametrize(
    "names", [["../private"], ["/tmp/secret"], ["a\\b"], ["a/../b"], ["a//b"], ["a/./b"], ["C:/key"]]
)
def test_output_allowlist_cannot_escape(names):
    with pytest.raises(DispatcherError):
        output_names(names)


def test_agent_replay_journal_never_resets_after_corruption_or_key_loss(tmp_path):
    from cryptography.fernet import Fernet
    from poster.remote.journal import Journal

    key = Fernet.generate_key()
    journal = Journal(tmp_path, key, create=True)
    journal.db.execute("INSERT INTO runs(id,state) VALUES('permanent-run','STARTED')")
    journal.close()
    with pytest.raises(DispatcherError, match="Ключ"):
        Journal(tmp_path, Fernet.generate_key())
    journal = Journal(tmp_path, key)
    assert journal.db.execute("SELECT state FROM runs").fetchone()[0] == "STARTED"
    journal.close()
    path = tmp_path / "agent-journal.sqlite"
    path.write_bytes(b"")
    with pytest.raises(DispatcherError, match="Ключ"):
        Journal(tmp_path, key)
    assert path.read_bytes() == b""  # No replacement schema or empty replay ledger.
    path.unlink()
    with pytest.raises(DispatcherError, match="Ключ"):
        Journal(tmp_path, key)
    assert not path.exists()


def test_schema2_schedule_migration_keeps_local_target_and_encrypted_inputs(tmp_path):
    import sqlite3
    from poster.dispatcher.store import SCHEMA, MIGRATION_2, APPLICATION_ID, SCHEMA_VERSION

    path = tmp_path / "old.sqlite"
    with sqlite3.connect(path) as db:
        db.executescript(SCHEMA + MIGRATION_2)
        db.execute(f"PRAGMA application_id={APPLICATION_ID}")
        db.execute("PRAGMA user_version=2")
        db.execute(
            "INSERT INTO schedules(id,project_id,version,name,source,spec,inputs_cipher,input_names,timeout,next_due,created,updated) VALUES('old','p','v','name','src','spec',?,'[]',300,123,1,1)",
            (b"unchanged encrypted inputs",),
        )
    store = Store(path)
    try:
        row = store.db.execute("SELECT * FROM schedules").fetchone()
        assert row["agent_id"] is None and row["remote_outputs"] == "[]"
        assert row["inputs_cipher"] == b"unchanged encrypted inputs" and row["next_due"] == 123
        assert store.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    finally:
        store.close()
