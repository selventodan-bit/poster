"""Native unified UI integration; Windows also runs actual OS mouse/keyboard tests."""

import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import json
import re

import pytest

from poster.maker.model import Document, Edge, Graph, Node
from poster.maker.profiles import Profiles


def test_document_redo_and_profile_management_are_backward_compatible(tmp_path):
    doc = Document()
    before = doc.graph.model_dump_json()
    node = doc.add("Browser.Click", {"selector": "#one"})
    doc.undo()
    assert doc.graph.model_dump_json() == before
    doc.redo()
    assert doc.graph.node(node.id).params == {"selector": "#one"}
    doc.undo()
    doc.add("Browser.Click", {"selector": "#two"})
    assert not doc.future
    profiles = Profiles(tmp_path)
    first, second = profiles.create("First"), profiles.create("Second")
    profiles.rename(first, "Renamed")
    assert profiles.open(first).data["name"] == "Renamed"
    profiles.delete(first)
    assert profiles.open(second).data["name"] == "Second"


def large_graph(count=1200):
    nodes = [Node(id="start", type="Start", x=0, y=0)]
    nodes += [
        Node(
            id="node_" + str(i),
            type="Variables.Set",
            title="Large " + str(i),
            params={"name": "value", "value": i},
            x=(i % 20) * 260,
            y=(i // 20 + 1) * 130,
        )
        for i in range(count - 2)
    ]
    nodes += [Node(id="end", type="End", x=0, y=(count // 20 + 1) * 130)]
    return Graph(
        entry="start",
        nodes=nodes,
        edges=[Edge(source=a.id, target=b.id) for a, b in zip(nodes, nodes[1:], strict=False)],
        max_steps=2000,
    )


@pytest.mark.skipif(
    os.environ.get("POSTER_TEST_QT") != "1" or importlib.util.find_spec("PySide6") is None,
    reason="Explicit native Qt suite on Windows/Xvfb",
)
def test_unified_native_record_edit_debug_profiles_and_linux_format(tmp_path):
    process = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=180,
    )
    if process.returncode:
        for line in process.stdout.splitlines():
            if line.startswith("::error title=Native Qt wait state::"):
                print(line, flush=True)
        frames = re.findall(r'File "([^"]+)", line (\d+)', process.stderr)
        print(
            "::error::Native Qt failure at "
            + " > ".join(Path(name).name + ":" + line for name, line in frames[-4:]),
            flush=True,
        )
        observer = tmp_path / "data-tabs-observer.json"
        if observer.is_file():
            state = json.loads(observer.read_text(encoding="utf-8"))
            print(
                "::error title=Native Qt geometry::"
                + json.dumps(
                    {
                        k: state.get(k)
                        for k in ("tools_page", "tools_tabs", "tools_bar", "tools_tab_titles", "panel_sizes")
                    }
                ),
                flush=True,
            )
    assert process.returncode == 0, process.stdout + process.stderr


def scenario(root):
    import asyncio
    import json
    import time

    from PySide6.QtCore import Qt, QPoint
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QLineEdit, QMessageBox, QInputDialog, QToolButton, QToolBar

    from poster.maker.qt_app import MakerWindow
    from poster.maker.qt_browser import configure_browser
    from poster.recorder.workspace import Workspace
    from poster.maker.storage import load_graph

    os.chdir(root)
    port = configure_browser(root)
    app = QApplication([])
    workspace = Workspace(Path(root) / ".poster")
    from poster.recorder.preferences import Preferences
    from poster.browser import find_browser

    Preferences(workspace.root / "recorder-settings.json").save(
        "https://saved.example/form?token=private", "saved-project"
    )
    browser = find_browser()
    if browser:
        (workspace.root / "browser.json").write_text(json.dumps({"path": browser}))
    window = MakerWindow(port, workspace=workspace)
    assert window.legacy.url.text() == "https://saved.example/form"
    assert window.legacy.project_id.text() == "saved-project"
    if browser:
        assert os.environ["POSTER_CHROMIUM"] == browser
    window.show()
    QMessageBox.question = lambda *args, **kwargs: QMessageBox.StandardButton.Yes

    def wait(predicate, label, timeout=35):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            app.processEvents()
            if window.last_error:
                raise AssertionError(label + ": " + str(window.last_error))
            if predicate():
                return
            # processEvents above services Qt; explicitly yield Python's worker
            # between polls, including cold XLSX imports on Windows.
            time.sleep(0.01)
        import traceback

        print(
            "::error title=Native Qt wait state::"
            + json.dumps(
                {
                    "label": label,
                    "busy": window.busy,
                    "run_state": window.run_state,
                    "last_results": [
                        {k: r.get(k) for k in ("node_id", "action", "status", "error_code", "details")}
                        for r in window.trace[-4:]
                    ],
                    "tools_bar": window.tools_tabs.tabBar().rect().getRect(),
                    "threads": [
                        [
                            Path(f.filename).name + ":" + str(f.lineno) + ":" + f.name
                            for f in traceback.extract_stack(frame)[-12:]
                        ]
                        for frame in sys._current_frames().values()
                    ],
                }
            ),
            flush=True,
        )
        raise AssertionError("Unified UI timeout: " + label)

    def action(name):
        control = window.controls[name]
        control.trigger() if hasattr(control, "trigger") else QTest.mouseClick(
            control, Qt.MouseButton.LeftButton
        )
        app.processEvents()

    def browser_work(coroutine):
        task = asyncio.run_coroutine_threadsafe(coroutine, window.worker.loop)
        wait(task.done, "browser operation")
        return task.result()

    def native_click(selector):
        async def box():
            page = window.controller.session().page
            locator = page.locator(selector)
            await window.controller.session().provider.prepare_pointer(locator, 10000)
            return await locator.bounding_box()

        rectangle = browser_work(box())
        from PySide6.QtCore import QPoint

        QTest.mouseClick(
            window.browser.current().focusProxy() or window.browser.current(),
            Qt.MouseButton.LeftButton,
            pos=QPoint(
                int(rectangle["x"] + rectangle["width"] / 2), int(rectangle["y"] + rectangle["height"] / 2)
            ),
        )
        QTest.qWait(60)

    try:
        wait(lambda: window.browser.current().page().devToolsId(), "window starts")
        assert window.browser.current().window() is window
        assert window.canvas.window() is window
        assert window.action_list.count() == len(window.registry.actions) == 78
        window.action_category.setFocus()
        QTest.keyClick(window.action_category, Qt.Key.Key_Home)
        QTest.keyClick(window.action_category, Qt.Key.Key_Down)
        window.action_search.setText("HTTP.Post")
        action("add")
        assert window.document.graph.node(window.selected_id).type == "HTTP.Post"
        action("undo")
        QTest.keyClick(window.action_category, Qt.Key.Key_Home)
        window.action_search.clear()
        window.live_values = {"runtime_number": 17, "runtime_secret": "••••"}
        window.refresh_variables()
        assert window.variables_table.rowCount() == 2
        window.variables_table.selectRow(0)
        assert window.variable_name.text() == "runtime_number"
        assert window.variable_value.toPlainText() == "17"
        window.variables_table.selectRow(1)
        assert window.variable_secret.isChecked()
        assert window.variable_value.toPlainText() == ""
        window.splitter.setSizes([280, 380])
        app.processEvents()
        assert window.browser.height() > 100 and window.canvas.height() > 100
        started = time.monotonic()
        window.replace(large_graph(), dirty=False)
        assert not window.live_values and window.variables_table.rowCount() == 0
        window.canvas.fit()
        app.processEvents()
        assert len(window.canvas.blocks) == 1200 and time.monotonic() - started < 12
        window.canvas.find("Large 1190")
        assert window.selected_id == "node_1190"
        action("copy")
        copied = window.selected_id
        assert len(window.document.graph.nodes) == 1201
        action("undo")
        assert len(window.document.graph.nodes) == 1200
        action("redo")
        search = window.findChild(QLineEdit, "node_search")
        search.setFocus()
        QTest.keyClicks(search, copied)
        QTest.keyClick(search, Qt.Key.Key_Return)
        wait(lambda: window.selected_id == copied, "copied block selected through search")
        action("delete")
        assert len(window.document.graph.nodes) == 1200
        assert copied not in window.canvas.blocks
        window.document.dirty = False
        action("new")
        wait(lambda: not window.busy, "new graph")
        action("demo")
        wait(lambda: bool(window.demo_base) and not window.busy, "owned demo")
        QTest.keyClick(window.browser.address, Qt.Key.Key_Return)
        wait(lambda: window.controller.session() is not None and not window.busy, "browser opened")
        action("record")
        wait(lambda: window.recording and not window.busy, "record started")
        native_click("#show")
        native_click("#note")
        QTest.keyClicks(
            window.browser.current().focusProxy() or window.browser.current(), "Recorded in one window"
        )
        native_click("#continue")
        wait(lambda: len(window.demo_server.posts) == 1, "native form submitted")
        action("record_stop")
        wait(lambda: not window.recording and not window.busy, "record stopped")
        assert len(window.document.graph.nodes) >= 8
        node = next(n for n in window.document.graph.nodes if n.type == "Browser.Input")
        window.canvas.choose([node.id])
        window.property_widgets["value"][1].setPlainText("Edited cube")
        action("apply")
        assert window.document.graph.node(node.id).params["value"] == "Edited cube"
        action("breakpoint")
        action("save")
        action("run")
        wait(lambda: window.run_state == "PAUSED" and not window.busy, "breakpoint")
        page = window.controller.runner.context.session.page
        browser_work(
            page.locator("#note").evaluate(
                "e=>{for(let i=0;i<30;i++)e.setAttribute('data-long-'+i,'attribute '.repeat(20))}"
            )
        )
        window.resize(1050, 650)
        window.tools_tabs.setCurrentIndex(2)
        action("pick")
        wait(lambda: not window.busy, "picker enabled")
        native_click("#note")
        wait(lambda: bool(window.last_pick), "element picked")
        window.selector.setText("xpath=//input[@id='note']")
        action("selector_check")
        wait(lambda: not window.busy, "XPath check")
        assert window.selector_result["count"] == 1
        assert not window.controls["selector_apply"].visibleRegion().isEmpty()
        assert window.element_info.height() <= 100
        action("selector_apply")
        assert window.document.graph.node(node.id).params["selector"] == "xpath=//input[@id='note']"
        window.tools_tabs.setCurrentIndex(0)
        window.property_widgets["value"][1].setPlainText("Edited while paused")
        action("apply")
        action("run")
        wait(lambda: window.run_state == "SUCCEEDED" and not window.busy, "resume")
        assert window.controller.runner.context.session.page is page
        assert window.demo_server.posts["2"]["note"] == "Edited while paused"
        action("save")
        graph, vault = load_graph(window.path)
        assert graph.schema_version == 2
        assert graph.metadata["maker_demo"] == window.demo_base
        assert all(r["status"] == "SUCCEEDED" for r in window.trace)
        window.grab().save(str(Path(root) / "unified-ui.png"))
        # A separately started Python process must never import either desktop framework.
        code = """import sys,importlib.abc
class NoGUI(importlib.abc.MetaPathFinder):
 def find_spec(self,fullname,path=None,target=None):
  if fullname.startswith(('tkinter','PySide6')):raise RuntimeError('GUI dependency in runner')
sys.meta_path.insert(0,NoGUI())
from poster.cli import main
raise SystemExit(main(sys.argv[1:]))
"""
        process = subprocess.run(
            [sys.executable, "-c", code, "maker-run", str(window.path)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert process.returncode == 0, process.stderr + process.stdout
        assert json.loads(process.stdout)["status"] == "SUCCEEDED"
        from poster.maker.example_projects import EXAMPLES

        QInputDialog.getText = lambda *args, **kwargs: (kwargs.get("text", ""), True)
        for label, filename in EXAMPLES:
            old = window.controller.runner
            QInputDialog.getItem = lambda *args, label=label, **kwargs: (label, True)
            action("data_examples")
            wait(
                lambda filename=filename: window.path and window.path.name == filename and not window.busy,
                "load " + label,
            )
            if old and old.context.http:
                assert old.context.http.client.is_closed
            action("run")
            wait(lambda: window.run_state == "SUCCEEDED" and not window.busy, "run " + label)
            assert window.controller.runner.context.session.page is None
            if filename.startswith("02"):
                window.canvas.choose(["n4"])
                assert window.result_table.rowCount() == 3
            elif filename.startswith("03"):
                window.canvas.choose(["n3"])
                assert window.result_table.rowCount() == 2 and window.result_table.columnCount() == 2
            elif filename.startswith("04"):
                assert window.live_values["result"] == "POSTER Ж"
            action("save")
        observation = Path(root) / "data-tabs-observer.json"
        old_observer = os.environ.get("POSTER_GUI_TEST_OUTPUT")
        os.environ["POSTER_GUI_TEST_OUTPUT"] = str(observation)
        try:
            for title in ("Результат", "Кубик", "Проект", "Переменные"):
                for _ in range(12):
                    app.processEvents()
                    window.observe()
                    state = json.loads(observation.read_text(encoding="utf-8"))
                    if any(name == title for name, _ in state["tools_tabs"]):
                        point = next(p for name, p in state["tools_tabs"] if name == title)
                        bar = window.tools_tabs.tabBar()
                        QTest.mouseClick(
                            bar, Qt.MouseButton.LeftButton, pos=bar.mapFromGlobal(QPoint(*point))
                        )
                        app.processEvents()
                        assert window.tools_tabs.tabText(window.tools_tabs.currentIndex()) == title
                        break
                    titles = state["tools_tab_titles"]
                    right = titles.index(title) > max(titles.index(name) for name, _ in state["tools_tabs"])
                    button = window.findChild(
                        QToolButton, "tools_scroll_right" if right else "tools_scroll_left"
                    )
                    assert button and button.isVisible()
                    QTest.mouseClick(button, Qt.MouseButton.LeftButton)
                    QTest.qWait(150)
                else:
                    raise AssertionError("Tool tab is unreachable: " + title)
        finally:
            if old_observer is None:
                os.environ.pop("POSTER_GUI_TEST_OUTPUT", None)
            else:
                os.environ["POSTER_GUI_TEST_OUTPUT"] = old_observer
        from poster.maker.example_projects import chain

        window.replace(chain("Ошибка данных", [("JSON.Parse", {"text": "not JSON"})]), dirty=False)
        window.path = None
        action("run")
        wait(lambda: window.run_state == "FAILED" and not window.busy, "data error shown in UI")
        assert "JSON_INVALID" in window.result_json.toPlainText()
        assert "Некорректный JSON" in window.result_json.toPlainText()
        # Emulate the constructor's Maker page with a layout saved on System.
        # Saving legacy-page visibility must not hide Maker controls on reopen.
        window.pages.setCurrentWidget(window.legacy.system_page)
        window.save_settings()
        window.pages.blockSignals(True)
        window.pages.setCurrentWidget(window.maker_page)
        window.pages.blockSignals(False)
        window.restore_settings()
        app.processEvents()
        toolbar = window.findChild(QToolBar, "project_toolbar")
        assert toolbar.widgetForAction(window.controls["data_examples"]).isVisible()
        assert window.tools_dock.isVisible()
        print("UNIFIED_NATIVE_SCENARIO_PASSED", flush=True)
    finally:
        window.document.dirty = False
        window.close()
        wait(lambda: window.close_ready, "clean close")
        wait(lambda: not window.worker.thread.is_alive(), "worker exits")
        assert not window.isVisible()


if __name__ == "__main__":
    scenario(Path(sys.argv[1]))
