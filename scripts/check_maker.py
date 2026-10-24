"""Real browser recording -> parameter edit -> replay -> save/load -> headless replay."""

import argparse
import asyncio
from dataclasses import asdict
import json
from pathlib import Path
import sys

from poster.auth import ensure_local_key
from poster.maker.fixtures import DEMO_PASSWORD, serve_maker
from poster.maker.recorder import ActionRecorder
from poster.maker.runtime import GraphRunner
from poster.maker.storage import load_graph, save_graph


async def scenario(directory):
    directory.mkdir(parents=True, exist_ok=True)
    ensure_local_key(directory / "private")
    gui_before = {name for name in sys.modules if name.startswith("tkinter")}
    with serve_maker() as (server, base):
        snapshots = []
        recording = ActionRecorder(headless=True, on_change=lambda g: snapshots.append(g))
        try:
            await recording.start()
            await recording.navigate(base + "/")
            page = recording.session.page
            await page.locator("#show").click()
            await page.locator("#note").fill("Записано Ж 日本語")
            await page.locator("#mode").focus()
            await page.locator("#mode").press("ArrowDown")
            await page.locator("#mode").press("Enter")
            await page.locator("#continue").click()
            await page.locator("#done").wait_for()
            graph = await recording.stop()
        finally:
            await recording.close()
        assert len(server.posts) == 1
        assert sum(n.type == "Browser.OpenURL" for n in graph.nodes) == 1, [
            (n.type, n.params) for n in graph.nodes
        ]
        assert any(n.type == "Browser.Click" for n in graph.nodes)
        assert any(n.type == "Browser.Select" for n in graph.nodes)
        entry = next(n for n in graph.nodes if n.type == "Browser.Input" and n.params["selector"] == "#note")
        entry.params["value"] = "Изменено в кубике Ж 日本語"
        entry.x += 31
        entry.y += 17
        graph.nodes.reverse()  # Execution must follow arrows, not file/list/coordinate order.
        runner = GraphRunner(graph, vault=recording.vault, headless=True)
        try:
            assert await runner.run() == "SUCCEEDED", [(r.action, r.error_code) for r in runner.results]
            assert (
                await (await runner.context.page()).locator("#result").inner_text() == entry.params["value"]
            )
        finally:
            await runner.close()
        path = directory / "edited.postergraph.json"
        save_graph(path, graph, recording.vault)
        restored, vault = load_graph(path)
        assert restored.node(entry.id).x == entry.x and restored.node(entry.id).y == entry.y
        restarted = GraphRunner(restored, vault=vault, headless=True)
        try:
            assert await restarted.run() == "SUCCEEDED"
        finally:
            await restarted.close()
        assert len(server.posts) == 3 and all(
            p["note"] == entry.params["value"] for k, p in server.posts.items() if k != "1"
        )

        secret_recording = ActionRecorder(headless=True, on_change=lambda g: snapshots.append(g))
        try:
            await secret_recording.start()
            await secret_recording.navigate(base + "/login")
            page = secret_recording.session.page
            await page.locator("#user").fill("demo")
            await page.locator("#password").fill(DEMO_PASSWORD)
            await page.locator("#login").click()
            await page.locator("#account").wait_for()
            secret_graph = await secret_recording.stop()
        finally:
            await secret_recording.close()
        assert server.login_count == 1
        password = next(
            n for n in secret_graph.nodes if n.type == "Browser.Input" and n.params["selector"] == "#password"
        )
        assert password.params["sensitive"] and set(password.params["value"]) == {"secret_ref"}
        secret_path = directory / "auth.postergraph.json"
        saved = save_graph(secret_path, secret_graph, secret_recording.vault)
        assert DEMO_PASSWORD not in secret_path.read_text(encoding="utf-8")
        assert DEMO_PASSWORD.encode("utf-8") not in (directory / saved.secret_file).read_bytes()
        restored, vault = load_graph(secret_path)
        authenticated = GraphRunner(restored, vault=vault, headless=True)
        try:
            assert await authenticated.run() == "SUCCEEDED", [
                (r.action, r.error_code) for r in authenticated.results
            ]
            assert server.login_count == 2
            assert DEMO_PASSWORD not in json.dumps(
                [asdict(r) for r in authenticated.results], ensure_ascii=False
            )
        finally:
            await authenticated.close()
        assert DEMO_PASSWORD not in json.dumps(snapshots, ensure_ascii=False)
        return {
            "status": "PASSED",
            "recorded_nodes": len(graph.nodes),
            "connected_actions": True,
            "edited_parameter_replayed": True,
            "save_reload_replayed": True,
            "coordinates_preserved": True,
            "normal_post_login": True,
            "encrypted_secret_replayed": True,
            "tk_imported": bool({name for name in sys.modules if name.startswith("tkinter")} - gui_before),
            "platform": sys.platform,
            "writes": len(server.posts),
            "logins": server.login_count,
        }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path(".poster/maker-check"))
    args = parser.parse_args()
    report = asyncio.run(scenario(args.output))
    (args.output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report))
