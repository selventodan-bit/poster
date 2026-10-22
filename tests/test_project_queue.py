import asyncio
import json
import shutil

import pytest
import yaml
from filelock import Timeout

from poster.engine import Engine
from poster.persistence import State
from poster.project import load_project, mark_ready
from poster.storage.local import Registry


def publish(p, folder):
    folder.mkdir(parents=True)
    (folder / "site.yaml").write_text(yaml.safe_dump(p.site.model_dump()), encoding="utf-8")
    (folder / "workflow.yaml").write_text(
        yaml.safe_dump(p.workflow.model_dump(by_alias=True)), encoding="utf-8"
    )
    mark_ready(folder)


async def wait_for(predicate):
    async with asyncio.timeout(10):
        while not predicate():
            await asyncio.sleep(0.02)


def test_registry_atomic_update_disable_delete_and_bad_manifest(tmp_path, make_project):
    p = make_project("https://safe.test")
    root = tmp_path / "projects"
    publish(p, root / "classic")
    r = Registry(root)
    assert len(r.poll()) == 1
    old = r.projects["classic"]
    path = root / "classic/site.yaml"
    data = yaml.safe_load(path.read_text())
    data["timeout"] = 5
    path.write_text(yaml.safe_dump(data))
    assert r.poll() == []  # no READY update; retain last good immutable object
    assert r.projects["classic"] is old
    mark_ready(root / "classic")
    assert len(r.poll()) == 1
    assert r.projects["classic"].site.timeout == 5
    assert old.site.timeout == 20
    data["enabled"] = False
    path.write_text(yaml.safe_dump(data))
    mark_ready(root / "classic")
    r.poll()
    assert not r.projects
    shutil.rmtree(root / "classic")
    r.poll()
    assert not r.projects


def test_manifest_escape_and_tamper(tmp_path, make_project):
    folder = tmp_path / "classic"
    publish(make_project("https://safe.test"), folder)
    (folder / "workflow.yaml").write_text("uncommitted")
    with pytest.raises(ValueError, match="checksum"):
        load_project(folder)
    (folder / "READY").write_text(json.dumps({"schema_version": 1, "files": {"../outside": "abc"}}))
    with pytest.raises(ValueError, match="escapes"):
        load_project(folder)


async def test_hot_add_running_engine_and_no_duplicate_restart(tmp_path, make_project, fixture_site):
    server, base = fixture_site
    root = tmp_path / "projects"
    state = State(tmp_path / "db.sqlite")
    r = Registry(root)
    e = Engine(r, state, poll_interval=0.03)
    stop = asyncio.Event()
    task = asyncio.create_task(e.run(stop))
    try:
        await wait_for(lambda: e.poll_count > 0)
        p = make_project(base)
        publish(p, root / "classic")
        await wait_for(lambda: "classic" in r.projects)
        loaded = r.projects["classic"]
        job_id = state.enqueue(loaded, {"title": "Unique", "body": "Body"}, "one")
        assert job_id == state.enqueue(loaded, {"title": "Duplicate", "body": "Wrong"}, "one")
        await wait_for(lambda: state.rows()[0]["status"] == "SUCCEEDED")
        assert len(server.posts) == 1
        publish(make_project(base, csrf=True), root / "csrf")
        await wait_for(lambda: "csrf" in r.projects)
        state.enqueue(r.projects["csrf"], {"title": "Second", "body": "Body"}, "two")
        await wait_for(lambda: len(state.rows()) == 2 and state.rows()[1]["status"] == "SUCCEEDED")
    finally:
        stop.set()
        await task
    await Engine(Registry(root), state).run(asyncio.Event(), once=True)
    assert len(server.posts) == 2
    assert all(row["attempt"] == 1 for row in state.rows())
    state.close()


def test_crash_recovery_consistent_backup_restore_and_lock(tmp_path, make_project):
    state = State(tmp_path / "db.sqlite")
    p = make_project("https://safe.test")
    state.enqueue(p, {"title": "A", "body": "B"}, "leased")
    running = state.enqueue(p, {"title": "C", "body": "D"}, "running")
    state.start(running)
    with state.db:
        state.db.execute("UPDATE jobs SET status='LEASED' WHERE dedup_key='leased'")
    with state.engine_lock():
        other = State(tmp_path / "db.sqlite")
        with pytest.raises(Timeout):
            with other.engine_lock():
                pass
        other.close()
    state.recover()
    statuses = {r["id"]: r["status"] for r in state.rows()}
    assert statuses[running] == "UNKNOWN"
    assert list(statuses.values()).count("PENDING") == 1
    state.backup(tmp_path / "backup.sqlite")
    State.restore(tmp_path / "backup.sqlite", tmp_path / "restored.sqlite")
    restored = State(tmp_path / "restored.sqlite")
    assert restored.rows() == state.rows()
    assert restored.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    with pytest.raises(ValueError):
        State.restore(tmp_path / "backup.sqlite", tmp_path / "restored.sqlite")
    restored.close()
    state.close()


async def test_reconcile_unknown_does_not_send_without_recovery(tmp_path, make_project):
    state = State(tmp_path / "db.sqlite")
    p = make_project("https://safe.test")
    job_id = state.enqueue(p, {"title": "A", "body": "B"}, "unknown")
    state.start(job_id)
    await Engine(Registry(tmp_path / "projects"), state).reconcile()
    assert state.rows()[0]["status"] == "UNKNOWN"
    assert not state.pending()
    state.close()
