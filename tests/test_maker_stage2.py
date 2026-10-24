import asyncio
from dataclasses import asdict
import json
import subprocess
import sys

import pytest

from poster.maker.actions import default_registry
from poster.maker.fixtures import serve_maker
from poster.maker.inspector import Inspector
from poster.maker.model import Edge, Graph, GraphError, Node, Variable, empty_graph
from poster.maker.recorder import ActionRecorder
from poster.maker.runtime import GraphRunner
from poster.maker.samples import loop_graph
from poster.maker.storage import Vault, load_graph, save_graph
from poster.maker.variables import Variables


def sequence(*nodes, variables=None):
    return Graph(
        entry=nodes[0].id,
        nodes=list(nodes),
        variables=variables or {},
        edges=[Edge(source=a.id, target=b.id) for a, b in zip(nodes, nodes[1:], strict=False)],
    )


@pytest.mark.parametrize("mode", ["history", "anchor", "addressbar"])
async def test_same_document_navigation_requires_real_action_causality(mode):
    with serve_maker() as (_, base):
        recorder = ActionRecorder(headless=True)
        try:
            await recorder.start()
            await recorder.navigate(base + "/spa")
            page = recorder.session.page
            await page.locator("#spa-input").fill("text")
            if mode == "history":
                await page.locator("#route").click()
                await page.wait_for_url("**step=two")
            elif mode == "anchor":
                await page.locator("#hash").click()
                await page.wait_for_url("**#part")
            else:
                await page.goto(base + "/spa#manual")
            await asyncio.sleep(0.08)
            graph = await recorder.stop()
        finally:
            await recorder.close()
        if mode == "addressbar":
            assert sum(n.type == "Browser.OpenURL" for n in graph.nodes) == 2
            assert not any(n.type == "Browser.WaitURL" for n in graph.nodes)
        else:
            assert sum(n.type == "Browser.OpenURL" for n in graph.nodes) == 1
            assert any(n.type == "Browser.WaitURL" and n.params.get("after_node") for n in graph.nodes)
        runner = GraphRunner(graph)
        try:
            assert await runner.run() == "SUCCEEDED", [
                (r.action, r.error_code, r.details) for r in runner.results
            ]
        finally:
            await runner.close()


async def test_api_key_urls_and_extracted_links_stay_protected():
    with serve_maker() as (_, base):
        private = "private-api-key-Ж"
        url = base + "/spa?api_key=" + private
        recorder = ActionRecorder(headless=True)
        try:
            await recorder.start()
            await recorder.navigate(url)
            graph = await recorder.stop()
        finally:
            await recorder.close()
        assert private not in graph.model_dump_json()
        assert isinstance(next(n for n in graph.nodes if n.type == "Browser.OpenURL").params["url"], dict)
        runner = GraphRunner(graph, vault=recorder.vault)
        try:
            assert await runner.run() == "SUCCEEDED"
            page = await runner.context.page()
            await page.locator("#hash").evaluate("(e,url)=>e.setAttribute('href',url)", url)
            snapshot = runner.graph.model_copy(deep=True)
            read = Node(
                type="Browser.ReadAttribute",
                params={"selector": "#hash", "attribute": "href"},
                save_as="link",
                result_path="value",
            )
            snapshot.nodes.append(read)
            runner.update_graph(snapshot)
            result = await runner.execute_selected(read.id)
            assert result.status == "SUCCEEDED" and result.value == {"protected": True}
            assert runner.context.variables.reveal(runner.context.variables.get("link")) == url
            selected = []
            inspector = Inspector(runner.context.session, on_pick=selected.append)
            await inspector.enable()
            await page.locator("#hash").click()
            for _ in range(100):
                if selected:
                    break
                await asyncio.sleep(0.02)
            assert selected and private not in json.dumps(selected, ensure_ascii=False)
            assert private not in json.dumps([asdict(r) for r in runner.results], ensure_ascii=False)
        finally:
            await runner.close()


