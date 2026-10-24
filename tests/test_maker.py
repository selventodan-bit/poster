import asyncio
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from poster.maker.actions import Action, Empty, default_registry
from poster.maker.fixtures import serve_maker
from poster.maker.model import Document, Edge, Graph, GraphError, Node, empty_graph
from poster.maker.runtime import GraphRunner
from poster.maker.recorder import ActionRecorder
from poster.maker.storage import Vault, load_graph, save_graph


def chain(*nodes):
    return Graph(
        entry=nodes[0].id,
        nodes=list(nodes),
        edges=[Edge(source=a.id, target=b.id) for a, b in zip(nodes, nodes[1:], strict=False)],
    )


def test_graph_is_versioned_separate_and_coordinates_are_roundtripped(tmp_path):
    graph = empty_graph()
    graph.nodes[0].x = 251.5
    graph.nodes[0].y = -20
    path = tmp_path / "project.postergraph.json"
    save_graph(path, graph, Vault())
    loaded, _ = load_graph(path)
    assert loaded == graph
    invalid = graph.model_dump()
    invalid["schema_version"] = 2
    path.write_text(json.dumps(invalid))
    with pytest.raises(GraphError, match="FILE_INVALID"):
        load_graph(path)
    legacy = tmp_path / "workflow.json"
    legacy.write_text('{"schema_version":1,"steps":[]}')
    before = legacy.read_bytes()
    with pytest.raises(GraphError):
        save_graph(legacy, graph, Vault())
    assert legacy.read_bytes() == before


def test_document_connections_copy_delete_and_undo():
    doc = Document()
    start = doc.graph.entry
    end = doc.graph.nodes[-1].id
    node = doc.add("Logic.If", {"condition": "equals", "left": "a", "expected": "a"})
    doc.connect(start, "success", node.id)
    doc.connect(node.id, "success", end)
    doc.connect(node.id, "error", end)
    assert doc.graph.successor(node.id, "success") == end and doc.graph.successor(node.id, "error") == end
    clone = doc.clone({node.id})[0]
    assert clone != node.id and doc.graph.node(clone).params == node.params
    doc.delete({clone})
    assert clone not in {n.id for n in doc.graph.nodes}
    doc.undo()
    assert clone in {n.id for n in doc.graph.nodes}
    with pytest.raises(GraphError):
        doc.connect(node.id, "success", start)
    assert doc.graph.successor(node.id, "success") == end


def test_vault_never_saves_plaintext_and_rejects_tampered_bundles(tmp_path, monkeypatch):
    vault = Vault()
    reference = vault.put("private-password-Ж")
    graph = empty_graph()
    graph.nodes.append(
        Node(type="Browser.Input", params={"selector": "#pw", "value": reference, "sensitive": True})
    )
    path = tmp_path / "secret.postergraph.json"
    saved = save_graph(path, graph, vault)
    bundle = tmp_path / saved.secret_file
    assert b"private-password" not in path.read_bytes() + bundle.read_bytes()
    loaded, restored = load_graph(path)
    assert restored.get(reference["secret_ref"]) == "private-password-Ж"
    bundle.write_bytes(bundle.read_bytes() + b" ")
    with pytest.raises(GraphError, match="SECRET_FILE_INVALID"):
        load_graph(path)
    monkeypatch.setenv("MAKER_TEST_SECRET", "ambient-private")
    with pytest.raises(GraphError):
        Vault().get("env:MAKER_TEST_SECRET")
    assert Vault(env_allowed=["MAKER_TEST_SECRET"]).get("env:MAKER_TEST_SECRET") == "ambient-private"


@pytest.mark.parametrize("condition_value,port", [("yes", "success"), ("no", "error")])
async def test_if_follows_real_edges_instead_of_node_order(condition_value, port):
    start = Node(type="Start")
    conditional = Node(
        type="Logic.If", params={"condition": "equals", "left": condition_value, "expected": "yes"}
    )
    yes = Node(type="End", title="YES")
    no = Node(type="End", title="NO")
    graph = Graph(
        entry=start.id,
        nodes=[no, yes, conditional, start],
        edges=[
            Edge(source=start.id, target=conditional.id),
            Edge(source=conditional.id, target=yes.id),
            Edge(source=conditional.id, port="error", target=no.id),
        ],
    )
    runner = GraphRunner(graph)
    try:
        assert await runner.run() == "SUCCEEDED"
        assert runner.results[1].port == port
        assert runner.results[-1].node_id == (yes.id if port == "success" else no.id)
        assert runner.context.session.browser is None
    finally:
        await runner.close()


