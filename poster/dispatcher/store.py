"""Separate WAL SQLite queue with checked, transactional state transitions."""

from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import time
import uuid

from filelock import FileLock

from .models import ACTIVE, MESSAGES, TERMINAL, TRANSITIONS, DispatcherError, Status
from .security import plain_path, private_dir, restrict

APPLICATION_ID = 0x50445350
SCHEMA_VERSION = 7

MIGRATION_7 = """
CREATE TABLE session_permits(id TEXT PRIMARY KEY,agent_id TEXT NOT NULL,project_id TEXT NOT NULL,version TEXT NOT NULL,expires REAL NOT NULL,remaining INTEGER NOT NULL,revoked INTEGER NOT NULL DEFAULT 0,data_cipher BLOB NOT NULL);
CREATE TABLE session_transfers(run_id TEXT PRIMARY KEY,permit_id TEXT NOT NULL REFERENCES session_permits(id),sent INTEGER NOT NULL DEFAULT 0,packet_cipher BLOB NOT NULL);
"""

MIGRATION_6 = """
ALTER TABLE jobs ADD COLUMN batch_id TEXT;
ALTER TABLE jobs ADD COLUMN batch_limit INTEGER NOT NULL DEFAULT 32 CHECK(batch_limit BETWEEN 1 AND 32);
CREATE INDEX jobs_batch_status ON jobs(batch_id,status);
CREATE TABLE admission_requests(id TEXT PRIMARY KEY,payload_cipher BLOB NOT NULL,created REAL NOT NULL);
CREATE TABLE cloud_sources(project_id TEXT NOT NULL,snapshot_version TEXT NOT NULL,metadata_cipher BLOB NOT NULL,PRIMARY KEY(project_id,snapshot_version));
CREATE TABLE cloud_results(mode TEXT NOT NULL,issuer TEXT NOT NULL,run_id TEXT NOT NULL,project_id TEXT NOT NULL,project_version TEXT NOT NULL,status TEXT NOT NULL,imported REAL NOT NULL,manifest_cipher BLOB NOT NULL,PRIMARY KEY(mode,issuer,run_id));
"""

MIGRATION_3 = """
ALTER TABLE jobs ADD COLUMN agent_id TEXT;
ALTER TABLE jobs ADD COLUMN remote_outputs TEXT NOT NULL DEFAULT '[]';
ALTER TABLE schedules ADD COLUMN agent_id TEXT;
ALTER TABLE schedules ADD COLUMN remote_outputs TEXT NOT NULL DEFAULT '[]';
CREATE TABLE remote_agents(id TEXT PRIMARY KEY,name TEXT NOT NULL,peer TEXT NOT NULL,relay_cipher BLOB NOT NULL,state TEXT NOT NULL,last_seen REAL NOT NULL DEFAULT 0,os TEXT,version TEXT,protocol INTEGER,last_error TEXT NOT NULL DEFAULT '');
CREATE TABLE remote_runs(run_id TEXT PRIMARY KEY REFERENCES executions(run_id),agent_id TEXT NOT NULL REFERENCES remote_agents(id),state TEXT NOT NULL,lease_until REAL NOT NULL,key_cipher BLOB);
CREATE TABLE remote_outbox(id TEXT PRIMARY KEY,agent_id TEXT NOT NULL,body BLOB NOT NULL);
CREATE TABLE remote_seen(id TEXT PRIMARY KEY,expires REAL NOT NULL);
CREATE INDEX jobs_agent ON jobs(agent_id,status);
"""

# Additive migration: the 4A queue, reports and state machine remain intact.
MIGRATION_2 = """
CREATE TABLE schedules(
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL, version TEXT NOT NULL, name TEXT NOT NULL,
 source TEXT NOT NULL, spec TEXT NOT NULL, inputs_cipher BLOB NOT NULL, input_names TEXT NOT NULL,
 timeout REAL NOT NULL, enabled INTEGER NOT NULL DEFAULT 1, deleted INTEGER NOT NULL DEFAULT 0,
 paused_code TEXT NOT NULL DEFAULT '', next_due INTEGER, created REAL NOT NULL, updated REAL NOT NULL);
CREATE INDEX schedules_due ON schedules(enabled,deleted,paused_code,next_due);
CREATE INDEX jobs_project_status ON jobs(project_id,status);
CREATE TABLE schedule_events(
 id TEXT PRIMARY KEY, schedule_id TEXT NOT NULL REFERENCES schedules(id), due INTEGER NOT NULL,
 through_due INTEGER NOT NULL, occurrences INTEGER NOT NULL DEFAULT 1,
 outcome TEXT NOT NULL, job_id TEXT UNIQUE REFERENCES jobs(id), created REAL NOT NULL,
 UNIQUE(schedule_id,due));
CREATE INDEX schedule_events_history ON schedule_events(schedule_id,due DESC);
CREATE TABLE schedule_reviews(job_id TEXT PRIMARY KEY REFERENCES jobs(id), reviewed REAL NOT NULL);
"""

