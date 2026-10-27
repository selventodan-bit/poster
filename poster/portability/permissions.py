"""Expiring session-consent records inside native dispatcher.sqlite, NOT a queue."""

import json
import time
import uuid

from poster.dispatcher.models import DispatcherError, Status
from poster.dispatcher.security import seal, unseal, plain_path
from poster.maker.profiles import Profiles
from poster.maker.storage import read_bounded
from poster.remote.registry import validate_target
from .sessions import select_origins
from .transfer import seal_session


class SessionPermissions:
    def __init__(self, store):
        self.store, self.db = store, store.db

    def rows(self):
        return [
            dict(row)
            for row in self.db.execute(
                "SELECT id,agent_id,project_id,version,expires,remaining,revoked FROM session_permits ORDER BY rowid DESC LIMIT 1000"
            )
        ]

    def create(
        self,
        agent_id,
        project_id,
        version,
        profile_root,
        profile_id,
        origins,
        *,
        key_file=None,
        ttl=120,
        max_runs=1,
        confirmed=False,
    ):
        if (
            confirmed is not True
            or type(ttl) is not int
            or not 5 <= ttl <= 900
            or type(max_runs) is not int
            or not 1 <= max_runs <= 1000
        ):
            raise DispatcherError("PROFILE_PERMISSION")
        validate_target(self.store, agent_id)
        agent = self.db.execute("SELECT version FROM remote_agents WHERE id=?", (agent_id,)).fetchone()
        if agent["version"] != "5D.1":
            raise DispatcherError("PROFILE_INCOMPATIBLE")
        from poster.cloud.format import identifier, version_id

        identifier(project_id)
        version_id(version)
        profiles = Profiles(plain_path(profile_root))
        try:
            if key_file:
                from cryptography.fernet import Fernet

                value = json.loads(
                    Fernet(plain_path(key_file).read_bytes().strip()).decrypt(
                        read_bounded(profiles.path(profile_id))
                    )
                )
            else:
                value = profiles.open(profile_id).data
            selected = select_origins(value["state"], origins)
        except Exception as exc:
            raise DispatcherError("PROFILE_PERMISSION") from exc
        permit = uuid.uuid4().hex
        self.db.execute(
            "INSERT INTO session_permits VALUES(?,?,?,?,?,?,0,?)",
            (
                permit,
                agent_id,
                project_id,
                version,
                time.time() + ttl,
                max_runs,
                seal(dict(name=value["name"], state=selected, origins=origins)),
            ),
        )
        return next(r for r in self.rows() if r["id"] == permit)

    def validate(self, permit_id, agent_id, project_id, version):
        row = self.db.execute("SELECT * FROM session_permits WHERE id=?", (permit_id,)).fetchone()
        if (
            row is None
            or row["agent_id"] != agent_id
            or row["project_id"] != project_id
            or row["version"] != version
            or row["revoked"]
            or row["expires"] <= time.time()
            or row["remaining"] <= 0
        ):
            raise DispatcherError("PROFILE_PERMISSION")
        validate_target(self.store, agent_id)
        return row

    def prepare(self, permit_id, agent_id, project_id, version, run_id, identity):
        with self.store.transaction():
            old = self.db.execute(
                "SELECT packet_cipher FROM session_transfers WHERE run_id=? AND permit_id=?",
                (run_id, permit_id),
            ).fetchone()
            if old:
                row = self.db.execute(
                    "SELECT revoked,expires FROM session_permits WHERE id=?", (permit_id,)
                ).fetchone()
                if row["revoked"] or row["expires"] <= time.time():
                    raise DispatcherError("PROFILE_PERMISSION")
                packet = unseal(old[0])
                if packet["dst"] != agent_id or packet["binding"] != dict(
                    run_id=run_id, project_id=project_id, version=version
                ):
                    raise DispatcherError("PROFILE_PERMISSION")
                return packet
            row = self.validate(permit_id, agent_id, project_id, version)
            peer = json.loads(
                self.db.execute("SELECT peer FROM remote_agents WHERE id=?", (agent_id,)).fetchone()[0]
            )
            value = unseal(row["data_cipher"])
            ttl = int(min(900, row["expires"] - time.time()))
            if ttl < 5:
                raise DispatcherError("PROFILE_PERMISSION")
            # State origin selection occurred only after explicit operator consent.
            state = value["state"]
            origins = value["origins"]
            packet = seal_session(
                identity,
                peer,
                state,
                origins=origins,
                trusted=True,
                ttl=ttl,
                binding=dict(run_id=run_id, project_id=project_id, version=version),
            )
            self.db.execute(
                "INSERT INTO session_transfers VALUES(?,?,0,?)", (run_id, permit_id, seal(packet))
            )
            self.db.execute("UPDATE session_permits SET remaining=remaining-1 WHERE id=?", (permit_id,))
            return packet

    def before_send(self, run_id):
        with self.store.transaction():
            row = self.db.execute(
                "SELECT p.revoked,p.expires FROM session_transfers t JOIN session_permits p ON p.id=t.permit_id WHERE t.run_id=?",
                (run_id,),
            ).fetchone()
            if row is None:
                return True
            if row["revoked"] or row["expires"] <= time.time():
                return False
            # Durable possible-disclosure marker immediately before network I/O.
            # A failed socket cannot prove that the recipient saw no bytes.
            self.db.execute("UPDATE session_transfers SET sent=1 WHERE run_id=?", (run_id,))
            return True

    def revoke(self, permit_id):
        with self.store.transaction():
            rows = self.db.execute(
                "SELECT t.run_id,t.sent,e.job_id,e.status FROM session_transfers t JOIN executions e ON e.run_id=t.run_id WHERE t.permit_id=?",
                (permit_id,),
            ).fetchall()
            if not self.db.execute(
                "UPDATE session_permits SET revoked=1,data_cipher=? WHERE id=?", (seal({}), permit_id)
            ).rowcount:
                raise DispatcherError("PROFILE_PERMISSION")
            for row in rows:
                if row["status"] in {"LEASED", "RUNNING"}:
                    if row["sent"]:
                        self.store._cancel(row["job_id"])
                    else:
                        now = time.time()
                        self.db.execute(
                            "UPDATE executions SET status='CANCELLED',code='PROFILE_PERMISSION',finished=? WHERE run_id=?",
                            (now, row["run_id"]),
                        )
                        self.db.execute(
                            "UPDATE jobs SET status='CANCELLED',cancel_requested=1,updated=? WHERE id=?",
                            (now, row["job_id"]),
                        )
                        self.store.event(row["job_id"], Status.CANCELLED, "PROFILE_PERMISSION")
                self.db.execute(
                    "DELETE FROM remote_outbox WHERE id IN (?,?)",
                    (row["run_id"] + ":offer", row["run_id"] + ":grant"),
                )
        return dict(
            revoked=True,
            revoked_before_transfer=not any(r["sent"] for r in rows),
            may_have_been_disclosed=any(r["sent"] for r in rows),
        )
