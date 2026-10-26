"""Explicit trust enrollment and secret-free monitoring over the Dispatcher store."""

import json
import time
import ssl
import re
from pathlib import PurePosixPath
from urllib.parse import urlsplit

from poster.dispatcher.models import DispatcherError
from poster.dispatcher.security import plain_path, seal, unseal, restrict
from poster.maker.storage import atomic
from .protocol import Identity, validate_peer, raw_json, LEASE


def read_json(path):
    path = plain_path(path)
    if path.stat().st_size > 100_000:
        raise DispatcherError("REMOTE_CONFIG")
    return json.loads(path.read_bytes())


def profile(value):
    try:
        url = urlsplit(value["url"])
        assert url.scheme == "https" and url.hostname and not url.username and not url.password
        assert url.port is None or 1 <= url.port <= 65535
        assert not url.query and not url.fragment and url.path in ("", "/")
        assert isinstance(value["token"], str) and len(value["token"]) >= 32
        assert len(value["channel"]) == 32 and all(c in "0123456789abcdef" for c in value["channel"])
        if value.get("ca"):
            plain_path(value["ca"]).read_bytes()
        ssl.create_default_context(cafile=value.get("ca"), cadata=value.get("ca_pem"))
        return {k: value[k] for k in ("url", "channel", "token", "ca", "ca_pem") if k in value}
    except Exception as exc:
        raise DispatcherError("REMOTE_CONFIG") from exc


def output_names(values):
    if not isinstance(values, (list, tuple)) or len(values) > 32:
        raise DispatcherError("REMOTE_LIMIT")
    for name in values:
        if not isinstance(name, str):
            raise DispatcherError("PATH_FORBIDDEN")
        path = PurePosixPath(name)
        if (
            not isinstance(name, str)
            or not name
            or len(name) > 200
            or path.is_absolute()
            or any(p in ("..", ".") for p in path.parts)
            or "\\" in name
            or ":" in name
            or str(path) != name
        ):
            raise DispatcherError("PATH_FORBIDDEN")
    return list(values)


def validate_target(store, agent_id):
    row = store.db.execute("SELECT state FROM remote_agents WHERE id=?", (agent_id,)).fetchone()
    if not row or row[0] != "TRUSTED":
        raise DispatcherError("REMOTE_REVOKED")


class Registry:
    def __init__(self, store):
        self.store, self.db = store, store.db

    def identity(self):
        path = self.store.path.parent / "remote-identity.enc"
        if not path.exists():
            if self.db.execute("SELECT 1 FROM remote_agents LIMIT 1").fetchone():
                raise DispatcherError("KEY_INVALID")
            atomic(path, seal(Identity().private()))
            restrict(path)
        return Identity(unseal(path.read_bytes()))

    def register(self, registration, relay):
        self.identity()
        peer = validate_peer(registration["peer"])
        if (
            registration["os"] != "linux"
            or type(registration["protocol"]) is not int
            or registration["protocol"] != 1
            or not isinstance(registration["name"], str)
            or not 1 <= len(registration["name"]) <= 80
            or not registration["name"].isprintable()
            or any(c in registration["name"] for c in "<>&")
            or not re.fullmatch(r"[A-Za-z0-9_.-]{1,40}", str(registration["version"]))
        ):
            raise DispatcherError("REMOTE_CONFIG")
        relay = profile(relay)
        with self.store.transaction():
            if (
                self.db.execute("SELECT count(*) FROM remote_agents WHERE state!='REVOKED'").fetchone()[0]
                >= 32
            ):
                raise DispatcherError("REMOTE_LIMIT")
            previous = self.db.execute("SELECT peer FROM remote_agents WHERE id=?", (peer["id"],)).fetchone()
            if previous:
                raise DispatcherError("REMOTE_IDENTITY")  # Never silently replace pins/credentials.
            self.db.execute(
                "INSERT INTO remote_agents(id,name,peer,relay_cipher,state,os,version,protocol) VALUES(?,?,?,?,'PENDING',?,?,?)",
                (
                    peer["id"],
                    registration["name"],
                    json.dumps(peer),
                    seal(relay),
                    registration["os"],
                    str(registration["version"])[:40],
                    1,
                ),
            )
        return peer["id"]

    def approve(self, agent_id):
        with self.store.transaction():
            if not self.db.execute(
                "UPDATE remote_agents SET state='TRUSTED' WHERE id=? AND state='PENDING'", (agent_id,)
            ).rowcount:
                raise DispatcherError("REMOTE_IDENTITY")
        return True

    def revoke(self, agent_id):
        with self.store.transaction():
            self.db.execute(
                "UPDATE remote_agents SET state='REVOKED',revoked_at=?,last_error='REMOTE_REVOKED' WHERE id=?",
                (time.time(), agent_id),
            )
            for execution in self.db.execute(
                "SELECT e.*,rr.state remote_state FROM executions e JOIN remote_runs rr ON rr.run_id=e.run_id WHERE rr.agent_id=? AND e.status IN ('LEASED','RUNNING')",
                (agent_id,),
            ).fetchall():
                status = "UNKNOWN" if execution["remote_state"] == "GRANTED" else "FAILED"
                self.db.execute(
                    "UPDATE executions SET status=?,code='REMOTE_REVOKED',finished=? WHERE run_id=?",
                    (status, time.time(), execution["run_id"]),
                )
                self.db.execute(
                    "UPDATE jobs SET status=?,updated=? WHERE id=?",
                    (status, time.time(), execution["job_id"]),
                )
                self.store.event(execution["job_id"], status, "REMOTE_REVOKED")
        return True

    def rows(self):
        active = {
            r[0]: r[1]
            for r in self.db.execute(
                "SELECT rr.agent_id,count(*) FROM remote_runs rr JOIN executions e ON e.run_id=rr.run_id WHERE e.status IN ('LEASED','RUNNING') GROUP BY rr.agent_id"
            )
        }
        projects = {}
        for r in self.db.execute(
            "SELECT agent_id,project_id FROM jobs WHERE agent_id IS NOT NULL UNION SELECT agent_id,project_id FROM schedules WHERE agent_id IS NOT NULL AND deleted=0"
        ):
            projects.setdefault(r[0], set()).add(r[1])
        running = self.db.execute("SELECT owner FROM control WHERE id=1").fetchone()[0] is not None
        return [
            {
                **dict(r),
                "online": running and r["state"] == "TRUSTED" and time.time() - r["last_seen"] < LEASE,
                "active": active.get(r["id"], 0),
                "projects": sorted(projects.get(r["id"], set())),
            }
            for r in self.db.execute(
                "SELECT id,name,state,last_seen,os,version,protocol,last_error FROM remote_agents ORDER BY name LIMIT 1000"
            )
        ]

    def export_identity(self, path):
        atomic(plain_path(path), raw_json(self.identity().public()))
        return True
