"""Fault injection around real GraphRunner HTTP execution, never a fake runner."""

import json
from pathlib import Path
from types import SimpleNamespace
import sys

import psutil
import pytest

from poster.dispatcher import worker
from poster.dispatcher.service import Dispatcher, enqueue
from poster.dispatcher.store import Store
from poster.maker.example_projects import chain
from poster.maker.storage import Vault, save_graph
from test_dispatcher_service import controlled_server

CANARY = "PRIVATE_WORKER_EXCEPTION_4B1_CANARY"


@pytest.mark.parametrize("fault", ["before_graph", "post_inflight", "result_serialization"])
async def test_unexpected_failure_uses_durable_effects_and_never_retries(tmp_path, capsys, fault):
    injected = pytest.MonkeyPatch()
    # execute() normally owns a one-shot worker process. Fault injection runs
    # it in pytest, so its import guard must not escape into unrelated tests.
    injected.setattr(sys, "meta_path", list(sys.meta_path))
    with controlled_server() as (server, base):
        source = tmp_path / "source" / "main.postergraph.json"
        source.parent.mkdir()
        graph = chain("Fault regression", [("HTTP.Post", {"url": base + "/hold/0"})])
        save_graph(source, graph, Vault())
        store = Store(tmp_path / "dispatcher.sqlite")
        job = enqueue(store, source, key="one-publication")
        execution = store.claim("fault-owner", 1, 1)
        store.record_process(
            execution["run_id"], "fault-owner", psutil.Process().pid, psutil.Process().create_time()
        )
        controls = SimpleNamespace(reason=None)

        def fail(*args, **kwargs):
            raise RuntimeError(CANARY)

        if fault == "before_graph":
            injected.setattr(worker.Snapshots, "materialize", fail)
        elif fault == "result_serialization":
            server.gates["0"].set()
            injected.setattr(worker, "seal", fail)
        else:

            class FaultControls:
                raised = False

                @property
                def reason(self):
                    if server.calls and not self.raised:
                        self.raised = True
                        raise RuntimeError(CANARY)
                    return None

            controls = FaultControls()
        try:
            assert await worker.execute(store, execution, "fault-owner", controls) == 1
            expected = "FAILED" if fault == "before_graph" else "UNKNOWN"
            assert store.show(job)["status"] == expected
            assert len(server.calls) == (0 if fault == "before_graph" else 1)
            report = json.loads(Path(store.history(job)[0]["report"]).read_bytes())
            assert report["code"] == "WORKER_INTERNAL_ERROR"
            assert CANARY not in json.dumps(report) and CANARY not in capsys.readouterr().out
            assert CANARY.encode() not in store.path.read_bytes()
        finally:
            server.gates["0"].set()
            injected.undo()
        Dispatcher(store).reconcile()
        Dispatcher(store).run(once=True)
        assert enqueue(store, source, key="one-publication") == job
        assert len(server.calls) == (0 if fault == "before_graph" else 1)
        assert store.show(job)["attempt"] == 1
        store.close()
