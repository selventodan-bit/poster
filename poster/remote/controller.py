"""Network sidecar to the ONE Dispatcher queue; never claims jobs or runs graphs."""

import json
import re
import threading
import time

from cryptography.fernet import Fernet
from filelock import FileLock

from poster.dispatcher.models import ACTIVE, TERMINAL, MESSAGES, DispatcherError, Status
from poster.dispatcher.security import seal, unseal, private_dir, plain_path, safe_json
from poster.dispatcher.snapshots import Snapshots, digest
from poster.dispatcher.store import Store
from poster.maker.storage import atomic
from .protocol import pack, unpack, raw_json, b64, un64, LEASE, MAX_MESSAGE
from .registry import Registry, output_names
from .relay import Client


class Controller:
    def __init__(self, path, agent_id=None):
        self.path = path
        self.agent_id = agent_id
        self.done = threading.Event()
        self.thread = None
        self.clients = {}
        self.last_error = ""
        self.steps = {}

    def start(self):
        self.thread = threading.Thread(target=self.run, name="poster-remote", daemon=True)
        self.thread.start()

    def close(self):
        self.done.set()
        if self.thread:
            self.thread.join(12)
        if self.thread and self.thread.is_alive():
            raise DispatcherError("REMOTE_TRANSPORT")

    def put(self, key, agent, value):
        self.store.db.execute("INSERT OR REPLACE INTO remote_outbox VALUES(?,?,?)", (key, agent, seal(value)))

    def drop(self, key):
        self.store.db.execute("DELETE FROM remote_outbox WHERE id=?", (key,))

    def offer(self, row):
        run_id = row["run_id"]
        if self.store.db.execute("SELECT 1 FROM remote_outbox WHERE id=?", (run_id + ":offer",)).fetchone():
            return
        execution = self.store.execution(run_id)
        job = self.store.payload(execution["job_id"])
        manifest, files = Snapshots(self.store.root).read(job["version"])
        if sum(map(len, files.values())) > 10_000_000:
            raise DispatcherError("REMOTE_LIMIT")
        payload = unseal(job["inputs_cipher"])
        permission_id = payload.pop("session_permission", None)
        if permission_id:
            from poster.portability.permissions import SessionPermissions

            payload["session_grant"] = SessionPermissions(self.store).prepare(
                permission_id, row["agent_id"], job["project_id"], job["version"], run_id, self.identity
            )
        from poster.auth import session_key

        master = session_key()
        if master in raw_json(payload) or any(master in raw for raw in files.values()):
            raise DispatcherError("REMOTE_MASTER_KEY")
        self.steps[job["version"]] = {n["id"] for n in json.loads(files[manifest["main"]])["nodes"]}
        if payload.get("profile"):
            raise DispatcherError("REMOTE_PROFILE")
        key = Fernet.generate_key()
        cipher = Fernet(key)
        encrypted_bundle = cipher.encrypt(
            json.dumps({"files": {n: b64(v) for n, v in files.items()}}).encode()
        )
        value = dict(
            kind="offer",
            run_id=run_id,
            manifest=manifest,
            bundle=b64(encrypted_bundle),
            inputs=b64(cipher.encrypt(raw_json(payload))),
            outputs=json.loads(job["remote_outputs"]),
            job={k: job[k] for k in ["id", "project_id", "version", "timeout"]},
            execution={k: execution[k] for k in ["run_id", "job_id", "attempt", "owner"]},
        )
        with self.store.transaction():
            self.store.db.execute(
                "UPDATE remote_runs SET key_cipher=? WHERE run_id=?", (seal({"key": key.decode()}), run_id)
            )
            self.put(run_id + ":offer", row["agent_id"], value)

    def handle(self, agent, packet):
        peer = json.loads(agent["peer"])
        value = unpack(self.identity, peer, packet)
        if self.store.db.execute("SELECT 1 FROM remote_seen WHERE id=?", (packet["id"],)).fetchone():
            return
        kind = value.get("kind")
        run_id = value.get("run_id")
        if (
            self.store.db.execute("SELECT state FROM remote_agents WHERE id=?", (agent["id"],)).fetchone()[0]
            != "TRUSTED"
        ):
            return
        if kind == "heartbeat":
            from .diagnostics import validate_capabilities

            capabilities = validate_capabilities(value["capabilities"]) if "capabilities" in value else {}
            if (
                not re.fullmatch(r"[A-Za-z0-9_.-]{1,40}", str(value.get("version", "")))
                or value.get("os") != "linux"
                or value.get("protocol") != 1
                or not isinstance(value.get("runs"), list)
                or len(value["runs"]) > 32
            ):
                raise DispatcherError("REMOTE_PROTOCOL")
            with self.store.transaction():
                self.store.db.execute(
                    "UPDATE remote_agents SET last_seen=max(last_seen,?),last_error='',os='linux',version=?,protocol=1 WHERE id=? AND state='TRUSTED'",
                    (min(time.time(), packet["issued"]), str(value["version"])[:40], agent["id"]),
                )
                self.store.db.execute(
                    "UPDATE remote_agents SET capabilities=? WHERE id=?",
                    (json.dumps(capabilities), agent["id"]),
                )
                for entry in value["runs"]:
                    rr = self.store.is_remote(entry["run_id"])
                    if rr and rr["agent_id"] == agent["id"] and rr["state"] == "GRANTED":
                        execution = self.store.execution(entry["run_id"])
                        if execution["status"] == "RUNNING":
                            pid = entry.get("pid")
                            birth = entry.get("birth")
                            if type(pid) is not int or not 0 < pid < 2**31 or type(birth) not in (float, int):
                                raise DispatcherError("REMOTE_PROTOCOL")
                            self.store.db.execute(
                                "UPDATE executions SET pid=?,birth=? WHERE run_id=?",
                                (pid, birth, entry["run_id"]),
                            )
                            self.store.db.execute(
                                "UPDATE remote_runs SET lease_until=? WHERE run_id=?",
                                (packet["issued"] + LEASE, entry["run_id"]),
                            )
                            self.drop(entry["run_id"] + ":grant")
                            confirmed = entry.get("confirmed")
                            job = self.store.payload(execution["job_id"], private=False)
                            if confirmed in self.steps.get(job["version"], set()):
                                self.store.db.execute(
                                    "UPDATE executions SET last_confirmed_step=? WHERE run_id=?",
                                    (confirmed, entry["run_id"]),
                                )
        else:
            row = self.store.is_remote(run_id)
            if row is None or row["agent_id"] != agent["id"]:
                raise DispatcherError("REMOTE_IDENTITY")
            execution = self.store.execution(run_id)
            job = self.store.payload(execution["job_id"])
            if kind == "accepted" and row["state"] == "OFFERED" and execution["status"] == "LEASED":
                with self.store.transaction():
                    current = self.store.db.execute(
                        "SELECT state FROM remote_agents WHERE id=?", (agent["id"],)
                    ).fetchone()[0]
                    fresh_job = self.store.payload(execution["job_id"], private=False)
                    fresh_run = self.store.is_remote(run_id)
                    if (
                        current != "TRUSTED"
                        or fresh_job["cancel_requested"]
                        or fresh_job["status"] != "LEASED"
                        or fresh_run["state"] != "OFFERED"
                        or fresh_run["lease_until"] <= time.time()
                    ):
                        return
                    # Atomic risk fence and grant outbox, BEFORE key release over any network.
                    now = time.time()
                    self.store.db.execute(
                        "UPDATE executions SET status='RUNNING',started=?,effects_possible=1 WHERE run_id=?",
                        (now, run_id),
                    )
                    self.store.db.execute(
                        "UPDATE jobs SET status='RUNNING',updated=? WHERE id=?", (now, job["id"])
                    )
                    self.store.event(job["id"], Status.RUNNING)
                    self.store.db.execute(
                        "UPDATE remote_runs SET state='GRANTED',lease_until=? WHERE run_id=?",
                        (now + LEASE, run_id),
                    )
                    self.drop(run_id + ":offer")
                    self.put(
                        run_id + ":grant",
                        agent["id"],
                        dict(
                            kind="grant",
                            run_id=run_id,
                            key=unseal(row["key_cipher"])["key"],
                            deadline=now + job["timeout"],
                        ),
                    )
            elif kind in {"report", "lost"}:
                if execution["status"] in ACTIVE:
                    if kind == "lost":
                        self.store.complete(
                            run_id,
                            Status.FAILED if row["state"] == "OFFERED" else Status.UNKNOWN,
                            code="NOT_STARTED" if row["state"] == "OFFERED" else "DISPATCHER_LOST",
                        )
                    else:
                        try:
                            self.import_report(row, execution, job, value)
                        except Exception:
                            self.store.complete(run_id, Status.UNKNOWN, code="REPORT_INVALID")
                if kind == "report" and self.store.execution(run_id)["status"] in {
                    "UNKNOWN",
                    "REVIEW_REQUIRED",
                }:
                    root = private_dir(self.store.root / "runs" / run_id)
                    # Preserve late authenticated evidence privately; never clear UNKNOWN.
                    path = root / "late-result.enc"
                    if not path.exists():
                        atomic(
                            path,
                            seal(
                                {
                                    "message": value,
                                    "key": unseal(row["key_cipher"]) if row["key_cipher"] else None,
                                }
                            ),
                        )
                with self.store.transaction():
                    for suffix in [":offer", ":grant", ":cancel"]:
                        self.drop(run_id + suffix)
                    self.store.db.execute(
                        "UPDATE remote_runs SET state='DONE',key_cipher=NULL WHERE run_id=?", (run_id,)
                    )
                    self.put(run_id + ":receipt", agent["id"], dict(kind="receipt", run_id=run_id))
            elif kind == "accepted" and execution["status"] not in ACTIVE:
                self.put(
                    run_id + ":cancel",
                    agent["id"],
                    dict(kind="cancel", run_id=run_id, reason="DISPATCHER_LOST"),
                )
            elif kind == "received":
                self.drop(run_id + ":receipt")
            elif kind != "accepted":
                raise DispatcherError("REMOTE_PROTOCOL")
        self.store.db.execute(
            "INSERT OR IGNORE INTO remote_seen VALUES(?,?)", (packet["id"], packet["expires"])
        )

    def import_report(self, row, execution, job, value):
        from poster.dispatcher.worker import safe_code

        raw = un64(value["report"])
        if len(raw) > 100_000 or digest(raw) != value["sha256"]:
            raise DispatcherError("REPORT_INVALID")
        result = json.loads(raw)
        if (
            result["format"] != "poster.dispatcher.report"
            or result["version"] != 1
            or result["job_id"] != job["id"]
            or result["run_id"] != row["run_id"]
            or result["project_version"] != job["version"]
            or result["project_id"] != job["project_id"]
            or result["attempt"] != execution["attempt"]
            or result["status"] not in TERMINAL
            or result["gui_imports_forbidden"] is not True
        ):
            raise DispatcherError("REPORT_INVALID")
        worker_id = result.get("worker_id")
        birth = value.get("birth")
        if (
            type(worker_id) is not int
            or not 0 < worker_id < 2**31
            or type(birth) not in (float, int)
            or not 0 < birth <= time.time() + 3
        ):
            raise DispatcherError("REPORT_INVALID")
        self.store.db.execute(
            "UPDATE executions SET pid=?,birth=? WHERE run_id=?", (worker_id, birth, row["run_id"])
        )
        confirmed = result.get("last_confirmed_step")
        if confirmed in self.steps.get(job["version"], set()):
            self.store.progress(row["run_id"], confirmed=confirmed)
        root = private_dir(self.store.root / "runs" / row["run_id"])
        work = private_dir(root / "work")
        allowed = output_names(json.loads(job["remote_outputs"]))
        if not set(value["files"]).issubset(allowed):
            raise DispatcherError("PATH_FORBIDDEN")
        contents = {}
        size = 0
        for name, entry in value["files"].items():
            content = un64(entry["content"])
            size += len(content)
            if size > 4_000_000 or digest(content) != entry["sha256"]:
                raise DispatcherError("REPORT_INVALID")
            contents[name] = content
        encrypted = None
        if value.get("results"):
            cipher = un64(value["results"])
            if len(cipher) > 4_000_000:
                raise DispatcherError("REMOTE_LIMIT")
            encrypted = json.loads(Fernet(unseal(row["key_cipher"])["key"].encode()).decrypt(cipher))
        for name, content in contents.items():
            target = plain_path(work / name)
            private_dir(target.parent)
            atomic(target, content)
        if encrypted is not None:
            atomic(root / "results.enc", seal(encrypted))
        code = safe_code(result["code"]) if result["code"] else ""
        report = dict(
            format="poster.dispatcher.report",
            version=1,
            job_id=job["id"],
            run_id=row["run_id"],
            project_id=job["project_id"],
            project_version=job["version"],
            attempt=execution["attempt"],
            status=result["status"],
            code=code,
            message=MESSAGES.get(code, ""),
            remote_agent=row["agent_id"],
            worker_id=int(result["worker_id"]),
            worker_frozen=False,
            worker_executable="remote-linux",
            workdir=str(work),
            encrypted_results=str(root / "results.enc"),
            gui_imports_forbidden=True,
            browser_imports_forbidden=bool(result.get("browser_imports_forbidden")),
            external_effects_may_have_occurred=bool(result["external_effects_may_have_occurred"]),
            outputs_may_be_partial=result["status"] != "SUCCEEDED",
            node_error_code=safe_code(result["node_error_code"]) if result.get("node_error_code") else None,
        )
        path = root / "report.json"
        safe_json(path, report)
        self.store.complete(
            row["run_id"],
            result["status"],
            code=code,
            report=path,
            report_sha256=digest(path.read_bytes()),
            exit_code=0 if result["status"] == "SUCCEEDED" else 1,
        )

    def maintain(self, agent):
        rows = self.store.db.execute(
            "SELECT rr.* FROM remote_runs rr JOIN executions e ON e.run_id=rr.run_id WHERE rr.agent_id=? AND e.status IN ('LEASED','RUNNING')",
            (agent["id"],),
        ).fetchall()
        for row in rows:
            execution = self.store.execution(row["run_id"])
            job = self.store.payload(execution["job_id"])
            if agent["state"] != "TRUSTED":
                self.store.complete(
                    row["run_id"],
                    Status.UNKNOWN if row["state"] == "GRANTED" else Status.FAILED,
                    code="REMOTE_REVOKED",
                )
            elif time.time() > row["lease_until"]:
                if row["state"] == "OFFERED":
                    self.store.recover_unstarted(row["run_id"])
                else:
                    self.store.complete(row["run_id"], Status.UNKNOWN, code="DISPATCHER_LOST")
            elif (
                job["cancel_requested"]
                or execution["started"]
                and time.time() - execution["started"] > job["timeout"]
            ):
                reason = "CANCEL_REQUESTED" if job["cancel_requested"] else "TIMEOUT"
                self.put(
                    row["run_id"] + ":cancel",
                    agent["id"],
                    dict(kind="cancel", run_id=row["run_id"], reason=reason),
                )
                if row["state"] == "OFFERED":
                    self.store.complete(row["run_id"], Status.CANCELLED, code=reason)
                elif time.time() - execution["started"] > job["timeout"] + 6:
                    self.store.complete(row["run_id"], Status.UNKNOWN, code=reason)
            elif row["state"] == "OFFERED":
                try:
                    self.offer(row)
                except DispatcherError as exc:
                    self.store.complete(row["run_id"], Status.FAILED, code=exc.code)
        # No old grant may survive a terminal decision or controller recovery.
        self.store.db.execute(
            "DELETE FROM remote_outbox WHERE (id LIKE '%:offer' OR id LIKE '%:grant') AND substr(id,1,32) IN (SELECT run_id FROM executions WHERE status NOT IN ('LEASED','RUNNING'))"
        )

    def run(self):
        if self.agent_id is not None:
            return self.run_channel()
        children = {}
        launched = {}
        try:
            with FileLock(str(self.path) + ".remote.lock", timeout=0):
                store = Store(self.path)
                try:
                    while not self.done.is_set():
                        ids = [
                            r[0]
                            for r in store.db.execute("SELECT id FROM remote_agents WHERE state!='PENDING'")
                        ]
                        for agent_id in ids:
                            if agent_id not in children or (
                                not children[agent_id].thread.is_alive()
                                and time.monotonic() - launched[agent_id] > 2
                            ):
                                child = children[agent_id] = Controller(self.path, agent_id)
                                child.start()
                                launched[agent_id] = time.monotonic()
                        for agent_id in set(children) - set(ids):
                            children.pop(agent_id).close()
                        self.done.wait(0.25)
                finally:
                    for child in children.values():
                        child.done.set()
                    for child in children.values():
                        child.close()
                    store.close()
        except Exception:
            self.last_error = "REMOTE_TRANSPORT"

    def run_channel(self):
        try:
            self.store = Store(self.path)
            try:
                self.identity = Registry(self.store).identity()
                while not self.done.is_set():
                    agents = self.store.db.execute(
                        "SELECT * FROM remote_agents WHERE id=?", (self.agent_id,)
                    ).fetchall()
                    for agent in agents:
                        if (
                            agent["state"] == "REVOKED"
                            and time.time() - (agent["revoked_at"] or 0) > LEASE * 2
                        ):
                            return
                        self.maintain(agent)
                        peer = json.loads(agent["peer"])
                        runs = [
                            r[0]
                            for r in self.store.db.execute(
                                "SELECT rr.run_id FROM remote_runs rr JOIN executions e ON e.run_id=rr.run_id WHERE rr.agent_id=? AND e.status='RUNNING'",
                                (agent["id"],),
                            )
                        ]
                        bodies = [dict(kind="pulse", runs=runs, revoked=agent["state"] == "REVOKED")]
                        bodies += [
                            unseal(r[0])
                            for r in self.store.db.execute(
                                "SELECT body FROM remote_outbox WHERE agent_id=? ORDER BY rowid LIMIT 6",
                                (agent["id"],),
                            )
                        ]
                        outgoing = []
                        size = 0
                        for body in bodies:
                            if body.get("kind") == "offer":
                                from poster.portability.permissions import SessionPermissions

                                if not SessionPermissions(self.store).before_send(body["run_id"]):
                                    self.drop(body["run_id"] + ":offer")
                                    self.store.complete(
                                        body["run_id"], Status.FAILED, code="PROFILE_PERMISSION"
                                    )
                                    continue
                            packet = pack(self.identity, peer, body)
                            length = len(raw_json(packet))
                            if size + length > MAX_MESSAGE:
                                break
                            outgoing.append(packet)
                            size += length
                        client = self.clients.get(agent["id"])
                        if client is None:
                            client = self.clients[agent["id"]] = Client(
                                unseal(agent["relay_cipher"]), "controller"
                            )
                        try:
                            packets = client.exchange(outgoing)
                            acknowledgements = []
                            for packet in packets:
                                try:
                                    self.handle(agent, packet)
                                except Exception:
                                    self.store.db.execute(
                                        "UPDATE remote_agents SET last_error='REMOTE_PROTOCOL' WHERE id=?",
                                        (agent["id"],),
                                    )
                                acknowledgements.append(packet["id"])
                            if acknowledgements:
                                client.exchange(acknowledgements=acknowledgements)
                        except DispatcherError as exc:
                            self.store.db.execute(
                                "UPDATE remote_agents SET last_error=? WHERE id=?",
                                (exc.code, agent["id"]),
                            )
                    self.store.db.execute("DELETE FROM remote_seen WHERE expires<?", (time.time() - 3,))
                    self.done.wait(0.25)
            finally:
                for client in self.clients.values():
                    client.close()
                self.store.close()
        except Exception:
            self.last_error = "REMOTE_TRANSPORT"
            try:
                db = Store(self.path)
                db.db.execute(
                    "UPDATE remote_agents SET last_error='REMOTE_TRANSPORT' WHERE id=?", (self.agent_id,)
                )
                db.close()
            except Exception:
                pass
