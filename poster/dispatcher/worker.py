"""Private subprocess entrypoint; GraphRunner remains the only action executor."""

import argparse
import asyncio
import importlib.abc
import json
import os
from pathlib import Path
import shutil
import sys
import threading
import time

from filelock import FileLock, Timeout

from .models import MESSAGES, DispatcherError, Status
from .processes import clean_descendants, terminate_tree, reap_detached
from .security import private_dir, safe_json, seal, unseal
from .snapshots import Snapshots, digest
from .store import Store


class HeadlessGuard(importlib.abc.MetaPathFinder):
    def __init__(self, browser):
        self.forbidden = {"tkinter", "_tkinter", "PySide6"} | (set() if browser else {"playwright"})
        if any(name.split(".")[0] in self.forbidden for name in sys.modules):
            raise ImportError("Dispatcher dependency imported before the headless guard")

    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in self.forbidden:
            raise ImportError("Dispatcher headless dependency contract")


class Controls:
    def __init__(self, pending=b""):
        self.pending = pending
        self.reason = None
        self.finished = threading.Event()
        self.requested = threading.Event()

    def request(self, reason):
        if self.reason is None:
            self.reason = reason
            self.requested.set()

    def read(self):
        pending = self.pending
        try:
            while True:
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    reason = line.decode("ascii", errors="ignore")
                    if reason in {"CANCEL_REQUESTED", "TIMEOUT"}:
                        self.request(reason)
                chunk = os.read(0, 4096)
                if not chunk:
                    self.request("DISPATCHER_LOST")
                    return
                pending += chunk
                if len(pending) > 4096:
                    self.request("DISPATCHER_LOST")
                    return
        except OSError:
            self.request("DISPATCHER_LOST")

    def watchdog(self):
        self.requested.wait()
        if not self.finished.wait(5):
            import psutil

            reap_detached(os.environ.get("POSTER_DISPATCHER_RUN"), psutil.Process().create_time())
            terminate_tree(os.getpid(), psutil.Process().create_time(), group=True)


def safe_code(code):
    from ..maker.runtime import ERRORS

    return code if code in ERRORS or code in MESSAGES else "EXECUTION_FAILED"


def exception_state(store, run_id):
    """A durable effects marker outranks an unexpected Python exception."""
    try:
        return Status.UNKNOWN if store.execution(run_id)["effects_possible"] else Status.FAILED
    except Exception:
        return Status.UNKNOWN  # Missing evidence cannot prove absence of a write.