def test_v1_migration_does_not_write_until_explicit_save(tmp_path):
    path = tmp_path / "old.postergraph.json"
    raw = {
        "format": "poster.maker.graph",
        "schema_version": 1,
        "id": "old",
        "name": "Old",
        "entry": "start",
        "nodes": [{"id": "start", "type": "Start", "x": 1, "y": 2}, {"id": "end", "type": "End"}],
        "edges": [{"source": "start", "port": "success", "target": "end"}],
    }
    original = json.dumps(raw).encode()
    path.write_bytes(original)
    graph, vault = load_graph(path)
    assert graph.schema_version == 2 and graph.metadata["migrated_from"] == 1
    assert path.read_bytes() == original and graph.node("start").x == 1
    save_graph(path, graph, vault)
    assert path.with_name(path.name + ".v1.bak").read_bytes() == original
    assert json.loads(path.read_bytes())["schema_version"] == 2


def test_variable_inputs_substitution_secrets_and_isolation(tmp_path):
    vault = Vault()
    definitions = {
        "name": Variable(value="guest", input=True),
        "number": Variable(value=3),
        "password": Variable(value=vault.put("private-Ж"), secret=True),
    }
    first = Variables(definitions, vault, {"name": "Alice"})
    second = Variables(definitions, vault)
    assert first.resolve("Hello {{name}} {{number}}") == "Hello Alice 3"
    assert first.resolve("{{number}}") == 3
    protected = first.resolve("prefix-{{password}}")
    assert vault.resolve(protected) == "prefix-private-Ж"
    assert first.snapshot()["password"] == "••••"
    first.set("number", 7)
    assert second.get("number") == 3
    with pytest.raises(GraphError, match="INPUT_UNKNOWN"):
        Variables(definitions, vault, {"unknown": 1})
    with pytest.raises(GraphError, match="INPUT_MISSING"):
        Variables({"x": Variable(input=True, required=True)}, vault)
    with pytest.raises(GraphError, match="VARIABLE_MISSING"):
        first.resolve("{{missing}}")
    graph = empty_graph()
    graph.variables = definitions
    saved = save_graph(tmp_path / "secret.json", graph, vault)
    assert (
        b"private-"
        not in (tmp_path / saved.secret_file).read_bytes() + (tmp_path / "secret.json").read_bytes()
    )
    restored, restored_vault = load_graph(tmp_path / "secret.json")
    assert restored_vault.resolve(restored.variables["password"].value) == "private-Ж"


async def test_example_runs_real_browser_loop_break_continue_switch_and_output_variables():
    with serve_maker() as (server, base):
        graph = loop_graph(base)
        runner = GraphRunner(graph, inputs={"prefix": "Проверка"})
        try:
            assert await runner.run() == "SUCCEEDED", [
                (r.action, r.error_code, r.details) for r in runner.results
            ]
            assert [p["note"] for p in server.posts.values()] == ["Проверка · one", "Проверка · two"]
            assert runner.context.variables.get("counter") == 2
            assert runner.context.variables.get("last_text") == "Проверка · two"
            assert {r.action for r in runner.results} >= {
                "Logic.ForEach",
                "Logic.Break",
                "Logic.Continue",
                "Logic.Switch",
                "Browser.ReadText",
            }
        finally:
            await runner.close()
        assert graph.variables["counter"].value == 0


async def test_counted_loop_exhaustion_and_limit_are_real_control_flow():
    start = Node(type="Start")
    loop = Node(type="Logic.Loop", params={"count": 3, "max_iterations": 3})
    increment = Node(type="Variables.Increment", params={"name": "total"})
    end = Node(type="End.Success")
    graph = Graph(
        entry=start.id,
        nodes=[start, loop, increment, end],
        variables={"total": Variable(value=0)},
        edges=[
            Edge(source=start.id, target=loop.id),
            Edge(source=loop.id, target=increment.id),
            Edge(source=increment.id, target=loop.id),
            Edge(source=loop.id, port="error", target=end.id),
        ],
    )
    runner = GraphRunner(graph)
    try:
        assert await runner.run() == "SUCCEEDED" and runner.context.variables.get("total") == 3
        assert runner.context.loops == []
    finally:
        await runner.close()
    loop.params["max_iterations"] = 2
    runner = GraphRunner(graph)
    try:
        assert await runner.run() == "SUCCEEDED"  # Explicit Error edge handles the limit error.
        assert any(r.error_code == "LOOP_LIMIT" for r in runner.results)
        assert runner.context.variables.get("total") == 0
    finally:
        await runner.close()


