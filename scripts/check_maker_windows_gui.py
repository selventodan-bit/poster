"""Portable Maker: real OS input in both the editor and its separate Chrome window."""

import argparse
import json
import os
import socket
from pathlib import Path
import time
import traceback

from poster.maker.fixtures import serve_maker


def check(executable, output):
    import psutil
    import win32api
    import win32clipboard
    import win32con
    import win32event
    import win32process
    from pywinauto import Desktop, keyboard, mouse

    output.mkdir(parents=True, exist_ok=False)
    observer = output / "observer-private.json"
    os.environ["POSTER_GUI_TEST_OUTPUT"] = str(observer)
    info = json.loads((executable.parent / "BUILD_INFO.json").read_text(encoding="utf-8"))
    expected = os.environ.get("EXPECTED_POSTER_COMMIT")
    if expected:
        assert info["commit"] == expected
    window = process = None
    phase = "startup"
    completed = []

    def snapshot():
        try:
            return json.loads(observer.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def wait(predicate, label, timeout=90):
        nonlocal phase
        phase = label
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = snapshot()
            maker = value.get("maker", {})
            if maker.get("error") and not maker.get("busy"):
                raise AssertionError("Maker reported an error")
            if predicate(value):
                return value
            time.sleep(0.12)
        raise AssertionError("Maker GUI timeout: " + label)

    def start():
        nonlocal window, process
        observer.unlink(missing_ok=True)
        os.startfile(str(executable))
        window = Desktop(backend="win32").window(title="Poster 3.0 — ProjectMaker")
        window.wait("visible", timeout=40)
        pid = win32process.GetWindowThreadProcessId(window.handle)[1]
        process = win32api.OpenProcess(win32con.SYNCHRONIZE | win32con.PROCESS_QUERY_INFORMATION, False, pid)
        initial = wait(
            lambda s: "maker_record" in s.get("maker", {}).get("positions", {}), "Maker opens by default"
        )
        assert initial["frozen"] and initial["utf8_mode"] == 1

    def point(name):
        return wait(lambda s: name in s.get("maker", {}).get("positions", {}), "control " + name)["maker"][
            "positions"
        ][name]

    def click(name):
        window.set_focus()
        time.sleep(0.15)
        mouse.click(coords=tuple(point(name)))

    def clipboard(value):
        win32clipboard.OpenClipboard()
        try:
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardText(value, win32con.CF_UNICODETEXT)
        finally:
            win32clipboard.CloseClipboard()
        keyboard.send_keys("^a^v", pause=0.12)

    def fill(name, value):
        click(name)
        clipboard(value)

    def confirm(title="Выполнение графа"):
        dialog = Desktop(backend="win32").window(title=title)
        dialog.wait("visible", timeout=10)
        dialog.set_focus()
        dialog.child_window(control_id=6).click_input()

    def choose_node(node_id):
        click("maker_fit")
        time.sleep(0.3)
        state = wait(lambda s: node_id in s["maker"]["nodes"], "visible node " + node_id)
        window.set_focus()
        mouse.click(coords=tuple(state["maker"]["nodes"][node_id]))
        wait(lambda s: s["maker"]["selected"] == [node_id], "selected node")

    def show_property(name):
        for _ in range(20):
            if name in snapshot().get("maker", {}).get("positions", {}):
                return
            mouse.scroll(coords=tuple(point("maker_properties")), wheel_dist=-3)
            time.sleep(0.15)
        raise AssertionError("Property is not visible")

    def project_input(value):
        dialog = Desktop(backend="win32").window(title="Параметр проекта: prefix")
        dialog.wait("visible", timeout=10)
        dialog.set_focus()
        keyboard.send_keys("{HOME}+{END}{BACKSPACE}", pause=0.1)
        clipboard(value)
        keyboard.send_keys("{ENTER}", pause=0.2)

    def picture(name):
        window.set_focus()
        time.sleep(0.2)
        window.capture_as_image().save(output / name)

    def close():
        nonlocal process
        if process:
            editor_pid = win32process.GetWindowThreadProcessId(window.handle)[1]
            children = psutil.Process(editor_pid).children(recursive=True)
            window.set_focus()
            keyboard.send_keys("%{F4}", pause=0.2)
            assert win32event.WaitForSingleObject(process, 30000) == win32con.WAIT_OBJECT_0
            assert win32process.GetExitCodeProcess(process) == 0
            win32api.CloseHandle(process)
            process = None
            _, alive = psutil.wait_procs(children, timeout=15)
            assert not [child for child in alive if child.status() != psutil.STATUS_ZOMBIE], (
                "Owned browser process survived GUI shutdown"
            )

    def browser_control(selector):
        state = wait(
            lambda s: any(
                c["selector"] == selector for c in s.get("maker", {}).get("browser", {}).get("controls", [])
            ),
            "browser " + selector,
        )
        view = state["maker"]["browser"]
        control = next(c for c in view["controls"] if c["selector"] == selector)
        if info.get("browser_provider") == "qt":
            from types import SimpleNamespace

            browser = Desktop(backend="win32").window(title="Poster Maker — браузер")
            browser.wait("visible", timeout=10)
            browser.set_focus()
            time.sleep(0.2)
            # Refetch read-only native Qt view geometry after focusing the window.
            state = wait(lambda s: bool(s["maker"]["browser"].get("qt")), "Qt view geometry")
            left, top, width, height = state["maker"]["browser"]["qt"]["view_rect"]
            rectangle = SimpleNamespace(left=left, top=top, width=lambda: width, height=lambda: height)
            assert state["maker"]["browser"]["qt"]["private_profile"]
        else:
            browsers = [
                w
                for w in Desktop(backend="win32").windows(class_name="Chrome_WidgetWin_1", visible_only=True)
                if "ProjectMaker" in w.window_text()
            ]
            assert browsers, "Controlled Chrome window missing"
            browser = browsers[-1]
            browser.set_focus()
            time.sleep(0.15)
            renderers = [
                w
                for w in browser.descendants(class_name="Chrome_RenderWidgetHostHWND")
                if w.rectangle().width() > 100 and w.rectangle().height() > 100
            ]
            assert renderers, "Chrome renderer HWND missing"
            rectangle = max(
                renderers, key=lambda w: w.rectangle().width() * w.rectangle().height()
            ).rectangle()
        x = (
            rectangle.left
            + (control["x"] + control["width"] / 2) * rectangle.width() / view["viewport"]["width"]
        )
        y = (
            rectangle.top
            + (control["y"] + control["height"] / 2) * rectangle.height() / view["viewport"]["height"]
        )
        mouse.click(coords=(int(x), int(y)))

    try:
        with serve_maker() as (server, base):
            start()
            fill("maker_url", base + "/")
            click("maker_record")
            wait(lambda s: s.get("maker", {}).get("recording") and not s["maker"]["busy"], "recording ready")
            browser_control("#show")
            browser_control("#note")
            clipboard("Записано через GUI Ж 日本語")
            browser_control("#mode")
            keyboard.send_keys("{DOWN}{ENTER}", pause=0.2)
            browser_control("#continue")
            wait(
                lambda s: s.get("maker", {}).get("browser", {}).get("title") == "ProjectMaker result",
                "recorded navigation",
            )
            assert len(server.posts) == 1
            picture("01-recording.png")
            click("maker_record_stop")
            recorded = wait(
                lambda s: not s.get("maker", {}).get("recording") and not s["maker"]["busy"],
                "recording stopped",
            )["maker"]
            nodes = recorded["graph"]["nodes"]
            count = len(nodes)
            assert any(n["type"] == "Browser.Select" for n in nodes)
            selected = next(
                n for n in nodes if n["type"] == "Browser.Input" and n["params"]["selector"] == "#note"
            )
            assert len(recorded["graph"]["edges"]) >= count - 1
            completed.append("real_browser_recording_to_connected_cubes")
            click("maker_fit")
            time.sleep(0.5)
            position = wait(lambda s: selected["id"] in s["maker"]["nodes"], "input cube visible")["maker"][
                "nodes"
            ][selected["id"]]
            window.set_focus()
            mouse.click(coords=tuple(position))
            time.sleep(0.4)
            for _ in range(15):
                if "maker_prop_value" in snapshot().get("maker", {}).get("positions", {}):
                    break
                mouse.scroll(coords=tuple(point("maker_properties")), wheel_dist=-3)
                time.sleep(0.2)
            fill("maker_prop_value", "Изменено в кубике GUI Ж 日本語")
            click("maker_apply")
            wait(
                lambda s: (
                    next(n for n in s["maker"]["graph"]["nodes"] if n["id"] == selected["id"])["params"][
                        "value"
                    ]
                    == "Изменено в кубике GUI Ж 日本語"
                ),
                "edited parameter applied",
            )
            position = snapshot()["maker"]["nodes"][selected["id"]]
            window.set_focus()
            mouse.press(coords=tuple(position))
            mouse.move(coords=(position[0] + 20, position[1] + 15))
            mouse.release(coords=(position[0] + 20, position[1] + 15))
            time.sleep(0.4)
            click("maker_copy")
            wait(lambda s: len(s["maker"]["graph"]["nodes"]) == count + 1, "copied cube")
            click("maker_delete")
            wait(lambda s: len(s["maker"]["graph"]["nodes"]) == count, "deleted copy")
            picture("02-edited-graph.png")
            completed.append("edit_parameter_drag_copy_delete")
            click("maker_step")
            confirm()
            wait(
                lambda s: (
                    s["maker"]["run_state"] == "PAUSED"
                    and len(s["maker"]["trace"]) == 1
                    and not s["maker"]["busy"]
                ),
                "one cube step",
            )
            picture("03-debug-step.png")
            click("maker_run")
            played = wait(
                lambda s: s["maker"]["run_state"] == "SUCCEEDED" and not s["maker"]["busy"], "graph replay"
            )["maker"]
            assert len(server.posts) == 2 and server.posts["2"]["note"] == "Изменено в кубике GUI Ж 日本語"
            assert all(r["status"] == "SUCCEEDED" for r in played["trace"]) and len(played["trace"]) == count
            picture("04-debug-success.png")
            completed.append("step_debug_highlight_and_real_edited_replay")
            click("maker_save")
            saved = wait(lambda s: bool(s["maker"]["saved_file"]), "saved graph")["maker"]
            saved_nodes = saved["graph"]["nodes"]
            close()
            start()
            restored = wait(
                lambda s: s["maker"]["graph"]["id"] == saved["graph"]["id"], "reopened saved graph"
            )["maker"]
            assert restored["graph"]["nodes"] == saved_nodes
            click("maker_run")
            confirm()
            wait(
                lambda s: s["maker"]["run_state"] == "SUCCEEDED" and not s["maker"]["busy"],
                "replay after reopening",
            )
            assert len(server.posts) == 3 and server.posts["3"]["note"] == "Изменено в кубике GUI Ж 日本語"
            picture("05-reopened-replay.png")
            completed.append("save_close_reopen_same_graph_and_replay")
            if info.get("browser_provider") == "qt":
                from poster.maker.fixtures import DEMO_PASSWORD

                click("maker_stop")
                wait(
                    lambda s: not s["maker"]["busy"] and s["maker"]["run_state"] is None,
                    "close prior browser",
                )
                click("maker_profile_create")
                dialog = Desktop(backend="win32").window(title="Новый профиль")
                dialog.wait("visible", timeout=10)
                dialog.set_focus()
                clipboard("Windows GUI profile")
                keyboard.send_keys("{ENTER}", pause=0.2)
                wait(
                    lambda s: bool(s["maker"]["graph"]["metadata"].get("browser_profile")),
                    "local profile selected",
                )
                fill("maker_url", base + "/tabs")
                click("maker_record")
                wait(lambda s: s["maker"]["recording"] and not s["maker"]["busy"], "Qt tabs recording")
                browser_control("#main-note")
                clipboard("Native Qt tabs")
                browser_control("#popup")
                wait(lambda s: s["maker"].get("browser", {}).get("qt", {}).get("tabs") == 2, "real popup")
                browser_control("#inner >> #user")
                clipboard("demo")
                browser_control("#inner >> #password")
                clipboard(DEMO_PASSWORD)
                browser_control("#inner >> #login")
                wait(lambda s: server.login_count == 1, "iframe POST authorized")
                browser = Desktop(backend="win32").window(title="Poster Maker — браузер")
                browser.set_focus()
                point_tab = snapshot()["maker"]["browser"]["qt"]["tab_points"][0]
                mouse.click(coords=tuple(point_tab))
                browser_control("#main-note")
                clipboard("Returned to main tab")
                click("maker_record_stop")
                recorded_tabs = wait(
                    lambda s: not s["maker"]["recording"] and not s["maker"]["busy"], "tabs stopped"
                )["maker"]["graph"]
                assert any(n["type"] == "Browser.WaitPopup" for n in recorded_tabs["nodes"])
                assert any(n["type"] == "Browser.SwitchTab" for n in recorded_tabs["nodes"])
                assert DEMO_PASSWORD not in json.dumps(recorded_tabs)
                click("maker_profile_save")
                wait(lambda s: not s["maker"]["busy"], "profile snapshot encrypted")
                click("maker_save")
                tabs_saved = wait(lambda s: bool(s["maker"]["saved_file"]), "tabs saved")["maker"]["graph"][
                    "id"
                ]
                picture("10-qt-tabs-iframe-graph.png")
                click("maker_run")
                confirm()
                wait(
                    lambda s: s["maker"]["run_state"] == "SUCCEEDED" and not s["maker"]["busy"],
                    "Qt tabs replay",
                )
                assert server.login_count == 2
                click("maker_stop")
                wait(
                    lambda s: not s["maker"]["busy"] and s["maker"]["run_state"] is None,
                    "stop before profile restore",
                )
                fill("maker_url", base + "/account")
                click("maker_tab_element")
                click("maker_browser_open")
                wait(
                    lambda s: (
                        s["maker"].get("browser", {}).get("title") == "Maker authorized"
                        and not s["maker"]["busy"]
                    ),
                    "authorized profile restored without another login",
                )
                assert server.login_count == 2
                picture("11-qt-profile-restored.png")
                click("maker_stop")
                wait(lambda s: not s["maker"]["busy"], "close restored session")
                close()
                start()
                wait(lambda s: s["maker"]["graph"]["id"] == tabs_saved, "saved tabs reopened")
                click("maker_run")
                confirm()
                wait(
                    lambda s: s["maker"]["run_state"] == "SUCCEEDED" and not s["maker"]["busy"],
                    "reopened Qt tabs replay",
                )
                assert server.login_count == 3
                completed.append("native_qt_popup_iframe_authorization_tab_switch_save_reopen")
            close()
        # This second project uses the product's own server, with no harness
        # keeping it alive between app processes. It must start again itself.
        start()
        click("maker_demo")
        wait(lambda s: bool(s["maker"].get("demo")) and not s["maker"]["busy"], "owned demo server")
        click("maker_record")
        wait(lambda s: s["maker"]["recording"] and not s["maker"]["busy"], "owned demo recording")
        browser_control("#show")
        browser_control("#note")
        clipboard("Сохранённый демо-проект Ж")
        browser_control("#continue")
        wait(lambda s: len((s["maker"].get("demo") or {}).get("posts", {})) == 1, "owned demo form posted")
        click("maker_record_stop")
        wait(lambda s: not s["maker"]["recording"] and not s["maker"]["busy"], "owned demo recording stopped")
        click("maker_save")
        owned = wait(lambda s: bool(s["maker"]["saved_file"]), "owned demo saved")["maker"]
        from urllib.parse import urlsplit

        port = urlsplit(owned["graph"]["metadata"]["maker_demo"]).port
        close()
        with socket.socket() as probe:
            assert probe.connect_ex(("127.0.0.1", port)) != 0
        start()
        wait(lambda s: s["maker"]["graph"]["id"] == owned["graph"]["id"], "owned demo graph reopened")
        click("maker_run")
        confirm()
        restored = wait(
            lambda s: s["maker"]["run_state"] == "SUCCEEDED" and not s["maker"]["busy"],
            "owned demo server restarted for replay",
        )["maker"]
        assert restored["demo"]["posts"]["1"]["note"] == "Сохранённый демо-проект Ж"
        picture("06-owned-demo-restarted.png")
        close()
        with socket.socket() as probe:
            assert probe.connect_ex(("127.0.0.1", port)) != 0
        completed.append("owned_demo_server_stops_and_restarts_with_saved_graph")
        # Continue a stopped recording at a selected node in the same browser.
        start()
        click("maker_demo")
        wait(lambda s: bool(s["maker"].get("demo")) and not s["maker"]["busy"], "continuation demo ready")
        click("maker_record")
        wait(lambda s: s["maker"]["recording"] and not s["maker"]["busy"], "first fragment recording")
        browser_control("#show")
        browser_control("#note")
        clipboard("Первый фрагмент")
        browser_control("#continue")
        wait(
            lambda s: s["maker"].get("browser", {}).get("title") == "ProjectMaker result",
            "first fragment navigation",
        )
        click("maker_record_stop")
        first = wait(
            lambda s: not s["maker"]["recording"] and not s["maker"]["busy"], "first fragment stopped"
        )["maker"]
        old_ids = {n["id"] for n in first["graph"]["nodes"]}
        end = next(n["id"] for n in first["graph"]["nodes"] if n["type"] == "End")
        tail = next(e["source"] for e in first["graph"]["edges"] if e["target"] == end)
        choose_node(tail)
        click("maker_record_resume")
        wait(lambda s: s["maker"]["recording"] and not s["maker"]["busy"], "continued recording")
        click("maker_navigate")
        browser_control("#show")
        browser_control("#note")
        clipboard("Второй фрагмент")
        browser_control("#continue")
        wait(
            lambda s: (
                s["maker"].get("browser", {}).get("title") == "ProjectMaker result"
                and len(s["maker"]["demo"]["posts"]) == 2
            ),
            "second fragment navigation",
        )
        click("maker_record_stop")
        continued = wait(
            lambda s: not s["maker"]["recording"] and not s["maker"]["busy"], "continued recording stopped"
        )["maker"]
        assert old_ids <= {n["id"] for n in continued["graph"]["nodes"]}
        assert len(continued["graph"]["nodes"]) > len(old_ids)
        click("maker_save")
        wait(lambda s: bool(s["maker"]["saved_file"]), "continued graph saved")
        click("maker_run")
        confirm()
        continued = wait(
            lambda s: s["maker"]["run_state"] == "SUCCEEDED" and not s["maker"]["busy"],
            "continued graph replay",
        )["maker"]
        assert len(continued["demo"]["posts"]) == 4
        assert continued["demo"]["posts"]["3"]["note"] == "Первый фрагмент"
        assert continued["demo"]["posts"]["4"]["note"] == "Второй фрагмент"
        picture("07-continued-recording.png")
        close()
        completed.append("continue_recording_in_existing_graph_and_browser")

        # A nontrivial project uses the same portable GUI, no command channel.
        start()
        click("maker_demo")
        wait(lambda s: bool(s["maker"].get("demo")) and not s["maker"]["busy"], "stage2 demo ready")
        click("maker_example")
        wait(
            lambda s: any(n["type"] == "Logic.ForEach" for n in s["maker"]["graph"]["nodes"]),
            "loop example created",
        )
        choose_node("input")
        click("maker_breakpoint")
        click("maker_run")
        project_input("Windows GUI")
        confirm()
        paused = wait(
            lambda s: s["maker"]["pause_reason"] == "breakpoint" and not s["maker"]["busy"],
            "real graph breakpoint",
        )["maker"]
        assert paused["current_node"] == "input" and paused["runtime_values"]["counter"] == 0
        show_property("maker_prop_value")
        fill("maker_prop_value", "Изменено: {{note}}")
        click("maker_apply")
        wait(
            lambda s: (
                next(n for n in s["maker"]["graph"]["nodes"] if n["id"] == "input")["params"]["value"]
                == "Изменено: {{note}}"
            ),
            "paused parameter edit",
        )
        choose_node("show")
        click("maker_selected_run")
        confirm("Выполнить кубик")
        wait(
            lambda s: any(r.get("isolated") for r in s["maker"]["trace"]) and not s["maker"]["busy"],
            "selected cube in existing browser",
        )
        assert snapshot()["maker"]["current_node"] == "input"
        choose_node("input")
        click("maker_tab_variables")
        fill("maker_variable_name", "debug_marker")
        fill("maker_variable_value", "live marker")
        click("maker_var_live")
        wait(
            lambda s: (
                s["maker"]["runtime_values"].get("debug_marker") == "live marker" and not s["maker"]["busy"]
            ),
            "live variable update",
        )
        picture("08-breakpoint-variables.png")
        click("maker_tab_element")
        click("maker_pick")
        browser_control("#note")
        wait(lambda s: (s["maker"].get("picked") or {}).get("css") == "#note", "real browser element picked")
        fill("maker_selector", "xpath=//input[@id='note']")
        click("maker_selector_check")
        wait(
            lambda s: (s["maker"].get("selector_result") or {}).get("count") == 1 and not s["maker"]["busy"],
            "XPath selector checked",
        )
        click("maker_selector_apply")
        wait(
            lambda s: next(n for n in s["maker"]["graph"]["nodes"] if n["id"] == "input")["params"][
                "selector"
            ].startswith("xpath="),
            "picked selector applied to cube",
        )
        picture("09-element-constructor.png")
        click("maker_breakpoint")
        click("maker_run")
        finished = wait(
            lambda s: s["maker"]["run_state"] == "SUCCEEDED" and not s["maker"]["busy"],
            "stage2 loop completed",
        )["maker"]
        assert finished["runtime_values"]["counter"] == 2
        assert finished["runtime_values"]["last_text"] == "Изменено: Windows GUI · two"
        assert len(finished["demo"]["posts"]) == 2
        click("maker_tab_variables")
        picture("10-stage2-loop-results.png")
        click("maker_save")
        saved_stage2 = wait(lambda s: bool(s["maker"]["saved_file"]), "stage2 graph saved")["maker"]["graph"][
            "id"
        ]
        close()
        start()
        wait(lambda s: s["maker"]["graph"]["id"] == saved_stage2, "stage2 graph reopened")
        click("maker_run")
        project_input("Повтор")
        confirm()
        reopened = wait(
            lambda s: s["maker"]["run_state"] == "SUCCEEDED" and not s["maker"]["busy"],
            "stage2 replay after restart",
        )["maker"]
        assert reopened["runtime_values"]["last_text"] == "Изменено: Повтор · two"
        assert reopened["runtime_values"]["counter"] == 2 and "debug_marker" not in reopened["runtime_values"]
        picture("11-stage2-reopened.png")
        close()
        completed.extend(
            [
                "variables_loops_breakpoints_live_edit_and_selected_action",
                "real_element_picker_xpath_and_readback_variables",
                "saved_stage2_graph_reopens_with_isolated_inputs",
            ]
        )
        report = {
            "status": "PASSED",
            "commit": info["commit"],
            "packaged_executable": True,
            "os_mouse_keyboard": True,
            "browser_actions_via_os_input": True,
            "dialogs_mocked": False,
            "graph_nodes": count,
            "writes": 3,
            "scenarios": completed,
            "browser_embedded": info.get("browser_provider") == "qt",
        }
        (output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print("::notice title=Maker Windows GUI E2E::" + json.dumps(report), flush=True)
    except Exception as exc:
        try:
            picture("failure.png")
        except Exception:
            pass
        locations = " > ".join(
            f"{Path(f.filename).name}:{f.lineno}" for f in traceback.extract_tb(exc.__traceback__)[-4:]
        )
        maker = snapshot().get("maker", {})
        print(
            "::error::Maker GUI " + type(exc).__name__ + " at " + locations + "; phase=" + phase, flush=True
        )
        print(
            "::error::Maker state "
            + json.dumps(
                {k: maker.get(k) for k in ["busy", "recording", "dirty", "run_state", "error", "status"]},
                ensure_ascii=False,
            ),
            flush=True,
        )
        raise
    finally:
        if process:
            try:
                close()
            except Exception:
                win32api.TerminateProcess(process, 1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--exe", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    check(args.exe.resolve(), args.output.resolve())
