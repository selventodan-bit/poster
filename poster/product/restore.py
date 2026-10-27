"""Staged, journalled same-user recovery. Restored jobs never run automatically."""

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import time
import uuid

from poster.dispatcher.models import DispatcherError
from poster.dispatcher.security import plain_path, private_dir, restrict
from . import backup, crypto
from .locking import lock_root, offline


def sync_directory(path):
    if os.name != "nt":
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def durable(path, value):
    path = plain_path(path)
    private_dir(path.parent)
    temporary = path.with_name("." + path.name + "." + uuid.uuid4().hex)
    try:
        with temporary.open("xb") as f:
            restrict(temporary)
            f.write(json.dumps(value, allow_nan=False).encode())
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def remove_owned(path):
    path = plain_path(path)
    if path.is_dir():
        # Refuse links anywhere before recursive removal, including Windows
        # junctions. Only transaction-owned staging/new roots reach this helper.
        for child in path.rglob("*"):
            plain_path(child)
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def checkpoint(phase):
    """A test may observe/kill the real process at a durable boundary."""


def file_identity(path):
    path = plain_path(path)
    if not path.exists():
        return None
    stat = path.stat()
    return [stat.st_dev, stat.st_ino]


def sanitize_native(path):
    from poster.dispatcher.store import Store

    store = Store(path)
    try:
        db = store.db
        db.execute("PRAGMA trusted_schema=OFF")
        counts = dict(db.execute("SELECT status,count(*) FROM jobs GROUP BY status").fetchall())
        with store.transaction():
            now = time.time()
            for status, target in [
                ("PENDING", "CANCELLED"),
                ("LEASED", "REVIEW_REQUIRED"),
                ("RUNNING", "REVIEW_REQUIRED"),
                ("UNKNOWN", "REVIEW_REQUIRED"),
            ]:
                ids = [r[0] for r in db.execute("SELECT id FROM jobs WHERE status=?", (status,))]
                for job in ids:
                    db.execute(
                        "UPDATE jobs SET status=?,owner=NULL,cancel_requested=0,updated=? WHERE id=?",
                        (target, now, job),
                    )
                    store.event(job, target, "REVIEW_REQUIRED")
            db.execute(
                "UPDATE executions SET status='REVIEW_REQUIRED',code='REVIEW_REQUIRED',pid=NULL,birth=NULL,finished=? WHERE status IN ('LEASED','RUNNING','UNKNOWN')",
                (now,),
            )
            db.execute("UPDATE schedules SET enabled=0,paused_code='REVIEW_REQUIRED'")
            db.execute("UPDATE control SET owner=NULL,stop=1")
            db.execute("DELETE FROM remote_outbox")
            db.execute("UPDATE remote_runs SET state='UNKNOWN',key_cipher=NULL")
            db.execute("UPDATE remote_agents SET state='REVOKED'")
            db.execute("DELETE FROM remote_pairings")
            db.execute("DELETE FROM admission_requests")
            db.execute("UPDATE session_permits SET revoked=1,remaining=0")
            db.execute("DELETE FROM session_transfers")
        if (
            db.execute("PRAGMA integrity_check").fetchone()[0] != "ok"
            or db.execute("PRAGMA foreign_key_check").fetchall()
        ):
            raise DispatcherError("BACKUP_INVALID")
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return counts
    finally:
        store.close()


def sanitize_legacy(path):
    if not path.exists():
        return
    with sqlite3.connect(path) as db:
        if db.execute("PRAGMA application_id").fetchone()[0] != 0:
            raise DispatcherError("BACKUP_INVALID")
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "schema_version" not in tables or db.execute("SELECT version FROM schema_version").fetchall() != [
            (1,)
        ]:
            raise DispatcherError("BACKUP_INVALID")
        db.execute("PRAGMA trusted_schema=OFF")
        db.execute(
            "UPDATE jobs SET status='UNKNOWN',reason='RESTORED_REVIEW_REQUIRED' WHERE status IN ('PENDING','LEASED','RUNNING','UNKNOWN')"
        )
        db.execute(
            "UPDATE executions SET status='UNKNOWN',reason='RESTORED_REVIEW_REQUIRED',finished=? WHERE status IN ('PENDING','LEASED','RUNNING')",
            (time.time(),),
        )
        db.commit()
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise DispatcherError("BACKUP_INVALID")


