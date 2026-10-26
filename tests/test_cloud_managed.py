"""Managed Windows/Linux history is authoritative; Drive export is storage only."""

import asyncio

from test_cloud_runtime import cloud  # noqa: F401
from test_dispatcher_host import host  # noqa: F401
from poster.cloud.crypto import recipient, verify_result, decrypt_output
from poster.cloud.desktop import DesktopCloud
from poster.cloud.results import context
from poster.dispatcher.host import request
from poster.maker.example_projects import chain
from poster.maker.storage import Vault, save_graph


async def test_managed_copies_export_unique_encrypted_results_without_queue_changes(cloud, host, tmp_path):  # noqa: F811
    lib, _, _ = cloud
    root, _ = host
    facade = DesktopCloud(tmp_path / "cloud-desktop", files=lib.files)
    source = tmp_path / "managed.postergraph.json"
    graph = chain("Managed Drive", [("Files.WriteText", {"path": "out.txt", "text": "managed-outputs"})])
    save_graph(source, graph, Vault())
    published = await facade.publish(source, outputs=["out.txt"])
    opened = await facade.open(graph.id, published["version"])
    origin = {k: opened[k] for k in ("project_id", "version", "folder_id")}
    item = request(root, "add", source=opened["source"], cloud=origin)
    request(root, "settings", max_workers=2, max_per_project=2, timeout=30)
    batch = request(
        root,
        "enqueue_batch",
        items=[dict(id=item["id"], count=2, max_concurrent=2)],
        request_id="managed-exports",
    )
    assert not batch["errors"]
    request(root, "start")
    for _ in range(300):
        if all(request(root, "show", id=j)["status"] == "SUCCEEDED" for j in batch["jobs"]):
            break
        await asyncio.sleep(0.03)
    before = request(root, "jobs")
    result = await facade.sync_results(root)
    assert len(result["exported"]) == 2 and result["imported"] == []
    assert request(root, "jobs") == before
    descriptor = await lib.descriptor(await lib.find(graph.id))
    folders = [
        f
        async for f in lib.files.list(
            descriptor["folders"]["results"],
            folders=True,
            properties={"poster_result_mode": "MANAGED_EXPORT"},
        )
    ]
    assert len(folders) == 2
    identity = recipient(facade.store)
    manifests = []
    for folder in folders:
        signed = await lib.files.json(folder["properties"]["poster_result_manifest"])
        value = verify_result(identity.public(), signed)
        assert value["mode"] == "MANAGED_EXPORT" and value["project_version"] == published["version"]
        claim = await lib.files.json(value["claim_id"])
        raw = await lib.files.read(value["outputs"]["out.txt"]["id"])
        assert decrypt_output(identity, raw, context(claim, value["run_id"], "out.txt")) == b"managed-outputs"
        manifests.append(value["run_id"])
    assert len(set(manifests)) == 2
    assert (await facade.sync_results(root))["exported"] == result["exported"]
    assert request(root, "cloud_results") == []