@pytest.mark.parametrize(
    "operation,source,other,expected",
    [
        ("upper", "жabc", "", "ЖABC"),
        ("replace", "abcabc", "b", "aXcaXc"),
        ("split", "a,b", "", ["a", "b"]),
        ("join", ["a", "b"], "", "a,b"),
        ("substring", "abcd", "", "bc"),
        ("length", [1, 2], "", 2),
    ],
)
async def test_string_actions_execute_without_eval(operation, source, other, expected):
    action = Node(
        type="Strings.Transform",
        params={
            "operation": operation,
            "value": source,
            "other": other,
            "replacement": "X",
            "start": 1,
            "end": 3,
        },
        save_as="result",
        result_path="value",
    )
    runner = GraphRunner(sequence(Node(type="Start"), action, Node(type="End")))
    try:
        assert await runner.run() == "SUCCEEDED"
        assert runner.context.variables.get("result") == expected
    finally:
        await runner.close()


async def test_breakpoint_edit_resume_and_selected_action_keep_browser_and_counter():
    with serve_maker() as (_, base):
        open_node = Node(type="Browser.OpenURL", params={"url": base + "/"})
        action = Node(type="Variables.Set", params={"name": "value", "value": "before"}, breakpoint=True)
        get = Node(type="Variables.Get", params={"name": "value"}, save_as="copy", result_path="value")
        graph = sequence(Node(type="Start"), open_node, action, get, Node(type="End.Success"))
        runner = GraphRunner(graph)
        try:
            assert await runner.run() == "PAUSED" and runner.current == action.id
            page = await runner.context.page()
            browser = runner.context.session.browser
            changed = action.model_copy(deep=True, update={"params": {"name": "value", "value": "after"}})
            runner.update_node(changed)
            await runner.execute_selected(action.id)
            assert runner.current == action.id and runner.context.variables.get("value") == "after"
            runner.set_variable("debug_value", 42)
            assert await runner.run() == "SUCCEEDED"
            assert runner.context.variables.get("copy") == "after"
            assert runner.context.session.browser is browser and await runner.context.page() is page
            assert any(r.isolated for r in runner.results)
            assert sum(r.action == "Browser.OpenURL" for r in runner.results) == 1
        finally:
            await runner.close()


async def test_resume_recording_preserves_graph_successor_error_links_and_browser_session():
    with serve_maker() as (server, base):
        first = ActionRecorder(headless=True)
        try:
            await first.start()
            await first.navigate(base + "/")
            page = first.session.page
            await page.locator("#show").click()
            await page.locator("#note").fill("one")
            await page.locator("#continue").click()
            await page.locator("#done").wait_for()
            original = await first.stop(close=False)
            old_ids = {n.id for n in original.nodes}
            after = first.tail
            end = first.end
            original.edges.append(Edge(source=after, port="error", target=end))
            resumed = ActionRecorder(
                headless=True, graph=original, after_node=after, session=first.session, vault=first.vault
            )
            try:
                await resumed.start()
                assert resumed.session.page is page
                await resumed.navigate(base + "/")
                await page.locator("#show").click()
                await page.locator("#note").fill("two")
                await page.locator("#continue").click()
                await page.locator("#done").wait_for()
                graph = await resumed.stop()
            finally:
                await resumed.close()
        finally:
            await first.close()
        assert old_ids <= {n.id for n in graph.nodes}
        assert graph.successor(after, "error") == end
        assert graph.successor(after, "success") != end
        assert len(
            {n.extensions["record_event"] for n in graph.nodes if "record_event" in n.extensions}
        ) == sum("record_event" in n.extensions for n in graph.nodes)
        runner = GraphRunner(graph)
        try:
            assert await runner.run() == "SUCCEEDED", [
                (r.action, r.error_code, r.details) for r in runner.results
            ]
        finally:
            await runner.close()
        assert [p["note"] for p in server.posts.values()] == ["one", "two", "one", "two"]