def journal_checked(path, roots):
    try:
        value = json.loads(plain_path(path).read_bytes())
        tx = value["id"]
        if (
            value["format"] != "poster.restore.v1"
            or not re.fullmatch("[a-f0-9]{32}", tx)
            or value["binding"] != backup.binding()
            or value["phase"] not in {"PREPARED", "APPLYING", "COMMITTED", "ROLLED_BACK"}
        ):
            raise ValueError()
        entries = value["entries"]
        if not isinstance(entries, list) or not 1 <= len(entries) <= len(roots):
            raise ValueError()
        seen = set()
        for row in entries:
            name = row["scope"]
            target = roots[name]
            if name in seen or row["target"] != str(target) or type(row["existed"]) is not bool:
                raise ValueError()
            seen.add(name)
            base = target.parent / (".poster-stage-" + tx + "-" + name)
            previous = target.with_name("." + target.name + ".poster-before-" + tx)
            if row["base"] != str(base) or row["previous"] != str(previous):
                raise ValueError()
            for key in ("old_id", "new_id"):
                identity = row[key]
                if identity is None and key == "old_id" and not row["existed"]:
                    continue
                if (
                    not isinstance(identity, list)
                    or len(identity) != 2
                    or any(type(n) is not int or n < 0 for n in identity)
                ):
                    raise ValueError()
            for p in [base, previous, target]:
                plain_path(p)
        if plain_path(path) != lock_root(roots[entries[0]["scope"]]) / "restore-transaction.json":
            raise ValueError()
        return value
    except Exception as exc:
        raise DispatcherError("RESTORE_RECOVERY") from exc


def discover(roots):
    roots = backup.roots_checked(roots)
    journals = set()
    for root in roots.values():
        folder = lock_root(root)
        marker = folder / "restore-pending.json"
        if marker.exists():
            try:
                value = json.loads(plain_path(marker).read_bytes())
                journal = plain_path(value["journal"])
                if journal.parent not in {lock_root(p) for p in roots.values()}:
                    raise ValueError()
                checked = journal_checked(journal, roots)
                if value["id"] != checked["id"]:
                    raise ValueError()
                journals.add(journal)
            except Exception as exc:
                raise DispatcherError("RESTORE_RECOVERY") from exc
        own = folder / "restore-transaction.json"
        if own.exists():
            journal_checked(own, roots)
            journals.add(own)
    return sorted(journals)


def rollback(journal, roots):
    value = journal_checked(journal, roots)
    if value["phase"] != "COMMITTED":
        for row in reversed(value["entries"]):
            target, previous, base = map(Path, [row["target"], row["previous"], row["base"]])
            if previous.exists():
                if file_identity(previous) != row["old_id"]:
                    raise DispatcherError("RESTORE_RECOVERY")
                if target.exists():
                    if file_identity(target) != row["new_id"]:
                        raise DispatcherError("RESTORE_RECOVERY")
                    remove_owned(target)
                os.replace(previous, target)
                sync_directory(target.parent)
            elif target.exists():
                identity = file_identity(target)
                if not row["existed"] and identity == row["new_id"]:
                    remove_owned(target)
                    sync_directory(target.parent)
                elif identity != row["old_id"]:
                    raise DispatcherError("RESTORE_RECOVERY")
            elif row["existed"]:
                raise DispatcherError("RESTORE_RECOVERY")
        value["phase"] = "ROLLED_BACK"
        durable(journal, value)
    for row in value["entries"]:
        base = Path(row["base"])
        if base.exists():
            remove_owned(base)
        marker = lock_root(roots[row["scope"]]) / "restore-pending.json"
        marker.unlink(missing_ok=True)
        sync_directory(marker.parent)
    journal.unlink()
    sync_directory(journal.parent)
    return value["phase"]


def recover(roots, *, confirmed=False):
    if confirmed is not True:
        raise DispatcherError("RESTORE_RECOVERY")
    roots = backup.roots_checked(roots)
    with offline(roots.values(), allow_recovery=True):
        return [rollback(p, roots) for p in discover(roots)]


