"""Explicit selection, pinned large resources and bounded data-only recovery."""

import zipfile

import pytest

from poster.cloud.errors import CloudError
from poster.cloud.package import PortableBundle
from poster.cloud.runtime import NotebookSession
from poster.cloud.results import ResultArchive
from poster.dispatcher.snapshots import Snapshots
from poster.dispatcher.store import Store
from poster.maker.example_projects import chain
from poster.maker.storage import Vault, save_graph
from poster.portability.blobs import FileBlob, CHUNK, capacity
from poster.portability.resources import preview, dependencies
from poster.portability.chunks import upload, download
from test_cloud_runtime import cloud  # noqa: F401


def large(path, megabytes=24):
    with path.open("wb") as stream:
        for _ in range(megabytes):
            stream.write(b"PORTABILITY_PUBLIC_DATA\n".ljust(CHUNK, b"."))
    return FileBlob.capture(path)


def test_directory_preview_dependencies_zip_bomb_and_capacity(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    selected = project / "data"
    selected.mkdir()
    (selected / "one.csv").write_bytes(b"n\n1\n")
    report = preview(project, [selected])
    assert report["files"][0]["name"] == "data/one.csv" and report["total_bytes"] == 4
    graph = chain(
        "Missing",
        [
            ("CSV.Read", {"path": "missing.csv"}),
            ("Files.Copy", {"source": "C:\\personal\\missing.txt", "destination": "out.txt"}),
        ],
    )
    assert {i["code"] for i in dependencies(graph, ["data/one.csv"])} == {
        "MISSING_DEPENDENCY",
        "ABSOLUTE_PATH",
    }
    (selected / "queue.key").write_text("private")
    with pytest.raises(CloudError, match="PRIVATE_RESOURCE"):
        preview(project, [selected])
    with zipfile.ZipFile(project / "bomb.zip", "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("huge.txt", b"x" * (2 * CHUNK))
    with pytest.raises(CloudError):
        preview(project, [project / "bomb.zip"])
    with pytest.raises(CloudError, match="MEMORY_LIMIT"):
        capacity(tmp_path, CHUNK, memory=0)
    with pytest.raises(CloudError, match="DISK_LIMIT"):
        capacity(tmp_path, CHUNK, disk=0)


async def test_large_pinned_resource_original_worker_and_encrypted_result(cloud, tmp_path):  # noqa: F811
    library, api, recipient = cloud
    folder = tmp_path / "project"
    folder.mkdir()
    resource = large(folder / "large.bin")
    graph = chain("Large copy", [("Files.Copy", {"source": "large.bin", "destination": "out.bin"})])
    source = folder / "project.postergraph.json"
    save_graph(source, graph, Vault())
    bundle = PortableBundle.capture(
        source, resources=["large.bin"], outputs=["out.bin"], recipient=recipient.public()
    )
    assert bundle.manifest["schema_version"] == 2
    published = await library.publish(bundle, name=graph.name, recipient=recipient.public())
    fetched = await library.fetch(bundle.project_id, bundle.version)
    assert isinstance(fetched.files["large.bin"], FileBlob)
    assert fetched.files["large.bin"].sha256 == resource.sha256
    # Native immutable snapshots own separate private copies, not paths to a moving source.
    materialized = fetched.materialize(tmp_path / "materialized")
    snapshots = Snapshots(tmp_path / "native")
    _, version = snapshots.create(materialized)
    (materialized.parent / "large.bin").write_bytes(b"changed")
    _, files = snapshots.read(version)
    assert files["large.bin"].sha256 == resource.sha256
    session = NotebookSession(library, tmp_path / "notebook")
    try:
        ticket = await session.submit(bundle.project_id, bundle.version, intent="large-once")
        original_upload = library.files.upload
        interrupted_output = False

        async def once(*args, **kwargs):
            nonlocal interrupted_output
            if args[1].endswith(".box") and not interrupted_output:
                interrupted_output = True
                raise CloudError("DRIVE_UNAVAILABLE")
            return await original_upload(*args, **kwargs)

        library.files.upload = once
        with pytest.raises(CloudError, match="DRIVE_UNAVAILABLE"):
            await session.wait(ticket, deadline=120)
        library.files.upload = original_upload
        proof = await session.wait(ticket, deadline=120)
        assert proof["runs"][0]["status"] == "SUCCEEDED", proof
        assert proof["runs"][0]["pid"]
        result = await library.files.json(ticket.claim["runs"][0][2])
        entry = result["outputs"]["out.bin"]
        assert entry["encrypted"] and len(entry["parts"]) == 24
        assert all(len(api.files[p["id"]]["raw"]) < 2 * CHUNK for p in entry["parts"])
        store = Store(tmp_path / "archive.sqlite")
        try:
            imported = await ResultArchive(store).import_result(library, ticket.claim, 0, recipient)
            assert (
                FileBlob.capture(__import__("pathlib").Path(imported["directory"]) / "out.bin").sha256
                == resource.sha256
            )
            again = await ResultArchive(store).import_result(library, ticket.claim, 0, recipient)
            assert again["already_imported"]
        finally:
            store.close()
        assert (await session.submit(bundle.project_id, bundle.version, intent="large-once")) is ticket
        assert len(ticket.jobs) == 1
        assert (await library.publish(bundle, name=graph.name, expected_version=bundle.version))["unchanged"]
        assert published["version"] == bundle.version
    finally:
        await session.close()


async def test_chunk_interruption_resume_corruption_and_create_only(cloud, tmp_path, monkeypatch):  # noqa: F811
    library, api, recipient = cloud
    source = large(tmp_path / "input.bin", 4)
    parent = (await library.files.folder("parts"))["id"]
    manifest_id = (await library.files.ids())[0]
    original, calls = library.files.upload, []

    async def interrupted(*args, **kwargs):
        calls.append(args[0])
        if len(calls) == 3:
            raise CloudError("DRIVE_UNAVAILABLE")
        return await original(*args, **kwargs)

    monkeypatch.setattr(library.files, "upload", interrupted)
    with pytest.raises(CloudError, match="DRIVE_UNAVAILABLE"):
        await upload(
            library.files,
            source,
            parent,
            "resume-output",
            recipient=recipient.public(),
            context=dict(run_id="one"),
            publication_id=manifest_id,
        )
    assert await library.files.info(manifest_id) is None
    monkeypatch.setattr(library.files, "upload", original)
    entry = await upload(
        library.files,
        source,
        parent,
        "resume-output",
        recipient=recipient.public(),
        context=dict(run_id="one"),
        publication_id=manifest_id,
    )
    assert len({p["id"] for p in entry["parts"]}) == 4
    api.corrupt_download = entry["parts"][2]["id"]
    with pytest.raises(CloudError, match="INTEGRITY"):
        await download(
            library.files, entry, tmp_path / "result.bin", recipient=recipient, context=dict(run_id="one")
        )
    assert not (tmp_path / "result.bin").exists()
    api.corrupt_download = None
    restored = await download(
        library.files, entry, tmp_path / "result.bin", recipient=recipient, context=dict(run_id="one")
    )
    assert restored.sha256 == source.sha256
    with pytest.raises(CloudError):
        await download(
            library.files,
            entry,
            tmp_path / "other.bin",
            recipient=recipient,
            context=dict(run_id="different"),
        )
    (tmp_path / "result.bin").write_bytes(b"other run")
    with pytest.raises(CloudError, match="INTEGRITY"):
        await download(
            library.files, entry, tmp_path / "result.bin", recipient=recipient, context=dict(run_id="one")
        )
    assert b"PORTABILITY_PUBLIC_DATA" not in library.files.journal.path.read_bytes()


def test_stream_copy_large_file_has_bounded_process_memory(tmp_path):
    import subprocess
    import sys
    import json

    path = tmp_path / "data.bin"
    large(path, 64)
    code = """
import json,sys,threading,time
from pathlib import Path
import psutil
from poster.portability.blobs import FileBlob,copy_blob
peak=[0]; done=threading.Event()
def sample():
 while not done.is_set():
  peak[0]=max(peak[0],psutil.Process().memory_info().rss)
  done.wait(0.005)
thread=threading.Thread(target=sample);thread.start()
try:
 source=FileBlob.capture(Path(sys.argv[1])); target=copy_blob(source,Path(sys.argv[2])); assert source.sha256==target.sha256
finally:
 done.set();thread.join()
print(json.dumps(dict(size=len(target),peak_rss=peak[0])))
"""
    value = subprocess.run(
        [sys.executable, "-c", code, str(path), str(tmp_path / "copy.bin")],
        check=True,
        capture_output=True,
        text=True,
    )
    result = json.loads(value.stdout)
    assert result["size"] == 64 * CHUNK and result["peak_rss"] < 100 * CHUNK, result
