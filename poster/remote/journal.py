"""Agent replay tombstones and encrypted outbox, not a dispatch queue or schedule database."""

from contextlib import contextmanager
import json
import sqlite3
import time

from cryptography.fernet import Fernet
from poster.dispatcher.models import DispatcherError
from poster.dispatcher.security import plain_path, private_dir, restrict
from .protocol import raw_json

APPLICATION_ID = 0x50524147  # PRAG: never open a Dispatcher/relay/foreign database.


class Journal:
    def __init__(self, root, key, *, create=False):
        self.root = private_dir(root)
        path = plain_path(self.root / "agent-journal.sqlite")
        self.cipher = Fernet(key)
        if create:
            path.open("xb").close()
        elif not path.is_file():
            raise DispatcherError("KEY_INVALID")
        self.db = sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        try:
            if create:
                self.db.execute("PRAGMA journal_mode=WAL")
                self.db.execute("PRAGMA synchronous=FULL")
                self.db.executescript("""CREATE TABLE runs(id TEXT PRIMARY KEY,state TEXT NOT NULL,data BLOB,pid INTEGER,birth REAL);
                CREATE TABLE outbox(id TEXT PRIMARY KEY,data BLOB NOT NULL);
                CREATE TABLE seen(id TEXT PRIMARY KEY,expires REAL NOT NULL);
                CREATE TABLE meta(id TEXT PRIMARY KEY,value TEXT);""")
                with self.transaction():
                    self.db.execute(f"PRAGMA application_id={APPLICATION_ID}")
                    self.db.execute("PRAGMA user_version=1")
                    self.db.execute(
                        "INSERT INTO meta VALUES('format',?)", (self.encode({"format": "poster.agent.v1"}),)
                    )
            elif (
                self.db.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID
                or self.db.execute("PRAGMA user_version").fetchone()[0] != 1
                or self.db.execute("PRAGMA quick_check").fetchone()[0] != "ok"
                or self.decode(self.db.execute("SELECT value FROM meta WHERE id='format'").fetchone()[0])
                != {"format": "poster.agent.v1"}
            ):
                raise ValueError()
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
            restrict(path)
        except Exception as exc:
            self.db.close()
            raise DispatcherError("KEY_INVALID") from exc

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

    def encode(self, value):
        return self.cipher.encrypt(raw_json(value))

    def decode(self, value):
        return json.loads(self.cipher.decrypt(value))

    def put(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO outbox VALUES(?,?)", (key, self.encode(value)))

    def drop(self, key):
        self.db.execute("DELETE FROM outbox WHERE id=?", (key,))

    def messages(self):
        return [self.decode(r[0]) for r in self.db.execute("SELECT data FROM outbox ORDER BY rowid LIMIT 6")]

    def seen(self, packet):
        self.db.execute("DELETE FROM seen WHERE expires<?", (time.time() - 3,))
        if self.db.execute("SELECT 1 FROM seen WHERE id=?", (packet["id"],)).fetchone():
            return True
        return False

    def remember(self, packet):
        self.db.execute("INSERT OR IGNORE INTO seen VALUES(?,?)", (packet["id"], packet["expires"]))

    def close(self):
        self.db.close()
