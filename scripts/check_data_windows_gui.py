"""Frozen Full Maker: real OS edits, four data demos, debugger, HTTP form and reopen."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import time


def check(executable, output):
    import psutil
    import win32clipboard
    import win32con
    from pywinauto import Desktop, keyboard, mouse

    output.mkdir(parents=True, exist_ok=False)
    observer = output / "observer-private.json"
    os.environ["POSTER_GUI_TEST_OUTPUT"] = str(observer)
    info = json.loads((executable.parent / "BUILD_INFO.json").read_text(encoding="utf-8"))
    assert info["commit"] == os.environ["EXPECTED_POSTER_COMMIT"] and info["ui"] == "qt"
    window = process = None
    phase = "start"
    completed = []

    def snapshot():
        try:
            return json.loads(observer.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def wait(predicate, label, timeout=80):
        nonlocal phase
        phase = label
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            s = snapshot()
            if s.get("error"):
                raise AssertionError("Maker error: " + s["error"]["code"])
            if predicate(s):
                return s
            time.sleep(0.1)
        raise AssertionError("Data GUI timeout: " + label)

    def start():
        nonlocal process, window
        observer.unlink(missing_ok=True)
        os.startfile(str(executable))
        window = Desktop(backend="win32").window(title="Poster 3.0 — ProjectMaker")
        window.wait("visible", timeout=50)
        process = psutil.Process(window.process_id())
        wait(lambda s: s.get("ui") == "qt", "Qt ready")

    def click(name):
        window.set_focus()
        time.sleep(0.1)
        if name.startswith("prop_") or name == "apply":
            for _ in range(22):
                s = snapshot()
                if name in s.get("positions", {}):
                    break
                bar = s.get("positions", {}).get("property_scrollbar")
                if not bar:
                    raise AssertionError("No visible scrollbar for " + name)
                mouse.scroll(coords=tuple(bar), wheel_dist=-3)
                time.sleep(0.15)
        point = wait(lambda s: name in s.get("positions", {}), "control " + name)["positions"][name]
        mouse.click(coords=tuple(point))

    def paste(text):
        win32clipboard.OpenClipboard()
        try:
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardText(text, win32con.CF_UNICODETEXT)
        finally:
            win32clipboard.CloseClipboard()
        keyboard.send_keys("^a^v", pause=0.08)

    def fill(name, text):
        click(name)
        paste(text)

    def tab(label):
        s = wait(lambda s: any(x[0] == label for x in s.get("tools_tabs", [])), "tools " + label)
        mouse.click(coords=tuple(next(x[1] for x in s["tools_tabs"] if x[0] == label)))
        wait(lambda s: s.get("tools_page") == label, "tab " + label)

    def yes(title="Выполнение графа"):
        s = wait(lambda s: any(d["title"] == title for d in s.get("dialogs", [])), "dialog " + title)
        dialog = next(d for d in s["dialogs"] if d["title"] == title)
        mouse.click(coords=tuple(next(b["point"] for b in dialog["buttons"] if b["role"] == "YesRole")))

    def choose(node):
        fill("node_search", node)
        keyboard.send_keys("{ENTER}")
        wait(lambda s: s.get("selected") == [node], "selected " + node)

    def example(index, prefix):
        click("data_examples")
        s = wait(
            lambda s: any(d["title"] == "Примеры 3B.2" and d.get("combos") for d in s.get("dialogs", [])),
            "example chooser",
        )
        d = next(d for d in s["dialogs"] if d["title"] == "Примеры 3B.2")
        mouse.click(coords=tuple(d["combos"][0]["point"]))
        keyboard.send_keys("{HOME}" + "{DOWN}" * index + "{ENTER}{ENTER}", pause=0.12)
        wait(
            lambda s: (
                str(s.get("saved_file", "")).replace("\\", "/").split("/")[-1].startswith(prefix)
                and not s["busy"]
            ),
            "example loaded",
        )

    def run(api_input=False):
        click("run")
        if api_input:
            dialog = Desktop(backend="win32").window(title="Параметр проекта: api_base")
            dialog.wait("visible", timeout=15)
            dialog.set_focus()
            keyboard.send_keys("{ENTER}")
        yes()

    def close():
        nonlocal process
        children = process.children(recursive=True)
        window.set_focus()
        keyboard.send_keys("%{F4}")
        assert process.wait(timeout=35) == 0
        _, alive = psutil.wait_procs(children, timeout=15)
        assert not [p for p in alive if p.is_running() and p.status() != psutil.STATUS_ZOMBIE]
        process = None

    def picture(name):
        window.capture_as_image().save(output / name)

    try:
        start()
        example(0, "01-")
        run(True)
        wait(lambda s: s["run_state"] == "SUCCEEDED" and not s["busy"], "HTTP JSON demo")
        assert snapshot()["values"]["number"] == 3 and not snapshot()["runner_browser_started"]
        click("save")
        completed.append("http_json_demo_without_graph_browser")
        example(1, "02-")
        choose("n0")
        tab("Кубик")
        fill("prop_items", '["Windows Ж", "data"]')
        click("apply")
        wait(
            lambda s: s["graph"]["nodes"][1]["params"]["items"] == ["Windows Ж", "data"],
            "typed JSON array applied",
        )
        choose("n6")
        click("breakpoint")
        run()
        wait(lambda s: s["run_state"] == "PAUSED" and not s["busy"], "data breakpoint")
        assert snapshot()["values"]["out"] == ["WINDOWS Ж", "DATA"]
        click("breakpoint")
        click("run")
        wait(lambda s: s["run_state"] == "SUCCEEDED" and not s["busy"], "data resume")
        choose("n4")
        tab("Результат")
        wait(lambda s: s.get("preview_rows") == 2, "list preview")
        picture("01-data-list-debug-preview.png")
        click("save")
        saved = wait(lambda s: s.get("saved_file") and not s["dirty"], "saved data graph")
        exported = output / "windows-data.postergraph.json"
        shutil.copyfile(saved["saved_file"], exported)
        close()
        start()
        run()
        wait(lambda s: s["run_state"] == "SUCCEEDED" and not s["busy"], "saved data reopened replay")
        assert not snapshot()["runner_browser_started"]
        assert json.loads(
            (Path(snapshot()["workdir"]) / "lists/result.json").read_text(encoding="utf-8")
        ) == ["WINDOWS Ж", "DATA"]
        completed.append("list_json_edit_breakpoint_preview_save_close_reopen")
        example(2, "03-")
        run()
        wait(lambda s: s["run_state"] == "SUCCEEDED" and not s["busy"], "CSV XLSX demo")
        assert snapshot()["values"]["csv"] == snapshot()["values"]["excel"]
        choose("n3")
        tab("Результат")
        wait(lambda s: s.get("preview_rows") == 2 and s.get("preview_columns") == 2, "table preview")
        picture("02-data-table-csv-xlsx.png")
        click("save")
        completed.append("csv_xlsx_real_readback_and_table_preview")
        example(3, "04-")
        run()
        wait(lambda s: s["run_state"] == "SUCCEEDED" and not s["busy"], "nested project demo")
        assert snapshot()["values"]["result"] == "POSTER Ж" and not snapshot()["runner_browser_started"]
        click("save")
        picture("03-data-subproject.png")
        completed.append("nested_project_inputs_outputs")
        # Manually add/configure a real HTTP cube through the action library.
        click("new")
        wait(lambda s: len(s["graph"]["nodes"]) == 2, "new graph")
        fill("action_search", "HTTP.Request")
        click("add")
        state = wait(
            lambda s: any(n["type"] == "HTTP.Request" for n in s["graph"]["nodes"]), "manual HTTP cube"
        )
        node = next(n["id"] for n in state["graph"]["nodes"] if n["type"] == "HTTP.Request")
        tab("Кубик")
        fill("prop_url", state["demo_base"] + "/api/data")
        click("prop_method")
        keyboard.send_keys("{HOME}{DOWN}{ENTER}")
        click("prop_body_type")
        keyboard.send_keys("{HOME}{DOWN}{ENTER}")
        fill("prop_body", '{"flag": true, "text": "Ж"}')
        click("apply")
        wait(
            lambda s: (
                next(n for n in s["graph"]["nodes"] if n["id"] == node)["params"]["body"]
                == {"flag": True, "text": "Ж"}
            ),
            "HTTP JSON body applied",
        )
        click("selected_run")
        yes("Выполнить кубик")
        wait(
            lambda s: (
                s.get("demo_api_posts") == 1
                and not s["busy"]
                and any(r["node_id"] == node and r["status"] == "SUCCEEDED" for r in s["trace"])
            ),
            "manual HTTP POST",
        )
        assert not snapshot()["runner_browser_started"]
        picture("04-data-http-form.png")
        click("stop")
        wait(lambda s: s["run_state"] is None and not s["busy"], "stop HTTP")
        click("save")
        close()
        completed.append("manual_http_registry_form_method_json_post")
        report = {
            "status": "PASSED",
            "commit": info["commit"],
            "os_mouse_keyboard": True,
            "dialogs_mocked": False,
            "graph_browser_session_started": False,
            "cases": completed,
            "windows_data_graph_sha256": hashlib.sha256(exported.read_bytes()).hexdigest(),
            "clean_process_shutdown": True,
        }
        (output / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print("::notice title=Stage3B2 Windows data GUI::" + json.dumps(report), flush=True)
    except Exception:
        try:
            picture("failure.png")
        except Exception:
            pass
        print("::error::Stage3B2 data GUI phase=" + phase, flush=True)
        raise
    finally:
        if process and process.is_running():
            for child in process.children(recursive=True):
                child.kill()
            process.kill()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--exe", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    check(args.exe.resolve(), args.output.resolve())
