"""Durable admission, bounded subprocess scheduling, stop/cancel and crash recovery."""

import json
from pathlib import Path
import time
import uuid

from filelock import FileLock, Timeout

from ..maker.model import Graph, secret_refs
from ..maker.variables import Variables
from ..maker.storage import Vault
from .models import ACTIVE, DispatcherError, Status
from .processes import WorkerProcess, terminate_tree, reap_detached
from .security import key_setup, plain_path, seal
from .snapshots import Snapshots, digest


def enqueue(
    store,
    source,
    *,
    inputs=None,
    secrets=None,
    key=None,
    timeout=300,
    profile_root=None,
    profile_id=None,
    key_file=None,
    project_key=None,
    profile_key_file=None,
):
    if key is not None and (previous := store.find_key(key)):
        return previous
    key_setup(store.root, key_file)
    snapshots = Snapshots(store.root)
    project_id, version = snapshots.create(source)
    return enqueue_snapshot(
        store,
        project_id,
        version,
        source,
        inputs=inputs,
        secrets=secrets,
        key=key,
        timeout=timeout,
        profile_root=profile_root,
        profile_id=profile_id,
        project_key=project_key,
        profile_key_file=profile_key_file,
    )


def enqueue_snapshot(
    store,
    project_id,
    version,
    source,
    *,
    inputs=None,
    secrets=None,
    key=None,
    timeout=300,
    profile_root=None,
    profile_id=None,
    project_key=None,
    profile_key_file=None,
):
    """Admit the inspected immutable version, never recapture a moving source."""
    if key is not None and (previous := store.find_key(key)):
        return previous
    cipher, names = prepare_snapshot(
        store,
        project_id,
        version,
        inputs=inputs,
        secrets=secrets,
        profile_root=profile_root,
        profile_id=profile_id,
        project_key=project_key,
        profile_key_file=profile_key_file,
    )
    return store.enqueue(project_id, version, plain_path(source), cipher, names, key=key, timeout=timeout)


def prepare_snapshot(
    store,
    project_id,
    version,
    *,
    inputs=None,
    secrets=None,
    profile_root=None,
    profile_id=None,
    project_key=None,
    profile_key_file=None,
):
    """Validate once and retain encrypted inputs for manual or scheduled admission."""
    snapshots = Snapshots(store.root)
    manifest, files = snapshots.read(version)
    if manifest["project_id"] != project_id:
        raise DispatcherError("SNAPSHOT_INVALID")
    graph = Graph.model_validate_json(files[manifest["main"]])
    inputs, secrets = inputs or {}, secrets or {}
    if set(inputs) & set(secrets):
        raise DispatcherError("INPUT_INVALID")
    try:
        if len(json.dumps([inputs, secrets], allow_nan=False).encode()) > 1_000_000:
            raise ValueError()
        vault = Vault()
        validated = dict(inputs)
        for name, value in inputs.items():
            if name in graph.variables and graph.variables[name].secret and not secret_refs(value):
                raise ValueError()
        for name, value in secrets.items():
            validated[name] = vault.put_value(value)
        Variables(graph.variables, vault, validated)
    except Exception as exc:
        raise DispatcherError("INPUT_INVALID") from exc
    envelope = {"inputs": inputs, "secrets": secrets}
    if project_key:
        from cryptography.fernet import Fernet

        try:
            Fernet(project_key.encode())
        except Exception as exc:
            raise DispatcherError("KEY_INVALID") from exc
        # Encrypted with the queue key; never an argv/environment hand-off.
        envelope["project_key"] = project_key
    if profile_id:
        if not profile_root:
            raise DispatcherError("PARAMETERS_INVALID")
        from ..maker.profiles import Profiles

        profiles = Profiles(plain_path(profile_root))
        if profile_key_file:
            from cryptography.fernet import Fernet
            from ..maker.profiles import Profile
            from ..maker.storage import read_bounded

            try:
                cipher = Fernet(plain_path(profile_key_file).read_bytes().strip())
                profile = Profile(
                    profiles, profile_id, json.loads(cipher.decrypt(read_bounded(profiles.path(profile_id))))
                )
            except Exception as exc:
                raise DispatcherError("PROJECT_KEY_REQUIRED") from exc
        else:
            profile = profiles.open(profile_id)
        envelope["profile"] = {
            "key": profile_id,
            "data": profile.data,
            "lock": str(profiles.path(profile_id)) + ".dispatcher-execution.lock",
        }
    return seal(envelope), sorted(set(inputs) | set(secrets))