async def execute(store, execution, owner, controls):
    from ..maker.model import Graph

    job = store.payload(execution["job_id"])
    run_dir = private_dir(store.root / "runs" / execution["run_id"])
    workdir = private_dir(run_dir / "work")
    report_path = run_dir / "report.json"
    started = time.time()
    runner = profile_lock = None
    state, code, node_code = Status.FAILED, "EXECUTION_FAILED", None
    uncertain = False
    browser = False
    cleanup_failure = None
    try:
        snapshots = Snapshots(store.root)
        manifest, files = snapshots.read(job["version"])
        # Child Browser.* is rejected by the existing Project.Run contract.
        # Unrelated browser graphs captured for dynamic dependency lookup must
        # not weaken the HTTP/data-only main graph's import restriction.
        main_graph = Graph.model_validate_json(files[manifest["main"]])
        browser = any(
            n.type.startswith("Browser.") or n.type == "Logic.If" and n.params.get("condition") != "equals"
            for n in main_graph.nodes
        )
        sys.meta_path.insert(0, HeadlessGuard(browser))
        if browser:
            import psutil

            if psutil.virtual_memory().available < 256 * 1024 * 1024:
                raise DispatcherError("MEMORY_LIMIT")
            from ..browser import find_browser

            try:
                if not find_browser():
                    raise ValueError()
            except ValueError as exc:
                raise DispatcherError("BROWSER_UNAVAILABLE") from exc
        source = snapshots.materialize(job["version"], run_dir / "project")
        from ..maker.storage import load_graph
        from ..maker.runtime import GraphRunner

        graph, vault = load_graph(source)
        if graph.metadata.get("poster.resources"):
            from poster.cloud.package import stage_resources

            stage_resources(graph, files, workdir)
        envelope = unseal(job["inputs_cipher"])
        for ref, value in envelope.get("secret_refs", {}).items():
            vault.put_value(value, ref)
        if envelope.get("project_key"):
            from ..auth import _legacy_keys

            _legacy_keys.append(envelope["project_key"].encode("ascii"))
        inputs = envelope["inputs"]
        for name, value in envelope.get("secrets", {}).items():
            inputs[name] = vault.put_value(value)
        if not store.start(execution["run_id"], owner, os.getpid()):
            state, code = Status.CANCELLED, "CANCEL_REQUESTED"
        else:
            profile = None
            if envelope.get("profile"):
                from ..maker.profiles import Profile, Profiles

                value = envelope["profile"]
                profile_lock = FileLock(
                    str(run_dir / "isolated-profile.lock")
                    if value.get("mode") == "isolated"
                    else value["lock"]
                )
                wait_reported = False
                while not controls.reason:
                    try:
                        profile_lock.acquire(timeout=0)
                        break
                    except Timeout:
                        if not wait_reported:
                            store.note(execution["run_id"], "PROFILE_BUSY")
                            wait_reported = True
                        await asyncio.sleep(0.05)
                if not controls.reason:
                    profile = Profile(Profiles(private_dir(run_dir / "profile")), value["key"], value["data"])

            def event(value):
                nonlocal uncertain
                kind = value.get("kind")
                if kind == "node":
                    node = graph.node(value["node_id"])
                    risk = node.type.startswith("Browser.") or node.type in {
                        "HTTP.Request",
                        "HTTP.Post",
                        "Project.Run",
                    }
                    if risk:
                        store.progress(execution["run_id"], effects=True)
                elif kind == "result":
                    result = value["result"]
                    uncertain |= result.get("error_code") == "HTTP_WRITE_UNKNOWN" or bool(
                        result.get("details", {}).get("write_outcome_unknown")
                    )
                    if result["status"] == "SUCCEEDED":
                        store.progress(execution["run_id"], confirmed=result["node_id"])
                elif kind == "child":
                    child = value.get("event", {})
                    result = child.get("result", {})
                    uncertain |= result.get("error_code") == "HTTP_WRITE_UNKNOWN" or bool(
                        result.get("details", {}).get("write_outcome_unknown")
                    )

            runner = GraphRunner(
                graph,
                vault=vault,
                inputs=inputs,
                workdir=workdir,
                project_path=source,
                max_seconds=job["timeout"],
                profile=profile,
                network=envelope.get("network"),
                on_event=event,
            )
            task = asyncio.create_task(runner.run(breakpoints=False))
            while not task.done():
                if controls.reason:
                    await runner.stop()
                await asyncio.wait({task}, timeout=0.025)
            result = await task
            node_code = next(
                (safe_code(r.error_code) for r in reversed(runner.results) if r.error_code), None
            )
            risk = store.execution(execution["run_id"])["effects_possible"]
            if controls.reason:
                code = controls.reason
                state = (
                    Status.UNKNOWN
                    if risk or code == "DISPATCHER_LOST"
                    else Status.TIMED_OUT
                    if code == "TIMEOUT"
                    else Status.CANCELLED
                )
            elif uncertain:
                state, code = Status.UNKNOWN, "REVIEW_REQUIRED"
            elif node_code == "RUN_TIMEOUT":
                state, code = (Status.UNKNOWN if risk else Status.TIMED_OUT), "TIMEOUT"
            else:
                state = Status.SUCCEEDED if result == "SUCCEEDED" else Status.FAILED
                code = "" if state == Status.SUCCEEDED else "EXECUTION_FAILED"
            # Raw values never enter the public report or SQLite. Even public
            # graph values may contain credentials accidentally marked ordinary.
            from ..maker.storage import atomic

            atomic(
                run_dir / "results.enc",
                seal(
                    {
                        "variables": runner.context.variables.snapshot(),
                        "results": [r.__dict__ for r in runner.results],
                    }
                ),
            )
    except Exception as exc:
        code = exc.code if isinstance(exc, DispatcherError) else "WORKER_INTERNAL_ERROR"
        if getattr(exc, "code", None):
            node_code = safe_code(exc.code)
        state = exception_state(store, execution["run_id"])
    finally:
        if runner:
            try:
                # Chromium/Playwright shutdown on a loaded Windows host can exceed 3s.
                # Cancellation and lost-lease process watchdogs keep their existing bounds.
                await asyncio.wait_for(runner.close(), 10)
            except BaseException as exc:
                state, code = Status.UNKNOWN, "PROCESS_CONTROL_FAILED"
                cleanup_failure = {"phase": "runner_close", "type": type(exc).__name__}
        try:
            if profile_lock and profile_lock.is_locked:
                profile_lock.release()
        except Exception as exc:
            state, code = Status.UNKNOWN, "PROCESS_CONTROL_FAILED"
            cleanup_failure = {"phase": "profile_unlock", "type": type(exc).__name__}
        try:
            if not clean_descendants():
                state, code = Status.UNKNOWN, "PROCESS_CONTROL_FAILED"
                cleanup_failure = {"phase": "descendants", "type": "ExitUnconfirmed"}
        except Exception as exc:
            state, code = Status.UNKNOWN, "PROCESS_CONTROL_FAILED"
            cleanup_failure = {"phase": "descendants", "type": type(exc).__name__}
        if controls.reason == "DISPATCHER_LOST":
            state, code = Status.UNKNOWN, "DISPATCHER_LOST"
        # Decrypted project files are private and ephemeral; the immutable
        # encrypted snapshot and intentional workdir outputs remain available.
        try:
            if (run_dir / "project").exists():
                for path in (run_dir / "project").rglob("*"):
                    if path.is_file():
                        path.chmod(0o600)
                shutil.rmtree(run_dir / "project")
        except Exception as exc:
            state, code = Status.UNKNOWN, "PROCESS_CONTROL_FAILED"
            cleanup_failure = {"phase": "project_cleanup", "type": type(exc).__name__}
    current = store.execution(execution["run_id"])
    from ..maker.runtime import ERRORS

    report = {
        "format": "poster.dispatcher.report",
        "version": 1,
        "job_id": job["id"],
        "run_id": execution["run_id"],
        "project_id": job["project_id"],
        "project_version": job["version"],
        "attempt": execution["attempt"],
        "worker_id": os.getpid(),
        "worker_executable": sys.executable,
        "worker_frozen": bool(getattr(sys, "frozen", False)),
        "status": state,
        "started": started,
        "finished": time.time(),
        "duration_seconds": time.time() - started,
        "code": code,
        "message": MESSAGES.get(code, ""),
        "node_error_code": node_code,
        "node_message": ERRORS.get(node_code, ""),
        "cleanup_failure": cleanup_failure,
        "last_confirmed_step": current["last_confirmed_step"],
        "external_effects_may_have_occurred": bool(current["effects_possible"]),
        "outputs_may_be_partial": state != Status.SUCCEEDED,
        "workdir": str(workdir),
        "encrypted_results": str(run_dir / "results.enc"),
        "gui_imports_forbidden": True,
        "browser_imports_forbidden": not browser,
    }
    safe_json(report_path, report)
    exit_code = 0 if state == Status.SUCCEEDED else 1
    store.complete(
        execution["run_id"],
        state,
        code=code,
        exit_code=exit_code,
        report=report_path,
        report_sha256=digest(report_path.read_bytes()),
    )
    return exit_code


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--run", required=True)
    parser.add_argument("--owner", required=True)
    args = parser.parse_args(argv)
    os.umask(0o077)
    raw = b""
    while b"\n" not in raw and len(raw) < 4096:
        chunk = os.read(0, 4096)
        if not chunk:
            return 2  # No gate, no execution.
        raw += chunk
    if b"\n" not in raw:
        return 2
    handshake, pending = raw.split(b"\n", 1)
    controls = Controls(pending)
    store = None
    try:
        os.environ["POSTER_SESSION_KEY"] = json.loads(handshake)["key"]
        threading.Thread(target=controls.read, daemon=True).start()
        threading.Thread(target=controls.watchdog, daemon=True).start()
        store = Store(args.db)
        execution = store.execution(args.run)
        return asyncio.run(execute(store, execution, args.owner, controls))
    except Exception as exc:
        # Last-resort, payload-free evidence for failures outside execute's
        # protected region (including report I/O / event-loop teardown).
        try:
            import traceback

            if store:
                safe_json(
                    store.root / "runs" / args.run / "worker-failure.json",
                    {
                        "type": type(exc).__name__,
                        "system_error": getattr(exc, "system_error", None),
                        "frames": [
                            Path(f.filename).name + ":" + str(f.lineno)
                            for f in traceback.extract_tb(exc.__traceback__)[-6:]
                        ],
                    },
                )
        except Exception:
            pass
        return 2  # Never print exception payloads or interpreter tracebacks.
    finally:
        controls.finished.set()
        if store:
            store.close()


if __name__ == "__main__":
    raise SystemExit(main())