async def test_wait_url_reports_actual_expected_and_never_resubmits():
    with serve_maker() as (server, base):
        graph = sequence(
            Node(type="Start"),
            Node(id="open", type="Browser.OpenURL", params={"url": base + "/"}),
            Node(type="Browser.Click", params={"selector": "#show"}),
            Node(type="Browser.Input", params={"selector": "#note", "value": "one"}),
            Node(id="post", type="Browser.Click", params={"selector": "#continue"}),
            Node(
                type="Browser.WaitURL",
                params={
                    "url": base + "/wrong?token=hidden-password",
                    "after_action": True,
                    "after_node": "post",
                    "timeout_ms": 100,
                },
            ),
            Node(type="End"),
        )
        runner = GraphRunner(graph)
        try:
            assert await runner.run() == "FAILED"
            failure = runner.results[-1]
            assert failure.error_code == "WAIT_URL_TIMEOUT" and failure.details["reason"] == "url_mismatch"
            assert "/result/1" in failure.details["actual_url"] and "/wrong" in failure.details["expected"]
            assert "hidden-password" not in json.dumps(asdict(failure))
            assert len(server.posts) == 1 and failure.details["form_retried"] is False
        finally:
            await runner.close()


async def test_real_element_picker_css_xpath_attributes_and_tabs():
    with serve_maker() as (server, base):
        runner = GraphRunner(empty_graph())
        try:
            page = await runner.context.page()
            await page.goto(base + "/")
            selected = []
            inspector = Inspector(runner.context.session, on_pick=selected.append)
            await inspector.enable()
            await page.locator("#show").click()
            for _ in range(100):
                if selected:
                    break
                await asyncio.sleep(0.02)
            assert selected[0]["css"] == "#show" and selected[0]["attributes"]["id"] == "show"
            assert await page.locator("#form").is_hidden()  # Picker click is consumed, never submitted.
            assert (await inspector.check(selected[0]["css"]))["count"] == 1
            assert (await inspector.check(selected[0]["xpath"]))["count"] == 1
            registry = default_registry()
            for kind, params in [
                ("Browser.NewTab", {"url": base + "/login"}),
                ("Browser.SwitchTab", {"index": 0}),
                ("Browser.CloseTab", {}),
            ]:
                node = Node(type=kind, params=params)
                spec, p = registry.parameters(node)
                await spec.execute(runner.context, p)
            assert len(runner.context.session.context.pages) == 1
            assert (await runner.context.page()).url.endswith("/login")
            assert server.posts == {}
        finally:
            await runner.close()


async def test_secret_variable_input_and_derived_results_are_redacted():
    vault = Vault()
    password = "secret-password-Ж"
    graph = sequence(
        Node(type="Start"),
        Node(
            type="Strings.Transform",
            params={"value": "{{password}}", "other": "tail"},
            save_as="derived",
            result_path="value",
        ),
        Node(type="Variables.Get", params={"name": "derived"}),
        Node(type="End"),
        variables={"password": Variable(input=True, secret=True, required=True)},
    )
    runner = GraphRunner(graph, vault=vault, inputs={"password": password})
    try:
        assert await runner.run() == "SUCCEEDED"
        assert vault.resolve(runner.context.variables.get("derived")) == password + "tail"
        assert password not in json.dumps([asdict(r) for r in runner.results], ensure_ascii=False)
        assert password not in json.dumps(runner.context.variables.snapshot(), ensure_ascii=False)
    finally:
        await runner.close()


