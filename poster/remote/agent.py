"""Outbound Linux agent; durable accepted/run tombstones, no queue claims or scheduler."""

import hashlib
import json
import os
from pathlib import Path
import platform
import queue
import re
import shutil
import subprocess
import sys
import threading
import time

from filelock import FileLock
import psutil

from poster.dispatcher.models import DispatcherError
from poster.dispatcher.processes import terminate_tree, reap_detached
from poster.dispatcher.security import private_dir, plain_path, safe_json
from poster.maker.storage import atomic
from .journal import Journal
from .protocol import Identity, validate_peer, unpack, pack, raw_json, b64, un64, LEASE, MAX_MESSAGE
from .registry import read_json, output_names
from .relay import Client

OUTPUT_LIMIT = 4_000_000


def close_input(process):
    # BufferedWriter.close flushes a pending lease even after the child exits.
    # A broken pipe is confirmed EOF, never grounds for losing a saved report.
    try:
        process.stdin.close()
    except OSError:
        pass


class Agent:
    def __init__(self, root):
        if sys.platform != "linux":
            raise DispatcherError("REMOTE_CONFIG")
        self.root = private_dir(root)
        self.config = read_json(self.root / "agent.json")
        self.identity = Identity(self.config["identity"])
        self.peer = validate_peer(self.config["controller"])
        if not (self.root / "agent-journal.sqlite").is_file():
            raise DispatcherError("KEY_INVALID")
        self.journal = Journal(root, self.config["storage_key"].encode())
        self.client = Client(self.config["relay"], "agent")
        self.workers = {}
        self.events = queue.SimpleQueue()
        self.last_pulse = 0
        self.permitted = set()
        self.done = False
        self.acks = []
        self.error = ""

    def recover(self):
        for row in self.journal.db.execute("SELECT * FROM runs WHERE state='STARTED'").fetchall():
            if row["pid"]:
                terminate_tree(row["pid"], row["birth"], group=True)
                reap_detached(row["id"], row["birth"])
            self.lost(row["id"])
            shutil.rmtree(self.root / "runs" / row["id"], ignore_errors=True)

    def lost(self, run_id):
        with self.journal.transaction():
            self.journal.db.execute("UPDATE runs SET state='LOST' WHERE id=?", (run_id,))
            self.journal.put(run_id + ":report", dict(kind="lost", run_id=run_id))
            self.journal.drop(run_id + ":accepted")

    def handle(self, packet):
        value = unpack(self.identity, self.peer, packet)
        if self.journal.seen(packet):
            return
        kind = value.get("kind")
        run_id = value.get("run_id", "")
        if run_id and not re.fullmatch("[a-f0-9]{32}", run_id):
            raise DispatcherError("REMOTE_PROTOCOL")
        if kind == "pulse":
            self.last_pulse = max(self.last_pulse, packet["issued"])
            if value.get("revoked"):
                self.journal.db.execute("INSERT OR REPLACE INTO meta VALUES('revoked','1')")
            permitted = self.permitted = set(value["runs"])
            for key, worker in self.workers.items():
                if key not in permitted:
                    self.cancel(worker, "DISPATCHER_LOST")
                elif worker["reason"] is None and not worker["process"].stdin.closed:
                    try:
                        worker["process"].stdin.write(
                            ("LEASE " + str(packet["issued"] + LEASE) + "\n").encode()
                        )
                        worker["process"].stdin.flush()
                    except OSError:
                        close_input(worker["process"])
        elif kind == "offer":
            if self.journal.db.execute("SELECT 1 FROM meta WHERE id='revoked'").fetchone():
                return
            if len(un64(value["bundle"])) > 30_000_000 or len(value["manifest"]["files"]) > 128:
                raise DispatcherError("REMOTE_LIMIT")
            if hashlib.sha256(raw_json(value["manifest"])).hexdigest() != value["job"]["version"]:
                # Snapshot hashing uses canonical UTF-8, not JSON's ASCII escapes.
                from poster.dispatcher.snapshots import canonical

                if hashlib.sha256(canonical(value["manifest"])).hexdigest() != value["job"]["version"]:
                    raise DispatcherError("SNAPSHOT_INVALID")
            output_names(value["outputs"])
            with self.journal.transaction():
                row = self.journal.db.execute("SELECT state FROM runs WHERE id=?", (run_id,)).fetchone()
                if row is None:
                    self.journal.db.execute(
                        "INSERT INTO runs(id,state,data) VALUES(?,'ACCEPTED',?)",
                        (run_id, self.journal.encode(value)),
                    )
                if row is None or row["state"] == "ACCEPTED":
                    self.journal.put(run_id + ":accepted", dict(kind="accepted", run_id=run_id))
        elif kind == "grant":
            row = self.journal.db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if (
                row
                and row["state"] == "ACCEPTED"
                and not self.journal.db.execute("SELECT 1 FROM meta WHERE id='revoked'").fetchone()
            ):
                if time.time() - self.last_pulse >= LEASE or run_id not in self.permitted:
                    return
                offer = self.journal.decode(row["data"])
                if value["deadline"] <= time.time():
                    return
                # Commit non-replay marker BEFORE creating a process or releasing a key.
                with self.journal.transaction():
                    self.journal.db.execute("UPDATE runs SET state='STARTED' WHERE id=?", (run_id,))
                    self.journal.drop(run_id + ":accepted")
                try:
                    self.launch(offer, value)
                except Exception:
                    self.lost(run_id)
        elif kind == "cancel":
            if run_id in self.workers:
                self.cancel(self.workers[run_id], value["reason"])
            else:
                row = self.journal.db.execute("SELECT state FROM runs WHERE id=?", (run_id,)).fetchone()
                if row and row["state"] == "ACCEPTED":
                    self.lost(run_id)
        elif kind == "receipt":
            row = self.journal.db.execute("SELECT state FROM runs WHERE id=?", (run_id,)).fetchone()
            if row and row["state"] in {"DONE", "LOST"}:
                with self.journal.transaction():
                    self.journal.drop(run_id + ":report")
                    self.journal.db.execute("UPDATE runs SET data=NULL WHERE id=?", (run_id,))
                self.journal.put(run_id + ":received", dict(kind="received", run_id=run_id))
                shutil.rmtree(self.root / "runs" / run_id, ignore_errors=True)
        else:
            raise DispatcherError("REMOTE_PROTOCOL")
        self.journal.remember(packet)

    def launch(self, offer, grant):
        run_id = offer["run_id"]
        root = private_dir(self.root / "runs" / run_id)
        # Preserve the exact manifest and graph bytes, re-encrypted per run by Controller.
        snapshot = private_dir(root / "snapshots" / offer["job"]["version"])
        from poster.dispatcher.snapshots import canonical

        atomic(snapshot / "manifest.json", canonical(offer["manifest"]))
        atomic(snapshot / "bundle.enc", un64(offer["bundle"]))
        job = dict(offer["job"], inputs_cipher=offer["inputs"])
        from cryptography.fernet import Fernet

        cipher = Fernet(grant["key"].encode())
        payload = json.loads(cipher.decrypt(un64(offer["inputs"])))
        capsule = payload.pop("session_grant", None)
        if capsule:
            from poster.portability.transfer import open_session

            state = open_session(
                self.identity,
                self.peer,
                capsule,
                binding=dict(
                    run_id=run_id, project_id=offer["job"]["project_id"], version=offer["job"]["version"]
                ),
            )
            payload["profile"] = dict(
                key="profile_" + run_id,
                data=dict(name="Explicit transferred session", revision=0, state=state),
                lock=str(root / "private-profile.lock"),
                mode="isolated",
            )
            job["inputs_cipher"] = b64(cipher.encrypt(raw_json(payload)))
        # JSON cannot carry bytes, adapter converts only the encrypted field at handoff.
        execution = offer["execution"]
        startup = (
            raw_json(dict(key=grant["key"], job=job, execution=execution, lease=self.last_pulse + LEASE))
            + b"\n"
        )
        if len(startup) > 2_000_000:
            raise DispatcherError("REMOTE_LIMIT")
        env = {
            k: v
            for k, v in os.environ.items()
            if k not in {"POSTER_SESSION_KEY", "PYTHONHOME", "POSTER_PYTHON"}
        }
        env["POSTER_DISPATCHER_RUN"] = run_id
        process = subprocess.Popen(
            [sys.executable, "-m", "poster.remote.worker", "--root", str(root)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
            start_new_session=True,
        )
        birth = psutil.Process(process.pid).create_time()
        self.journal.db.execute("UPDATE runs SET pid=?,birth=? WHERE id=?", (process.pid, birth, run_id))
        worker = dict(
            process=process,
            birth=birth,
            created=time.monotonic(),
            deadline=grant["deadline"],
            reason=None,
            cancelled=None,
            offer=offer,
            root=root,
            key=grant["key"],
        )
        self.workers[run_id] = worker

        def read():
            try:
                while True:
                    raw = process.stdout.readline(100_001)
                    if not raw:
                        break
                    if len(raw) > 100_000:
                        raise ValueError()
                    event = json.loads(raw)
                    self.events.put((run_id, event))
            except Exception:
                pass

        worker["reader"] = threading.Thread(target=read, daemon=True)
        worker["reader"].start()
        process.stdin.write(startup)
        process.stdin.flush()

    def cancel(self, worker, reason):
        if worker["reason"] is not None:
            return
        if reason not in {"CANCEL_REQUESTED", "TIMEOUT", "DISPATCHER_LOST"}:
            raise DispatcherError("REMOTE_PROTOCOL")
        worker["reason"], worker["cancelled"] = reason, time.monotonic()
        if reason == "DISPATCHER_LOST":
            close_input(worker["process"])  # Existing worker control EOF/watchdog.
        elif not worker["process"].stdin.closed:
            try:
                worker["process"].stdin.write(reason.encode() + b"\n")
                worker["process"].stdin.flush()
            except OSError:
                close_input(worker["process"])

    def finish(self, run_id, worker, event):
        path = plain_path(event["report"])
        if not path.is_relative_to(worker["root"]) or path.stat().st_size > 100_000:
            raise DispatcherError("REPORT_INVALID")
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != event["sha256"]:
            raise DispatcherError("REPORT_INVALID")
        report = json.loads(raw)
        files = {}
        size = 0
        for name in worker["offer"]["outputs"]:
            file = plain_path(Path(report["workdir"]) / name)
            if file.is_file():
                if file.stat().st_size + size > OUTPUT_LIMIT:
                    raise DispatcherError("REMOTE_LIMIT")
                content = file.read_bytes()
                size += len(content)
                files[name] = dict(content=b64(content), sha256=hashlib.sha256(content).hexdigest())
        encrypted = Path(report["encrypted_results"])
        if encrypted.is_file() and encrypted.stat().st_size > OUTPUT_LIMIT:
            raise DispatcherError("REMOTE_LIMIT")
        value = dict(
            kind="report",
            run_id=run_id,
            birth=worker["birth"],
            report=b64(raw),
            sha256=event["sha256"],
            files=files,
            results=b64(encrypted.read_bytes()) if encrypted.is_file() else None,
        )
        with self.journal.transaction():
            self.journal.db.execute("UPDATE runs SET state='DONE' WHERE id=?", (run_id,))
            self.journal.put(run_id + ":report", value)

    def tick(self):
        while not self.events.empty():
            run_id, event = self.events.get()
            worker = self.workers.get(run_id)
            if not worker:
                continue
            if event.get("kind") == "complete":
                worker["complete"] = event
            elif event.get("kind") == "progress":
                worker["progress"] = event
        for run_id, worker in list(self.workers.items()):
            process = worker["process"]
            if time.time() >= worker["deadline"]:
                self.cancel(worker, "TIMEOUT")
            elif time.time() - self.last_pulse >= LEASE:
                self.cancel(worker, "DISPATCHER_LOST")
            if worker["cancelled"] and time.monotonic() - worker["cancelled"] > 6:
                terminate_tree(process.pid, worker["birth"], group=True)
                reap_detached(run_id, worker["birth"])
            if process.poll() is not None:
                worker["reader"].join(1)
                # Drain the completion line enqueued just before process exit.
                while not self.events.empty():
                    event_id, event = self.events.get()
                    if event_id in self.workers and event.get("kind") == "complete":
                        self.workers[event_id]["complete"] = event
                try:
                    if "complete" in worker:
                        self.finish(run_id, worker, worker["complete"])
                    else:
                        self.lost(run_id)
                except Exception:
                    self.lost(run_id)
                finally:
                    reap_detached(run_id, worker["birth"])
                    process.stdout.close()
                    close_input(process)
                    process.wait()
                    del self.workers[run_id]

    def run(self):
        from .diagnostics import capabilities

        with FileLock(str(self.root / "agent.lock"), timeout=0):
            self.recover()
            try:
                while not self.done:
                    self.tick()
                    heartbeat = dict(
                        kind="heartbeat",
                        os="linux",
                        version=__import__("poster.remote", fromlist=["VERSION"]).VERSION,
                        protocol=1,
                        capabilities=capabilities(self.root),
                        runs=[
                            dict(
                                run_id=key,
                                pid=w["process"].pid,
                                birth=w["birth"],
                                confirmed=w.get("progress", {}).get("confirmed"),
                            )
                            for key, w in self.workers.items()
                        ],
                    )
                    messages = []
                    size = 0
                    bodies = [heartbeat, *self.journal.messages()]
                    for body in bodies:
                        packet = pack(self.identity, self.peer, body)
                        length = len(raw_json(packet))
                        if size + length > MAX_MESSAGE:
                            break
                        messages.append(packet)
                        size += length
                    try:
                        packets = self.client.exchange(messages, self.acks)
                        self.acks = []
                        for body in bodies[: len(messages)]:
                            if body.get("kind") == "received":
                                self.journal.drop(body["run_id"] + ":received")
                        self.error = ""
                        for packet in packets:
                            try:
                                self.handle(packet)
                            except DispatcherError:
                                self.error = "REMOTE_PROTOCOL"
                            self.acks.append(packet["id"])
                    except DispatcherError as exc:
                        self.error = exc.code
                    safe_json(
                        self.root / "status.json",
                        dict(
                            id=self.identity.public()["id"],
                            os=platform.system(),
                            active=len(self.workers),
                            error=self.error,
                            pid=os.getpid(),
                            birth=psutil.Process().create_time(),
                            at=time.time(),
                            phase="RUNNING",
                            controller_seen=self.last_pulse,
                        ),
                    )
                    time.sleep(0.25)
            finally:
                try:
                    self.client.exchange(
                        [
                            pack(
                                self.identity,
                                self.peer,
                                dict(
                                    kind="heartbeat",
                                    os="linux",
                                    version=__import__("poster.remote", fromlist=["VERSION"]).VERSION,
                                    protocol=1,
                                    capabilities=capabilities(self.root, phase="STOPPED"),
                                    runs=[],
                                ),
                            )
                        ]
                    )
                except Exception:
                    pass  # Optional STOPPED diagnostics must never bypass owned-process cleanup.
                for worker in self.workers.values():
                    self.cancel(worker, "DISPATCHER_LOST")
                for run_id, worker in self.workers.items():
                    terminate_tree(worker["process"].pid, worker["birth"], group=True)
                    reap_detached(run_id, worker["birth"])
                self.client.close()
                self.journal.close()
                safe_json(
                    self.root / "status.json",
                    dict(
                        id=self.identity.public()["id"],
                        pid=os.getpid(),
                        birth=psutil.Process().create_time(),
                        at=time.time(),
                        phase="STOPPED",
                        active=0,
                        error=self.error,
                    ),
                )
