"""Abrupt process death and concurrent admission exercise SQLite's real boundaries."""

import concurrent.futures
from datetime import datetime
import os
import subprocess
import sys

from poster.dispatcher.schedule_time import parse_spec
from poster.dispatcher.schedules import Scheduler
from poster.dispatcher.store import Store
from test_dispatcher_schedules import scheduled, START, spec, stamp  # noqa: F401


def test_power_loss_inside_enqueue_has_no_partial_commit(scheduled):  # noqa: F811
    store, schedules, schedule, _, _ = scheduled
    script = """import os,sys
from pathlib import Path
from poster.dispatcher.store import Store
from poster.dispatcher.schedules import Scheduler
s=Store(Path(sys.argv[1]))
original=s._enqueue
def crash(*a,**k):
    original(*a,**k)
    os._exit(73)
s._enqueue=crash
Scheduler(s).tick(now=int(sys.argv[2]))
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(store.path), str(START)], env=os.environ.copy(), timeout=20
    )
    assert result.returncode == 73
    assert not store.rows() and not schedules.history(schedule)["rows"]
    assert schedules.get(schedule)["next_due"] == START
    Scheduler(store).tick(now=START)
    assert len(store.rows()) == 1


def test_concurrent_ticks_share_one_event_and_manual_queue_limits(scheduled):  # noqa: F811
    store, schedules, schedule, item, source = scheduled

    def tick(_):
        connection = Store(store.path)
        try:
            Scheduler(connection).tick(now=START)
        finally:
            connection.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(tick, range(4)))
    assert len(store.rows()) == schedules.history(schedule)["total"] == 1
    job = store.enqueue(item["project_id"], item["version"], source, b"cipher")
    first = store.claim("owner", 2, 1)
    assert first and store.claim("owner", 2, 1) is None
    store.cancel(first["job_id"])
    store.complete(first["run_id"], "CANCELLED", code="CANCEL_REQUESTED")
    assert store.claim("owner", 2, 1)["job_id"] == job


def test_southern_and_half_hour_dst_calendar_does_not_double():
    for zone, day in [("Australia/Lord_Howe", "2026-04-05"), ("America/New_York", "2026-11-01")]:
        value = parse_spec(
            spec("daily", start=stamp("2026-01-01T00:00:00+00:00"), timezone=zone, time="01:45:00")
        )
        from zoneinfo import ZoneInfo

        first = int(
            datetime.fromisoformat(day + "T01:45:00").replace(tzinfo=ZoneInfo(zone), fold=0).timestamp()
        )
        assert value.next_after(first - 1) == first
        assert value.next_after(first) - first >= 86400


def test_clock_rollback_and_explicit_edit_cannot_duplicate_committed_slot(scheduled):  # noqa: F811
    store, schedules, schedule, _, _ = scheduled
    Scheduler(store).tick(now=START)
    schedules.change(schedule, spec=spec(), now=START - 1)
    Scheduler(store).tick(now=START)
    assert len(store.rows()) == schedules.history(schedule)["total"] == 1
    assert schedules.get(schedule)["next_due"] == START + 60
