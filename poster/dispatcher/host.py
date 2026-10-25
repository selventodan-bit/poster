"""Per-user headless service. The GUI is only an authenticated JSON client.

No pickle, TCP listener, GUI imports, or lifetime tie to a client process.
SQLite connections are created and used by their owning thread.
"""

import argparse
import hashlib
import json
from multiprocessing.connection import Client, Listener
from multiprocessing import AuthenticationError
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import queue

from filelock import FileLock
import psutil

from .catalog import Catalog
from .launcher import build_info, command, environment, process_options
from .models import DispatcherError, Status
from .security import plain_path, private_dir, restrict, safe_json
from .service import Dispatcher
from .store import Store
from .workspace import queue_key, workspace_path

MAX_MESSAGE = 2_000_000


def endpoint(root):
    identity = hashlib.sha256(str(plain_path(root)).encode()).hexdigest()[:24]
    if os.name == "nt":
        return "\\\\.\\pipe\\PosterDispatcher-" + identity, "AF_PIPE"
    directory = private_dir(Path(tempfile.gettempdir()) / ("poster-dispatcher-" + identity))
    return str(directory / "service.sock"), "AF_UNIX"


def auth_key(root, *, create=False):
    path = plain_path(root / "ipc.key")
    if not path.exists() and create:
        with path.open("xb") as stream:
            stream.write(os.urandom(32))
        restrict(path)
    raw = path.read_bytes()
    if len(raw) != 32:
        raise DispatcherError("KEY_INVALID")
    return raw


def _request(root, operation, arguments):
    """Blocking transport, deliberately called off the GUI thread."""
    try:
        address, family = endpoint(root)
        with Client(address, family=family, authkey=auth_key(root)) as connection:
            connection.send_bytes(json.dumps({"op": operation, **arguments}, allow_nan=False).encode())
            if not connection.poll(15):
                raise DispatcherError("SERVICE_UNAVAILABLE")
            reply = json.loads(connection.recv_bytes(MAX_MESSAGE))
        if not reply["ok"]:
            raise DispatcherError(reply["code"])
        return reply["value"]
    except DispatcherError:
        raise
    except Exception as exc:
        raise DispatcherError("SERVICE_UNAVAILABLE") from exc


def request(root, operation, **arguments):
    # Authentication itself can block when a crashed server still has an open
    # listening handle. Bound the ENTIRE exchange, not only response.poll().
    result = queue.SimpleQueue()

    def exchange():
        try:
            result.put((_request(root, operation, arguments), None))
        except DispatcherError as exc:
            result.put((None, exc.code))

    thread = threading.Thread(target=exchange, name="poster-ipc", daemon=True)
    thread.start()
    try:
        value, error = result.get(timeout=15)
    except queue.Empty:
        raise DispatcherError("SERVICE_UNAVAILABLE") from None
    if error:
        raise DispatcherError(error)
    return value


