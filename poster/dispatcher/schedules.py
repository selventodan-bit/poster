"""Durable calendar admission into the existing queue, never an execution engine."""

import hashlib
import json
import time
import uuid

from .models import DispatcherError
from .schedule_time import parse_spec
from .security import unseal
from .snapshots import Snapshots


def event_id(schedule_id, due):
    return hashlib.sha256(f"{schedule_id}:{due}".encode()).hexdigest()


class Schedules:
    def __init__(self, store):
        self.store = store
        self.db = store.db

    def get(self, schedule_id):
        row = self.db.execute("SELECT * FROM schedules WHERE id=? AND deleted=0", (schedule_id,)).fetchone()
        if row is None:
            raise DispatcherError("SCHEDULE_MISSING")
        return dict(row)

    def create(self, item, spec, cipher, names, *, timeout=300, now=None, agent_id=None, remote_outputs=()):
        from poster.remote.registry import output_names, validate_target

        if agent_id:
            validate_target(self.store, agent_id)
        remote_outputs = output_names(remote_outputs)
        spec = parse_spec(spec)
        if type(timeout) not in (int, float) or not 1 <= timeout <= 86400:
            raise DispatcherError("PARAMETERS_INVALID")
        now = time.time() if now is None else now
        schedule_id = uuid.uuid4().hex
        # Past start is retained for explicit misfire accounting, never silently shifted.
        due = spec.next_after(spec.start - 1)
        with self.store.transaction():
            self.db.execute(
                "INSERT INTO schedules(id,project_id,version,name,source,spec,inputs_cipher,input_names,timeout,next_due,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    schedule_id,
                    item["project_id"],
                    item["version"],
                    item["name"],
                    item["source"],
                    spec.model_dump_json(),
                    cipher,
                    json.dumps(names),
                    timeout,
                    due,
                    now,
                    now,
                ),
            )
            self.db.execute(
                "UPDATE schedules SET agent_id=?,remote_outputs=? WHERE id=?",
                (agent_id, json.dumps(remote_outputs), schedule_id),
            )
        return schedule_id

    def change(
        self,
        schedule_id,
        *,
        spec=None,
        enabled=None,
        delete=False,
        resume=False,
        replacement=None,
        now=None,
        target=None,
    ):
        with self.store.transaction():
            row = self.get(schedule_id)
            now = time.time() if now is None else now
            if target is not None:
                from poster.remote.registry import validate_target, output_names

                agent_id = target.get("agent_id")
                if agent_id:
                    validate_target(self.store, agent_id)
                outputs = output_names(target.get("remote_outputs", []))
                self.db.execute(
                    "UPDATE schedules SET agent_id=?,remote_outputs=? WHERE id=?",
                    (agent_id, json.dumps(outputs), schedule_id),
                )
            if spec is not None:
                parsed = parse_spec(spec)
                self.db.execute(
                    "UPDATE schedules SET spec=?,next_due=? WHERE id=?",
                    (parsed.model_dump_json(), parsed.next_after(now), schedule_id),
                )
            if replacement is not None:
                item, cipher, names = replacement
                # Explicit version update only; retain project identity and historical events.
                if item["project_id"] != row["project_id"]:
                    raise DispatcherError("SNAPSHOT_INVALID")
                self.db.execute(
                    "UPDATE schedules SET version=?,source=?,name=?,inputs_cipher=?,input_names=? WHERE id=?",
                    (item["version"], item["source"], item["name"], cipher, json.dumps(names), schedule_id),
                )
            if enabled is not None:
                if type(enabled) is not bool:
                    raise DispatcherError("PARAMETERS_INVALID")
                self.db.execute("UPDATE schedules SET enabled=? WHERE id=?", (int(enabled), schedule_id))
            if resume:
                self.db.execute(
                    "INSERT OR IGNORE INTO schedule_reviews SELECT id,? FROM jobs WHERE project_id=? AND status IN ('UNKNOWN','REVIEW_REQUIRED')",
                    (now, row["project_id"]),
                )
                self.db.execute(
                    "UPDATE schedules SET paused_code='' WHERE project_id=?", (row["project_id"],)
                )
            if delete:
                self.db.execute("UPDATE schedules SET deleted=1,enabled=0 WHERE id=?", (schedule_id,))
            self.db.execute("UPDATE schedules SET updated=? WHERE id=?", (now, schedule_id))
        return True

    @staticmethod
    def public(row):
        return {k: v for k, v in row.items() if k not in {"inputs_cipher", "input_names", "source"}}

    def rows(self, *, search="", offset=0, order="next_due", descending=False, state=""):
        if order not in {"name", "version", "next_due", "updated", "enabled", "project_id", "created"}:
            raise DispatcherError("PARAMETERS_INVALID")
        if state not in {"", "enabled", "paused", "disabled"}:
            raise DispatcherError("PARAMETERS_INVALID")
        self.db.create_function("poster_fold", 1, str.casefold, deterministic=True)
        where = "deleted=0 AND (poster_fold(name) LIKE ? OR project_id LIKE ? OR id LIKE ?)"
        if state == "enabled":
            where += " AND enabled=1 AND paused_code=''"
        elif state == "paused":
            where += " AND paused_code!=''"
        elif state == "disabled":
            where += " AND enabled=0"
        args = ["%" + search[:200].casefold() + "%"] * 3
        total = self.db.execute("SELECT count(*) FROM schedules WHERE " + where, args).fetchone()[0]
        rows = [
            self.public(dict(r))
            for r in self.db.execute(
                "SELECT id,project_id,version,name,spec,timeout,enabled,deleted,paused_code,next_due,created,updated,agent_id,remote_outputs FROM schedules WHERE "
                + where
                + " ORDER BY "
                + order
                + (" DESC" if descending else " ASC")
                + ",id LIMIT 200 OFFSET ?",
                [*args, max(0, int(offset))],
            )
        ]
        if rows:
            ids = [r["id"] for r in rows]
            marks = ",".join("?" for _ in ids)
            stats = {
                r["schedule_id"]: dict(r)
                for r in self.db.execute(
                    f"SELECT se.schedule_id,count(se.job_id) executions,sum(coalesce(j.status='SUCCEEDED',0)) successes,sum(coalesce(j.status IN ('FAILED','TIMED_OUT','UNKNOWN','REVIEW_REQUIRED'),0)) errors,sum(CASE WHEN se.outcome='MISSED' THEN se.occurrences ELSE 0 END) missed,max(e.started) last_run FROM schedule_events se LEFT JOIN jobs j ON j.id=se.job_id LEFT JOIN executions e ON e.job_id=j.id AND e.attempt=j.attempt WHERE se.schedule_id IN ({marks}) GROUP BY se.schedule_id",
                    ids,
                )
            }
            latest = {
                r["schedule_id"]: dict(r)
                for r in self.db.execute(
                    f"SELECT se.schedule_id,coalesce(j.status,se.outcome) last_result,se.due last_due FROM schedule_events se LEFT JOIN jobs j ON j.id=se.job_id WHERE se.schedule_id IN ({marks}) AND se.due=(SELECT max(x.due) FROM schedule_events x WHERE x.schedule_id=se.schedule_id)",
                    ids,
                )
            }
            for row in rows:
                row["spec"] = json.loads(row["spec"])
                row["outputs"] = json.loads(row.pop("remote_outputs"))
                row.update(stats.get(row["id"], {}))
                row.update(latest.get(row["id"], {}))
                row["status"] = row["paused_code"] or ("ENABLED" if row["enabled"] else "DISABLED")
        return {"rows": rows, "total": total, "offset": offset}

    def history(self, schedule_id, offset=0):
        # Tombstones preserve history after deletion.
        total = self.db.execute(
            "SELECT count(*) FROM schedule_events WHERE schedule_id=?", (schedule_id,)
        ).fetchone()[0]
        rows = [
            dict(r)
            for r in self.db.execute(
                "SELECT se.id,se.due,se.through_due,se.occurrences,se.outcome,se.job_id,j.status,j.version,e.started,e.finished,e.pid,e.code,e.last_confirmed_step FROM schedule_events se LEFT JOIN jobs j ON j.id=se.job_id LEFT JOIN executions e ON e.job_id=j.id AND e.attempt=j.attempt WHERE se.schedule_id=? ORDER BY se.due DESC LIMIT 200 OFFSET ?",
                (schedule_id, max(0, int(offset))),
            )
        ]
        return {"rows": rows, "total": total, "offset": offset}


