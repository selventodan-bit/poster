"""4A databases migrate transactionally without touching Engine databases."""

import sqlite3

from poster.dispatcher.store import APPLICATION_ID, SCHEMA, SCHEMA_VERSION, Store


def test_v1_upgrade_preserves_queue_and_control(tmp_path):
    path = tmp_path / "dispatcher.sqlite"
    with sqlite3.connect(path) as db:
        db.executescript(SCHEMA)
        db.execute(f"PRAGMA application_id={APPLICATION_ID}")
        db.execute("PRAGMA user_version=1")
        db.execute("INSERT INTO projects VALUES('project','version','original',12)")
        db.execute(
            "INSERT INTO jobs(id,project_id,version,inputs_cipher,input_names,status,timeout,created,updated) VALUES('job','project','version',?,'[]','PENDING',300,12,12)",
            (b"encrypted",),
        )
    for _ in range(2):
        store = Store(path)
        try:
            assert store.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
            assert store.payload("job")["inputs_cipher"] == b"encrypted"
            assert store.payload("job")["status"] == "PENDING"
            assert not store.db.execute("SELECT * FROM schedules").fetchall()
            assert store.db.execute("PRAGMA foreign_key_check").fetchall() == []
        finally:
            store.close()