def ensure_service(root):
    root = private_dir(root)
    with FileLock(str(root / "startup.lock"), timeout=25):
        try:
            value = request(root, "ping")
        except DispatcherError:
            # The host takes its own lock before touching the queue. A slow or
            # unresponsive existing host can never create a second scheduler.
            subprocess.Popen(
                command("service", "--workspace", root),
                cwd=root,
                env=environment(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                **process_options(),
            )
            deadline = time.monotonic() + 20
            while True:
                try:
                    value = request(root, "ping")
                    break
                except DispatcherError:
                    failure = root / "startup-error.json"
                    if failure.is_file():
                        raise DispatcherError(json.loads(failure.read_bytes())["code"]) from None
                    if time.monotonic() > deadline:
                        raise DispatcherError("SERVICE_UNAVAILABLE") from None
                    time.sleep(0.1)
        if value["commit"] != build_info()["commit"]:
            raise DispatcherError("SERVICE_VERSION")
        return value


class Host:
    def __init__(self, root):
        self.root = private_dir(root)
        queue_key(root)  # Must precede creation of an empty database.
        self.store = Store(root / "dispatcher.sqlite")
        self.catalog = Catalog(self.store)
        self.done = threading.Event()
        self.thread = None
        self.scheduler_error = None
        self.settings_path = root / "settings.json"
        self.settings = {"max_workers": 2, "max_per_project": 1, "timeout": 300}
        if self.settings_path.exists():
            self.set_settings(json.loads(self.settings_path.read_bytes()))
        self.names = {}

    @property
    def running(self):
        return bool(self.thread and self.thread.is_alive())

    def set_settings(self, settings):
        workers, per, timeout = (settings[k] for k in ["max_workers", "max_per_project", "timeout"])
        if (
            type(workers) is not int
            or type(per) is not int
            or not 1 <= workers <= 32
            or not 1 <= per <= 32
            or not 1 <= timeout <= 86400
        ):
            raise DispatcherError("PARAMETERS_INVALID")
        self.settings = {"max_workers": workers, "max_per_project": per, "timeout": timeout}
        safe_json(self.settings_path, self.settings)

    def start(self):
        if self.running:
            return
        # Resolve/verify the packaged worker before claiming any job.
        command("worker")
        self.scheduler_error = None
        settings = dict(self.settings)
        ready = threading.Event()

        def run():
            store = None
            try:
                store = Store(self.store.path)
                Dispatcher(
                    store,
                    max_workers=settings["max_workers"],
                    max_per_project=settings["max_per_project"],
                    on_started=ready.set,
                ).run()
            except Exception as exc:
                self.scheduler_error = exc.code if isinstance(exc, DispatcherError) else "SERVICE_FAILED"
            finally:
                if store:
                    store.close()
                ready.set()

        self.thread = threading.Thread(target=run, name="poster-scheduler")
        self.thread.start()
        if not ready.wait(10):
            raise DispatcherError("SERVICE_UNAVAILABLE")
        if self.scheduler_error:
            raise DispatcherError(self.scheduler_error)

    def project_name(self, project_id, version):
        if version not in self.names:
            from .snapshots import Snapshots

            try:
                manifest, files = Snapshots(self.store.root).read(version)
                self.names[version] = json.loads(files[manifest["main"]])["name"]
            except Exception:
                self.names[version] = project_id
        return self.names[version]

    def jobs(self, search="", status="", offset=0, order="created", descending=True):
        columns = {
            name: "j." + name
            for name in ["id", "created", "updated", "status", "project_id", "attempt", "timeout"]
        }
        columns.update(
            name="poster_project_name(j.project_id,j.version)",
            pid="e.pid",
            code="e.code",
            duration="duration",
        )
        self.store.db.create_function(
            "poster_project_name",
            2,
            lambda project_id, version: self.project_name(project_id, version).casefold(),
            deterministic=True,
        )
        if order not in columns:
            raise DispatcherError("PARAMETERS_INVALID")
        if status and status not in Status._value2member_map_:
            raise DispatcherError("PARAMETERS_INVALID")
        clauses, args = [], []
        if status:
            clauses.append("j.status=?")
            args.append(status)
        if search:
            clauses.append(
                "(j.id LIKE ? OR j.project_id LIKE ? OR poster_project_name(j.project_id,j.version) LIKE ?)"
            )
            args.extend(["%" + search[:200].casefold() + "%"] * 3)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        total = self.store.db.execute("SELECT count(*) FROM jobs j" + where, args).fetchone()[0]
        rows = self.store.db.execute(
            "SELECT j.id,j.project_id,j.version,j.status,j.attempt,j.created,j.updated,j.timeout,"
            "e.pid,e.code,e.last_confirmed_step,e.report,e.report_sha256,"
            "round(coalesce(e.finished,?)-e.started,2) duration FROM jobs j "
            "LEFT JOIN executions e ON e.job_id=j.id AND e.attempt=j.attempt"
            + where
            + " ORDER BY "
            + columns[order]
            + (" DESC" if descending else " ASC")
            + " LIMIT 200 OFFSET ?",
            [time.time(), *args, max(0, int(offset))],
        ).fetchall()
        values = [{**dict(r), "name": self.project_name(r["project_id"], r["version"])} for r in rows]
        return {"rows": values, "total": total, "offset": offset}

    def projects(self):
        aggregates = {
            r["project_id"]: dict(r)
            for r in self.store.db.execute(
                "SELECT j.project_id,count(e.run_id) executions,"
                "sum(j.status IN ('FAILED','TIMED_OUT','UNKNOWN','REVIEW_REQUIRED')) errors,"
                "sum(j.status IN ('LEASED','RUNNING')) active,max(e.started) last_run,"
                "sum(coalesce(e.finished-e.started,0)) elapsed FROM jobs j "
                "LEFT JOIN executions e ON e.job_id=j.id AND e.attempt=j.attempt GROUP BY j.project_id"
            )
        }
        rows = []
        for item in self.catalog.rows():
            stats = aggregates.get(item["project_id"], {})
            latest = self.store.db.execute(
                "SELECT status FROM jobs WHERE project_id=? ORDER BY created DESC LIMIT 1",
                (item["project_id"],),
            ).fetchone()
            rows.append(
                {
                    **item,
                    **stats,
                    "status": latest[0] if latest else "",
                    "elapsed": round(stats.get("elapsed", 0), 2),
                }
            )
        return rows

    def dispatch(self, value):
        op = value.pop("op")
        if op == "ping":
            return {
                "pid": os.getpid(),
                "birth": psutil.Process().create_time(),
                "commit": build_info()["commit"],
                "running": self.running,
                "stopping": self.running and self.store.stopping(),
                "settings": self.settings,
                "counts": self.store.counts(),
                "error": self.scheduler_error,
                "workspace": str(self.root),
            }
        if op == "projects":
            return self.projects()
        if op == "dashboard":
            return {
                "service": self.dispatch({"op": "ping"}),
                "projects": self.projects(),
                "jobs": self.jobs(**value),
            }
        if op == "add":
            return self.catalog.inspect(**value)
        if op == "remove":
            self.catalog.remove(value["ids"])
            return True
        if op == "enqueue":
            return [
                self.catalog.enqueue(
                    item,
                    timeout=self.settings["timeout"],
                    inputs=value.get("inputs"),
                    secrets=value.get("secrets"),
                    key=value["request_id"] + ":" + item,
                )
                for item in value["ids"]
            ]
        if op == "enqueue_batch":
            if not 1 <= len(value["items"]) <= 100:
                raise DispatcherError("PARAMETERS_INVALID")
            jobs, errors = [], []
            for item in value["items"]:
                try:
                    jobs.append(
                        self.catalog.enqueue(
                            item["id"],
                            timeout=item.get("timeout", self.settings["timeout"]),
                            inputs=item.get("inputs"),
                            secrets=item.get("secrets"),
                            profile_root=item.get("profile_root"),
                            profile_id=item.get("profile_id"),
                            profile_key_file=item.get("profile_key_file"),
                            key=value["request_id"] + ":" + item["id"],
                        )
                    )
                except Exception as exc:
                    errors.append(
                        {
                            "id": item["id"],
                            "code": exc.code if isinstance(exc, DispatcherError) else "INPUT_INVALID",
                        }
                    )
            return {"jobs": jobs, "errors": errors}
        if op == "jobs":
            return self.jobs(**value)
        if op == "show":
            result = self.store.show(value["id"])
            history = self.store.history(value["id"])
            result["report"] = None
            result["workdir"] = (
                str(self.store.root / "runs" / history[0]["run_id"] / "work") if history else None
            )
            if history and history[0]["report"]:
                execution = history[0]
                if not Dispatcher(self.store).validate_confirmation(execution):
                    raise DispatcherError("REPORT_INVALID")
                result["report"] = json.loads(plain_path(execution["report"]).read_bytes())
            return result
        if op == "cancel":
            return [self.store.cancel(item)["status"] for item in value["ids"]]
        if op == "start":
            self.start()
            return True
        if op == "stop":
            self.store.request_stop()
            return True
        if op == "settings":
            if self.running:
                raise DispatcherError("DISPATCHER_BUSY")
            self.set_settings(value)
            return self.settings
        if op == "reconcile":
            return Dispatcher(self.store).reconcile()
        if op == "shutdown":
            self.store.request_stop()
            self.done.set()
            return True
        raise DispatcherError("PARAMETERS_INVALID")

    def serve(self):
        address, family = endpoint(self.root)
        if family == "AF_UNIX":
            Path(address).unlink(missing_ok=True)  # Only under the singleton host lock.
        listener = Listener(address, family=family, authkey=auth_key(self.root, create=True))
        safe_json(self.root / "service.json", self.dispatch({"op": "ping"}))

        def clients():
            while not self.done.is_set():
                try:
                    connection = listener.accept()
                except AuthenticationError:
                    continue
                except (OSError, EOFError):
                    if self.done.is_set():
                        return
                    time.sleep(0.05)
                    continue  # A client dying during authentication is not host shutdown.
                with connection:
                    try:
                        if not connection.poll(5):
                            continue
                        value = json.loads(connection.recv_bytes(MAX_MESSAGE))
                        reply = {"ok": True, "value": self.dispatch(value)}
                    except Exception as exc:
                        reply = {
                            "ok": False,
                            "code": exc.code if isinstance(exc, DispatcherError) else "PARAMETERS_INVALID",
                        }
                    try:
                        connection.send_bytes(json.dumps(reply, allow_nan=False).encode())
                    except OSError:
                        pass

        # Store ownership follows the command thread, never the Qt client.
        self.store.close()

        def manager():
            self.store = Store(self.root / "dispatcher.sqlite")
            self.catalog.store = self.store
            try:
                clients()
            finally:
                self.store.close()

        thread = threading.Thread(target=manager, name="poster-rpc", daemon=True)
        thread.start()
        self.done.wait()
        if self.thread:
            self.thread.join()  # Drain admitted jobs; do not tie this to GUI lifetime.
        listener.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, default=None)
    args = parser.parse_args()
    root = private_dir(args.workspace or workspace_path())
    try:
        with FileLock(str(root / "service.lock"), timeout=0):
            (root / "startup-error.json").unlink(missing_ok=True)
            Host(root).serve()
    except Exception as exc:
        from filelock import Timeout

        if not isinstance(exc, Timeout):
            safe_json(
                root / "startup-error.json",
                {"code": exc.code if isinstance(exc, DispatcherError) else "SERVICE_FAILED"},
            )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