SCHEMA = """
CREATE TABLE projects(
 project_id TEXT NOT NULL, version TEXT NOT NULL, source TEXT NOT NULL, registered REAL NOT NULL,
 PRIMARY KEY(project_id,version));
CREATE TABLE jobs(
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL, version TEXT NOT NULL,
 inputs_cipher BLOB NOT NULL, input_names TEXT NOT NULL, dedup_hash TEXT UNIQUE,
 status TEXT NOT NULL, attempt INTEGER NOT NULL DEFAULT 0, timeout REAL NOT NULL CHECK(timeout>0),
 cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK(cancel_requested IN (0,1)),
 owner TEXT, created REAL NOT NULL, updated REAL NOT NULL,
 FOREIGN KEY(project_id,version) REFERENCES projects(project_id,version));
CREATE TABLE executions(
 run_id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id), attempt INTEGER NOT NULL,
 owner TEXT NOT NULL, status TEXT NOT NULL, leased REAL NOT NULL, started REAL, finished REAL,
 pid INTEGER, birth REAL, exit_code INTEGER, code TEXT, report TEXT, report_sha256 TEXT,
 last_confirmed_step TEXT, effects_possible INTEGER NOT NULL DEFAULT 0,
 UNIQUE(job_id,attempt));
CREATE TABLE events(
 id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL REFERENCES jobs(id),
 status TEXT NOT NULL, code TEXT NOT NULL, at REAL NOT NULL);
CREATE TABLE control(id INTEGER PRIMARY KEY CHECK(id=1), owner TEXT, stop INTEGER NOT NULL DEFAULT 0);
INSERT INTO control(id) VALUES(1);
CREATE INDEX jobs_queue ON jobs(status,created);
CREATE INDEX executions_job ON executions(job_id,attempt);
CREATE INDEX events_job ON events(job_id,id);
"""


def dedup_hash(value):
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > 1000:
        raise DispatcherError("PARAMETERS_INVALID")
    return hashlib.sha256(value.encode()).hexdigest()


