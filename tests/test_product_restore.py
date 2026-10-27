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
    "phase",
    ["STAGING", "STAGE_FILE_0", "PREPARED", "OLD_MOVED_0", "NEW_INSTALLED_0", "NEW_INSTALLED_1", "COMMITTED"],
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


def test_nested_state_roots_are_copied_once_and_native_queue_is_still_quarantined(product, tmp_path):
    import os

    roots = product["roots"]
    maker = roots["maker"]
    for name in ["dispatcher", "cloud"]:
        target = maker / name
        os.replace(roots[name], target)
        roots[name] = target
    (maker / "marker.txt").write_text("nested-backup")
    archive = tmp_path / "nested.pbackup"
    proof = backup.create(roots, archive, PASSWORD, confirmed=True)
    manifest = backup.inspect(archive, PASSWORD, roots)
    assert len(manifest["scopes"]) == 1 and {s["name"] for s in manifest["logical"]} == {
        "maker",
        "dispatcher",
        "cloud",
    }
    (maker / "marker.txt").write_text("nested-current")
    result = restore.restore(archive, PASSWORD, roots, expected_sha256=proof["sha256"], confirmed=True)
    assert result["status"] == "PASSED" and (maker / "marker.txt").read_text() == "nested-backup"
    store = Store(roots["dispatcher"] / "dispatcher.sqlite")
    try:
        assert [store.show(j)["status"] for j in product["jobs"]] == [
            "REVIEW_REQUIRED",
            "REVIEW_REQUIRED",
            "CANCELLED",
        ]
        assert store.claim("no-replay", 5, 5) is None
    finally:
        store.close()
    assert SecretStore(roots["cloud"]).get("synthetic-test") == "backup-secret"


def test_restore_cannot_move_its_source_or_private_lease_registry(product):
    import shutil
    from poster.product.locking import registry_path

    source = product["roots"]["maker"] / "inside.pbackup"
    shutil.copyfile(product["archive"], source)
    with pytest.raises(DispatcherError):
        restore.restore(source, PASSWORD, product["roots"], expected_sha256=product["sha"], confirmed=True)
    assert source.read_bytes() == product["archive"].read_bytes()
    with pytest.raises(DispatcherError):
        backup.roots_checked({"maker": registry_path()})


def test_crash_before_journal_does_not_change_or_gate_original_state(product):
    code = """
import json,os,sys
from poster.product import restore
p=json.load(sys.stdin)
def crash(phase):
 if phase=='BEFORE_STAGING':os._exit(77)
restore.checkpoint=crash
restore.restore(p['archive'],p['password'],p['roots'],expected_sha256=p['sha'],confirmed=True)
"""
    process = subprocess.run(
        [sys.executable, "-c", code],
        input=json.dumps(
            dict(
                archive=str(product["archive"]),
                password=PASSWORD,
                roots={k: str(v) for k, v in product["roots"].items()},
                sha=product["sha"],
            )
        ),
        text=True,
        capture_output=True,
        timeout=45,
    )
    assert process.returncode == 77
    assert not restore.discover(product["roots"])
    with application_lease(product["roots"]["maker"], "maker"):
        assert (product["roots"]["maker"] / "marker.txt").read_text() == "current-version"
    assert restore.recover(product["roots"], confirmed=True) == []
    from poster.product import scratch

    assert scratch.cleanup_abandoned() == 0


def test_external_kill_during_staging_recovers_all_roots(product, tmp_path):
    import psutil
    from test_dispatcher_service import wait

    signal_file = tmp_path / "ready"
    code = """
import json,sys,time
from pathlib import Path
from poster.product import restore
p=json.load(sys.stdin)
def pause(phase):
 if phase=='STAGE_FILE_0':
  Path(p['ready']).touch()
  while True:time.sleep(0.1)
restore.checkpoint=pause
restore.restore(p['archive'],p['password'],p['roots'],expected_sha256=p['sha'],confirmed=True)
"""
    process = subprocess.Popen(
        [sys.executable, "-c", code],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    data = dict(
        archive=str(product["archive"]),
        password=PASSWORD,
        roots={k: str(v) for k, v in product["roots"].items()},
        sha=product["sha"],
        ready=str(signal_file),
    )
    try:
        process.stdin.write(json.dumps(data))
        process.stdin.close()
        wait(lambda: signal_file.exists() or process.poll() is not None)
        assert signal_file.exists() and process.poll() is None
        owner = psutil.Process(process.pid)
        assert owner.create_time() > 0
        owner.kill()  # SIGKILL / Windows TerminateProcess; no Python cleanup.
        process.wait(timeout=15)
        assert restore.recover(product["roots"], confirmed=True) == ["ROLLED_BACK"]
        assert (product["roots"]["maker"] / "marker.txt").read_text() == "current-version"
        assert not list(signal_file.parent.rglob(".poster-stage-*"))
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=15)


