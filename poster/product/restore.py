"""Staged, journalled same-user recovery. Restored jobs never run automatically."""

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time
import uuid

from poster.dispatcher.models import DispatcherError
from poster.dispatcher.security import plain_path, private_dir, restrict
from . import backup, crypto, scratch
from .locking import lock_root, lock_path, legacy_lock_path, legacy_folders, offline
from .atomic import durable, sync_directory, remove_owned, file_identity


def checkpoint(phase):
    """A test may observe/kill the real process at a durable boundary."""


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
                "UPDATE executions SET status='UNKNOWN',code='REVIEW_REQUIRED',pid=NULL,birth=NULL,finished=? WHERE status IN ('LEASED','RUNNING','UNKNOWN')",
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
    with closing(sqlite3.connect(path)) as db:
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
        path = plain_path(path)
        if path.stat().st_size > 100000:
            raise ValueError()
        value = json.loads(path.read_bytes())
        tx = value["id"]
        if (
            value["format"] not in {"poster.restore.v1", "poster.restore.v2"}
            or not re.fullmatch("[a-f0-9]{32}", tx)
            or value["binding"] != backup.binding()
            or value["phase"] not in {"STAGING", "PREPARED", "APPLYING", "COMMITTED", "ROLLED_BACK"}
        ):
            raise ValueError()
        v2 = value["format"] == "poster.restore.v2"
        prepared = value.get("prepared", True)
        if type(prepared) is not bool or (not prepared and value["phase"] not in {"STAGING", "ROLLED_BACK"}):
            raise ValueError()
        if v2 and value.get("scratch") is not None:
            scratch.record_path(value["scratch"])
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
            for key in ["old_id", "new_id", "base_id"] if v2 else ["old_id", "new_id"]:
                identity = row[key]
                if identity is None and (
                    (key == "old_id" and not row["existed"]) or (key != "old_id" and not prepared)
                ):
                    continue
                if (
                    not isinstance(identity, list)
                    or len(identity) != 2
                    or any(type(n) is not int or n < 0 for n in identity)
                ):
                    raise ValueError()
            for item in [base, previous, target]:
                plain_path(item)
        target = roots[entries[0]["scope"]]
        if path not in {
            lock_path(target) / "restore-transaction.json",
            legacy_lock_path(target) / "restore-transaction.json",
        }:
            raise ValueError()
        return value
    except Exception as exc:
        raise DispatcherError("RESTORE_RECOVERY") from exc


def discover(roots):
    roots = backup.roots_checked(roots)
    journals = set()
    folders = {lock_path(p) for root in roots.values() for p in [root, *root.parents]}
    folders |= legacy_folders(roots.values())
    allowed = {folder for p in roots.values() for folder in [lock_path(p), legacy_lock_path(p)]}
    for folder in folders:
        marker = folder / "restore-pending.json"
        if marker.exists():
            try:
                if marker.stat().st_size > 20000:
                    raise ValueError()
                value = json.loads(plain_path(marker).read_bytes())
                journal = plain_path(value["journal"])
                if journal.parent not in allowed:
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


def quarantine(path, destination):
    """Preserve interrupted installed data; never recursively erase a live target."""
    if destination.exists():
        raise DispatcherError("RESTORE_RECOVERY")
    os.replace(path, destination)
    sync_directory(path.parent)


def rollback(journal, roots):
    value = journal_checked(journal, roots)
    if value["phase"] not in {"COMMITTED", "ROLLED_BACK"}:
        if value.get("prepared", True):
            for row in reversed(value["entries"]):
                target, previous = map(Path, [row["target"], row["previous"]])
                failed = target.with_name("." + target.name + ".poster-interrupted-" + value["id"])
                if previous.exists():
                    if file_identity(previous) != row["old_id"]:
                        raise DispatcherError("RESTORE_RECOVERY")
                    if target.exists():
                        if file_identity(target) != row["new_id"]:
                            raise DispatcherError("RESTORE_RECOVERY")
                        quarantine(target, failed)
                    os.replace(previous, target)
                    sync_directory(target.parent)
                elif target.exists():
                    identity = file_identity(target)
                    if not row["existed"] and identity == row["new_id"]:
                        quarantine(target, failed)
                    elif identity != row["old_id"]:
                        raise DispatcherError("RESTORE_RECOVERY")
                elif row["existed"]:
                    raise DispatcherError("RESTORE_RECOVERY")
        value["phase"] = "ROLLED_BACK"
        durable(journal, value)
    if value["phase"] == "COMMITTED":
        if any(file_identity(Path(row["target"])) != row["new_id"] for row in value["entries"]):
            raise DispatcherError("RESTORE_RECOVERY")
    for row in value["entries"]:
        base = Path(row["base"])
        if base.exists():
            known = row.get("base_id") == file_identity(base)
            if value["format"] == "poster.restore.v1":
                known = file_identity(base / "data") == row["new_id"] or not any(base.iterdir())
            if known:
                remove_owned(base)
                sync_directory(base.parent)
            else:
                # Death between mkdir and recording its inode gives no licence
                # to remove unknown contents. Retain the folder for inspection.
                quarantine(base, base.with_name(base.name.replace(".poster-stage-", ".poster-orphan-", 1)))
        for folder in {lock_path(roots[row["scope"]]), legacy_lock_path(roots[row["scope"]])}:
            marker = folder / "restore-pending.json"
            if marker.exists():
                data = json.loads(plain_path(marker).read_bytes())
                if data != dict(id=value["id"], journal=str(journal)):
                    raise DispatcherError("RESTORE_RECOVERY")
                marker.unlink()
                sync_directory(marker.parent)
    if value.get("scratch"):
        scratch.cleanup(value["scratch"])
    journal.unlink()
    sync_directory(journal.parent)
    return value["phase"]