async def test_action_errors_use_error_edge_and_unhandled_errors_stop():
    registry = default_registry()

    async def fail(ctx, params):
        raise GraphError("TEST_ERROR")

    registry.register(Action("Test.Failure", "Test", Empty, fail))
    start = Node(type="Start")
    action = Node(type="Test.Failure")
    end = Node(type="End")
    graph = chain(start, action, end)
    graph.edges = [
        Edge(source=start.id, target=action.id),
        Edge(source=action.id, port="error", target=end.id),
    ]
    runner = GraphRunner(graph, registry=registry)
    assert await runner.run() == "SUCCEEDED" and runner.results[1].status == "FAILED"
    await runner.close()
    graph.edges = graph.edges[:1]
    runner = GraphRunner(graph, registry=registry)
    assert await runner.run() == "FAILED"
    await runner.close()


async def test_cycles_are_bounded_and_step_is_one_cube():
    start = Node(type="Start")
    conditional = Node(type="Logic.If", params={"condition": "equals", "left": "x", "expected": "x"})
    graph = Graph(
        entry=start.id,
        nodes=[start, conditional],
        max_steps=3,
        edges=[
            Edge(source=start.id, target=conditional.id),
            Edge(source=conditional.id, target=conditional.id),
        ],
    )
    runner = GraphRunner(graph)
    await runner.step()
    assert len(runner.results) == 1 and runner.state == "PAUSED" and runner.current == conditional.id
    assert await asyncio.wait_for(runner.run(), 2) == "FAILED"
    assert runner.results[-1].error_code == "STEP_LIMIT"
    await runner.close()


