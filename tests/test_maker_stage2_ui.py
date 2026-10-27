"""Native widget/canvas integration; packaged Windows tests also use real OS input."""

import os
from pathlib import Path
import subprocess
import sys
import time

import pytest


@pytest.mark.skipif(not os.environ.get("DISPLAY") and os.name != "nt", reason="Native GUI required")
def test_manual_stage2_graph_and_live_debugger_in_native_editor(tmp_path):
    source = """import os,sys
sys.path.insert(0,sys.argv[1]);os.chdir(sys.argv[2])
from test_maker_stage2_ui import scenario
scenario()
"""
    process = subprocess.run(
        [sys.executable, "-c", source, str(Path(__file__).parent), str(tmp_path)],
        text=True,
        capture_output=True,
        timeout=150,
    )
    if process.returncode:
        for line in process.stdout.splitlines():
            if line.startswith("::error title=Maker Tk state::"):
                print(line, flush=True)
    assert process.returncode == 0, process.stderr


def scenario():
    import tkinter as tk
    from tkinter import messagebox
    from poster.recorder.app import App
    from poster.maker.storage import load_graph

    root = tk.Tk()
    app = App(root)
    maker = app.maker
    app.notebook.select(maker.frame)
    root.geometry("940x640")
    root.update()
    maker.tools_notebook.select(maker.browser_page)
    root.update()
    assert "maker_selector_apply" in maker.observation()["positions"]
    assert maker.buttons["selector_apply"].winfo_width() == maker.buttons["selector_apply"].winfo_reqwidth()
    maker.tools_notebook.select(maker.properties_page)
    messagebox.askyesno = lambda *a, **k: True

    def wait(predicate, phase, timeout=40):
        # Browser launch itself permits 30 seconds. Keep pumping real Tk events
        # while a cold Windows browser starts; never re-invoke the GUI action.
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            root.update()
            if predicate():
                return
            time.sleep(0.02)
            if not maker.busy and (maker.last_error or maker.runner and maker.runner.state == "FAILED"):
                break
        import json

        print(
            "::error title=Maker Tk state::"
            + json.dumps(
                dict(
                    phase=phase,
                    busy=maker.busy,
                    state=maker.runner.state if maker.runner else None,
                    pause=maker.runner.pause_reason if maker.runner else None,
                    code=maker.last_error,
                )
            ),
            flush=True,
        )
        raise AssertionError("Maker UI state timeout: " + phase)

    def fill(widget, text):
        if isinstance(widget, tk.Text):
            widget.delete("1.0", "end")
            widget.insert("1.0", text)
        else:
            widget.delete(0, "end")
            widget.insert(0, text)

    def add(kind, **params):
        maker.library.selection_clear(0, "end")
        maker.library.selection_set(maker.action_names.index(kind))
        maker.buttons["add"].invoke()
        root.update()
        node = maker.document.graph.node(next(iter(maker.canvas.selected)))
        for key, value in params.items():
            widget = maker.props[key][1]
            if hasattr(widget, "set"):
                widget.set(value)
            else:
                fill(widget, value)
        assert maker.apply(), maker.status.get()
        return node

    def click(point):
        canvas = maker.canvas.widget
        x = int(point[0] - canvas.canvasx(0))
        y = int(point[1] - canvas.canvasy(0))
        canvas.event_generate("<Motion>", x=x, y=y)
        root.update()
        canvas.event_generate("<ButtonPress-1>", x=x, y=y)
        canvas.event_generate("<ButtonRelease-1>", x=x, y=y)
        root.update()

    def connect(source, port, target):
        maker.canvas.fit()
        root.update()
        click(maker.canvas.port_points[source, port])
        click(maker.canvas.port_points[target, "input"])
        assert maker.document.graph.successor(source, port) == target, (source, port, target)

    try:
        maker.variable_name.set("counter")
        fill(maker.variable_value, "0")
        maker.buttons["var_save"].invoke()
        assert maker.document.graph.variables["counter"].value == 0
        maker.buttons["demo"].invoke()
        wait(lambda: not maker.busy, "demo ready")
        open_url = add("Browser.OpenURL", url=maker.url.get())
        assert maker.document.graph.metadata["maker_demo"] == maker.demo_base
        start = maker.document.graph.entry
        end = next(n.id for n in maker.document.graph.nodes if n.type == "End")
        loop = add("Logic.Loop", count="3", max_iterations="5")
        increment = add("Variables.Increment", name="counter")
        compare = add("Variables.Compare", left="{{counter}}", right="2", operator="ge")
        break_node = add("Logic.Break")
        continue_node = add("Logic.Continue")
        switch = add("Logic.Switch", value="{{counter}}", cases="[2]")
        fail = add("End.Failure")
        for a, port, b in [
            (start, "success", open_url.id),
            (open_url.id, "success", loop.id),
            (loop.id, "success", increment.id),
            (increment.id, "success", compare.id),
            (compare.id, "success", break_node.id),
            (compare.id, "error", continue_node.id),
            (loop.id, "error", switch.id),
            (switch.id, "case:0", end),
            (switch.id, "success", fail.id),
        ]:
            connect(a, port, b)
        maker.canvas.selected = {increment.id}
        maker.select(increment.id)
        maker.buttons["breakpoint"].invoke()
        assert increment.breakpoint
        maker.buttons["run"].invoke()
        wait(
            lambda: maker.runner and maker.runner.pause_reason == "breakpoint" and not maker.busy,
            "browser ready at breakpoint",
        )
        assert maker.runner.context.variables.get("counter") == 0
        assert maker.runner.current == increment.id
        fill(maker.props["amount"][1], "2")
        maker.output_name.set("last_increment")
        maker.output_path.set("value")
        assert maker.apply(), maker.status.get()
        maker.buttons["run"].invoke()
        wait(lambda: maker.runner.state == "SUCCEEDED" and not maker.busy, "resumed graph complete")
        assert maker.runtime_values["counter"] == 2
        assert maker.runtime_values["last_increment"] == 2
        assert maker.runner.context.session.browser is not None
        assert maker.save()
        path = maker.path
        loaded, _ = load_graph(path)
        assert loaded.node(increment.id).breakpoint and loaded.node(increment.id).params["amount"] == 2
        maker.buttons["stop"].invoke()
        wait(lambda: maker.runner is None, "runner closed")
        maker.load(path)
        maker.canvas.selected = {increment.id}
        maker.select(increment.id)
        maker.buttons["run"].invoke()
        wait(
            lambda: maker.runner and maker.runner.pause_reason == "breakpoint" and not maker.busy,
            "browser ready at breakpoint",
        )
        assert maker.runtime_values["counter"] == 0
        maker.buttons["breakpoint"].invoke()
        maker.buttons["run"].invoke()
        wait(lambda: maker.runner.state == "SUCCEEDED" and not maker.busy, "resumed graph complete")
        assert maker.runtime_values["counter"] == 2
        maker.buttons["stop"].invoke()
        wait(lambda: maker.runner is None and not maker.busy, "runner closed and idle")
        from poster.maker.example_projects import EXAMPLES, source_directory
        from poster.maker.fixtures import serve_maker
        from tkinter import simpledialog

        simpledialog.askstring = lambda *a, **k: k.get("initialvalue", "")
        with serve_maker() as (_, base):
            for _, filename in EXAMPLES:
                maker.load(source_directory() / filename)
                if filename.startswith("01"):
                    maker.document.graph.variables["api_base"].value = base
                maker.buttons["run"].invoke()
                wait(
                    lambda: maker.runner and maker.runner.state == "SUCCEEDED" and not maker.busy,
                    "example " + filename,
                )
                assert maker.runner.context.session.page is None
                maker.buttons["stop"].invoke()
                wait(lambda: maker.runner is None and not maker.busy, "runner closed and idle")
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