def recover(roots, *, confirmed=False):
    if confirmed is not True:
        raise DispatcherError("RESTORE_RECOVERY")
    roots = backup.roots_checked(roots)
    with offline(roots.values(), allow_recovery=True):
        result = [rollback(p, roots) for p in discover(roots)]
        scratch.cleanup_abandoned()
        return result


def restore(source, password, roots, *, expected_sha256, confirmed=False):
    if confirmed is not True:
        raise DispatcherError("BACKUP_SCOPE")
    if not isinstance(expected_sha256, str) or not re.fullmatch("[a-f0-9]{64}", expected_sha256):
        raise DispatcherError("BACKUP_INVALID")
    roots = backup.roots_checked(roots)
    source = backup.archive_path(source)
    if any(source == p or source.is_relative_to(p) for p in roots.values()):
        raise DispatcherError("BACKUP_SCOPE")
    with offline(roots.values()):
        if discover(roots):
            raise DispatcherError("RESTORE_RECOVERY")
        with backup.opened(source, password, roots, expected_sha256=expected_sha256) as (archive, manifest):
            tx = uuid.uuid4().hex
            entries = []
            summary = {}
            checkpoint("BEFORE_STAGING")
            for scope in manifest["scopes"]:
                name = scope["name"]
                target = roots[name]
                base = target.parent / (".poster-stage-" + tx + "-" + name)
                previous = target.with_name("." + target.name + ".poster-before-" + tx)
                if base.exists() or previous.exists():
                    raise DispatcherError("BACKUP_IO")
                entries.append(
                    dict(
                        scope=name,
                        target=str(target),
                        base=str(base),
                        previous=str(previous),
                        existed=target.exists(),
                        old_id=file_identity(target),
                        new_id=None,
                        base_id=None,
                    )
                )
            journal = lock_root(roots[entries[0]["scope"]]) / "restore-transaction.json"
            value = dict(
                format="poster.restore.v2",
                id=tx,
                binding=backup.binding(),
                phase="STAGING",
                prepared=False,
                entries=entries,
                scratch=getattr(archive, "_poster_scratch_id", None),
            )
            try:
                durable(journal, value)
                for row in entries:
                    durable(
                        lock_root(roots[row["scope"]]) / "restore-pending.json",
                        dict(id=tx, journal=str(journal)),
                    )
                checkpoint("STAGING")
                file_index = 0
                for scope, row in zip(manifest["scopes"], entries, strict=True):
                    name = scope["name"]
                    target, base = Path(row["target"]), Path(row["base"])
                    # Staging and renames stay on this target's filesystem.
                    plain_path(target.parent).mkdir(parents=True, exist_ok=True, mode=0o700)
                    backup.space(
                        target.parent, sum(f["size"] for f in manifest["files"] if f["scope"] == name)
                    )
                    base.mkdir(mode=0o700)
                    restrict(base, directory=True)
                    row["base_id"] = file_identity(base)
                    durable(journal, value)
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
                                checkpoint("STAGE_FILE_" + str(file_index))
                            dst.flush()
                            os.fsync(dst.fileno())
                        if count != item["size"] or sha.hexdigest() != item["sha256"]:
                            raise DispatcherError("BACKUP_INVALID")
                        file_index += 1
                    sync_directory(base)
                    row["new_id"] = file_identity(base / "data")
                    durable(journal, value)
                for logical_name, logical_path in roots.items():
                    row = next((r for r in entries if logical_path.is_relative_to(Path(r["target"]))), None)
                    if row is None:
                        continue
                    staged = Path(row["base"]) / "data"
                    if logical_path != Path(row["target"]):
                        staged /= logical_path.relative_to(Path(row["target"]))
                    if logical_name == "location" and staged.is_file():
                        if staged.stat().st_size > 20000:
                            raise DispatcherError("BACKUP_INVALID")
                        location = json.loads(staged.read_bytes())
                        if location.get("format") != "poster.maker.location.v1" or plain_path(
                            location.get("path", "")
                        ) != roots.get("maker"):
                            raise DispatcherError("BACKUP_SCOPE")
                    if logical_name == "dispatcher" or logical_name.startswith("dispatcher_"):
                        if (staged / "dispatcher.sqlite").is_file():
                            summary.setdefault("native_original_statuses", {})[logical_name] = (
                                sanitize_native(staged / "dispatcher.sqlite")
                            )
                    if logical_name == "maker" or logical_name.startswith("maker_"):
                        if (staged / "state.sqlite").is_file():
                            sanitize_legacy(staged / "state.sqlite")
                value.update(prepared=True, phase="PREPARED")
                durable(journal, value)
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
                rollback(journal, roots)
                return dict(
                    status="PASSED",
                    archive_sha256=manifest["archive_sha256"],
                    scopes=[r["scope"] for r in entries],
                    previous_directories=[r["previous"] for r in entries if r["existed"]],
                    manual_review=True,
                    schedules_disabled=True,
                    remote_revocation="UNCONFIRMED",
                    **summary,
                )
            except BaseException as exc:
                try:
                    if journal.exists():
                        rollback(journal, roots)
                except Exception as recovery:
                    raise DispatcherError("RESTORE_RECOVERY") from recovery
                if isinstance(exc, DispatcherError) or not isinstance(exc, Exception):
                    raise
                raise DispatcherError("BACKUP_IO") from exc
