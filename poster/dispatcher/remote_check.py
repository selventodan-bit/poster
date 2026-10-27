"""An explicit harmless graph admitted through the existing queue, never a hidden RPC worker."""

import hashlib
import uuid

from .models import DispatcherError
from .security import private_dir


def enqueue_check(host, agent_id, *, browser=False):
    from poster.maker.example_projects import chain
    from poster.maker.storage import save_graph, Vault
    from poster.remote.registry import validate_target

    validate_target(host.store, agent_id)
    folder = private_dir(host.root / "connection-check" / uuid.uuid4().hex)
    source = folder / "connection-check.postergraph.json"
    # A fresh isolated source keeps this fixed diagnostic independent of old
    # sources opened/edited by the user and of the snapshot tree scanner.
    save_graph(
        source,
        chain(
            "Проверка Linux Agent",
            ([("Browser.ReadText", {"selector": "body"})] if browser else [])
            + [
                (
                    "Files.WriteText",
                    {
                        "path": "poster-remote-check.txt",
                        "text": "Poster remote connection OK",
                        "overwrite": False,
                    },
                )
            ],
        ),
        Vault(),
    )
    item = host.catalog.inspect(source)
    try:
        job = host.catalog.enqueue(
            item["id"],
            agent_id=agent_id,
            remote_outputs=["poster-remote-check.txt"],
            timeout=30,
            key="remote-check:" + uuid.uuid4().hex,
        )
    finally:
        host.catalog.remove([item["id"]])  # Keep internal diagnostics out of the user project catalog.
    host.start()
    return {"job": job, "browser_tested": browser}


def check_status(host, job):
    value = host.dispatch({"op": "show", "id": job})
    if not value.get("agent_id"):
        raise DispatcherError("PARAMETERS_INVALID")
    from pathlib import Path

    output = Path(value["workdir"]) / "poster-remote-check.txt" if value["workdir"] else None
    returned = (
        value["status"] == "SUCCEEDED"
        and output is not None
        and output.is_file()
        and output.read_bytes() == b"Poster remote connection OK"
    )
    return dict(
        job=job,
        status=value["status"],
        file_returned=returned,
        sha256=hashlib.sha256(output.read_bytes()).hexdigest() if returned else None,
        workdir=value["workdir"] if returned else None,
        agent_id=value["agent_id"],
    )
