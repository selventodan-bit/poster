"""Actual native subprocesses + Drive protocol fixture; no live provider claim."""

import asyncio
import json
from pathlib import Path
import uuid

from cryptography.fernet import Fernet
import httpx
import pytest

from cloud_drive_fixture import DriveFixture, fixture_token
from poster.cloud.admission import claims, completed, resolve
from poster.cloud.capabilities import probe
from poster.cloud.drive_files import DriveFiles, Journal
from poster.cloud.errors import CloudError
from poster.cloud.library import Library
from poster.cloud.package import PortableBundle
from poster.cloud.results import ResultArchive
from poster.cloud.runtime import NotebookSession
from poster.dispatcher.security import key_setup
from poster.dispatcher.models import DispatcherError
from poster.dispatcher.store import Store
from poster.maker.example_projects import chain
from poster.maker.model import Variable
from poster.maker.storage import Vault, save_graph
from poster.remote.protocol import Identity
from poster.storage.drive import Drive
from test_dispatcher_service import controlled_server


@pytest.fixture
async def cloud(tmp_path, monkeypatch):
    monkeypatch.delenv("POSTER_SESSION_KEY", raising=False)
    key_setup(tmp_path / "desktop")
    api = DriveFixture()
    async with httpx.AsyncClient(
        base_url="https://www.googleapis.com/drive/v3/", transport=httpx.MockTransport(api)
    ) as client:
        files = DriveFiles(
            Drive("", tmp_path / "cache", fixture_token, client=client),
            Journal(tmp_path / "transport", key=Fernet.generate_key()),
        )
        yield Library(files), api, Identity()


async def publish(
    cloud, tmp_path, actions, *, variables=None, resources=None, outputs=("out.txt",), protected=False
):
    library, _, recipient = cloud
    directory = tmp_path / uuid.uuid4().hex
    directory.mkdir()
    graph = chain("Notebook Ж", actions)
    graph.variables.update(variables or {})
    vault = Vault()
    if protected:
        graph.variables["password"] = Variable(
            secret=True, value=vault.put_value("CLOUD_PRIVATE_CANARY", "protected-ref")
        )
    source = directory / "project.postergraph.json"
    save_graph(source, graph, vault)
    for name, raw in (resources or {}).items():
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    bundle = PortableBundle.capture(
        source, resources=list(resources or {}), outputs=list(outputs), recipient=recipient.public()
    )
    await library.publish(bundle, name=graph.name, recipient=recipient.public())
    return bundle


async def test_real_graphrunner_detached_resources_inputs_encrypted_output_import_and_duplicate(
    cloud, tmp_path
):
    lib, api, recipient = cloud
    bundle = await publish(
        cloud,
        tmp_path,
        [
            ("Files.ReadText", {"path": "data.json"}),
            ("Files.WriteText", {"path": "out.txt", "text": "{{label}}"}),
        ],
        resources={"data.json": b'{"n":23}'},
        variables={"label": Variable(input=True, required=True)},
        protected=True,
    )
    session = NotebookSession(lib, tmp_path / "notebook")
    try:
        args = dict(
            intent="stable-click",
            inputs={"label": "autonomous"},
            secret_refs={"protected-ref": "CLOUD_PRIVATE_CANARY"},
        )
        with pytest.raises(DispatcherError, match="вход"):
            await session.submit(bundle.project_id, intent="bad-input")
        descriptor = await lib.descriptor(await lib.find(bundle.project_id))
        assert (await claims(lib, descriptor))[0] == []  # validation consumed no permission
        ticket = await session.submit(bundle.project_id, **args)
        assert await session.submit(bundle.project_id, **args) is ticket
        state = await session.wait(ticket, deadline=30)
        assert state["runs"][0]["status"] == "SUCCEEDED", state
        assert state["runs"][0]["pid"] != session.process.pid
        assert (await completed(lib, ticket.claim))["results"][0]["status"] == "SUCCEEDED"
        # Windows/desktop did not exist during execution. Import into its own
        # SQLite history now, without changing any managed job.
        store = Store(tmp_path / "desktop" / "dispatcher.sqlite")
        archive = ResultArchive(store)
        try:
            result = await archive.import_result(lib, ticket.claim, 0, recipient)
            assert (Path(result["directory"]) / "out.txt").read_text() == "autonomous"
            assert (await archive.import_result(lib, ticket.claim, 0, recipient))["already_imported"]
            assert archive.rows()[0]["mode"] == "DETACHED"
            assert store.db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0
        finally:
            store.close()
        public = json.dumps(api.files, default=str).encode()
        assert b"CLOUD_PRIVATE_CANARY" not in public and b"autonomous" not in public
    finally:
        await session.close()


