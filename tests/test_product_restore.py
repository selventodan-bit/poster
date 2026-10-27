"""Actual staged restores and process crashes, without replaying native jobs."""

import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

import pytest

from poster.cloud.auth import SecretStore
from poster.dispatcher.catalog import Catalog
from poster.dispatcher.models import DispatcherError
from poster.dispatcher.schedules import Schedules
from poster.dispatcher.service import enqueue, prepare_snapshot
from poster.dispatcher.store import Store
from poster.dispatcher.workspace import queue_key
from poster.maker.example_projects import chain
from poster.maker.storage import Vault, save_graph
from poster.persistence import State
from poster.product import backup, restore
from poster.product.locking import application_lease
from poster.remote.registry import Registry
from test_product_crypto import PASSWORD


@pytest.fixture
def product(tmp_path, make_project):
    roots = {k: tmp_path / k for k in ["maker", "dispatcher", "cloud"]}
    for root in roots.values():
        root.mkdir()
    source = roots["maker"] / "maker-projects/project.postergraph.json"
    source.parent.mkdir()
    save_graph(
        source,
        chain("Recovery fixture", [("Files.WriteText", dict(path="must-not-run.txt", text="not executed"))]),
        Vault(),
    )
    (roots["maker"] / "marker.txt").write_text("backup-version")
    key = queue_key(roots["dispatcher"])
    store = Store(roots["dispatcher"] / "dispatcher.sqlite")
    try:
        jobs = [enqueue(store, source, key=str(i)) for i in range(3)]
        first = store.claim("fixture-owner", 2, 2)
        store.complete(first["run_id"], "UNKNOWN")
        store.claim("fixture-owner", 2, 2)
        item = Catalog(store).inspect(source)
        cipher, names = prepare_snapshot(store, item["project_id"], item["version"])
        schedule = Schedules(store).create(
            item, dict(mode="interval", start=int(time.time()) + 600), cipher, names
        )
        identity = Registry(store).identity().public()["id"]
    finally:
        store.close()
    legacy = State(roots["maker"] / "state.sqlite")
    try:
        legacy_job = legacy.enqueue(make_project("http://fixture.example"), {}, "old-pending")
    finally:
        legacy.close()
    SecretStore(roots["cloud"]).put("synthetic-test", "backup-secret")
    archive = tmp_path / "product.pbackup"
    proof = backup.create(roots, archive, PASSWORD, confirmed=True)
    (roots["maker"] / "marker.txt").write_text("current-version")
    return dict(
        roots=roots,
        source=source,
        archive=archive,
        sha=proof["sha256"],
        jobs=jobs,
        schedule=schedule,
        identity=identity,
        key=key,
        legacy_job=legacy_job,
    )


def apply(product):
    return restore.restore(
        product["archive"], PASSWORD, product["roots"], expected_sha256=product["sha"], confirmed=True
    )


def test_restore_preserves_identity_data_and_quarantines_native_and_legacy_jobs(product):
    proof = apply(product)
    roots = product["roots"]
    assert proof["status"] == "PASSED" and proof["manual_review"] and proof["schedules_disabled"]
    assert (roots["maker"] / "marker.txt").read_text() == "backup-version"
    assert (roots["dispatcher"] / "queue.key").read_bytes() == product["key"]
    store = Store(roots["dispatcher"] / "dispatcher.sqlite")
    try:
        assert [store.show(j)["status"] for j in product["jobs"]] == [
            "REVIEW_REQUIRED",
            "REVIEW_REQUIRED",
            "CANCELLED",
        ]
        assert store.claim("new-owner", 5, 5) is None
        assert (
            store.db.execute("SELECT enabled FROM schedules WHERE id=?", (product["schedule"],)).fetchone()[0]
            == 0
        )
        assert Registry(store).identity().public()["id"] == product["identity"]
        assert store.db.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        store.close()
    with sqlite3.connect(roots["maker"] / "state.sqlite") as db:
        assert (
            db.execute("SELECT status FROM jobs WHERE id=?", (product["legacy_job"],)).fetchone()[0]
            == "UNKNOWN"
        )
    assert SecretStore(roots["cloud"]).get("synthetic-test") == "backup-secret"
    previous = next(Path(p) for p in proof["previous_directories"] if (Path(p) / "marker.txt").exists())
    assert (previous / "marker.txt").read_text() == "current-version"
    assert not restore.discover(roots)
    with application_lease(roots["maker"], "maker"):
        pass


def test_restore_requires_reviewed_archive_and_explicit_consent(product):
    roots = product["roots"]
    with pytest.raises(DispatcherError):
        restore.restore(product["archive"], PASSWORD, roots, expected_sha256=product["sha"])
    with pytest.raises(DispatcherError):
        restore.restore(product["archive"], PASSWORD, roots, expected_sha256="0" * 64, confirmed=True)
    assert (roots["maker"] / "marker.txt").read_text() == "current-version"
    assert not restore.discover(roots)


@pytest.mark.parametrize(
    "phase", ["PREPARED", "OLD_MOVED_0", "NEW_INSTALLED_0", "NEW_INSTALLED_1", "COMMITTED"]
)
def test_genuine_killed_restore_is_recovered_without_partial_state(product, tmp_path, phase):
    roots = product["roots"]
    # Password crosses only an anonymous stdin pipe, never process arguments or
    # environment. The real process exits without finally/rollback cleanup.
    program = """
import json,os,sys
from pathlib import Path
from poster.product import restore
p=json.load(sys.stdin)
def crash(phase):
 if phase==p['phase']:os._exit(77)
restore.checkpoint=crash
restore.restore(p['archive'],p['password'],{k:Path(v) for k,v in p['roots'].items()},expected_sha256=p['sha'],confirmed=True)
"""
    data = dict(
        archive=str(product["archive"]),
        password=PASSWORD,
        roots={k: str(v) for k, v in roots.items()},
        sha=product["sha"],
        phase=phase,
    )
    process = subprocess.run(
        [sys.executable, "-c", program], input=json.dumps(data), text=True, capture_output=True, timeout=45
    )
    assert process.returncode == 77
    with pytest.raises(DispatcherError):
        with application_lease(roots["maker"], "maker"):
            pass
    assert restore.recover(roots, confirmed=True) == ["COMMITTED" if phase == "COMMITTED" else "ROLLED_BACK"]
    assert (roots["maker"] / "marker.txt").read_text() == (
        "backup-version" if phase == "COMMITTED" else "current-version"
    )
    assert not restore.discover(roots)
    assert restore.recover(roots, confirmed=True) == []


def test_io_failure_rolls_back_every_root(product, monkeypatch):
    def fault(phase):
        if phase == "NEW_INSTALLED_1":
            raise OSError("synthetic failed rename")

    monkeypatch.setattr(restore, "checkpoint", fault)
    with pytest.raises(DispatcherError):
        apply(product)
    assert (product["roots"]["maker"] / "marker.txt").read_text() == "current-version"
    store = Store(product["roots"]["dispatcher"] / "dispatcher.sqlite")
    try:
        assert store.show(product["jobs"][2])["status"] == "PENDING"
    finally:
        store.close()
    assert not restore.discover(product["roots"])