async def test_real_record_edit_save_reopen_and_normal_post_login(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "maker_check", Path(__file__).parents[1] / "scripts/check_maker.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    report = await module.scenario(tmp_path)
    assert report["status"] == "PASSED" and report["writes"] == 3 and report["logins"] == 2


async def test_stop_interrupts_wait_and_never_executes_next_node(tmp_path):
    with serve_maker() as (_, base):
        graph = chain(
            Node(type="Start"),
            Node(type="Browser.OpenURL", params={"url": base + "/"}),
            Node(type="Browser.WaitElement", params={"selector": "#never", "timeout_ms": 120000}),
            Node(type="End"),
        )
        runner = GraphRunner(graph)
        task = asyncio.create_task(runner.run())
        try:
            for _ in range(300):
                if runner.current == graph.nodes[2].id and runner.state == "RUNNING":
                    break
                await asyncio.sleep(0.02)
            await runner.stop()
            assert await asyncio.wait_for(task, 5) == "STOPPED"
            assert not any(r.action == "End" for r in runner.results)
        finally:
            await runner.close()


async def test_recorded_delayed_navigation_does_not_match_the_old_page():
    with serve_maker() as (_, base):
        recorder = ActionRecorder(headless=True)
        try:
            await recorder.start()
            await recorder.navigate(base + "/async")
            await recorder.session.page.locator("#async").click()
            await recorder.session.page.wait_for_url("**stage=two")
            graph = await recorder.stop()
        finally:
            await recorder.close()
        wait_node = next(n for n in graph.nodes if n.type == "Browser.WaitURL")
        assert wait_node.params["after_action"]
        runner = GraphRunner(graph)
        try:
            assert await runner.run() == "SUCCEEDED"
            assert (await runner.context.page()).url.endswith("?stage=two")
        finally:
            await runner.close()


def test_same_graph_executes_in_linux_process_without_gui_imports(tmp_path):
    with serve_maker() as (server, base):
        graph = chain(
            Node(type="Start"),
            Node(type="Browser.OpenURL", params={"url": base + "/"}),
            Node(type="Browser.Click", params={"selector": "#show"}),
            Node(type="Browser.Input", params={"selector": "#note", "value": "CLI Ж 日本語"}),
            Node(type="Browser.Select", params={"selector": "#mode", "values": ["two"]}),
            Node(type="Browser.Click", params={"selector": "#continue"}),
            Node(type="Browser.WaitElement", params={"selector": "#done"}),
            Node(type="End"),
        )
        path = tmp_path / "headless.postergraph.json"
        save_graph(path, graph, Vault())
        code = """import sys,importlib.abc
class NoGUI(importlib.abc.MetaPathFinder):
 def find_spec(self,fullname,path=None,target=None):
  if fullname.startswith('tkinter'):raise RuntimeError('GUI imported by graph runtime')
sys.meta_path.insert(0,NoGUI())
from poster.cli import main
raise SystemExit(main(sys.argv[1:]))
"""
        result = subprocess.run(
            [sys.executable, "-c", code, "maker-run", str(path)], capture_output=True, text=True, timeout=60
        )
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["status"] == "SUCCEEDED"
        assert list(server.posts.values()) == [{"note": "CLI Ж 日本語", "mode": "two"}]


def pump(root, predicate, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        root.update()
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("Maker GUI timeout")


@pytest.mark.skipif(not os.environ.get("DISPLAY") and os.name != "nt", reason="Native GUI required")
def test_canvas_is_an_editable_executable_graph(tmp_path, monkeypatch):
    # One Tk application per process, including Windows. Worker APIs are unchanged.
    code = """import sys,os
sys.path.insert(0,sys.argv[1]);os.chdir(sys.argv[2])
from test_maker import canvas_scenario
canvas_scenario()
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(Path(__file__).parent), str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert result.returncode == 0, result.stderr


def canvas_scenario():
    import tkinter as tk
    from tkinter import messagebox
    from poster.recorder.app import App

    with serve_maker() as (_, base):
        root = tk.Tk()
        app = App(root)
        maker = app.maker
        app.notebook.select(maker.frame)
        root.update()
        messagebox.askyesno = lambda *a, **k: True
        try:
            index = maker.action_names.index("Browser.OpenURL")
            maker.library.selection_set(index)
            maker.buttons["add"].invoke()
            root.update()
            node = next(n for n in maker.document.graph.nodes if n.type == "Browser.OpenURL")
            entry = maker.props["url"][1]
            entry.delete(0, "end")
            entry.insert(0, base + "/")
            maker.buttons["apply"].invoke()
            start = maker.document.graph.entry
            end = next(n.id for n in maker.document.graph.nodes if n.type == "End")
            maker.canvas.fit()
            root.update()

            def click(point):
                x = int(point[0] - maker.canvas.widget.canvasx(0))
                y = int(point[1] - maker.canvas.widget.canvasy(0))
                maker.canvas.widget.event_generate("<Motion>", x=x, y=y)
                root.update()
                maker.canvas.widget.event_generate("<ButtonPress-1>", x=x, y=y)
                maker.canvas.widget.event_generate("<ButtonRelease-1>", x=x, y=y)
                root.update()

            click(maker.canvas.port_points[start, "success"])
            click(maker.canvas.port_points[node.id, "input"])
            click(maker.canvas.port_points[node.id, "success"])
            click(maker.canvas.port_points[end, "input"])
            assert maker.document.graph.successor(start, "success") == node.id
            assert maker.document.graph.successor(node.id, "success") == end
            click(
                (
                    (maker.canvas.node_boxes[node.id][0] + maker.canvas.node_boxes[node.id][2]) / 2,
                    (maker.canvas.node_boxes[node.id][1] + maker.canvas.node_boxes[node.id][3]) / 2,
                )
            )
            before = (node.x, node.y)
            c = maker.canvas.widget
            x = int(node.x * maker.canvas.zoom + 50)
            y = int(node.y * maker.canvas.zoom + 20)
            c.event_generate("<Motion>", x=x, y=y)
            root.update()
            c.event_generate("<ButtonPress-1>", x=x, y=y)
            c.event_generate("<B1-Motion>", x=x + 25, y=y + 20)
            c.event_generate("<ButtonRelease-1>", x=x + 25, y=y + 20)
            root.update()
            assert (node.x, node.y) != before
            count = len(maker.document.graph.nodes)
            maker.buttons["copy"].invoke()
            assert len(maker.document.graph.nodes) == count + 1
            maker.buttons["delete"].invoke()
            assert len(maker.document.graph.nodes) == count
            maker.buttons["step"].invoke()
            pump(root, lambda: not maker.busy)
            assert len(maker.trace) == 1 and maker.runner.state == "PAUSED"
            maker.buttons["run"].invoke()
            pump(root, lambda: not maker.busy)
            assert maker.runner.state == "SUCCEEDED" and len(maker.trace) == 3
            assert maker.save()
            loaded, _ = load_graph(maker.path)
            assert loaded.node(node.id).x == node.x
        finally:
            maker.document.dirty = False
            app.close()
            deadline = time.monotonic() + 10
            while app.thread.is_alive() and time.monotonic() < deadline:
                try:
                    root.update()
                except tk.TclError:
                    break
                time.sleep(0.02)
        assert not app.thread.is_alive()
