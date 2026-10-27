"""Actual redirector/worker PID handoff, preserving the original execution gate."""

import json
import os
import subprocess
import sys

import psutil
import pytest

from poster.dispatcher.models import DispatcherError
from poster.dispatcher.processes import verified_worker_identity
from poster.dispatcher.service import Dispatcher, enqueue
from poster.dispatcher.store import Store
from test_dispatcher_service import graph_file


def test_real_redirector_records_the_actual_graphrunner_pid(tmp_path, monkeypatch):
    import poster.dispatcher.processes as processes

    original = processes.command
    wrapper = "import subprocess,sys;sys.exit(subprocess.call(sys.argv[1:]))"

    def redirect(role, *args):
        return [sys.executable, "-c", wrapper, *original(role, *args)]

    monkeypatch.setattr(processes, "command", redirect)
    monkeypatch.setattr(processes, "needs_pid_ack", lambda: True)
    source = graph_file(tmp_path, [("Files.WriteText", dict(path="out.txt", text="Original GraphRunner"))])
    store = Store(tmp_path / "queue.sqlite")
    try:
        job = enqueue(store, source)
        Dispatcher(store, max_workers=1, max_per_project=1).run(once=True)
        history = store.history(job)
        assert store.payload(job)["status"] == "SUCCEEDED", history
        assert len(history) == 1 and history[0]["pid"]
        report = json.loads(__import__("pathlib").Path(history[0]["report"]).read_bytes())
        assert report["worker_id"] == history[0]["pid"]
        assert report["worker_parent_id"] != history[0]["pid"]
        assert (
            __import__("pathlib").Path(report["workdir"]) / "out.txt"
        ).read_text() == "Original GraphRunner"
    finally:
        store.close()


def test_pid_pipe_cannot_claim_an_unrelated_process():
    process = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(30)"])
    try:
        birth = psutil.Process(process.pid).create_time()
        assert verified_worker_identity(
            process, birth, dict(format="poster.worker.pid.v1", pid=process.pid, birth=birth)
        ) == (process.pid, birth)
        for packet in [
            None,
            dict(format="poster.worker.pid.v1", pid=os.getpid(), birth=psutil.Process().create_time()),
            dict(format="poster.worker.pid.v1", pid=process.pid, birth=float("nan")),
        ]:
            with pytest.raises(DispatcherError) as caught:
                verified_worker_identity(process, birth, packet)
            assert caught.value.code == "PROCESS_CONTROL_FAILED"
    finally:
        process.terminate()
        process.wait(10)


def test_cancelled_remote_adapter_remains_compatible_with_start_diagnostics(tmp_path):
    import uuid
    from pathlib import Path
    from poster.auth import session_key
    from poster.dispatcher.service import prepare_snapshot
    from poster.dispatcher.snapshots import Snapshots
    from poster.remote.protocol import b64

    source = graph_file(tmp_path, [("Files.WriteText", dict(path="must-not-exist.txt", text="not executed"))])
    store = Store(tmp_path / "queue.sqlite")
    try:
        from poster.dispatcher.security import key_setup

        key_setup(store.root)
        project_id, version = Snapshots(store.root).create(source)
        cipher, _ = prepare_snapshot(store, project_id, version)
        run_id, job_id = uuid.uuid4().hex, uuid.uuid4().hex
        packet = dict(
            key=session_key().decode(),
            root=str(store.root),
            job=dict(
                id=job_id, project_id=project_id, version=version, timeout=30, inputs_cipher=b64(cipher)
            ),
            execution=dict(run_id=run_id, job_id=job_id, attempt=1, owner="unit-owner"),
        )
        code = """
import asyncio,json,os,sys,time
from pathlib import Path
from poster.dispatcher.worker import execute
from poster.remote.worker import ExecutionAdapter,LeaseControls
packet=json.load(sys.stdin);os.environ['POSTER_SESSION_KEY']=packet.pop('key')
controls=LeaseControls(b'',time.time()+30);controls.request('CANCEL_REQUESTED')
adapter=ExecutionAdapter(Path(packet.pop('root')),packet,controls)
assert asyncio.run(execute(adapter,adapter.current,'unit-owner',controls))==1
"""
        process = subprocess.run(
            [sys.executable, "-c", code], input=json.dumps(packet), capture_output=True, text=True, timeout=30
        )
        assert process.returncode == 0
        report = json.loads((store.root / "runs" / run_id / "report.json").read_bytes())
        assert report["status"] == "CANCELLED" and report["code"] == "CANCEL_REQUESTED"
        assert report["start_rejection"]["cancel_requested"]
        assert not (Path(report["workdir"]) / "must-not-exist.txt").exists()
    finally:
        store.close()
