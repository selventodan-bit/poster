"""One bounded notebook adapter around the ORIGINAL Dispatcher and GraphRunner.

Drive is project/result storage and a create-once permission gate. It is never a
managed queue, lease store, Relay, or remote controller. Windows can be offline.
"""

import asyncio
from dataclasses import dataclass, field
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

from poster.dispatcher.host import request
from poster.dispatcher.models import DispatcherError
from poster.dispatcher.security import private_dir
from poster.remote.protocol import Identity
from . import admission
from .capabilities import Lifecycle, probe
from .errors import CloudError
from .results import publish_result, TERMINAL


@dataclass
class Ticket:
    intent: str
    claim: dict
    descriptor: dict
    bundle: object
    jobs: list[str]
    arguments: dict = field(default_factory=dict, repr=False)
    results: dict = field(default_factory=dict)
    finished: bool = False


class NotebookSession:
    def __init__(self, library, root, *, provider="generic", capacity=None):
        self.library = library
        self.root = private_dir(Path(root) / uuid.uuid4().hex)
        self.provider = provider
        self.capacity = capacity or probe(provider, root=self.root)
        self.identity = Identity()
        import re

        candidate = os.environ.get("POSTER_CLOUD_SOURCE_COMMIT", os.environ.get("GITHUB_SHA", ""))
        self.code_commit = candidate if re.fullmatch("[a-f0-9]{40}", candidate) else "UNAVAILABLE"
        self.process = None
        self.lifecycle = Lifecycle.READY
        self.intents, self.blocked = {}, set()
        self.gate = asyncio.Lock()

    async def _rpc(self, op, **kwargs):
        return await asyncio.to_thread(request, self.root / "queue", op, **kwargs)

    async def start(self):
        if self.process:
            if self.process.poll() is not None:
                self.lifecycle = Lifecycle.EXPIRED
                raise CloudError("RUNTIME_UNAVAILABLE")
            return
        self.lifecycle = Lifecycle.STARTING
        env = {
            k: v
            for k, v in os.environ.items()
            if k
            in {
                "PATH",
                "SYSTEMROOT",
                "WINDIR",
                "TEMP",
                "TMP",
                "HOME",
                "USERPROFILE",
                "LOCALAPPDATA",
                "PLAYWRIGHT_BROWSERS_PATH",
                "LANG",
                "LC_ALL",
                "LD_LIBRARY_PATH",
                "SSL_CERT_FILE",
                "SSL_CERT_DIR",
                "POSTER_BROWSER_EXECUTABLE",
                "POSTER_CHROMIUM",
            }
        }
        env["PYTHONUTF8"] = "1"
        # No provider OAuth/ADC/API tokens or inherited queue/session keys.
        self.process = subprocess.Popen(
            [sys.executable, "-m", "poster.cloud.supervisor", str(self.root / "queue")],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
        )
        for _ in range(200):
            if self.process.poll() is not None:
                self.lifecycle = Lifecycle.UNAVAILABLE
                raise CloudError("RUNTIME_UNAVAILABLE")
            try:
                await self._rpc("ping")
                await self._rpc(
                    "settings",
                    max_workers=self.capacity.max_parallel,
                    max_per_project=self.capacity.max_parallel,
                    timeout=300,
                )
                self.lifecycle = Lifecycle.AVAILABLE
                return
            except DispatcherError:
                await asyncio.sleep(0.05)
        await self.close()
        raise CloudError("RUNTIME_UNAVAILABLE")

    async def catalog(self):
        return [p async for p in self.library.projects()]

    async def versions(self, project_id):
        folder = await self.library.find(project_id)
        if folder is None:
            raise CloudError("INTEGRITY")
        history, _ = await self.library.versions(await self.library.descriptor(folder))
        return history

    async def submit(
        self,
        project_id,
        version=None,
        *,
        intent=None,
        count=1,
        parallel=1,
        inputs=None,
        secrets=None,
        secret_refs=None,
        timeout=300,
    ):
        if intent is None:
            raise CloudError("INPUT_INVALID")  # UI explicitly prepares a new intent.
        async with self.gate:
            arguments = dict(
                project_id=project_id,
                version=version,
                count=count,
                parallel=parallel,
                inputs=inputs or {},
                secrets=secrets or {},
                secret_refs=secret_refs or {},
                timeout=timeout,
            )
            if intent in self.intents:
                if self.intents[intent].arguments != arguments:
                    raise CloudError("INPUT_INVALID")
                return self.intents[intent]
            if intent in self.blocked:
                raise CloudError("RUN_UNCERTAIN")
            bundle = await self.library.fetch(project_id, version)
            self.capacity.validate(bundle, count, parallel)
            if bundle.manifest.get("result_recipient") is None:
                raise CloudError("RECIPIENT_REQUIRED")
            await self.start()
            source = bundle.materialize(self.root / "projects" / uuid.uuid4().hex)
            item = await self._rpc("add", source=str(source))
            spec = dict(
                id=item["id"],
                count=count,
                max_concurrent=parallel,
                inputs=inputs or {},
                secrets=secrets or {},
                secret_refs=secret_refs or {},
                timeout=timeout,
            )
            # Validate all protected and ordinary inputs before any permission
            # is consumed. This operation never enqueues or executes a graph.
            await self._rpc("validate_batch", items=[spec])
            descriptor = await self.library.descriptor(await self.library.find(project_id))
            self.blocked.add(intent)
            claim = await admission.acquire(
                self.library,
                descriptor,
                bundle,
                intent,
                self.identity,
                count,
                parallel,
                code_commit=self.code_commit,
            )
            reply = await self._rpc("enqueue_batch", items=[spec], request_id=intent)
            if reply["errors"] or len(reply["jobs"]) != count:
                self.lifecycle = Lifecycle.UNKNOWN
                raise CloudError("RUN_UNCERTAIN")
            from copy import deepcopy

            ticket = Ticket(intent, claim, descriptor, bundle, reply["jobs"], arguments=deepcopy(arguments))
            self.intents[intent] = ticket
            await self._rpc("start")
            self.lifecycle = Lifecycle.EXECUTING
            return ticket

    async def refresh(self, ticket):
        if not self.process or self.process.poll() is not None:
            self.lifecycle = Lifecycle.EXPIRED
            raise CloudError("RUNTIME_UNAVAILABLE")
        rows = []
        for index, job in enumerate(ticket.jobs):
            execution = await self._rpc("show", id=job)
            state = execution["status"]
            rows.append(
                dict(
                    run_id=ticket.claim["runs"][index][0],
                    status=state,
                    pid=(execution["history"][0]["pid"] if execution["history"] else None),
                )
            )
            if state in TERMINAL and index not in ticket.results:
                ticket.results[index] = await publish_result(
                    self.library,
                    ticket.descriptor,
                    ticket.claim,
                    index,
                    self.identity,
                    execution,
                    ticket.bundle,
                )
        if len(ticket.results) == len(ticket.jobs) and not ticket.finished:
            await admission.finish(
                self.library,
                ticket.descriptor,
                ticket.claim,
                self.identity,
                [ticket.results[i] for i in range(len(ticket.jobs))],
            )
            ticket.finished = True
        if all(t.finished for t in self.intents.values()):
            self.lifecycle = Lifecycle.FINISHED
        return dict(
            mode="DETACHED", lifecycle=self.lifecycle.value, runs=rows, results_committed=ticket.finished
        )

    async def wait(self, ticket, *, deadline=300):
        until = time.monotonic() + deadline
        while time.monotonic() < until:
            state = await self.refresh(ticket)
            if ticket.finished:
                return state
            await asyncio.sleep(0.05)
        raise CloudError("RUNTIME_UNAVAILABLE")

    async def cancel(self, ticket, index=None):
        await self._rpc("cancel", ids=ticket.jobs if index is None else [ticket.jobs[index]])

    async def close(self):
        self.lifecycle = Lifecycle.STOPPING
        if self.process:
            if self.process.stdin:
                self.process.stdin.close()
            try:
                await asyncio.to_thread(self.process.wait, timeout=15)
            except subprocess.TimeoutExpired:
                from poster.dispatcher.processes import terminate_tree
                import psutil

                terminate_tree(self.process.pid, psutil.Process(self.process.pid).create_time())
                await asyncio.to_thread(self.process.wait, timeout=5)
        self.lifecycle = Lifecycle.EXPIRED