def restore(source, password, roots, *, expected_sha256, confirmed=False):
    if confirmed is not True:
        raise DispatcherError("BACKUP_SCOPE")
    if not isinstance(expected_sha256, str) or not re.fullmatch("[a-f0-9]{64}", expected_sha256):
        raise DispatcherError("BACKUP_INVALID")
    roots = backup.roots_checked(roots)
    source = plain_path(source)
    with offline(roots.values()):
        if discover(roots):
            raise DispatcherError("RESTORE_RECOVERY")
        with backup.opened(source, password, roots, source.parent, expected_sha256=expected_sha256) as (
            archive,
            manifest,
        ):
            tx = uuid.uuid4().hex
            entries = []
            journal = None
            summary = {}
            try:
                for scope in manifest["scopes"]:
                    name = scope["name"]
                    target = roots[name]
                    # A user may restore below a shared parent. Do not change
                    # that parent's permissions; only our staging tree is private.
                    plain_path(target.parent).mkdir(parents=True, exist_ok=True, mode=0o700)
                    backup.space(
                        target.parent, sum(f["size"] for f in manifest["files"] if f["scope"] == name)
                    )
                    base = target.parent / (".poster-stage-" + tx + "-" + name)
                    previous = target.with_name("." + target.name + ".poster-before-" + tx)
                    if base.exists() or previous.exists():
                        raise DispatcherError("BACKUP_IO")
                    private_dir(base)
                    entries.append(
                        dict(
                            scope=name,
                            target=str(target),
                            base=str(base),
                            previous=str(previous),
                            existed=target.exists(),
                            old_id=file_identity(target),
                        )
                    )
                    if scope["kind"] == "directory":
                        private_dir(base / "data")
                    for item in manifest["directories"]:
                        if item["scope"] == name:
                            private_dir(base / "data" / item["path"])
                    for item in manifest["files"]:
                        if item["scope"] != name:
                            continue
                        destination = (
                            base / "data" / item["path"] if scope["kind"] == "directory" else base / "data"
                        )
                        private_dir(destination.parent)
                        sha = hashlib.sha256()
                        count = 0
                        with archive.open(item["member"]) as src, destination.open("xb") as dst:
                            restrict(destination)
                            while raw := src.read(crypto.CHUNK):
                                count += len(raw)
                                dst.write(raw)
                                sha.update(raw)
                            dst.flush()
                            os.fsync(dst.fileno())
                        if count != item["size"] or sha.hexdigest() != item["sha256"]:
                            raise DispatcherError("BACKUP_INVALID")
                    sync_directory(base)
                    entries[-1]["new_id"] = file_identity(base / "data")
                # Logical state roots can be nested inside one physical tree.
                # Quarantine the configured queues even when a parent owns the
                # archive bytes; they must never be treated as ordinary resources.
                for logical_name, logical_path in roots.items():
                    row = next((r for r in entries if logical_path.is_relative_to(Path(r["target"]))), None)
                    if row is None:
                        continue
                    staged = Path(row["base"]) / "data"
                    if logical_path != Path(row["target"]):
                        staged /= logical_path.relative_to(Path(row["target"]))
                    if logical_name == "dispatcher" or logical_name.startswith("dispatcher_"):
                        if (staged / "dispatcher.sqlite").is_file():
                            summary.setdefault("native_original_statuses", {})[logical_name] = (
                                sanitize_native(staged / "dispatcher.sqlite")
                            )
                    if logical_name == "maker" or logical_name.startswith("maker_"):
                        if (staged / "state.sqlite").is_file():
                            sanitize_legacy(staged / "state.sqlite")
                journal = lock_root(roots[entries[0]["scope"]]) / "restore-transaction.json"
                value = dict(
                    format="poster.restore.v1",
                    id=tx,
                    binding=backup.binding(),
                    phase="PREPARED",
                    entries=entries,
                )
                durable(journal, value)
                for row in entries:
                    durable(
                        lock_root(roots[row["scope"]]) / "restore-pending.json",
                        dict(id=tx, journal=str(journal)),
                    )
                checkpoint("PREPARED")
                value["phase"] = "APPLYING"
                durable(journal, value)
                for index, row in enumerate(entries):
                    target, previous, base = map(Path, [row["target"], row["previous"], row["base"]])
                    if row["existed"]:
                        if previous.exists() or file_identity(target) != row["old_id"]:
                            raise DispatcherError("RESTORE_RECOVERY")
                        restrict(target, directory=target.is_dir())
                        os.replace(target, previous)
                        sync_directory(target.parent)
                        checkpoint("OLD_MOVED_" + str(index))
                    os.replace(base / "data", target)
                    sync_directory(target.parent)
                    checkpoint("NEW_INSTALLED_" + str(index))
                value["phase"] = "COMMITTED"
                durable(journal, value)
                checkpoint("COMMITTED")
                rollback(journal, roots)  # COMMITTED only removes markers/stages.
                return dict(
                    status="PASSED",
                    archive_sha256=manifest["archive_sha256"],
                    scopes=[r["scope"] for r in entries],
                    previous_directories=[r["previous"] for r in entries if r["existed"]],
                    manual_review=True,
                    schedules_disabled=True,
                    **summary,
                )
            except BaseException as exc:
                try:
                    if journal is not None and journal.exists():
                        rollback(journal, roots)
                    else:
                        for row in entries:
                            if Path(row["base"]).exists():
                                remove_owned(Path(row["base"]))
                except Exception as recovery:
                    raise DispatcherError("RESTORE_RECOVERY") from recovery
                if isinstance(exc, DispatcherError):
                    raise
                if not isinstance(exc, Exception):
                    raise
                raise DispatcherError("BACKUP_IO") from exc
