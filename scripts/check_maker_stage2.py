"""Real browser/data/debugger graph, then the same saved format in a GUI-free process."""

import argparse
import asyncio
import json
from pathlib import Path
import subprocess
import sys

from poster.auth import ensure_local_key
from poster.maker.fixtures import serve_maker
from poster.maker.inspector import Inspector
from poster.maker.runtime import GraphRunner
from poster.maker.samples import loop_graph
from poster.maker.storage import Vault, save_graph


async def scenario(directory):
    directory.mkdir(parents=True, exist_ok=False)
    ensure_local_key(directory / "private")
    with serve_maker() as (server, base):
        graph = loop_graph(base)
        graph.node("input").breakpoint = True
        runner = GraphRunner(graph, inputs={"prefix": "Stage2"})
        try:
            assert await runner.run() == "PAUSED" and runner.current == "input"
            page = await runner.context.page()
            browser = runner.context.session.browser
            assert len(server.posts) == 0
            selected = []
            inspector = Inspector(runner.context.session, on_pick=selected.append)
            await inspector.enable()
            await page.locator("#note").click()
            for _ in range(100):
                if selected:
                    break
                await asyncio.sleep(0.02)
            assert selected and (await inspector.check(selected[0]["xpath"]))["count"] == 1
            node = graph.node("input")
            node.params["selector"] = selected[0]["xpath"]
            node.params["value"] = "Изменено: {{note}}"
            node.breakpoint = False
            runner.update_node(node)
            await runner.execute_selected("input")
            assert runner.current == "input" and len(server.posts) == 0
            runner.set_variable("debug_marker", "only-this-run")
            assert await runner.run() == "SUCCEEDED"
            assert runner.context.session.browser is browser and await runner.context.page() is page
            assert runner.context.variables.get("last_text") == "Изменено: Stage2 · two"
            assert runner.context.variables.get("counter") == 2
            assert len(server.posts) == 2
        finally:
            await runner.close()
        path = directory / "stage2.postergraph.json"
        save_graph(path, graph, Vault())
        code = """import sys,importlib.abc
class NoGUI(importlib.abc.MetaPathFinder):
 def find_spec(self,fullname,path=None,target=None):
  if fullname.startswith(('tkinter','PySide6')):raise RuntimeError('GUI dependency in batch runtime')
sys.meta_path.insert(0,NoGUI())
from poster.cli import main
raise SystemExit(main(sys.argv[1:]))
"""
        replay = subprocess.run(
            [sys.executable, "-c", code, "maker-run", str(path), "--input", "prefix=Reload"],
            text=True,
            capture_output=True,
            timeout=60,
        )
        assert replay.returncode == 0
        result = json.loads(replay.stdout)
        assert result["status"] == "SUCCEEDED" and result["variables"]["counter"] == 2
        assert result["variables"]["last_text"] == "Изменено: Reload · two"
        assert "debug_marker" not in result["variables"] and len(server.posts) == 4
        report = {
            "status": "PASSED",
            "platform": sys.platform,
            "graph_version": 2,
            "nodes": len(graph.nodes),
            "breakpoint_live_edit_same_browser": True,
            "isolated_selected_action_preserves_cursor": True,
            "picker_css_xpath": True,
            "variables_inputs_substitution_outputs": True,
            "foreach_break_continue_switch": True,
            "save_reload_new_process": True,
            "runtime_data_isolation": True,
            "gui_imports_forbidden": True,
            "form_posts": len(server.posts),
        }
        (directory / "report.json").write_text(json.dumps(report, indent=2))
        print("::notice title=Maker stage2 headless E2E::" + json.dumps(report), flush=True)
        return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(scenario(args.output))
