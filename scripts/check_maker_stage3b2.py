"""Execute the four committed example graphs in fresh CLI processes without desktop/browser imports."""

import argparse
import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit
import subprocess
import sys
from poster.maker.example_projects import EXAMPLES, source_directory
from poster.maker.fixtures import serve_maker

NO_BROWSER = """import sys,importlib.abc
class NoDesktopOrBrowser(importlib.abc.MetaPathFinder):
 def find_spec(self,fullname,path=None,target=None):
  if fullname.split('.')[0] in {'tkinter','PySide6','playwright'}:raise RuntimeError('GUI/browser imported by data runner')
sys.meta_path.insert(0,NoDesktopOrBrowser())
from poster.cli import main
raise SystemExit(main(sys.argv[1:]))
"""


def execute(path, workdir, inputs=()):
    command = [sys.executable, "-c", NO_BROWSER, "maker-run", str(path), "--workdir", str(workdir)]
    for value in inputs:
        command += ["--input", value]
    process = subprocess.run(command, capture_output=True, text=True, timeout=100)
    if process.returncode:
        raise AssertionError(
            "Data CLI failed: "
            + path.name
            + " exit="
            + str(process.returncode)
            + " "
            + process.stdout[-1500:]
        )
    report = json.loads(process.stdout)
    assert report["status"] == "SUCCEEDED"
    return report


def check_windows_graphs(directory, output):
    names = [
        "windows-http.postergraph.json",
        "windows-data.postergraph.json",
        "windows-csv.postergraph.json",
        "windows-parent.postergraph.json",
        "child.postergraph.json",
    ]
    before = {name: (directory / name).read_bytes() for name in names}
    cases = []
    for name in names[:-1]:
        path = directory / name
        graph = json.loads(before[name])
        assert all(not n["type"].startswith("Browser.") for n in graph["nodes"])
        assert not graph.get("secret_file")
        workdir = output / name.split(".")[0]
        if name.startswith("windows-http"):
            base = graph["variables"]["api_base"]["value"]
            address = urlsplit(base)
            assert address.scheme == "http" and address.hostname == "127.0.0.1" and address.port > 1024
            assert not address.username and not address.password and not address.query
            assert all(
                n["type"] == "HTTP.Get" and n["params"]["url"] == "{{api_base}}/api/data"
                for n in graph["nodes"]
                if n["type"].startswith("HTTP.")
            )
            with serve_maker(port=address.port):
                result = execute(path, workdir)
            assert result["variables"]["number"] == 3
        else:
            assert all(not n["type"].startswith("HTTP.") for n in graph["nodes"])
            result = execute(path, workdir)
            if name.startswith("windows-data"):
                items = next(n["params"]["items"] for n in graph["nodes"] if n["id"] == "n0")
                assert json.loads((workdir / "lists/result.json").read_text(encoding="utf-8")) == [
                    x.upper() for x in items
                ]
            elif name.startswith("windows-csv"):
                assert result["variables"]["csv"] == result["variables"]["excel"]
                assert (workdir / "tables/output.xlsx").is_file()
            else:
                assert result["variables"]["result"] == "POSTER Ж"
                assert (workdir / "nested/result.txt").read_text(encoding="utf-8") == "POSTER Ж"
                assert "private_local" not in result["variables"]
        cases.append(
            {
                "name": "exact Windows " + name,
                "status": "PASSED",
                "sha256": hashlib.sha256(before[name]).hexdigest(),
                "same_graph_bytes": True,
            }
        )
    assert all((directory / name).read_bytes() == raw for name, raw in before.items())
    return cases


def check(output, windows_graph=None, windows_dir=None):
    output.mkdir(parents=True, exist_ok=True)
    cases = []
    with serve_maker() as (_, base):
        for label, name in EXAMPLES:
            workdir = output / name.split(".")[0]
            result = execute(
                source_directory() / name, workdir, ["api_base=" + base] if name.startswith("01") else []
            )
            if name.startswith("01"):
                assert result["variables"]["number"] == 3
            if name.startswith("02"):
                assert json.loads((workdir / "lists/result.json").read_text(encoding="utf-8")) == [
                    "ALPHA",
                    "BETA",
                    "ГАММА",
                ]
            if name.startswith("03"):
                assert (
                    result["variables"]["csv"] == result["variables"]["excel"]
                    and (workdir / "tables/output.xlsx").is_file()
                )
            if name.startswith("04"):
                assert (workdir / "nested/result.txt").read_text(
                    encoding="utf-8"
                ) == "POSTER Ж" and "private_local" not in result["variables"]
            cases.append({"name": label, "status": "PASSED", "nodes_executed": len(result["results"])})
    if windows_graph:
        raw = windows_graph.read_bytes()
        graph = json.loads(raw)
        assert all(not n["type"].startswith(("Browser.", "HTTP.", "Project.")) for n in graph["nodes"])
        items = next(
            n["params"]["items"] for n in graph["nodes"] if n["id"] == "n0" and n["type"] == "Lists.Create"
        )
        result = execute(windows_graph, output / "windows-graph")
        assert json.loads((output / "windows-graph/lists/result.json").read_text(encoding="utf-8")) == [
            x.upper() for x in items
        ]
        assert windows_graph.read_bytes() == raw
        cases.append(
            {
                "name": "exact saved Windows data graph",
                "status": "PASSED",
                "sha256": hashlib.sha256(raw).hexdigest(),
                "same_graph_bytes": True,
            }
        )
    if windows_dir:
        cases.extend(check_windows_graphs(windows_dir, output / "exact-windows"))
    report = {
        "status": "PASSED",
        "platform": sys.platform,
        "cases": cases,
        "browser_imports_forbidden": True,
        "gui_imports_forbidden": True,
        "fresh_process_per_graph": True,
    }
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("::notice title=Stage3B2 data E2E::" + json.dumps(report), flush=True)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--windows-graph", type=Path)
    parser.add_argument("--windows-dir", type=Path)
    args = parser.parse_args()
    check(args.output.resolve(), args.windows_graph, args.windows_dir)