class Store:
    def __init__(self, path):
        self.path = plain_path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.root = private_dir(self.path.with_name(self.path.name + ".data"))
        if not self.path.exists():
            try:
                self.path.touch(mode=0o600, exist_ok=False)
            except FileExistsError:
                pass  # Another connection completed the exclusive first creation.
            restrict(self.path)
        self.db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        try:
            # Serialize identity reads, journal configuration and DDL together.
            # BEGIN IMMEDIATE alone cannot protect earlier PRAGMA/header reads.
            lock_path = plain_path(Path(str(self.path) + ".schema.lock"))
            with FileLock(str(lock_path), timeout=30):
                self._initialize()
        except Exception:
            self.db.close()
            raise

    def _initialize(self):
        self.db.execute("PRAGMA busy_timeout=30000")
        tables = {r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        app = self.db.execute("PRAGMA application_id").fetchone()[0]
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if (tables and (app != APPLICATION_ID or version not in (1, 2, 3, 4, 5, 6, SCHEMA_VERSION))) or (
            not tables and (app or version)
        ):
            raise DispatcherError("STORE_INVALID")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        with self.transaction():
            # Recheck under the write lock: concurrent first opens are harmless.
            if self.db.execute("PRAGMA user_version").fetchone()[0] == 0:
                for statement in SCHEMA.split(";"):
                    if statement.strip():
                        self.db.execute(statement)
                states = ",".join("'" + s.value + "'" for s in Status)
                allowed = " OR ".join(
                    "(OLD.status='"
                    + old.value
                    + "' AND NEW.status IN ("
                    + ",".join("'" + s.value + "'" for s in targets)
                    + "))"
                    for old, targets in TRANSITIONS.items()
                )
                self.db.execute(
                    f"CREATE TRIGGER jobs_insert BEFORE INSERT ON jobs WHEN NEW.status NOT IN ({states}) BEGIN SELECT RAISE(ABORT,'INVALID_STATE'); END"
                )
                self.db.execute(
                    f"CREATE TRIGGER jobs_transition BEFORE UPDATE OF status ON jobs WHEN OLD.status!=NEW.status AND NOT ({allowed}) BEGIN SELECT RAISE(ABORT,'INVALID_TRANSITION'); END"
                )
                self.db.execute(f"PRAGMA application_id={APPLICATION_ID}")
                self.db.execute("PRAGMA user_version=1")
            if self.db.execute("PRAGMA user_version").fetchone()[0] == 1:
                for statement in MIGRATION_2.split(";"):
                    if statement.strip():
                        self.db.execute(statement)
                self.db.execute("PRAGMA user_version=2")
            if self.db.execute("PRAGMA user_version").fetchone()[0] == 2:
                for statement in MIGRATION_3.split(";"):
                    if statement.strip():
                        self.db.execute(statement)
                self.db.execute("PRAGMA user_version=3")
            if self.db.execute("PRAGMA user_version").fetchone()[0] == 3:
                self.db.execute("ALTER TABLE remote_agents ADD COLUMN revoked_at REAL")
                self.db.execute("PRAGMA user_version=4")
            if self.db.execute("PRAGMA user_version").fetchone()[0] == 4:
                self.db.execute(
                    "ALTER TABLE remote_agents ADD COLUMN capabilities TEXT NOT NULL DEFAULT '{}'"
                )
                self.db.execute(
                    "CREATE TABLE remote_pairings(id TEXT PRIMARY KEY,expires REAL NOT NULL,controller TEXT NOT NULL,profile_cipher BLOB NOT NULL,consumed TEXT)"
                )
                self.db.execute("PRAGMA user_version=5")
            if self.db.execute("PRAGMA user_version").fetchone()[0] == 5:
                for statement in MIGRATION_6.split(";"):
                    if statement.strip():
                        self.db.execute(statement)
                self.db.execute("PRAGMA user_version=6")
            if self.db.execute("PRAGMA user_version").fetchone()[0] == 6:
                for statement in MIGRATION_7.split(";"):
                    if statement.strip():
                        self.db.execute(statement)
                self.db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        for suffix in ("", "-wal", "-shm"):
            item = Path(str(self.path) + suffix)
            if item.exists():
                restrict(item)

    def close(self):
        self.db.close()

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.db.rollback()
            raise
        else:
            self.db.commit()

    def event(self, job_id, status, code=""):
        self.db.execute(
            "INSERT INTO events(job_id,status,code,at) VALUES(?,?,?,?)", (job_id, status, code, time.time())
        )
        self.db.execute(
            "DELETE FROM events WHERE job_id=? AND id NOT IN (SELECT id FROM events WHERE job_id=? ORDER BY id DESC LIMIT 200)",
            (job_id, job_id),
        )
        self.db.execute(
            "DELETE FROM events WHERE id NOT IN (SELECT id FROM events ORDER BY id DESC LIMIT 10000)"
        )

    def find_key(self, key):
        row = self.db.execute("SELECT id FROM jobs WHERE dedup_hash=?", (dedup_hash(key),)).fetchone()
        return row[0] if row else None

    def reserve_admission(self, request_id, payload):
        """Persist an encrypted intent before per-item admission; safe after a crash."""
        from .security import seal, unseal
        from .snapshots import canonical

        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 200:
            raise DispatcherError("PARAMETERS_INVALID")
        raw = canonical(payload)
        if len(raw) > 1_000_000:
            raise DispatcherError("INPUT_INVALID")
        with self.transaction():
            previous = self.db.execute(
                "SELECT payload_cipher FROM admission_requests WHERE id=?", (request_id,)
            ).fetchone()
            if previous:
                if canonical(unseal(previous[0])) != raw:
                    raise DispatcherError("PARAMETERS_INVALID")
            else:
                self.db.execute(
                    "INSERT INTO admission_requests VALUES(?,?,?)", (request_id, seal(payload), time.time())
                )

    def enqueue(
        self,
        project_id,
        version,
        source,
        inputs_cipher,
        input_names=(),
        *,
        key=None,
        timeout=300,
        agent_id=None,
        remote_outputs=(),
        batch_id=None,
        batch_limit=32,
    ):
        with self.transaction():
            return self._enqueue(
                project_id,
                version,
                source,
                inputs_cipher,
                input_names,
                key=key,
                timeout=timeout,
                agent_id=agent_id,
                remote_outputs=remote_outputs,
                batch_id=batch_id,
                batch_limit=batch_limit,
            )

    def _enqueue(
        self,
        project_id,
        version,
        source,
        inputs_cipher,
        input_names=(),
        *,
        key=None,
        timeout=300,
        agent_id=None,
        remote_outputs=(),
        batch_id=None,
        batch_limit=32,
    ):
        """Caller owns BEGIN IMMEDIATE, including any schedule event and cursor update."""
        if not 0.05 <= timeout <= 86400:
            raise DispatcherError("PARAMETERS_INVALID")
        if (
            type(batch_limit) is not int
            or not 1 <= batch_limit <= 32
            or (batch_id is not None and (not isinstance(batch_id, str) or len(batch_id) > 300))
        ):
            raise DispatcherError("PARAMETERS_INVALID")
        if agent_id:
            from poster.remote.registry import validate_target, output_names

            validate_target(self, agent_id)
            remote_outputs = output_names(remote_outputs)
        now, job_id = time.time(), uuid.uuid4().hex
        hashed = dedup_hash(key)
        if hashed:
            previous = self.db.execute("SELECT id FROM jobs WHERE dedup_hash=?", (hashed,)).fetchone()
            if previous:
                return previous[0]
        self.db.execute(
            "INSERT OR IGNORE INTO projects VALUES(?,?,?,?)", (project_id, version, str(source), now)
        )
        self.db.execute(
            "INSERT INTO jobs(id,project_id,version,inputs_cipher,input_names,dedup_hash,status,timeout,created,updated,batch_id,batch_limit) VALUES(?,?,?,?,?,?,'PENDING',?,?,?,?,?)",
            (
                job_id,
                project_id,
                version,
                inputs_cipher,
                json.dumps(sorted(input_names)),
                hashed,
                timeout,
                now,
                now,
                batch_id,
                batch_limit,
            ),
        )
        self.db.execute(
            "UPDATE jobs SET agent_id=?,remote_outputs=? WHERE id=?",
            (agent_id, json.dumps(list(remote_outputs)), job_id),
        )
        self.event(job_id, Status.PENDING)
        return job_id

    def payload(self, job_id, *, private=True):
        columns = (
            "*"
            if private
            else "id,project_id,version,input_names,dedup_hash,status,attempt,timeout,cancel_requested,owner,created,updated,agent_id,remote_outputs"
        )
        row = self.db.execute("SELECT " + columns + " FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise DispatcherError("JOB_MISSING")
        return dict(row)

    def show(self, job_id):
        value = self.payload(job_id, private=False)
        value.pop("dedup_hash")
        value["input_names"] = json.loads(value["input_names"])
        value["history"] = self.history(job_id)
        value["events"] = [
            dict(r)
            for r in self.db.execute(
                "SELECT status,code,at FROM events WHERE job_id=? ORDER BY id DESC LIMIT 200", (job_id,)
            )
        ]
        return value

    def rows(self, limit=100):
        return [
            dict(r)
            for r in self.db.execute(
                "SELECT id,project_id,version,status,attempt,timeout,cancel_requested,created,updated FROM jobs ORDER BY created LIMIT ?",
                (max(1, min(limit, 1000)),),
            )
        ]

    def history(self, job_id):
        return [
            dict(r)
            for r in self.db.execute(
                "SELECT * FROM executions WHERE job_id=? ORDER BY attempt DESC LIMIT 1000", (job_id,)
            )
        ]

    def execution(self, run_id):
        row = self.db.execute("SELECT * FROM executions WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise DispatcherError("JOB_MISSING")
        return dict(row)

    def claim(self, owner, max_workers, max_per_project):
        from poster.remote.protocol import LEASE

        with self.transaction():
            if self.stopping():
                return None
            if (
                self.db.execute("SELECT count(*) FROM jobs WHERE status IN ('LEASED','RUNNING')").fetchone()[
                    0
                ]
                >= max_workers
            ):
                return None
            row = self.db.execute(
                "SELECT * FROM jobs j WHERE status='PENDING' AND (agent_id IS NULL OR EXISTS(SELECT 1 FROM remote_agents a WHERE a.id=j.agent_id AND a.state='TRUSTED' AND coalesce(json_extract(a.capabilities,'$.phase'),'RUNNING')='RUNNING' AND a.last_seen>"
                + str(time.time() - LEASE)
                + ")) AND NOT (EXISTS(SELECT 1 FROM schedule_events se WHERE se.job_id=j.id) AND EXISTS(SELECT 1 FROM jobs u LEFT JOIN schedule_reviews r ON r.job_id=u.id WHERE u.project_id=j.project_id AND u.status IN ('UNKNOWN','REVIEW_REQUIRED') AND r.job_id IS NULL)) AND (SELECT count(*) FROM jobs a WHERE a.project_id=j.project_id AND a.status IN ('LEASED','RUNNING'))<? AND (j.batch_id IS NULL OR (SELECT count(*) FROM jobs b WHERE b.batch_id=j.batch_id AND b.status IN ('LEASED','RUNNING'))<j.batch_limit) ORDER BY created LIMIT 1",
                (max_per_project,),
            ).fetchone()
            if row is None:
                return None
            now, run_id = time.time(), uuid.uuid4().hex
            self.db.execute(
                "UPDATE jobs SET status='LEASED',attempt=attempt+1,owner=?,updated=? WHERE id=? AND status='PENDING'",
                (owner, now, row["id"]),
            )
            self.db.execute(
                "INSERT INTO executions(run_id,job_id,attempt,owner,status,leased) VALUES(?,?,?,?,'LEASED',?)",
                (run_id, row["id"], row["attempt"] + 1, owner, now),
            )
            if row["agent_id"]:
                self.db.execute(
                    "INSERT INTO remote_runs(run_id,agent_id,state,lease_until) VALUES(?,?,'OFFERED',?)",
                    (run_id, row["agent_id"], now + 30),
                )
            self.event(row["id"], Status.LEASED)
        return self.execution(run_id)

    def is_remote(self, run_id):
        return self.db.execute("SELECT * FROM remote_runs WHERE run_id=?", (run_id,)).fetchone()

    def record_process(self, run_id, owner, pid, birth):
        with self.transaction():
            return bool(
                self.db.execute(
                    "UPDATE executions SET pid=?,birth=? WHERE run_id=? AND owner=? AND status='LEASED' AND pid IS NULL",
                    (pid, birth, run_id, owner),
                ).rowcount
            )

    def start(self, run_id, owner, pid):
        with self.transaction():
            execution = self.execution(run_id)
            job = self.payload(execution["job_id"], private=False)
            if (
                execution["owner"] != owner
                or execution["pid"] != pid
                or job["status"] != "LEASED"
                or job["cancel_requested"]
            ):
                return False
            now = time.time()
            self.db.execute("UPDATE executions SET status='RUNNING',started=? WHERE run_id=?", (now, run_id))
            self.db.execute("UPDATE jobs SET status='RUNNING',updated=? WHERE id=?", (now, job["id"]))
            self.event(job["id"], Status.RUNNING)
        return True

    def progress(self, run_id, *, confirmed=None, effects=False):
        with self.transaction():
            self.db.execute(
                "UPDATE executions SET last_confirmed_step=coalesce(?,last_confirmed_step),effects_possible=max(effects_possible,?) WHERE run_id=? AND status='RUNNING'",
                (confirmed, int(effects), run_id),
            )

    def note(self, run_id, code):
        if code not in MESSAGES:
            raise DispatcherError("PARAMETERS_INVALID")
        with self.transaction():
            execution = self.execution(run_id)
            if execution["status"] == Status.RUNNING:
                self.event(execution["job_id"], Status.RUNNING, code)

    def complete(self, run_id, status, *, code="", exit_code=None, report=None, report_sha256=None):
        status = Status(status)
        if status not in TERMINAL or (code and code not in MESSAGES):
            raise DispatcherError("PARAMETERS_INVALID")
        with self.transaction():
            execution = self.execution(run_id)
            job = self.payload(execution["job_id"], private=False)
            if job["owner"] != execution["owner"] or job["status"] not in ACTIVE:
                return False
            now = time.time()
            self.db.execute(
                "UPDATE executions SET status=?,finished=?,code=?,exit_code=?,report=?,report_sha256=? WHERE run_id=?",
                (status, now, code, exit_code, str(report) if report else None, report_sha256, run_id),
            )
            self.db.execute("UPDATE jobs SET status=?,updated=? WHERE id=?", (status, now, job["id"]))
            self.event(job["id"], status, code)
        return True

    def cancel(self, job_id):
        with self.transaction():
            self._cancel(job_id)
        return self.show(job_id)

    def _cancel(self, job_id):
        job = self.payload(job_id, private=False)
        if job["status"] == Status.PENDING:
            self.db.execute(
                "UPDATE jobs SET status='CANCELLED',cancel_requested=1,updated=? WHERE id=?",
                (time.time(), job_id),
            )
            self.event(job_id, Status.CANCELLED, "CANCEL_REQUESTED")
        elif job["status"] in ACTIVE:
            self.db.execute("UPDATE jobs SET cancel_requested=1,updated=? WHERE id=?", (time.time(), job_id))

    def cancel_many(self, ids):
        """One durable transaction for a UI selection, retaining single-job semantics."""
        if not isinstance(ids, list) or len(ids) > 1000:
            raise DispatcherError("PARAMETERS_INVALID")
        with self.transaction():
            for job_id in ids:
                self._cancel(job_id)
        return [self.payload(job_id, private=False)["status"] for job_id in ids]

    def active(self):
        return [
            dict(r)
            for r in self.db.execute(
                "SELECT e.* FROM executions e JOIN jobs j ON j.id=e.job_id WHERE j.status IN ('LEASED','RUNNING') AND e.attempt=j.attempt"
            )
        ]

    def recover_unstarted(self, run_id):
        with self.transaction():
            execution = self.execution(run_id)
            if execution["status"] != Status.LEASED or execution["pid"] is not None:
                return False
            target = (
                Status.CANCELLED if self.payload(execution["job_id"])["cancel_requested"] else Status.PENDING
            )
            self.db.execute(
                "UPDATE jobs SET status=?,owner=NULL,updated=? WHERE id=? AND status='LEASED'",
                (target, time.time(), execution["job_id"]),
            )
            self.db.execute(
                "UPDATE executions SET status='FAILED',finished=?,code='NOT_STARTED' WHERE run_id=?",
                (time.time(), run_id),
            )
            self.event(execution["job_id"], target, "NOT_STARTED")
        return True

    def review(self, job_id):
        with self.transaction():
            if self.db.execute(
                "UPDATE jobs SET status='REVIEW_REQUIRED',updated=? WHERE id=? AND status='UNKNOWN'",
                (time.time(), job_id),
            ).rowcount:
                self.event(job_id, Status.REVIEW_REQUIRED, "REVIEW_REQUIRED")

    def request_stop(self):
        with self.transaction():
            self.db.execute("UPDATE control SET stop=1 WHERE id=1")

    def begin_service(self, owner):
        with self.transaction():
            self.db.execute("UPDATE control SET owner=?,stop=0 WHERE id=1", (owner,))

    def stopping(self):
        return bool(self.db.execute("SELECT stop FROM control WHERE id=1").fetchone()[0])

    def set_exit_code(self, run_id, code):
        with self.transaction():
            self.db.execute("UPDATE executions SET exit_code=? WHERE run_id=?", (code, run_id))

    def invalidate_report(self, run_id):
        with self.transaction():
            execution = self.execution(run_id)
            if execution["status"] == Status.SUCCEEDED:
                self.db.execute(
                    "UPDATE executions SET status='UNKNOWN',code='REPORT_INVALID' WHERE run_id=?", (run_id,)
                )
                self.db.execute(
                    "UPDATE jobs SET status='UNKNOWN',updated=? WHERE id=? AND status='SUCCEEDED'",
                    (time.time(), execution["job_id"]),
                )
                self.event(execution["job_id"], Status.UNKNOWN, "REPORT_INVALID")

    def end_service(self, owner):
        with self.transaction():
            self.db.execute("UPDATE control SET owner=NULL WHERE id=1 AND owner=?", (owner,))

    def counts(self):
        return {r[0]: r[1] for r in self.db.execute("SELECT status,count(*) FROM jobs GROUP BY status")}
