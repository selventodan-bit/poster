"""Exact Windows-produced graph artifacts, Linux Controller + Linux Agent, real TLS.

This proves graph portability, NOT a simultaneous Windows/Linux controller link.
"""

from contextlib import nullcontext
import hashlib
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

from poster.dispatcher.host import request
from poster.maker.fixtures import serve_maker
from poster.maker.model import Graph, graph_secret_refs
from remote_linux_e2e import terminal

pytest_plugins = ["remote_linux_e2e"]


def origin(value):
    parsed = urlsplit(value)
    assert parsed.scheme == "http" and parsed.hostname == "127.0.0.1" and parsed.port and parsed.port >= 1024
    return parsed


def test_exact_windows_maker_graphs_through_remote_agent(remote):
    directory = Path(os.environ["POSTER_REMOTE_WINDOWS_DIR"]).resolve()
    browser = Path(os.environ["POSTER_REMOTE_BROWSER_GRAPH"]).resolve()
    cases = [
        (directory / "windows-http.postergraph.json", []),
        (directory / "windows-data.postergraph.json", ["lists/result.json"]),
        (directory / "windows-csv.postergraph.json", ["tables/output.csv", "tables/output.xlsx"]),
        (directory / "windows-parent.postergraph.json", ["nested/result.txt"]),
        (browser, []),
    ]
    for path, outputs in cases:
        raw = path.read_bytes()
        graph = Graph.model_validate_json(raw)
        assert not graph_secret_refs(graph) and not graph.secret_file
        context = nullcontext()
        if path == browser:
            base = graph.metadata["maker_demo"]
            address = origin(base)
            assert all(n.params["url"] == base + "/" for n in graph.nodes if n.type == "Browser.OpenURL")
            context = serve_maker(port=address.port)
        elif path.name.startswith("windows-http"):
            address = origin(graph.variables["api_base"].value)
            assert all(
                n.type == "HTTP.Get" and n.params["url"] == "{{api_base}}/api/data"
                for n in graph.nodes
                if n.type.startswith("HTTP.")
            )
            context = serve_maker(port=address.port)
        else:
            assert not any(n.type.startswith(("HTTP.", "Browser.")) for n in graph.nodes)
        with context as fixture:
            item = request(remote["controller"], "add", source=str(path))
            queued = request(
                remote["controller"],
                "enqueue_batch",
                items=[dict(id=item["id"], agent_id=remote["agent_id"], remote_outputs=outputs)],
                request_id=path.name,
            )
            assert not queued["errors"]
            result = terminal(remote, queued["jobs"][0])
            if result["status"] != "SUCCEEDED":
                print(
                    "::error title=Remote Windows graph result::"
                    + json.dumps(
                        dict(
                            file=path.name,
                            status=result["status"],
                            code=result["history"][0]["code"],
                            node_code=(result.get("report") or {}).get("node_error_code"),
                        )
                    ),
                    flush=True,
                )
            assert result["status"] == "SUCCEEDED" and result["report"]["remote_agent"] == remote["agent_id"]
            for output in outputs:
                assert (Path(result["workdir"]) / output).is_file()
            if path == browser:
                assert len(fixture[0].posts) == 1
        assert path.read_bytes() == raw
        print(
            "::notice title=Exact Windows graph on remote Linux::"
            + str(
                dict(
                    file=path.name,
                    sha256=hashlib.sha256(raw).hexdigest(),
                    source="actual frozen Windows Maker",
                    controller_os="Linux",
                    agent_os="Linux",
                    status="PASSED",
                )
            )
        )
