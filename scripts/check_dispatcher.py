"""Public CLI -> durable queue -> real GraphRunner subprocesses; safe CI report only."""

import argparse
from contextlib import nullcontext
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from urllib.parse import urlsplit

from poster.maker.example_projects import EXAMPLES, source_directory
from poster.maker.fixtures import serve_maker
from poster.maker.model import Graph, graph_secret_refs


def cli(directory, *arguments):
    process = subprocess.run(
        [sys.executable, "-m", "poster", "dispatcher", *map(str, arguments)],
        cwd=directory,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if process.returncode:
        # Never forward unfiltered subprocess output into CI annotations.
        raise AssertionError("Dispatcher CLI failed: " + str(process.returncode))
    return json.loads(process.stdout)


def execute(path, output, *, inputs=()):
    output.mkdir(parents=True, exist_ok=False)
    db = output / "dispatcher.sqlite"
    options = []
    for value in inputs:
        options += ["--input", value]
    raw = path.read_bytes()
    queued = cli(output, "enqueue", path, "--db", db, "--key", "controlled-demo", *options)
    assert (
        cli(output, "enqueue", path, "--db", db, "--key", "controlled-demo", *options)["id"] == queued["id"]
    )
    cli(output, "run", "--db", db, "--once", "--max-workers", 2)
    result = cli(output, "show", queued["id"], "--db", db, "--report")
    assert result["status"] == "SUCCEEDED" and result["attempt"] == 1
    assert len(cli(output, "history", queued["id"], "--db", db)["executions"]) == 1
    report = result["report"]
    assert report["gui_imports_forbidden"]
    assert path.read_bytes() == raw
    return report, {
        "graph": path.name,
        "status": "PASSED",
        "sha256": hashlib.sha256(raw).hexdigest(),
        "same_graph_bytes": True,
        "worker_id": report["worker_id"],
        "browser_imports_forbidden": report["browser_imports_forbidden"],
    }


def controlled_origin(value):
    origin = urlsplit(value)
    assert origin.scheme == "http" and origin.hostname == "127.0.0.1" and origin.port > 1024
    assert not origin.username and not origin.password and not origin.query
    return origin


def check(output, windows_dir=None, browser_graph=None):
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    cases = []
    with serve_maker() as (_, base):
        for i, (_, name) in enumerate(EXAMPLES):
            result, case = execute(
                source_directory() / name,
                output / ("source-" + str(i)),
                inputs=["api_base=" + base] if i == 0 else [],
            )
            assert result["browser_imports_forbidden"]
            cases.append(case)
    if windows_dir:
        windows_dir = windows_dir.resolve()
        before = {p.name: p.read_bytes() for p in windows_dir.glob("*.postergraph.json")}
        for i, name in enumerate(
            [
                "windows-http.postergraph.json",
                "windows-data.postergraph.json",
                "windows-csv.postergraph.json",
                "windows-parent.postergraph.json",
            ]
        ):
            path = windows_dir / name
            graph = Graph.model_validate_json(before[name])
            assert not graph_secret_refs(graph) and not graph.secret_file
            assert not any(n.type.startswith("Browser.") for n in graph.nodes)
            context = nullcontext()
            if i == 0:
                base = graph.variables["api_base"].value
                origin = controlled_origin(base)
                assert all(
                    n.type == "HTTP.Get" and n.params["url"] == "{{api_base}}/api/data"
                    for n in graph.nodes
                    if n.type.startswith("HTTP.")
                )
                context = serve_maker(port=origin.port)
            else:
                assert not any(n.type.startswith("HTTP.") for n in graph.nodes)
            with context:
                report, case = execute(path, output / ("windows-" + str(i)))
            assert report["browser_imports_forbidden"]
            workdir = Path(report["workdir"])
            if i == 1:
                items = graph.node("n0").params["items"]
                assert json.loads((workdir / "lists/result.json").read_text(encoding="utf-8")) == [
                    v.upper() for v in items
                ]
            if i == 2:
                assert (workdir / "tables/output.xlsx").is_file()
            if i == 3:
                assert (workdir / "nested/result.txt").read_text(encoding="utf-8") == "POSTER Ж"
            case["source"] = "exact Windows Maker graph"
            cases.append(case)
        assert all((windows_dir / name).read_bytes() == raw for name, raw in before.items())
    if browser_graph:
        browser_graph = browser_graph.resolve()
        graph = Graph.model_validate_json(browser_graph.read_bytes())
        assert not graph_secret_refs(graph) and not graph.secret_file
        base = graph.metadata["maker_demo"]
        origin = controlled_origin(base)
        for node in graph.nodes:
            if node.type == "Browser.OpenURL":
                assert node.params["url"] == base + "/"
        with serve_maker(port=origin.port) as (server, _):
            report, case = execute(browser_graph, output / "windows-browser")
            assert len(server.posts) == 1 and not report["browser_imports_forbidden"]
        case["source"] = "exact Windows Maker browser graph"
        case["writes"] = 1
        cases.append(case)
    report = {
        "status": "PASSED",
        "platform": sys.platform,
        "public_cli": True,
        "real_subprocesses": True,
        "history_survives_cli_restart": True,
        "cases": cases,
    }
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("::notice title=Dispatcher E2E::" + json.dumps(report), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--windows-dir", type=Path)
    parser.add_argument("--browser-graph", type=Path)
    args = parser.parse_args()
    check(args.output, args.windows_dir, args.browser_graph)
