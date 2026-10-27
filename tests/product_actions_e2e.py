"""Complete original-runner coverage, including explicit terminal failure and tab lifecycle."""

from poster.maker.example_projects import chain
from poster.maker.runtime import GraphRunner
from poster.maker.model import Node
from poster.maker.fixtures import serve_maker
from test_maker_stage2 import sequence


async def test_original_browser_new_tab_close_tab_and_variable_delete():
    with serve_maker() as (_, base):
        graph = chain(
            "Tab lifecycle",
            [
                ("Browser.OpenURL", dict(url=base + "/tabs")),
                ("Browser.NewTab", dict(url=base + "/login", tab_id="extra")),
                ("Browser.CloseTab", {}),
                ("Variables.Set", dict(name="transient", value="temporary")),
                ("Variables.Delete", dict(name="transient")),
            ],
        )
        runner = GraphRunner(graph)
        try:
            assert await runner.run() == "SUCCEEDED"
            assert len(runner.context.session.context.pages) == 1
            assert "transient" not in runner.context.variables.data
            assert all(result.status == "SUCCEEDED" for result in runner.results)
        finally:
            await runner.close()


async def test_explicit_failure_terminal_is_executed_by_original_runner():
    runner = GraphRunner(sequence(Node(type="Start"), Node(type="End.Failure")))
    try:
        assert await runner.run() == "FAILED"
        assert runner.results[-1].action == "End.Failure"
    finally:
        await runner.close()