def test_stage2_graph_in_fresh_process_without_any_gui_import(tmp_path):
    with serve_maker() as (server, base):
        graph = loop_graph(base)
        graph.node("input").breakpoint = True
        path = tmp_path / "loop.json"
        save_graph(path, graph, Vault())
        code = """import sys,importlib.abc
class NoGUI(importlib.abc.MetaPathFinder):
 def find_spec(self,fullname,path=None,target=None):
  if fullname.startswith(('tkinter','PySide6')):raise RuntimeError('GUI dependency in runtime')
sys.meta_path.insert(0,NoGUI())
from poster.cli import main
raise SystemExit(main(sys.argv[1:]))
"""
        process = subprocess.run(
            [sys.executable, "-c", code, "maker-run", str(path), "--input", "prefix=Linux override"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert process.returncode == 0, process.stderr + process.stdout
        assert json.loads(process.stdout)["status"] == "SUCCEEDED"
        assert json.loads(process.stdout)["variables"]["counter"] == 2
        assert len(server.posts) == 2
        assert server.posts["1"]["note"] == "Linux override · one"


async def test_recording_iframe_authorization_preserves_password_references():
    from poster.maker.fixtures import DEMO_PASSWORD

    with serve_maker() as (server, base):
        recorder = ActionRecorder(headless=True)
        warnings = []
        recorder.on_warning = warnings.append
        try:
            await recorder.start()
            await recorder.navigate(base + "/iframe")
            frame = recorder.session.page.frame_locator("#inner")
            await frame.locator("#user").fill("demo")
            await frame.locator("#password").fill(DEMO_PASSWORD)
            await frame.locator("#login").click()
            await frame.locator("#account").wait_for()
            graph = await recorder.stop()
        finally:
            await recorder.close()
        assert warnings == []
        password = next(
            n for n in graph.nodes if n.type == "Browser.Input" and n.params["selector"] == "#password"
        )
        assert password.params["frame"] == ["#inner"] and password.params["sensitive"]
        assert DEMO_PASSWORD not in graph.model_dump_json()
        runner = GraphRunner(graph, vault=recorder.vault)
        try:
            assert await runner.run() == "SUCCEEDED", [(r.action, r.error_code) for r in runner.results]
            await (await runner.context.page()).frame_locator("#inner").locator("#account").wait_for()
            assert server.login_count == 2
        finally:
            await runner.close()


async def test_wait_url_uses_trigger_anchor_even_when_input_followed_navigation():
    with serve_maker() as (_, base):
        graph = sequence(
            Node(type="Start"),
            Node(type="Browser.OpenURL", params={"url": base + "/after-nav"}),
            Node(id="trigger", type="Browser.Click", params={"selector": "#move"}),
            Node(type="Browser.Input", params={"selector": "#late", "value": "after navigation"}),
            Node(
                type="Browser.WaitURL",
                params={
                    "url": base + "/after-nav?stage=two",
                    "after_node": "trigger",
                    "after_action": True,
                    "timeout_ms": 200,
                },
            ),
            Node(type="End"),
        )
        runner = GraphRunner(graph)
        try:
            assert await runner.run() == "SUCCEEDED"
            assert (await runner.context.page()).url.endswith("?stage=two")
        finally:
            await runner.close()

        # The v1 last-action fallback is deliberately conservative: it cannot
        # claim a navigation after the later input when none actually occurred.
        graph.nodes[-2].params.pop("after_node")
        runner = GraphRunner(graph)
        try:
            assert await runner.run() == "FAILED"
            failure = runner.results[-1]
            assert failure.error_code == "WAIT_URL_TIMEOUT"
            assert failure.details["reason"] == "no_navigation_after_action"
            assert "stage=two" in failure.details["actual_url"]
        finally:
            await runner.close()


@pytest.mark.parametrize(
    "value,protected,expected", [(3, False, "3"), (3, True, "3"), (False, True, "false")]
)
async def test_numeric_and_protected_variables_can_fill_browser_fields(value, protected, expected):
    with serve_maker() as (server, base):
        vault = Vault()
        stored = vault.put_value(value) if protected else value
        graph = sequence(
            Node(type="Start"),
            Node(type="Browser.OpenURL", params={"url": base + "/"}),
            Node(type="Browser.Click", params={"selector": "#show"}),
            Node(type="Browser.Input", params={"selector": "#note", "value": "{{number}}"}),
            Node(type="Browser.Click", params={"selector": "#continue"}),
            Node(type="End"),
            variables={"number": Variable(value=stored, secret=protected)},
        )
        runner = GraphRunner(graph, vault=vault)
        try:
            assert await runner.run() == "SUCCEEDED"
            assert server.posts["1"]["note"] == expected
            if protected:
                assert runner.context.variables.reveal(runner.context.variables.get("number")) == value
        finally:
            await runner.close()


async def test_protected_action_results_preserve_json_types_for_later_comparisons():
    first = Node(
        type="Variables.Compare",
        params={"left": 3, "right": 3},
        save_as="ok",
        result_path="value",
        protect_result=True,
    )
    second = Node(type="Variables.Compare", params={"left": "{{ok}}", "right": True})
    runner = GraphRunner(sequence(Node(type="Start"), first, second, Node(type="End.Success")))
    try:
        assert await runner.run() == "SUCCEEDED"
        assert runner.context.variables.reveal(runner.context.variables.get("ok")) is True
        assert runner.context.variables.snapshot()["ok"] == "••••"
    finally:
        await runner.close()
