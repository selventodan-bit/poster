"""Run the exact non-secret demo graph saved by frozen Windows Maker on Linux."""

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from urllib.parse import urlsplit

from poster.maker.fixtures import serve_maker
from poster.maker.model import Graph, graph_secret_refs


def check(path, output):
    raw = path.read_bytes()
    graph = Graph.model_validate_json(raw)
    url = urlsplit(graph.metadata["maker_demo"])
    if url.scheme != "http" or url.hostname != "127.0.0.1" or not url.port or url.port < 1024:
        raise ValueError("Expected the controlled loopback demo")
    if graph_secret_refs(graph) or graph.secret_file:
        raise ValueError("Do not export credentials to the CI graph artifact")
    expected = next(n.params["value"] for n in graph.nodes if n.type == "Browser.Input")
    if not isinstance(expected, str) or "{{" in expected:
        raise ValueError("Expected the recorded controlled demo input")
    for node in graph.nodes:
        if node.type == "Browser.OpenURL" and node.params.get("url") != graph.metadata["maker_demo"] + "/":
            raise ValueError("Only the owned demo URL may be executed")
    code = """import sys,importlib.abc
class NoGUI(importlib.abc.MetaPathFinder):
 def find_spec(self,fullname,path=None,target=None):
  if fullname.startswith(('tkinter','PySide6')):raise RuntimeError('Desktop framework imported by Linux runner')
sys.meta_path.insert(0,NoGUI())
from poster.cli import main
raise SystemExit(main(sys.argv[1:]))
"""
    with serve_maker(port=url.port) as (server, _):
        process = subprocess.run(
            [sys.executable, "-c", code, "maker-run", str(path)], capture_output=True, text=True, timeout=90
        )
        if process.returncode:
            raise AssertionError("Independent CLI failed: exit " + str(process.returncode))
        result = json.loads(process.stdout)
        assert result["status"] == "SUCCEEDED"
        assert len(server.posts) == 1
        assert server.posts["1"]["note"] == expected
        assert path.read_bytes() == raw
        report = {
            "status": "PASSED",
            "platform": sys.platform,
            "graph_sha256": hashlib.sha256(raw).hexdigest(),
            "nodes": len(graph.nodes),
            "gui_imports_forbidden": True,
            "writes": len(server.posts),
            "source": "exact JSON saved by frozen Windows Maker",
            "same_graph_bytes": True,
        }
    output.mkdir(parents=True, exist_ok=True)
    (output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print("::notice title=Windows graph on Linux::" + json.dumps(report), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("graph", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    check(args.graph.resolve(), args.output.resolve())
