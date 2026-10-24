"""Real saved demonstration graphs; generation never executes their actions."""

from pathlib import Path
import hashlib
import shutil
import sys
from .model import Edge, Graph, Node, Variable

EXAMPLES = [
    ("HTTP + JSON", "01-http-json.postergraph.json"),
    ("Список и цикл", "02-lists-loop.postergraph.json"),
    ("CSV / Excel", "03-csv-excel.postergraph.json"),
    ("Подпроект", "04-parent.postergraph.json"),
]


def chain(name, actions):
    nodes = [Node(id="start", type="Start", x=40, y=40)]
    for i, (kind, params, *options) in enumerate(actions):
        nodes.append(
            Node(
                id="n" + str(i),
                type=kind,
                params=params,
                x=40 + (i + 1) % 4 * 250,
                y=40 + (i + 1) // 4 * 140,
                **(options[0] if options else {}),
            )
        )
    nodes.append(Node(id="end", type="End.Success", x=800, y=500))
    return Graph(
        name=name,
        entry="start",
        nodes=nodes,
        edges=[Edge(source=a.id, target=b.id) for a, b in zip(nodes, nodes[1:], strict=False)],
    )


def definitions():
    http = chain(
        "HTTP + JSON",
        [
            (
                "HTTP.Get",
                {"url": "{{api_base}}/api/data", "protect_response": False},
                {"save_as": "response", "result_path": "text"},
            ),
            ("JSON.Parse", {"text": "{{response}}"}, {"save_as": "document", "result_path": "value"}),
            (
                "JSON.Get",
                {"value": "{{document}}", "path": "/items/0/value"},
                {"save_as": "number", "result_path": "value"},
            ),
            ("Variables.Compare", {"left": "{{number}}", "right": 3}),
        ],
    )
    http.variables["api_base"] = Variable(value="http://127.0.0.1:8765", input=True, required=True)
    lists = chain(
        "Список и цикл",
        [
            (
                "Lists.Create",
                {"items": ["alpha", "beta", "гамма"]},
                {"save_as": "words", "result_path": "value"},
            ),
            ("Lists.Create", {"items": []}, {"save_as": "out", "result_path": "value"}),
            ("Logic.ForEach", {"items": "{{words}}", "item": "word"}),
            (
                "Strings.Transform",
                {"operation": "upper", "value": "{{word}}"},
                {"save_as": "upper", "result_path": "value"},
            ),
            (
                "Lists.Add",
                {"items": "{{out}}", "item": "{{upper}}"},
                {"save_as": "out", "result_path": "value"},
            ),
            ("Logic.Continue", {}),
            (
                "JSON.Stringify",
                {"value": "{{out}}", "pretty": True},
                {"save_as": "json_text", "result_path": "value"},
            ),
            ("Files.WriteText", {"path": "lists/result.json", "text": "{{json_text}}", "overwrite": True}),
        ],
    )
    lists.edges.append(Edge(source="n2", port="error", target="n6"))
    tables = chain(
        "CSV / Excel",
        [
            (
                "Files.WriteText",
                {
                    "path": "tables/input.csv",
                    "text": 'name;n\n"Ж ""quote""";2\n"line\nbreak";3\nskip;1\n',
                    "encoding": "utf-8-sig",
                    "overwrite": True,
                },
            ),
            (
                "CSV.Read",
                {"path": "tables/input.csv", "delimiter": ";", "infer_json": True},
                {"save_as": "table", "result_path": "value"},
            ),
            (
                "Tables.Filter",
                {"table": "{{table}}", "column": "n", "operator": "ge", "value": 2},
                {"save_as": "table", "result_path": "value"},
            ),
            (
                "Tables.SetCell",
                {"table": "{{table}}", "row": 0, "column": "name", "value": "Edited Ж"},
                {"save_as": "table", "result_path": "value"},
            ),
            (
                "CSV.Write",
                {"table": "{{table}}", "path": "tables/output.csv", "delimiter": ";", "overwrite": True},
            ),
            (
                "Excel.Write",
                {
                    "table": "{{table}}",
                    "path": "tables/output.xlsx",
                    "sheet": "Results",
                    "update_existing": True,
                    "replace_sheet": True,
                },
            ),
            (
                "CSV.Read",
                {"path": "tables/output.csv", "delimiter": ";", "infer_json": True},
                {"save_as": "csv", "result_path": "value"},
            ),
            (
                "Excel.Read",
                {"path": "tables/output.xlsx", "sheet": "Results"},
                {"save_as": "excel", "result_path": "value"},
            ),
            ("Variables.Compare", {"left": "{{csv}}", "right": "{{excel}}"}),
        ],
    )
    child = chain(
        "Дочерняя обработка",
        [
            (
                "Strings.Transform",
                {"operation": "upper", "value": "{{argument}}"},
                {"save_as": "result", "result_path": "value"},
            ),
            ("Variables.Set", {"name": "private_local", "value": 123}),
        ],
    )
    child.variables["argument"] = Variable(value=None, input=True, required=True)
    child.extensions["outputs"] = ["result"]
    parent = chain(
        "Подпроект",
        [
            (
                "Project.Run",
                {"path": "child.postergraph.json", "inputs": {"argument": "Poster Ж"}},
                {"save_as": "child", "result_path": "outputs"},
            ),
            (
                "JSON.Get",
                {"value": "{{child}}", "path": "/result"},
                {"save_as": "result", "result_path": "value"},
            ),
            ("Files.WriteText", {"path": "nested/result.txt", "text": "{{result}}", "overwrite": True}),
            ("Variables.Compare", {"left": "{{result}}", "right": "POSTER Ж"}),
        ],
    )
    return {
        EXAMPLES[0][1]: http,
        EXAMPLES[1][1]: lists,
        EXAMPLES[2][1]: tables,
        EXAMPLES[3][1]: parent,
        "child.postergraph.json": child,
    }


def source_directory():
    root = (
        Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parents[2]
    )
    return root / "examples" / "maker-stage3b2"


def copy_examples(workspace):
    source = source_directory()
    files = sorted(source.glob("*.postergraph.json"))
    digest = hashlib.sha256(b"".join(p.read_bytes() for p in files)).hexdigest()[:12]
    target = workspace / "maker-examples" / digest
    target.mkdir(parents=True, exist_ok=True)
    for path in files:
        if not (target / path.name).exists():
            shutil.copyfile(path, target / path.name)
    return target
