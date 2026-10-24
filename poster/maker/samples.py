"""Small executable examples, shared by the GUI and CI (no Tk imports)."""

from .model import Edge, Graph, Node, Variable


def loop_graph(base):
    definitions = [
        ("start", "Start", {}),
        ("loop", "Logic.ForEach", {"items": "{{words}}", "item": "word", "index": "i", "max_iterations": 5}),
        ("delay", "Logic.Delay", {"milliseconds": 10}),
        (
            "compose",
            "Strings.Transform",
            {"operation": "concat", "value": "{{prefix}}", "other": " · {{word}}"},
        ),
        ("open", "Browser.OpenURL", {"url": base + "/"}),
        ("show", "Browser.Click", {"selector": "#show"}),
        ("input", "Browser.Input", {"selector": "#note", "value": "{{note}}"}),
        ("post", "Browser.Click", {"selector": "#continue"}),
        ("wait", "Browser.WaitURL", {"url": base + "/result/*", "after_action": True, "after_node": "post"}),
        ("read", "Browser.ReadText", {"selector": "#result"}),
        ("increment", "Variables.Increment", {"name": "counter", "amount": 1}),
        ("compare", "Variables.Compare", {"left": "{{counter}}", "right": 2, "operator": "ge"}),
        ("break", "Logic.Break", {}),
        ("continue", "Logic.Continue", {}),
        ("switch", "Logic.Switch", {"value": "{{counter}}", "cases": [2]}),
        ("success", "End.Success", {}),
        ("failure", "End.Failure", {}),
    ]
    nodes = [
        Node(id=key, type=kind, params=params, x=40 + (index % 4) * 240, y=45 + (index // 4) * 145)
        for index, (key, kind, params) in enumerate(definitions)
    ]
    by_id = {node.id: node for node in nodes}
    by_id["compose"].save_as, by_id["compose"].result_path = "note", "value"
    by_id["read"].save_as, by_id["read"].result_path = "last_text", "value"
    edges = [
        Edge(source=a, target=b)
        for a, b in zip(
            [
                "start",
                "loop",
                "delay",
                "compose",
                "open",
                "show",
                "input",
                "post",
                "wait",
                "read",
                "increment",
            ],
            [
                "loop",
                "delay",
                "compose",
                "open",
                "show",
                "input",
                "post",
                "wait",
                "read",
                "increment",
                "compare",
            ],
            strict=True,
        )
    ]
    edges += [
        Edge(source="compare", target="break"),
        Edge(source="compare", port="error", target="continue"),
        Edge(source="loop", port="error", target="switch"),
        Edge(source="switch", port="case:0", target="success"),
        Edge(source="switch", target="failure"),
        Edge(source="switch", port="error", target="failure"),
    ]
    return Graph(
        name="Цикл, переменные и отладка",
        entry="start",
        nodes=nodes,
        edges=edges,
        variables={
            "prefix": Variable(value="Maker Ж", input=True, required=True),
            "words": Variable(value=["one", "two", "three"]),
            "counter": Variable(value=0),
        },
        metadata={"maker_demo": base},
    )