class Scheduler:
    def __init__(self, store, *, clock=time.time):
        self.store, self.db, self.clock = store, store.db, clock
        self.last_tick = float("-inf")

    def tick(self, *, now=None):
        if now is None:
            # Admission does not add a query per GUI row or per 30ms worker poll.
            if time.monotonic() - self.last_tick < 0.5:
                return
            self.last_tick = time.monotonic()
            now = self.clock()
        # Bounded batches, one shared queue, original FIFO created ordering.
        # Pauses also cover manual jobs with an uncertain external effect.
        with self.store.transaction():
            if self.store.stopping():
                return
            self.db.execute(
                "UPDATE schedules SET paused_code='SCHEDULE_REVIEW' WHERE deleted=0 AND paused_code!='SCHEDULE_REVIEW' AND project_id IN (SELECT j.project_id FROM jobs j LEFT JOIN schedule_reviews r ON r.job_id=j.id WHERE j.status IN ('UNKNOWN','REVIEW_REQUIRED') AND r.job_id IS NULL)"
            )
        ids = [
            r[0]
            for r in self.db.execute(
                "SELECT id FROM schedules WHERE enabled=1 AND deleted=0 AND paused_code='' AND next_due<=? ORDER BY next_due,id LIMIT 100",
                (now,),
            )
        ]
        for schedule_id in ids:
            with self.store.transaction():
                if self.store.stopping():
                    return
                row = self.db.execute(
                    "SELECT * FROM schedules WHERE id=? AND enabled=1 AND deleted=0 AND paused_code='' AND next_due<=?",
                    (schedule_id, now),
                ).fetchone()
                if row is None:
                    continue
                row = dict(row)
                if self.db.execute(
                    "SELECT 1 FROM jobs j LEFT JOIN schedule_reviews r ON r.job_id=j.id WHERE j.project_id=? AND j.status IN ('UNKNOWN','REVIEW_REQUIRED') AND r.job_id IS NULL LIMIT 1",
                    (row["project_id"],),
                ).fetchone():
                    self.db.execute(
                        "UPDATE schedules SET paused_code='SCHEDULE_REVIEW' WHERE project_id=?",
                        (row["project_id"],),
                    )
                    continue
                spec = parse_spec(json.loads(row["spec"]))
                first = row["next_due"]
                count, last = spec.due_span(first, now)
                admit = spec.misfire == "latest" or now - last <= spec.grace
                missed = count - int(admit)
                if missed:
                    through = spec.due_span(first, last - 1)[1] if admit else last
                    self.db.execute(
                        "INSERT OR IGNORE INTO schedule_events VALUES(?,?,?,?,?,'MISSED',NULL,?)",
                        (event_id(schedule_id, first), schedule_id, first, through, missed, now),
                    )
                if admit:
                    try:
                        # Fail closed on missing/corrupt key or changed snapshot; never recapture source.
                        unseal(row["inputs_cipher"])
                        manifest, _ = Snapshots(self.store.root).read(row["version"])
                        if manifest["project_id"] != row["project_id"]:
                            raise DispatcherError("SNAPSHOT_INVALID")
                        identity = event_id(schedule_id, last)
                        job = self.store._enqueue(
                            row["project_id"],
                            row["version"],
                            row["source"],
                            row["inputs_cipher"],
                            json.loads(row["input_names"]),
                            key="schedule:" + identity,
                            timeout=row["timeout"],
                            agent_id=row["agent_id"],
                            remote_outputs=json.loads(row["remote_outputs"]),
                        )
                        self.db.execute(
                            "INSERT OR IGNORE INTO schedule_events VALUES(?,?,?,?,?,'ENQUEUED',?,?)",
                            (identity, schedule_id, last, last, 1, job, now),
                        )
                    except DispatcherError as exc:
                        self.db.execute(
                            "UPDATE schedules SET paused_code=? WHERE id=?", (exc.code, schedule_id)
                        )
                        # Do not advance; explicit review retries the same stable identity.
                        # Missed summary must roll back as well to avoid overlapping ranges.
                        if missed:
                            self.db.execute(
                                "DELETE FROM schedule_events WHERE id=?", (event_id(schedule_id, first),)
                            )
                        continue
                self.db.execute(
                    "UPDATE schedules SET next_due=?,updated=? WHERE id=?",
                    (spec.next_after(now), now, schedule_id),
                )
