"""Drive protocol fixtures + real package/filesystem boundaries; no Google account claims."""

import asyncio
import json

import httpx
import pytest

from cloud_drive_fixture import DriveFixture, fixture_token
from poster.cloud.drive_files import DriveFiles, Journal
from poster.cloud.errors import CloudError
from poster.cloud.format import relative
from poster.cloud.library import Library
from poster.cloud.package import PortableBundle
from poster.dispatcher.security import key_setup
from poster.maker.example_projects import chain
from poster.maker.model import Variable
from poster.maker.storage import Vault, save_graph
from poster.storage.drive import Drive


def project(tmp_path, *, name="Общий проект", text="one"):
    folder = tmp_path / text
    folder.mkdir()
    graph = chain(name, [("Files.WriteText", {"path": "out.txt", "text": text})])
    source = folder / "project.postergraph.json"
    save_graph(source, graph, Vault())
    return source, graph


@pytest.fixture
async def library(tmp_path, monkeypatch):
    monkeypatch.delenv("POSTER_SESSION_KEY", raising=False)
    key_setup(tmp_path / "private")
    api = DriveFixture()
    async with httpx.AsyncClient(
        base_url="https://www.googleapis.com/drive/v3/", transport=httpx.MockTransport(api)
    ) as client:
        transport = Drive("", tmp_path / "cache", fixture_token, client=client)
        files = DriveFiles(transport, Journal(tmp_path / "private"))
        yield Library(files), api


async def test_root_multiple_projects_duplicate_names_rename_pagination_and_versions(library, tmp_path):
    lib, api = library
    sources = [project(tmp_path, text=str(i))[0] for i in range(4)]
    first = PortableBundle.capture(sources[0], outputs=["out.txt"])
    published = await lib.publish(first, name="Общий проект")
    assert (await lib.publish(first, name="Общий проект"))["unchanged"]
    for source in sources[1:]:
        await lib.publish(PortableBundle.capture(source, outputs=["out.txt"]), name="Общий проект")
    rows = [p async for p in lib.projects()]
    assert len(rows) == 4 and len({p["project_id"] for p in rows}) == 4
    assert (
        len([f for f in api.files.values() if f["meta"].get("properties", {}).get("poster_root") == "v1"])
        == 1
    )
    assert any("pageToken=" in u for _, u in api.requests)
    await lib.rename(first.project_id, "Переименован вручную")
    assert (
        next(p for p in [p async for p in lib.projects()] if p["project_id"] == first.project_id)["name"]
        == "Переименован вручную"
    )
    graph, vault = __import__("poster.maker.storage", fromlist=["load_graph"]).load_graph(sources[0])
    graph.name = "Новая версия"
    save_graph(sources[0], graph, vault)
    second = PortableBundle.capture(sources[0], outputs=["out.txt"])
    await lib.publish(second, name=graph.name, expected_version=first.version)
    assert (await lib.fetch(first.project_id, first.version)).files == first.files
    assert (await lib.fetch(first.project_id)).version == second.version
    with pytest.raises(CloudError, match="DRIVE_CONFLICT"):
        await lib.publish(first, name="old", expected_version=first.version)
    assert (await lib.fetch(first.project_id)).version == second.version
    assert published["folder_id"] == (await lib.find(first.project_id))["id"]


async def test_resumable_upload_lost_ack_and_hash_integrity(library, tmp_path):
    lib, api = library
    parent = (await lib.files.folder("resources"))["id"]
    file_id = (await lib.files.ids())[0]
    raw = b"a" * (2 * 1024 * 1024 + 5)
    api.drop_chunk_once = True
    await lib.files.upload(file_id, "data.csv", parent, raw)
    assert await lib.files.read(file_id) == raw
    assert len([f for f in api.files.values() if f["meta"].get("name") == "data.csv"]) == 1
    assert b"upload_id=" not in lib.files.journal.path.read_bytes()
    api.corrupt_download = file_id
    with pytest.raises(CloudError, match="INTEGRITY"):
        await lib.files.read(file_id)


async def test_ambiguous_execution_claim_never_retries_create(library):
    lib, api = library
    parent = (await lib.files.folder("claims"))["id"]
    claim = (await lib.files.ids())[0]
    api.drop_claim_once = True
    with pytest.raises(CloudError, match="RUN_UNCERTAIN"):
        await lib.files.create_json(claim, "claim.json", parent, {"format": "claim"}, strict_new=True)
    assert claim in api.files
    with pytest.raises(CloudError, match="RUN_ALREADY_CLAIMED"):
        await lib.files.create_json(claim, "claim.json", parent, {"format": "claim"}, strict_new=True)
    assert len([f for f in api.files.values() if f["meta"].get("name") == "claim.json"]) == 1


