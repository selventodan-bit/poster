"""Pre-upgrade snapshots for the accepted native queue schema generations."""

import hashlib
import sqlite3

import pytest

from poster.dispatcher.models import DispatcherError
from poster.dispatcher import store as module


def previous(path, version):
    with sqlite3.connect(path) as db:
        db.executescript(module.SCHEMA)
        db.execute(f"PRAGMA application_id={module.APPLICATION_ID}")
        db.execute("INSERT INTO projects VALUES('project','version','source',1)")
        db.execute(
            "INSERT INTO jobs(id,project_id,version,inputs_cipher,input_names,status,timeout,created,updated) VALUES('job','project','version',?,'[]','PENDING',300,1,1)",
            (b"PRIVATE_ENCRYPTED_INPUT",),
        )
        for number in range(2, version + 1):
            if number in {2, 3, 6, 7}:
                db.executescript(getattr(module, f"MIGRATION_{number}"))
            elif number == 4:
                db.execute("ALTER TABLE remote_agents ADD COLUMN revoked_at REAL")
            elif number == 5:
                db.execute("ALTER TABLE remote_agents ADD COLUMN capabilities TEXT NOT NULL DEFAULT '{}'")
                db.execute(
                    "CREATE TABLE remote_pairings(id TEXT PRIMARY KEY,expires REAL NOT NULL,controller TEXT NOT NULL,profile_cipher BLOB NOT NULL,consumed TEXT)"
                )
        db.execute(f"PRAGMA user_version={version}")


@pytest.mark.parametrize("version", range(1, 8))
def test_accepted_schema_upgrade_has_verified_original_snapshot(tmp_path, version):
    path = tmp_path / "dispatcher.sqlite"
    previous(path, version)
    store = module.Store(path)
    try:
        assert store.payload("job")["inputs_cipher"] == b"PRIVATE_ENCRYPTED_INPUT"
        assert store.db.execute("PRAGMA foreign_key_check").fetchall() == []
        backups = list((store.root / "migrations").glob("*.sqlite"))
        assert len(backups) == int(version < module.SCHEMA_VERSION)
        if backups:
            with sqlite3.connect(backups[0]) as db:
                assert db.execute("PRAGMA user_version").fetchone()[0] == version
                assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
                assert (
                    db.execute("SELECT inputs_cipher FROM jobs").fetchone()[0] == b"PRIVATE_ENCRYPTED_INPUT"
                )
    finally:
        store.close()


def test_failed_backup_never_starts_migration(tmp_path, monkeypatch):
    path = tmp_path / "dispatcher.sqlite"
    previous(path, 1)
    before = hashlib.sha256(path.read_bytes()).digest()
    restrict = module.restrict

    def full_disk(path, **kwargs):
        if path.name.startswith("schema-"):
            raise OSError("synthetic full disk")
        return restrict(path, **kwargs)

    monkeypatch.setattr(module, "restrict", full_disk)
    with pytest.raises(DispatcherError) as caught:
        module.Store(path)
    assert caught.value.code == "MIGRATION_BACKUP_FAILED"
    assert hashlib.sha256(path.read_bytes()).digest() == before
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
