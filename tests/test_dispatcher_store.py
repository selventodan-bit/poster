import concurrent.futures
import json
import os
import sqlite3
import subprocess

import pytest

from poster.dispatcher.models import DispatcherError
from poster.dispatcher.security import key_setup, seal
from poster.dispatcher.snapshots import Snapshots
from poster.dispatcher.store import APPLICATION_ID, Store
from poster.maker.example_projects import chain
from poster.maker.model import Variable
from poster.maker.storage import Vault, load_graph, save_graph


def project(path, text="before"):
    graph = chain("Dispatcher", [("Files.WriteText", {"path": "result.txt", "text": text})])
    save_graph(path, graph, Vault())
    return graph


def queue(store, source, key=None):
    key_setup(store.root)
    project_id, version = Snapshots(store.root).create(source)
    return store.enqueue(project_id, version, source, seal({"inputs": {}}), key=key)


def test_store_migration_dedup_transitions_history_and_legacy_separation(tmp_path):
    source = tmp_path / "project.postergraph.json"
    project(source)
    path = tmp_path / "dispatcher.sqlite"
    store = Store(path)
    assert store.db.execute("PRAGMA application_id").fetchone()[0] == APPLICATION_ID
    assert store.db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    job = queue(store, source, "one")
    assert queue(store, source, "one") == job
    run = store.claim("owner", 1, 1)
    assert run and store.claim("other", 1, 1) is None
    assert store.record_process(run["run_id"], "owner", 123, 1.0)
    assert not store.start(run["run_id"], "wrong-owner", 123)
    assert store.start(run["run_id"], "owner", 123)
    with pytest.raises(sqlite3.IntegrityError):
        store.db.execute("UPDATE jobs SET status='PENDING' WHERE id=?", (job,))
    assert store.complete(run["run_id"], "SUCCEEDED", exit_code=0)
    assert not store.complete(run["run_id"], "FAILED")
    for _ in range(220):
        store.event(job, "SUCCEEDED")
    assert len(store.show(job)["events"]) == 200
    store.close()
    store = Store(path)
    assert store.show(job)["status"] == "SUCCEEDED" and len(store.history(job)) == 1
    store.close()
    legacy = tmp_path / "state.sqlite"
    with sqlite3.connect(legacy) as db:
        db.execute("CREATE TABLE jobs(id TEXT)")
    raw = legacy.read_bytes()
    with pytest.raises(DispatcherError, match="поддерживаемая база"):
        Store(legacy)
    assert legacy.read_bytes() == raw


def test_atomic_claim_and_idempotency_across_connections(tmp_path):
    path = tmp_path / "q.sqlite"
    source = tmp_path / "a.postergraph.json"
    project(source)
    store = Store(path)
    job = queue(store, source, "key")
    store.close()

    def claim(_):
        connection = Store(path)
        try:
            return connection.claim("owner", 4, 4)
        finally:
            connection.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(claim, range(4)))
    assert len([r for r in results if r]) == 1
    store = Store(path)
    assert store.show(job)["attempt"] == 1
    store.close()


def test_snapshot_original_edits_integrity_sidecars_and_dynamic_children(tmp_path):
    source = tmp_path / "source" / "main.postergraph.json"
    source.parent.mkdir()
    graph = project(source, "unchanged")
    child = source.parent / "children" / "child.postergraph.json"
    child.parent.mkdir()
    vault = Vault()
    secret = "SECRET_SNAPSHOT_CANARY"
    graph.variables["password"] = Variable(secret=True, value=vault.put(secret))
    save_graph(source, graph, vault)
    project(child, "child")
    store = Store(tmp_path / "q.sqlite")
    key_setup(store.root)
    snapshots = Snapshots(store.root)
    pid, version = snapshots.create(source)
    manifest, files = snapshots.read(version)
    assert pid == graph.id and child.relative_to(source.parent).as_posix() in files
    assert any(n.endswith(".secrets.enc") for n in files)
    graph.nodes[1].params["text"] = "changed"
    save_graph(source, graph, vault)
    saved = snapshots.materialize(version, tmp_path / "runtime")
    assert load_graph(saved)[0].nodes[1].params["text"] == "unchanged"
    assert secret.encode() not in b"".join(p.read_bytes() for p in (snapshots.root / version).iterdir())
    manifest_path = snapshots.root / version / "manifest.json"
    manifest_path.write_text(json.dumps({**manifest, "main": "other.json"}))
    with pytest.raises(DispatcherError):
        snapshots.read(version)
    store.close()


def test_snapshot_rejects_parent_escape_links_and_limits(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    outside = tmp_path / "outside.postergraph.json"
    project(outside)
    source = root / "main.postergraph.json"
    graph = chain("escape", [("Project.Run", {"path": "../outside.postergraph.json"})])
    save_graph(source, graph, Vault())
    store = Store(tmp_path / "q.sqlite")
    key_setup(store.root)
    snapshots = Snapshots(store.root)
    with pytest.raises(DispatcherError):
        snapshots.create(source)
    project(source)
    link = root / "linked"
    if os.name == "nt":
        result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(tmp_path)], capture_output=True)
        assert result.returncode == 0
    else:
        link.symlink_to(tmp_path, target_is_directory=True)
    try:
        with pytest.raises(DispatcherError):
            snapshots.create(source)
    finally:
        if os.name == "nt":
            os.rmdir(link)
        else:
            link.unlink()
    for i in range(130):
        project(root / f"{i}.postergraph.json")
    with pytest.raises(DispatcherError):
        snapshots.create(source)
    store.close()


def test_pending_cancel_and_proven_unstarted_recovery(tmp_path):
    source = tmp_path / "a.postergraph.json"
    project(source)
    store = Store(tmp_path / "q.sqlite")
    first = queue(store, source)
    second = queue(store, source)
    store.cancel(first)
    assert store.show(first)["status"] == "CANCELLED"
    run = store.claim("owner", 1, 1)
    assert run["job_id"] == second
    assert store.recover_unstarted(run["run_id"])
    assert store.show(second)["status"] == "PENDING"
    run = store.claim("owner2", 1, 1)
    store.record_process(run["run_id"], "owner2", 123, 1)
    assert not store.recover_unstarted(run["run_id"])
    store.complete(run["run_id"], "UNKNOWN", code="WORKER_CRASH")
    store.review(second)
    assert store.show(second)["status"] == "REVIEW_REQUIRED" and store.claim("owner", 1, 1) is None
    store.close()