def test_changed_working_directory_does_not_change_restore_targets(product, tmp_path, monkeypatch):
    other = tmp_path / "new-cwd"
    other.mkdir()
    monkeypatch.chdir(other)
    apply(product)
    assert (product["roots"]["maker"] / "marker.txt").read_text() == "backup-version"
    assert not (other / "maker").exists()


def test_disk_shortage_before_staging_leaves_original_data(product, monkeypatch):
    monkeypatch.setattr(backup, "space", lambda *args: (_ for _ in ()).throw(DispatcherError("BACKUP_LIMIT")))
    with pytest.raises(DispatcherError):
        apply(product)
    assert (product["roots"]["maker"] / "marker.txt").read_text() == "current-version"
    assert not restore.discover(product["roots"])


def test_actual_replace_failure_rolls_back_and_preserves_source(product, monkeypatch):
    import os

    original = os.replace
    failed = False

    def replace(src, dst):
        nonlocal failed
        if not failed and str(src).endswith("data") and Path(dst) == product["roots"]["dispatcher"]:
            failed = True
            raise OSError("synthetic disk rename failure")
        return original(src, dst)

    monkeypatch.setattr(os, "replace", replace)
    with pytest.raises(DispatcherError):
        apply(product)
    assert failed and (product["roots"]["maker"] / "marker.txt").read_text() == "current-version"
    assert product["archive"].exists() and not restore.discover(product["roots"])


def test_restored_unknown_post_is_not_replayed_by_real_service_and_worker(tmp_path):
    import psutil
    from poster.dispatcher.host import ensure_service, request
    from test_dispatcher_host import stopped, register
    from test_dispatcher_service import controlled_server, wait

    roots = {"maker": tmp_path / "maker", "dispatcher": tmp_path / "dispatcher"}
    roots["maker"].mkdir()
    info = ensure_service(roots["dispatcher"])
    with controlled_server() as (server, base):
        try:
            item = register(
                roots["dispatcher"],
                roots["maker"] / "once.postergraph.json",
                [("HTTP.Post", {"url": base + "/hold/0"})],
            )
            job = request(roots["dispatcher"], "enqueue", ids=[item["id"]], request_id="restore-once")[0]
            request(roots["dispatcher"], "start")
            wait(lambda: bool(server.calls))
            execution = request(roots["dispatcher"], "show", id=job)["history"][0]
            assert execution["pid"] != info["pid"] and execution["birth"] > 0
            with pytest.raises(DispatcherError) as caught:
                backup.create(roots, tmp_path / "busy.pbackup", PASSWORD, confirmed=True)
            assert caught.value.code == "PRODUCT_BUSY"
            psutil.Process(info["pid"]).kill()
            wait(lambda: stopped(info))
            from poster.dispatcher.processes import same_process

            wait(
                lambda: (
                    same_process(execution["pid"], execution["birth"]) is None
                    or psutil.Process(execution["pid"]).status() == psutil.STATUS_ZOMBIE
                )
            )
            archive = tmp_path / "once.pbackup"
            proof = backup.create(roots, archive, PASSWORD, confirmed=True)
            restore.restore(archive, PASSWORD, roots, expected_sha256=proof["sha256"], confirmed=True)
            replacement = ensure_service(roots["dispatcher"])
            info = replacement
            assert request(roots["dispatcher"], "show", id=job)["status"] == "REVIEW_REQUIRED"
            request(roots["dispatcher"], "start")
            assert request(roots["dispatcher"], "enqueue", ids=[item["id"]], request_id="restore-once") == [
                job
            ]
            time.sleep(0.5)
            assert len(server.calls) == 1
            assert request(roots["dispatcher"], "show", id=job)["status"] == "REVIEW_REQUIRED"
        finally:
            server.gates["0"].set()
            if not stopped(info):
                request(roots["dispatcher"], "shutdown")
                wait(lambda: stopped(info))