async def test_two_sessions_cannot_replay_claim_and_provider_limit_is_real(cloud, tmp_path):
    lib, _, _ = cloud
    bundle = await publish(cloud, tmp_path, [("Files.WriteText", {"path": "out.txt", "text": "yes"})])
    one = NotebookSession(lib, tmp_path / "one")
    two = NotebookSession(lib, tmp_path / "two", provider="colab")
    try:
        with pytest.raises(CloudError, match="CAPABILITY"):
            await two.submit(bundle.project_id, intent="unsupported", count=2, parallel=2)
        ticket = await one.submit(bundle.project_id, intent="write-once")
        with pytest.raises(CloudError, match="RUN_ALREADY_CLAIMED"):
            await two.submit(bundle.project_id, intent="write-once")
        await one.wait(ticket, deadline=30)
    finally:
        await one.close()
        await two.close()


async def test_five_independent_native_processes_cancel_one_and_concurrent_results(cloud, tmp_path):
    lib, _, _ = cloud
    with controlled_server() as (server, base):
        bundle = await publish(
            cloud,
            tmp_path,
            [
                ("HTTP.Post", {"url": base + "/hold/0"}),
                ("Files.WriteText", {"path": "out.txt", "text": "independent"}),
            ],
        )
        capacity = probe()
        assert capacity.max_parallel >= 5, capacity
        session = NotebookSession(lib, tmp_path / "five", capacity=capacity)
        try:
            ticket = await session.submit(bundle.project_id, intent="five", count=5, parallel=5)
            for _ in range(300):
                if len(server.calls) == 5:
                    break
                await asyncio.sleep(0.05)
            assert len(server.calls) == 5
            state = await session.refresh(ticket)
            assert len({r["pid"] for r in state["runs"]}) == 5
            await session.cancel(ticket, 0)
            await asyncio.sleep(0.2)
            server.gates["0"].set()
            state = await session.wait(ticket, deadline=30)
            assert state["runs"][0]["status"] in {"CANCELLED", "UNKNOWN"}
            assert all(r["status"] == "SUCCEEDED" for r in state["runs"][1:]), state
            assert len({r[1] for r in ticket.claim["runs"]}) == 5
            assert len(server.calls) == 5
        finally:
            await session.close()


async def test_session_crash_unknown_blocks_new_post_until_manual_review(cloud, tmp_path):
    lib, _, _ = cloud
    with controlled_server() as (server, base):
        bundle = await publish(cloud, tmp_path, [("HTTP.Post", {"url": base + "/hold/0"})])
        one = NotebookSession(lib, tmp_path / "crash")
        two = NotebookSession(lib, tmp_path / "reopened")
        try:
            ticket = await one.submit(bundle.project_id, intent="unsafe-once")
            for _ in range(300):
                if server.calls:
                    break
                await asyncio.sleep(0.05)
            assert len(server.calls) == 1
            one.process.kill()
            one.process.wait(10)
            with pytest.raises(CloudError, match="RUNTIME_UNAVAILABLE"):
                await one.refresh(ticket)
            with pytest.raises(CloudError, match="RUN_UNCERTAIN"):
                await two.submit(bundle.project_id, intent="new-click")
            assert len(server.calls) == 1
            with pytest.raises(CloudError):
                await resolve(lib, ticket.claim)
            await resolve(lib, ticket.claim, explicitly_reviewed=True)
            # Operator can authorize a new intent only after reviewing effects;
            # this test deliberately does not send a second POST.
        finally:
            server.gates["0"].set()
            await one.close()
            await two.close()
