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
