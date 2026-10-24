import asyncio
import importlib.util
import json
import os
import subprocess
import sys

import pytest

from poster.maker.fixtures import DEMO_PASSWORD, serve_maker
from poster.maker.inspector import Inspector
from poster.maker.model import GraphError, Node
from poster.maker.profiles import Profiles
from poster.maker.recorder import ActionRecorder
from poster.maker.runtime import BrowserSession, GraphRunner
from poster.maker.storage import load_graph, save_graph
from test_maker_stage2 import sequence


def providers():
    return [
        "chromium",
        pytest.param(
            "qt",
            marks=pytest.mark.skipif(
                os.environ.get("POSTER_TEST_QT") != "1" or importlib.util.find_spec("PySide6") is None,
                reason="Qt native suite is run explicitly on Windows / Xvfb",
            ),
        ),
    ]


async def eventually(predicate, seconds=10):
    deadline = asyncio.get_running_loop().time() + seconds
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("Expected browser event was not observed")
        await asyncio.sleep(0.03)


@pytest.mark.parametrize("provider", providers())
async def test_real_record_popup_iframe_edit_breakpoint_save_replay(tmp_path, provider):
    with serve_maker() as (server, base):
        session = BrowserSession(True, provider=provider)
        warnings = []
        recorder = ActionRecorder(session=session, on_warning=warnings.append)
        try:
            await recorder.start()
            await recorder.navigate(base + "/tabs")
            main = session.page
            await main.locator("#main-note").fill("recorded")
            await main.locator("#choice").select_option("b")
            await main.locator("#popup").click()
            await eventually(lambda: len(session.context.pages) == 2 and len(session.tabs) == 2)
            popup = session.context.pages[-1]
            await popup.wait_for_load_state()
            frame = popup.frame_locator("#inner")
            await frame.locator("#user").fill("demo")
            await frame.locator("#password").fill(DEMO_PASSWORD)
            await session.provider.prepare_pointer(frame.locator("#login"), 10000)
            await frame.locator("#login").click()
            await frame.locator("#account").wait_for()
            await eventually(lambda: server.login_count == 1)
            await main.locator("#main-note").fill("after-popup")
            graph = await recorder.stop()
        finally:
            await recorder.close()
        assert not warnings, warnings
        assert DEMO_PASSWORD not in graph.model_dump_json()
        assert sum(n.type == "Browser.WaitPopup" for n in graph.nodes) == 1
        assert any(n.type == "Browser.SwitchTab" for n in graph.nodes)
        assert any(n.params.get("frame") == ["#inner"] for n in graph.nodes)
        assert all(n.extensions.get("browser_tab") for n in graph.nodes if n.type.startswith("Browser."))
        input_node = next(n for n in graph.nodes if n.type == "Browser.Input")
        input_node.breakpoint = True
        path = tmp_path / "recorded.json"
        save_graph(path, graph, recorder.vault)
        # The SAME portable graph runs through both providers, including Linux without GUI.
        for replay_provider in dict.fromkeys([provider, "chromium"]):
            saved, vault = load_graph(path)
            runner = GraphRunner(saved, vault=vault, provider=replay_provider)
            try:
                assert await runner.run() == "PAUSED"
                changed = saved.node(input_node.id).model_copy(deep=True)
                changed.params["value"] = "edited"
                runner.update_node(changed)
                assert await runner.run() == "SUCCEEDED", [(r.action, r.error_code) for r in runner.results]
                assert (
                    await runner.context.session.tabs["main"].locator("#main-note").input_value()
                    == "after-popup"
                )
                assert server.login_count >= 2
                save_graph(path, runner.graph, vault)
            finally:
                await runner.close()


@pytest.mark.parametrize("provider", providers())
async def test_encrypted_profile_auth_restore_isolation_and_explicit_save(tmp_path, provider):
    store = Profiles(tmp_path / "profiles")
    key, other = store.create("Account A"), store.create("Account B")
    with serve_maker() as (_, base):
        session = BrowserSession(True, provider=provider, profile=store.open(key))
        second = BrowserSession(True, provider=provider, profile=store.open(other))
        try:
            await session.start()
            await session.page.goto(base + "/login")
            await session.page.locator("#user").fill("demo")
            await session.page.locator("#password").fill(DEMO_PASSWORD)
            await session.page.locator("#login").click()
            await session.page.locator("#account").wait_for()
            await session.page.evaluate("localStorage.setItem('test','private-session')")
            await session.profile.save(session)
            await second.start()
            assert (await second.page.goto(base + "/account")).status == 401
        finally:
            await session.close()
            await second.close()
        assert b"private-session" not in store.path(key).read_bytes()
        restored = BrowserSession(True, provider=provider, profile=store.open(key))
        try:
            await restored.start()
            assert (await restored.page.goto(base + "/account")).status == 200
            assert await restored.page.evaluate("localStorage.getItem('test')") == "private-session"
            await restored.page.evaluate("localStorage.clear()")
            await restored.page.reload()
            assert await restored.page.evaluate("localStorage.length") == 0
        finally:
            await restored.close()
        store.clear(other)
        assert store.open(key).data["state"]["cookies"]


def test_profile_revision_conflict_and_path_safety(tmp_path):
    store = Profiles(tmp_path)
    key = store.create("Test")
    first, second = store.open(key), store.open(key)
    first.commit({"cookies": []})
    with pytest.raises(GraphError, match="PROFILE_CONFLICT"):
        second.commit({})
    for name in ("../other", "", "profile_../bad"):
        with pytest.raises(GraphError, match="PROFILE_INVALID"):
            store.open(name)


async def test_missing_recorded_tab_fails_without_clicking_another_page():
    click = Node(type="Browser.Click", params={"selector": "body"}, extensions={"browser_tab": "missing"})
    runner = GraphRunner(sequence(Node(type="Start"), click, Node(type="End")))
    try:
        assert await runner.run() == "FAILED"
        assert runner.results[-1].error_code == "TAB_CONTEXT_MISSING"
    finally:
        await runner.close()


@pytest.mark.parametrize("provider", providers())
async def test_inspector_uses_exact_tab_and_frame_and_provider_shutdown(provider):
    with serve_maker() as (_, base):
        session = BrowserSession(True, provider=provider)
        picked = []
        process = None
        try:
            await session.start()
            page = await session.new_page("form")
            await page.goto(base + "/iframe")
            inspector = Inspector(session, on_pick=picked.append)
            await inspector.enable()
            await session.provider.prepare_pointer(page.frame_locator("#inner").locator("#user"), 10000)
            await page.frame_locator("#inner").locator("#user").click()
            await eventually(lambda: bool(picked))
            assert picked[0]["tab_id"] == "form" and picked[0]["frame"] == ["#inner"]
            assert (await inspector.check("xpath=//*[@id='user']", ["#inner"]))["count"] == 1
            process = getattr(session.provider, "process", None)
        finally:
            await session.close()
        if process:
            assert process.returncode == 0
            assert not os.path.exists(session.provider.directory.name)


def test_default_runner_import_does_not_load_desktop_frameworks():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from poster.maker.runtime import GraphRunner; import sys,json; print(json.dumps([x for x in sys.modules if x.startswith(('tkinter','PySide6'))]))",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(result.stdout) == []
