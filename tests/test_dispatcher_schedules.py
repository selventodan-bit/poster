"""Controlled UTC clocks, durable admission, fail-closed secrets and real workers."""

from datetime import datetime
import json
from pathlib import Path

import pytest

from poster.dispatcher.catalog import Catalog
from poster.dispatcher.models import DispatcherError
from poster.dispatcher.schedule_time import parse_spec, preview
from poster.dispatcher.schedules import Schedules, Scheduler
from poster.dispatcher.security import key_setup, seal
from poster.dispatcher.service import prepare_snapshot
from poster.dispatcher.store import Store
from test_dispatcher_store import project
from test_dispatcher_host import host  # noqa: F401


def stamp(value):
    return int(datetime.fromisoformat(value).timestamp())


START = stamp("2026-10-25T12:00:00+00:00")


def spec(mode="interval", **kw):
    return {"mode": mode, "start": START, **kw}


@pytest.fixture
def scheduled(tmp_path):
    store = Store(tmp_path / "dispatcher.sqlite")
    key_setup(store.root)
    source = tmp_path / "project.postergraph.json"
    project(source)
    catalog = Catalog(store)
    item = catalog.inspect(source)
    cipher, names = prepare_snapshot(store, item["project_id"], item["version"])
    schedules = Schedules(store)
    schedule = schedules.create(item, spec(), cipher, names, now=START - 10)
    try:
        yield store, schedules, schedule, item, source
    finally:
        store.close()


def test_once_interval_disable_delete_and_history(scheduled):
    store, schedules, schedule, item, _ = scheduled
    engine = Scheduler(store)
    engine.tick(now=START - 1)
    assert not store.rows()
    engine.tick(now=START)
    first = store.rows()[0]["id"]
    engine.tick(now=START)
    assert len(store.rows()) == 1
    assert schedules.get(schedule)["next_due"] == START + 60
    schedules.change(schedule, enabled=False)
    engine.tick(now=START + 60)
    assert len(store.rows()) == 1
    schedules.change(schedule, enabled=True)
    engine.tick(now=START + 60)
    assert len(store.rows()) == 2
    schedules.change(schedule, delete=True)
    engine.tick(now=START + 120)
    assert len(store.rows()) == 2
    assert schedules.history(schedule)["total"] == 2
    assert schedules.rows()["total"] == 0
    assert store.show(first)["status"] == "PENDING"
    once = schedules.create(item, spec("once"), seal({}), [], now=START - 1)
    engine.tick(now=START)
    engine.tick(now=START + 1)
    assert schedules.get(once)["next_due"] is None
    assert schedules.history(once)["total"] == 1


def test_misfires_are_compressed_and_catchup_is_one(scheduled):
    store, schedules, schedule, item, _ = scheduled
    engine = Scheduler(store)
    engine.tick(now=START + 60001)
    history = schedules.history(schedule)["rows"]
    assert len(history) == 2
    assert sum(r["occurrences"] for r in history if r["outcome"] == "MISSED") == 1000
    assert len(store.rows()) == 1
    # Strict skip, no occurrence inside grace.
    other = schedules.create(item, spec(interval=60, grace=0), seal({}), [], now=START)
    engine.tick(now=START + 60001)
    assert schedules.history(other)["rows"][0]["occurrences"] == 1001
    assert schedules.history(other)["rows"][0]["outcome"] == "MISSED"
    late = schedules.create(item, spec(interval=60, grace=0, misfire="latest"), seal({}), [], now=START)
    engine.tick(now=START + 60001)
    assert len([r for r in schedules.history(late)["rows"] if r["job_id"]]) == 1