class Dispatcher:
    def __init__(self, store, *, max_workers=2, max_per_project=1, poll=0.03, key_file=None, on_started=None):
        if not 1 <= max_workers <= 32 or not 1 <= max_per_project <= 32 or not 0.01 <= poll <= 5:
            raise DispatcherError("PARAMETERS_INVALID")
        self.store = store
        self.max_workers, self.max_per_project, self.poll = max_workers, max_per_project, poll
        self.key_file = key_file
        self.owner = uuid.uuid4().hex
        self.workers = {}
        self.stop_requested = False
        self.on_started = on_started

    def lock(self):
        return FileLock(str(plain_path(Path(str(self.store.path) + ".dispatcher.lock"))), timeout=0)

    def recover(self):
        for execution in self.store.active():
            if self.store.recover_unstarted(execution["run_id"]):
                continue
            if execution["pid"]:
                terminate_tree(execution["pid"], execution["birth"], group=True)
                reap_detached(execution["run_id"], execution["birth"])
            self.store.complete(execution["run_id"], Status.UNKNOWN, code="DISPATCHER_LOST")

    def reconcile(self):
        try:
            with self.lock():
                self.recover()
                jobs = self.store.db.execute(
                    "SELECT id FROM jobs WHERE status='UNKNOWN' ORDER BY created LIMIT 1000"
                ).fetchall()
                for job in jobs:
                    self.store.review(job["id"])
                return {"status": "RECONCILED", "counts": self.store.counts(), "executed": 0}
        except Timeout as exc:
            raise DispatcherError("DISPATCHER_BUSY") from exc

    def validate_confirmation(self, execution):
        try:
            path = plain_path(execution["report"])
            if path.stat().st_size > 100_000:
                return False
            raw = path.read_bytes()
            value = json.loads(raw)
            job = self.store.payload(execution["job_id"], private=False)
            return (
                digest(raw) == execution["report_sha256"]
                and value["format"] == "poster.dispatcher.report"
                and value["job_id"] == execution["job_id"]
                and value["run_id"] == execution["run_id"]
                and value["status"] == execution["status"]
                and value["project_version"] == job["version"]
            )
        except Exception:
            return False

    def harvest(self, run_id, worker):
        execution = self.store.execution(run_id)
        code = worker.process.returncode
        worker.close(force=True)  # Also closes Windows Job Object / remaining POSIX group.
        if execution["status"] in ACTIVE:
            if worker.reason and not execution["effects_possible"]:
                state = Status.TIMED_OUT if worker.reason == "TIMEOUT" else Status.CANCELLED
            else:
                state = Status.UNKNOWN
            self.store.complete(run_id, state, code=worker.reason or "WORKER_CRASH", exit_code=code)
        elif execution["status"] == Status.SUCCEEDED and not self.validate_confirmation(execution):
            self.store.invalidate_report(run_id)
        self.store.set_exit_code(run_id, code)

    def tick(self):
        for run_id, worker in list(self.workers.items()):
            if worker.process.poll() is not None:
                self.harvest(run_id, worker)
                del self.workers[run_id]
                continue
            execution = self.store.execution(run_id)
            job = self.store.payload(execution["job_id"], private=False)
            if job["cancel_requested"]:
                worker.request("CANCEL_REQUESTED")
            elif time.monotonic() - worker.created > job["timeout"]:
                worker.request("TIMEOUT")
            if worker.cancelled_at is not None and time.monotonic() - worker.cancelled_at > 6:
                worker.close(force=True)
                self.harvest(run_id, worker)
                del self.workers[run_id]

    def run(self, *, once=False):
        key_setup(self.store.root, self.key_file)
        try:
            with self.lock():
                self.recover()
                self.store.begin_service(self.owner)
                try:
                    if self.on_started:
                        self.on_started()
                    while True:
                        self.tick()
                        stopping = self.stop_requested or self.store.stopping()
                        if not stopping:
                            from .schedules import Scheduler

                            if not hasattr(self, "schedule_engine"):
                                self.schedule_engine = Scheduler(self.store)
                            self.schedule_engine.tick()
                            while (
                                len(self.workers) < self.max_workers
                                and not self.stop_requested
                                and not self.store.stopping()
                            ):
                                execution = self.store.claim(
                                    self.owner, self.max_workers, self.max_per_project
                                )
                                if not execution:
                                    break
                                try:
                                    worker = WorkerProcess(self.store, execution)
                                    self.workers[execution["run_id"]] = worker
                                    worker.permit()
                                except Exception:
                                    if execution["run_id"] in self.workers:
                                        worker = self.workers.pop(execution["run_id"])
                                        worker.close(force=True)
                                    self.store.complete(
                                        execution["run_id"],
                                        Status.UNKNOWN
                                        if self.store.execution(execution["run_id"])["pid"]
                                        else Status.FAILED,
                                        code="PROCESS_CONTROL_FAILED",
                                    )
                        if not self.workers and (
                            stopping or once and not self.store.counts().get("PENDING", 0)
                        ):
                            break
                        time.sleep(self.poll)
                finally:
                    for worker in self.workers.values():
                        worker.request("CANCEL_REQUESTED")
                    deadline = time.monotonic() + 6
                    while self.workers and time.monotonic() < deadline:
                        self.tick()
                        time.sleep(self.poll)
                    for run_id, worker in list(self.workers.items()):
                        worker.close(force=True)
                        self.harvest(run_id, worker)
                        del self.workers[run_id]
                    self.store.end_service(self.owner)
        except Timeout as exc:
            raise DispatcherError("DISPATCHER_BUSY") from exc
        return {
            "status": "STOPPED" if self.stop_requested or self.store.stopping() else "IDLE",
            "counts": self.store.counts(),
            "owner": self.owner,
        }
