"""Agent replay tombstones and encrypted outbox, not a dispatch queue or schedule database."""

from contextlib import contextmanager
import json
import sqlite3
import time

from cryptography.fernet import Fernet
from poster.dispatcher.security import private_dir, restrict
from .protocol import raw_json


class Journal:
    def __init__(self, root, key):
        self.root = private_dir(root)
        path = self.root / "agent-journal.sqlite"
        self.cipher = Fernet(key)
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY,state TEXT NOT NULL,data BLOB,pid INTEGER,birth REAL);
        CREATE TABLE IF NOT EXISTS outbox(id TEXT PRIMARY KEY,data BLOB NOT NULL);
        CREATE TABLE IF NOT EXISTS seen(id TEXT PRIMARY KEY,expires REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS meta(id TEXT PRIMARY KEY,value TEXT);""")
        restrict(path)

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