def test_crash_at_enqueue_rolls_back_job_event_and_cursor(scheduled, monkeypatch):
    store, schedules, schedule, _, _ = scheduled
    original = store._enqueue

    def crash(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("power loss before event commit")

    monkeypatch.setattr(store, "_enqueue", crash)
    with pytest.raises(RuntimeError):
        Scheduler(store).tick(now=START)
    assert not store.rows()
    assert schedules.get(schedule)["next_due"] == START
    assert not schedules.history(schedule)["rows"]
    monkeypatch.setattr(store, "_enqueue", original)
    Scheduler(store).tick(now=START)
    Scheduler(store).tick(now=START)
    assert len(store.rows()) == 1 and schedules.history(schedule)["total"] == 1
    # A new SQLite connection sees the same cursor and event.
    reopened = Store(store.path)
    try:
        Scheduler(reopened).tick(now=START)
        assert len(reopened.rows()) == 1
    finally:
        reopened.close()


@pytest.mark.parametrize("state", ["UNKNOWN", "REVIEW_REQUIRED"])
def test_uncertain_manual_job_blocks_whole_project_until_explicit_resume(scheduled, state):
    store, schedules, schedule, item, source = scheduled
    job = store.enqueue(item["project_id"], item["version"], source, seal({}))
    run = store.claim("owner", 1, 1)
    store.complete(run["run_id"], state, code="REVIEW_REQUIRED")
    Scheduler(store).tick(now=START)
    assert schedules.get(schedule)["paused_code"] == "SCHEDULE_REVIEW"
    assert len(store.rows()) == 1
    schedules.change(schedule, enabled=True)  # Enabling alone is not review.
    Scheduler(store).tick(now=START)
    assert len(store.rows()) == 1
    schedules.change(schedule, resume=True)
    Scheduler(store).tick(now=START)
    assert len(store.rows()) == 2
    assert store.show(job)["status"] == state
    assert store.claim("owner", 1, 1)


def test_queued_schedule_waits_when_another_job_becomes_unknown(scheduled):
    store, _, _, _, _ = scheduled
    Scheduler(store).tick(now=START)
    Scheduler(store).tick(now=START + 60)
    run = store.claim("owner", 1, 1)
    assert store.claim("owner", 1, 1) is None
    store.complete(run["run_id"], "UNKNOWN", code="WORKER_CRASH")
    assert store.claim("owner", 1, 1) is None


def test_corrupt_cipher_and_snapshot_pause_without_admission(scheduled):
    store, schedules, schedule, _, _ = scheduled
    store.db.execute("UPDATE schedules SET inputs_cipher=?", (b"bad key data",))
    Scheduler(store).tick(now=START)
    assert schedules.get(schedule)["paused_code"] == "KEY_INVALID"
    assert not store.rows()
    store.db.execute("UPDATE schedules SET inputs_cipher=?,paused_code='',version='bad-version'", (seal({}),))
    Scheduler(store).tick(now=START)
    assert schedules.get(schedule)["paused_code"] == "SNAPSHOT_INVALID"
    assert not store.rows()


def test_edit_does_not_follow_live_source_and_version_requires_explicit_change(scheduled):
    store, schedules, schedule, item, source = scheduled
    old = item["version"]
    from poster.maker.storage import load_graph, save_graph, Vault

    graph, vault = load_graph(source)
    graph.name = "Explicit new version"
    save_graph(source, graph, vault or Vault())
    catalog = Catalog(store)
    changed = catalog.inspect(source)
    assert changed["version"] != old
    schedules.change(schedule, spec=spec("once", start=4102444799))
    assert schedules.get(schedule)["version"] == old
    assert schedules.get(schedule)["next_due"] == 4102444799
    cipher, names = prepare_snapshot(store, changed["project_id"], changed["version"])
    schedules.change(schedule, replacement=(changed, cipher, names))
    assert schedules.get(schedule)["version"] == changed["version"]
    with pytest.raises(DispatcherError):
        schedules.change(schedule, replacement=({**changed, "project_id": "foreign"}, cipher, names))


def test_many_schedules_are_paged_and_batch_admission_is_bounded(scheduled):
    store, schedules, _, item, _ = scheduled
    for _ in range(410):
        schedules.create(item, spec("once"), seal({}), [], now=START)
    first = schedules.rows(order="created")
    second = schedules.rows(order="created", offset=200)
    assert first["total"] == 411
    assert len(first["rows"]) == len(second["rows"]) == 200
    assert not {r["id"] for r in first["rows"]} & {r["id"] for r in second["rows"]}
    engine = Scheduler(store)
    engine.tick(now=START)
    assert len(store.rows(1000)) == 100
    for _ in range(4):
        engine.tick(now=START)
    assert len(store.rows(1000)) == 411
    assert store.db.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize(
    "mode,days,expected",
    [
        ("daily", [0], "2026-10-26T09:00:00+03:00"),
        ("weekly", [2, 4], "2026-10-28T09:00:00+03:00"),
    ],
)
def test_daily_weekly_timezone(mode, days, expected):
    value = spec(mode, timezone="Europe/Moscow", weekdays=days)
    assert preview(value, after=START)[0]["utc"] == stamp(expected)


def test_dst_gap_skipped_fold_runs_only_first_and_utc_interval_is_fixed():
    daily = parse_spec(
        spec("daily", start=stamp("2026-03-27T00:00:00+00:00"), timezone="Europe/Berlin", time="02:30:00")
    )
    assert daily.next_after(stamp("2026-03-28T02:30:00+01:00")) == stamp("2026-03-30T02:30:00+02:00")
    first = stamp("2026-10-25T02:30:00+02:00")
    assert daily.next_after(first - 1) == first
    assert daily.next_after(first) == stamp("2026-10-26T02:30:00+01:00")
    interval = parse_spec(spec(start=first, interval=3600, timezone="Europe/Berlin"))
    assert interval.next_after(first) == first + 3600


@pytest.mark.parametrize(
    "bad",
    [
        {"timezone": "Invalid/Zone"},
        {"time": "25:00:00"},
        {"weekdays": []},
        {"weekdays": [1, 1]},
        {"interval": 0},
        {"mode": "cron"},
        {"start": float("nan")},
    ],
)
def test_invalid_schedule_is_rejected(bad):
    with pytest.raises(DispatcherError) as error:
        parse_spec(spec(**bad))
    assert error.value.code == "SCHEDULE_INVALID"


def test_real_service_scheduled_worker_without_gui_and_restart(host, tmp_path):  # noqa: F811
    import time
    from poster.dispatcher.host import ensure_service, request
    from test_dispatcher_host import register, stopped
    from test_dispatcher_service import wait

    root, info = host
    item = register(
        root,
        tmp_path / "real.postergraph.json",
        [("Files.WriteText", {"path": "out.txt", "text": "scheduled worker"})],
    )
    due = int(time.time()) + 2
    schedule = request(root, "schedule_create", item_id=item["id"], spec=spec("once", start=due))
    wait(lambda: request(root, "schedule_history", id=schedule)["total"] == 1)
    job = request(root, "schedule_history", id=schedule)["rows"][0]["job_id"]
    wait(lambda: request(root, "show", id=job)["status"] == "SUCCEEDED")
    result = request(root, "show", id=job)
    assert result["history"][0]["pid"] != info["pid"]
    assert (Path(result["workdir"]) / "out.txt").read_text() == "scheduled worker"
    request(root, "shutdown")
    wait(lambda: stopped(info))
    replacement = ensure_service(root)
    try:
        assert request(root, "schedule_history", id=schedule)["total"] == 1
        assert request(root, "jobs")["total"] == 1
        assert request(root, "schedule_list")["rows"][0]["next_due"] is None
    finally:
        request(root, "shutdown")
        wait(lambda: stopped(replacement))


def test_secretref_and_typed_protected_inputs_recur_without_reentry(host, tmp_path, monkeypatch):  # noqa: F811
    import time
    from cryptography.fernet import Fernet
    from poster.dispatcher.host import request
    from poster.maker.example_projects import chain
    from poster.maker.model import Variable
    from poster.maker.storage import Vault, save_graph
    from test_dispatcher_service import wait

    root, _ = host
    maker = tmp_path / "Maker"
    private = maker / ".poster/private"
    private.mkdir(parents=True)
    key = Fernet.generate_key()
    (private / "session.key").write_bytes(key)
    monkeypatch.setenv("POSTER_SESSION_KEY", key.decode())
    vault = Vault()
    graph = chain(
        "Pinned secrets",
        [
            ("JSON.Stringify", {"value": {"variable": "saved"}}),
            ("JSON.Stringify", {"value": {"variable": "typed"}}),
        ],
    )
    graph.variables["saved"] = Variable(secret=True, value=vault.put("SCHEDULE_SAVED_CANARY"))
    graph.variables["typed"] = Variable(secret=True, input=True, required=True)
    source = maker / "secret.postergraph.json"
    save_graph(source, graph, vault)
    item = request(root, "add", source=str(source))
    schedule = request(
        root,
        "schedule_create",
        item_id=item["id"],
        spec=spec(start=int(time.time()), interval=1),
        options={"secrets": {"typed": {"token": "SCHEDULE_INPUT_CANARY", "n": 42, "ok": True}}},
    )
    source.unlink()
    wait(lambda: request(root, "ping")["counts"].get("SUCCEEDED", 0) >= 2)
    request(root, "schedule_change", id=schedule, enabled=False)
    value = request(root, "schedule_list")
    assert value["rows"][0]["version"] == item["version"]
    assert "inputs_cipher" not in json.dumps(value)
    for file in root.rglob("*"):
        if file.is_file() and file.suffix not in {".key", ".lock"}:
            raw = file.read_bytes()
            assert b"SCHEDULE_INPUT_CANARY" not in raw
            assert b"SCHEDULE_SAVED_CANARY" not in raw
            assert key not in raw


def test_real_scheduled_write_service_crash_pauses_without_retry(host, tmp_path):  # noqa: F811
    import time
    import psutil
    from poster.dispatcher.host import ensure_service, request
    from test_dispatcher_host import register, stopped
    from test_dispatcher_service import controlled_server, wait

    root, info = host
    with controlled_server() as (server, base):
        item = register(root, tmp_path / "write.postergraph.json", [("HTTP.Post", {"url": base + "/hold/0"})])
        schedule = request(
            root, "schedule_create", item_id=item["id"], spec=spec(start=int(time.time()), interval=1)
        )
        wait(lambda: server.calls)
        psutil.Process(info["pid"]).kill()
        wait(lambda: stopped(info))
        replacement = ensure_service(root)
        try:
            wait(lambda: request(root, "schedule_list")["rows"][0]["paused_code"] == "SCHEDULE_REVIEW")
            history = request(root, "schedule_history", id=schedule)["rows"]
            assert history[0]["status"] == "UNKNOWN"
            assert len(server.calls) == 1
            request(root, "schedule_change", id=schedule, enabled=True)
            assert request(root, "schedule_list")["rows"][0]["paused_code"] == "SCHEDULE_REVIEW"
        finally:
            server.gates["0"].set()
            request(root, "shutdown")
            wait(lambda: stopped(replacement))


def test_schedule_search_is_case_insensitive_for_russian_projects(scheduled):  # noqa: F811
    store, schedules, schedule, _, _ = scheduled
    store.db.execute("UPDATE schedules SET name=? WHERE id=?", ("Ежедневный Проект", schedule))
    assert schedules.rows(search="ежедневный проект")["total"] == 1
    assert schedules.rows(search="ЕЖЕДНЕВНЫЙ")["total"] == 1
