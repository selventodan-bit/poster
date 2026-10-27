"""Transactional local queue, immutable inputs, and conservative crash recovery."""

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager, closing
from pathlib import Path

from filelock import FileLock

from .content_generator.generator import Content
from .project import Project
from .runtime import Result

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version(version INTEGER NOT NULL);
INSERT INTO schema_version SELECT 1 WHERE NOT EXISTS(SELECT 1 FROM schema_version);
CREATE TABLE IF NOT EXISTS projects(
 id TEXT PRIMARY KEY, revision TEXT NOT NULL, snapshot TEXT NOT NULL, active INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS content_variants(
 id TEXT PRIMARY KEY, title TEXT NOT NULL, body TEXT NOT NULL, metadata TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS jobs(
 id TEXT PRIMARY KEY, dedup_key TEXT UNIQUE NOT NULL, project_id TEXT NOT NULL,
 project_revision TEXT NOT NULL, snapshot TEXT NOT NULL, variables TEXT NOT NULL,
 content_id TEXT, status TEXT NOT NULL, attempt INTEGER NOT NULL DEFAULT 0,
 result_url TEXT, reason TEXT NOT NULL DEFAULT '', created REAL NOT NULL, updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS executions(
 run_id TEXT PRIMARY KEY, job_id TEXT NOT NULL, attempt INTEGER NOT NULL,
 started REAL NOT NULL, finished REAL, status TEXT NOT NULL, result_url TEXT, reason TEXT);
CREATE TABLE IF NOT EXISTS replay_artifacts(
 run_id TEXT PRIMARY KEY, operations TEXT NOT NULL);
"""


class State:
    def __init__(self, path: Path):
        from .product.locking import connection_lease

        self.path = path
        self._data_lease = connection_lease(path.parent)
        try:
            self._open(path)
        except BaseException:
            if getattr(self, "db", None) is not None:
                self.db.close()
            self._data_lease.close()
            raise

    def _open(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=30)
        self.db.row_factory = sqlite3.Row
        tables = {r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if tables:
            if "schema_version" not in tables or [
                r[0] for r in self.db.execute("SELECT version FROM schema_version")
            ] != [1]:
                raise ValueError("Unsupported database schema version")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        if self.db.execute("SELECT version FROM schema_version").fetchone()[0] != 1:
            raise ValueError("Unsupported database schema version")

    def close(self):
        try:
            self.db.close()
        finally:
            self._data_lease.close()

    @contextmanager
    def engine_lock(self):
        # OS-held lock releases after a crash. DB must be on a local filesystem.
        with FileLock(str(self.path) + ".engine.lock", timeout=0):
            yield

    def register(self, projects: dict[str, Project]):
        with self.db:
            self.db.execute("UPDATE projects SET active=0")
            for p in projects.values():
                self.db.execute(
                    "INSERT INTO projects VALUES(?,?,?,1) ON CONFLICT(id) DO UPDATE SET "
                    "revision=excluded.revision,snapshot=excluded.snapshot,active=1",
                    (p.site.id, p.revision, p.serialize()),
                )

    def restore_registry(self):
        return {
            r["id"]: Project.deserialize(r["snapshot"])
            for r in self.db.execute("SELECT * FROM projects WHERE active=1")
        }

    def enqueue(self, project: Project, variables: dict, dedup_key: str, content: Content | None = None):
        job_id, now = uuid.uuid4().hex, time.time()
        if content:
            variables = {**variables, "title": content.title, "body": content.body}
        with self.db:
            if content:
                self.db.execute(
                    "INSERT OR IGNORE INTO content_variants VALUES(?,?,?,?)",
                    (
                        content.content_id,
                        content.title,
                        content.body,
                        json.dumps(content.metadata, ensure_ascii=False),
                    ),
                )
            self.db.execute(
                "INSERT OR IGNORE INTO jobs(id,dedup_key,project_id,project_revision,snapshot,"
                "variables,content_id,status,created,updated) VALUES(?,?,?,?,?,?,?,'PENDING',?,?)",
                (
                    job_id,
                    dedup_key,
                    project.site.id,
                    project.revision,
                    project.serialize(),
                    json.dumps(variables, ensure_ascii=False),
                    content.content_id if content else None,
                    now,
                    now,
                ),
            )
        return self.db.execute("SELECT id FROM jobs WHERE dedup_key=?", (dedup_key,)).fetchone()[0]

    def pending(self):
        return list(self.db.execute("SELECT * FROM jobs WHERE status='PENDING' ORDER BY created"))

    def has_key(self, key):
        return self.db.execute("SELECT 1 FROM jobs WHERE dedup_key=?", (key,)).fetchone() is not None

    def start(self, job_id):
        now, run_id = time.time(), uuid.uuid4().hex
        with self.db:
            changed = self.db.execute(
                "UPDATE jobs SET status='LEASED',updated=? WHERE id=? AND status='PENDING'", (now, job_id)
            ).rowcount
            if not changed:
                return None
        # Each transition is committed, so a crash has an explicit recovery rule.
        with self.db:
            self.db.execute(
                "UPDATE jobs SET status='RUNNING',attempt=attempt+1,updated=? WHERE id=?", (now, job_id)
            )
            attempt = self.db.execute("SELECT attempt FROM jobs WHERE id=?", (job_id,)).fetchone()[0]
            self.db.execute(
                "INSERT INTO executions(run_id,job_id,attempt,started,status) VALUES(?,?,?,?,'RUNNING')",
                (run_id, job_id, attempt, now),
            )
        return run_id

    def finish(self, job_id: str, run_id: str, result: Result):
        now = time.time()
        with self.db:
            self.db.execute(
                "UPDATE jobs SET status=?,result_url=?,reason=?,updated=? WHERE id=?",
                (result.status, result.result_url, result.reason, now, job_id),
            )
            self.db.execute(
                "UPDATE executions SET status=?,finished=?,result_url=?,reason=? WHERE run_id=?",
                (result.status, now, result.result_url, result.reason, run_id),
            )
            self.db.execute(
                "INSERT OR REPLACE INTO replay_artifacts VALUES(?,?)", (run_id, json.dumps(result.operations))
            )

    def recover(self):
        with self.db:
            self.db.execute("UPDATE jobs SET status='PENDING' WHERE status='LEASED'")
            self.db.execute(
                "UPDATE jobs SET status='UNKNOWN',reason='Engine stopped during execution; verify first' "
                "WHERE status='RUNNING'"
            )
            self.db.execute(
                "UPDATE executions SET status='UNKNOWN',finished=? WHERE status='RUNNING'", (time.time(),)
            )

    def rows(self):
        return [
            dict(r)
            for r in self.db.execute(
                "SELECT id,project_id,project_revision,content_id,status,attempt,result_url,reason,created,updated "
                "FROM jobs ORDER BY created"
            )
        ]

    def backup(self, destination: Path):
        if destination.resolve() == self.path.resolve() or destination.exists():
            raise ValueError("Backup destination must be a new file")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(destination)) as target:
            self.db.backup(target)
            if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("Backup integrity check failed")

    @staticmethod
    def restore(snapshot: Path, destination: Path):
        if destination.exists():
            raise ValueError("Restore destination must not exist")
        from .product.locking import connection_lease

        with (
            connection_lease(destination.parent),
            closing(sqlite3.connect(f"file:{snapshot.resolve()}?mode=ro", uri=True)) as src,
        ):
            if src.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("Invalid snapshot")
            version = src.execute("SELECT version FROM schema_version").fetchone()[0]
            if version != 1:
                raise ValueError("Unsupported snapshot version")
            destination.parent.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(destination)) as dst:
                src.backup(dst)