async def test_rate_limit_uses_existing_transport_and_bounded_backoff(library, monkeypatch):
    lib, api = library
    sleeps = []

    async def sleep(value):
        sleeps.append(value)

    monkeypatch.setattr("poster.storage.drive.asyncio.sleep", sleep)
    api.quota_once = True
    assert len(await lib.files.ids(2)) == 2
    assert sleeps == [1]


def test_portable_resources_immutable_hashes_and_no_private_sidecars(tmp_path):
    source, graph = project(tmp_path)
    (source.parent / "data.csv").write_text("n\n17\n")
    graph.variables["password"] = Variable(value=None, input=True, required=True, secret=True)
    vault = Vault()
    graph.nodes[1].params["text"] = vault.put("PRIVATE_CAPTURE_CANARY")
    save_graph(source, graph, vault)
    bundle = PortableBundle.capture(source, resources=["data.csv"], outputs=["out.txt"])
    assert all(not n.endswith(".enc") for n in bundle.files)
    assert b"PRIVATE_CAPTURE_CANARY" not in b"".join(bundle.files.values())
    assert bundle.manifest["required_secret_refs"]
    copy = bundle.materialize(tmp_path / "copy")
    assert (copy.parent / "data.csv").read_text() == "n\n17\n"
    assert json.loads(copy.read_bytes())["secret_file"] is None
    altered = PortableBundle(bundle.manifest, {**bundle.files, "data.csv": b"CORRUPTED"})
    with pytest.raises(CloudError, match="INTEGRITY"):
        altered.verify()
    with pytest.raises(TypeError):
        bundle.files["data.csv"] = b"CORRUPTED"


@pytest.mark.parametrize(
    "name",
    [
        "../outside.csv",
        "C:/private.csv",
        "link/../file.csv",
        "x\\y.csv",
        "CON.txt",
        "a//b.csv",
        "/etc/passwd",
        "x:stream",
    ],
)
def test_portable_rejects_nonportable_paths(name):
    with pytest.raises(CloudError):
        relative(name)


async def test_two_publishers_same_commit_slot_have_one_winner(library, tmp_path, monkeypatch):
    lib, api = library
    source, graph = project(tmp_path)
    initial = PortableBundle.capture(source)
    await lib.publish(initial, name=graph.name)
    branches = []
    from poster.maker.storage import load_graph

    for i in range(2):
        graph, vault = load_graph(source)
        graph.name = f"writer {i}"
        save_graph(source, graph, vault)
        branches.append(PortableBundle.capture(source))
    other_files = DriveFiles(lib.files.transport, Journal(tmp_path / "other-client"))
    other = Library(other_files, root_id=lib.root_id)
    barrier = asyncio.Barrier(2)
    for files in (lib.files, other_files):
        original = files.create_json

        async def create(*args, _original=original, **kwargs):
            if args[1] == "commit.json":
                await barrier.wait()
            return await _original(*args, **kwargs)

        monkeypatch.setattr(files, "create_json", create)
    result = await asyncio.gather(
        lib.publish(branches[0], name="writer 0", expected_version=initial.version),
        other.publish(branches[1], name="writer 1", expected_version=initial.version),
        return_exceptions=True,
    )
    assert len([v for v in result if isinstance(v, dict)]) == 1
    errors = [v for v in result if isinstance(v, CloudError)]
    assert len(errors) == 1 and errors[0].code == "DRIVE_CONFLICT"
    history, _ = await lib.versions(await lib.descriptor(await lib.find(initial.project_id)))
    assert len(history) == 2 and history[-1]["version"] in {b.version for b in branches}
    assert (await lib.fetch(initial.project_id, initial.version)).files == initial.files


def test_portable_does_not_publish_unrelated_graphs_or_symlink_resources(tmp_path):
    source, graph = project(tmp_path)
    other = chain("Другой проект", [("Files.WriteText", {"path": "x", "text": "do not publish"})])
    save_graph(source.parent / "other.postergraph.json", other, Vault())
    bundle = PortableBundle.capture(source)
    assert list(bundle.files) == [source.name]
    outside = tmp_path / "outside.csv"
    outside.write_text("private")
    (source.parent / "linked.csv").symlink_to(outside)
    with pytest.raises(ValueError):
        PortableBundle.capture(source, resources=["linked.csv"])
