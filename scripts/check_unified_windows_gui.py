"""Full frozen Maker through OS mouse/keyboard. Observer only reports geometry/state."""

import argparse
import base64
import json
import os
from pathlib import Path
import socket
import shutil
import time
import traceback


def check(executable, output):
    import psutil
    import win32clipboard
    import win32con
    from pywinauto import Desktop, keyboard, mouse

    from poster.maker.fixtures import DEMO_PASSWORD, serve_maker
    from poster.fixtures_readiness import serve_readiness
    from poster.maker.model import Edge, Graph, Node
    from poster.maker.storage import Vault, save_graph

    output.mkdir(parents=True, exist_ok=False)
    observer = output / "observer-private.json"
    os.environ["POSTER_GUI_TEST_OUTPUT"] = str(observer)
    info = json.loads((executable.parent / "BUILD_INFO.json").read_text(encoding="utf-8"))
    assert info["ui"] == "qt" and info["browser_provider"] == "qt"
    assert info["commit"] == os.environ["EXPECTED_POSTER_COMMIT"]
    window = process = None
    phase, scenarios = "startup", []

    def snapshot():
        # File sharing can briefly prevent an atomic snapshot from being read on Windows.
        # Retry only this read, never a user action or a network request.
        for _ in range(4):
            try:
                return json.loads(observer.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                time.sleep(0.02)
        return {}

    def wait(predicate, label, timeout=80):
        nonlocal phase
        phase = label
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = snapshot()
            if not value.get("ui"):
                time.sleep(0.1)
                continue
            if value.get("error"):
                raise AssertionError("Maker error: " + value["error"]["code"])
            if predicate(value):
                return value
            time.sleep(0.1)
        raise AssertionError("GUI timeout: " + label)

    def start():
        nonlocal window, process
        observer.unlink(missing_ok=True)
        os.startfile(str(executable))
        window = Desktop(backend="win32").window(title="Poster 3.0 — ProjectMaker")
        window.wait("visible", timeout=50)
        process = psutil.Process(window.process_id())
        state = wait(lambda s: s.get("ui") == "qt" and "record" in s.get("positions", {}), "unified UI ready")
        assert state["frozen"] and state["qt"].startswith("6.")
        # Both native views are physically inside the same top-level window.
        r = window.rectangle()
        x, y, width, height = state["browser"]["view_rect"]
        assert r.left <= x < x + width <= r.right and r.top <= y < y + height <= r.bottom
        assert not Desktop(backend="win32").windows(title="Poster Maker — браузер", visible_only=True)

    def point(name):
        return wait(lambda s: name in s.get("positions", {}), "control " + name)["positions"][name]

    def click(name):
        window.set_focus()
        time.sleep(0.1)
        state = snapshot()
        page = state.get("main_page")
        legacy_bars = {
            "Сайт и обучение": "site",
            "Проекты": "projects",
            "Очередь": "queue",
            "Результаты": "results",
            "Система": "system",
        }
        if name not in state.get("positions", {}) and page in legacy_bars:
            ensure_visible(name, "legacy_" + legacy_bars[page] + "_scrollbar")
        mouse.click(coords=tuple(point(name)))

    def paste(value):
        win32clipboard.OpenClipboard()
        try:
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardText(value, win32con.CF_UNICODETEXT)
        finally:
            win32clipboard.CloseClipboard()
        keyboard.send_keys("^a^v", pause=0.08)

    def fill(name, text):
        click(name)
        paste(text)

    def dialog_yes(title):
        state = wait(
            lambda s: any(
                d["title"] == title and any(b["role"] == "YesRole" for b in d["buttons"])
                for d in s.get("dialogs", [])
            ),
            "confirmation " + title,
        )
        dialog = next(d for d in state["dialogs"] if d["title"] == title)
        yes = next(b for b in dialog["buttons"] if b["role"] == "YesRole")
        mouse.click(coords=tuple(yes["point"]))

    def input_dialog(title, value):
        dialog = Desktop(backend="win32").window(title=title)
        dialog.wait("visible", timeout=15)
        dialog.set_focus()
        paste(value)
        keyboard.send_keys("{ENTER}", pause=0.12)

    def tab(label, tools=False):
        key = "tools_tabs" if tools else "main_tabs"
        if tools:
            for _ in range(12):
                state = snapshot()
                if any(x[0] == label for x in state.get(key, [])):
                    break
                titles = state.get("tools_tab_titles", [])
                if not titles:
                    time.sleep(0.1)
                    continue
                visible = [titles.index(x[0]) for x in state.get(key, [])]
                right = not visible or titles.index(label) > max(visible)
                click("tools_scroll_right" if right else "tools_scroll_left")
                time.sleep(0.15)
        state = wait(lambda s: any(x[0] == label for x in s.get(key, [])), "tab " + label)
        window.set_focus()
        mouse.click(coords=tuple(next(x[1] for x in state[key] if x[0] == label)))
        wait(lambda s: s.get("tools_page" if tools else "main_page") == label, "selected tab " + label)

    def choose_node(node_id):
        def visible(state):
            if node_id not in state.get("nodes", {}) or not state.get("canvas_rect"):
                return False
            x, y = state["nodes"][node_id]
            left, top, width, height = state["canvas_rect"]
            return left < x < left + width and top < y < top + height

        frame = snapshot().get("canvas_frame", 0)
        click("fit")
        state = wait(lambda s: s.get("canvas_frame", 0) > frame, "graph fit painted")
        if not visible(state) or len(state.get("graph", {}).get("nodes", [])) > 1000:
            # Large graphs can exceed the minimum zoom; use the real search UI
            # to select and reveal the cube. Enter already selects it: do not
            # send a second click using geometry from before the search paint.
            fill("node_search", node_id)
            frame = snapshot().get("canvas_frame", 0)
            keyboard.send_keys("{ENTER}")
            wait(
                lambda s: visible(s) and s.get("selected") == [node_id] and s.get("canvas_frame", 0) > frame,
                "node selected through search and painted",
            )
            return
        window.set_focus()
        mouse.click(coords=tuple(state["nodes"][node_id]))
        wait(lambda s: s.get("selected") == [node_id], "node selected")

    def ensure_visible(name, scrollbar="property_scrollbar"):
        for _ in range(20):
            state = snapshot()
            if name in state.get("positions", {}):
                return
            point_bar = state.get("positions", {}).get(scrollbar)
            if point_bar is None:
                raise AssertionError("Hidden control without a visible scrollbar: " + name)
            window.set_focus()
            mouse.scroll(coords=tuple(point_bar), wheel_dist=-3)
            time.sleep(0.15)
        raise AssertionError("Control remains outside the scrollable panel: " + name)

    def property_visible(name):
        ensure_visible(name)

    def browser_control(selector, focus=False):
        def has_control(s):
            return any(c["selector"] == selector for c in s.get("browser", {}).get("controls", []))

        state = wait(has_control, "browser element " + selector)
        for _ in range(18):
            view = state["browser"]
            control = next(c for c in view["controls"] if c["selector"] == selector)
            x, y, width, height = view["view_rect"]
            vw, vh = view["viewport"]["width"], view["viewport"]["height"]
            clip = control.get("clip", {"x": 0, "y": 0, "width": vw, "height": vh})
            left, top = max(0, control["x"], clip["x"]), max(0, control["y"], clip["y"])
            right = min(vw, control["x"] + control["width"], clip["x"] + clip["width"])
            bottom = min(vh, control["y"] + control["height"], clip["y"] + clip["height"])
            window.set_focus()
            if right - left > 3 and bottom - top > 3:
                if " >> " in selector:
                    print(
                        "::notice title=Native iframe geometry::"
                        + json.dumps(
                            {
                                "selector": selector,
                                "viewport": view["viewport"],
                                "element": {k: control[k] for k in ["x", "y", "width", "height"]},
                                "clip": clip,
                                "visible": [left, top, right, bottom],
                            }
                        ),
                        flush=True,
                    )
                mouse.click(
                    coords=(
                        int(x + (left + right) / 2 * width / vw),
                        int(y + (top + bottom) / 2 * height / vh),
                    )
                )
                if focus:
                    wait(
                        lambda s: s.get("browser", {}).get("focused") == selector,
                        "native input focused " + selector,
                    )
                return
            # Scroll the actual visible frame (or page) through the OS. Geometry
            # comes only from the observer; no JS scroll/click command is sent.
            sx = min(vw - 10, max(10, clip["x"] + clip["width"] / 2))
            sy = min(vh - 10, max(10, clip["y"] + clip["height"] / 2))
            frame = state.get("browser_frame", 0)
            mouse.scroll(
                coords=(int(x + sx * width / vw), int(y + sy * height / vh)),
                wheel_dist=3 if control["y"] < 0 else -3,
            )
            state = wait(
                lambda s, previous_frame=frame: s.get("browser_frame", 0) > previous_frame and has_control(s),
                "browser scrolled " + selector,
            )
        raise AssertionError("Browser control remains clipped: " + selector)

    def picture(name):
        window.set_focus()
        window.capture_as_image().save(output / name)

    def close():
        nonlocal process
        children = process.children(recursive=True)
        window.set_focus()
        keyboard.send_keys("%{F4}", pause=0.12)
        assert process.wait(timeout=35) == 0
        _, alive = psutil.wait_procs(children, timeout=15)
        assert not [p for p in alive if p.is_running() and p.status() != psutil.STATUS_ZOMBIE], (
            "Owned process survived shutdown"
        )
        process = None

    try:
        start()
        click("demo")
        wait(lambda s: s.get("demo_base") and not s["busy"], "owned demo server")
        click("browser_url")
        keyboard.send_keys("{ENTER}")
        wait(
            lambda s: s.get("browser", {}).get("title") == "ProjectMaker demo" and not s["busy"],
            "native embedded page",
        )
        click("record")
        wait(lambda s: s["recording"] and not s["busy"], "recording started")
        browser_control("#show")
        browser_control("#note", focus=True)
        paste("Настоящая запись в одном окне Ж")
        browser_control("#mode")
        keyboard.send_keys("{DOWN}{ENTER}", pause=0.15)
        browser_control("#continue")
        wait(lambda s: len(s["demo_posts"]) == 1, "native form posted")
        click("record_stop")
        graph = wait(lambda s: not s["recording"] and not s["busy"], "recording stopped")["graph"]
        input_node = next(n for n in graph["nodes"] if n["type"] == "Browser.Input")
        assert any(n["type"] == "Browser.Select" for n in graph["nodes"])
        choose_node(input_node["id"])
        tab("Кубик", tools=True)
        property_visible("prop_value")
        fill("prop_value", "Изменённый кубик Ж")
        property_visible("apply")
        click("apply")
        click("breakpoint")
        click("run")
        dialog_yes("Выполнение графа")
        paused = wait(lambda s: s["run_state"] == "PAUSED" and not s["busy"], "breakpoint")["browser"][
            "active"
        ]
        tab("Элемент", tools=True)
        click("pick")
        browser_control("#note")
        wait(lambda s: (s.get("picked") or {}).get("css") == "#note", "picker from embedded browser")
        fill("selector", "xpath=//input[@id='note']")
        click("selector_check")
        wait(lambda s: (s.get("selector_result") or {}).get("count") == 1 and not s["busy"], "XPath checked")
        ensure_visible("selector_apply", "element_scrollbar")
        click("selector_apply")
        wait(
            lambda s: (
                next(n for n in s["graph"]["nodes"] if n["id"] == input_node["id"])["params"]["selector"]
                == "xpath=//input[@id='note']"
            ),
            "selector applied to selected cube",
        )
        tab("Кубик", tools=True)
        property_visible("prop_value")
        fill("prop_value", "Правка на паузе Ж")
        property_visible("apply")
        click("apply")
        click("run")
        finished = wait(lambda s: s["run_state"] == "SUCCEEDED" and not s["busy"], "resume in same session")
        assert finished["browser"]["active"] == paused
        assert finished["demo_posts"]["2"]["note"] == "Правка на паузе Ж"
        picture("01-unified-record-edit-debug.png")
        click("save")
        saved = wait(lambda s: s.get("saved_file") and not s["dirty"], "saved graph")
        shutil.copyfile(saved["saved_file"], output / "windows-maker-demo.postergraph.json")
        demo_port = (
            __import__("urllib.parse", fromlist=["urlsplit"])
            .urlsplit(saved["graph"]["metadata"]["maker_demo"])
            .port
        )
        close()
        with socket.socket() as probe:
            assert probe.connect_ex(("127.0.0.1", demo_port)) != 0
        start()
        wait(lambda s: s["graph"]["id"] == saved["graph"]["id"], "saved project reopened")
        click("run")
        dialog_yes("Выполнение графа")
        wait(lambda s: s["run_state"] == "PAUSED" and not s["busy"], "saved breakpoint")
        click("run")
        wait(lambda s: s["run_state"] == "SUCCEEDED" and not s["busy"], "replay after reopening")
        scenarios.append("one_window_record_edit_picker_breakpoint_same_session_save_reopen")

        click("stop")
        wait(lambda s: s["run_state"] is None and not s["busy"], "browser stopped")
        # Native splitters resize inside the same root window.
        view = snapshot()["browser"]["view_rect"]
        p = (view[0] + view[2] // 2, view[1] + view[3] + 3)
        mouse.press(coords=p)
        mouse.move(coords=(p[0], p[1] - 65))
        mouse.release(coords=(p[0], p[1] - 65))
        scenarios.append("resizable_native_browser_graph_panels")

        with serve_maker() as (server, base):
            tab("Профили", tools=True)
            click("profile_create")
            input_dialog("Новый профиль", "Windows unified profile")
            wait(lambda s: bool(s["graph"]["metadata"].get("browser_profile")), "profile selected")
            click("save")
            fill("browser_url", base + "/tabs")
            keyboard.send_keys("{ENTER}")
            wait(lambda s: s.get("browser", {}).get("title") == "Maker tabs" and not s["busy"], "tabs page")
            click("record")
            wait(lambda s: s["recording"] and not s["busy"], "tabs recording")
            browser_control("#main-note", focus=True)
            paste("Main tab")
            browser_control("#popup")
            wait(lambda s: len(s["browser"]["tabs"]) == 2, "popup opened")
            browser_control("#inner >> #user", focus=True)
            paste("demo")
            browser_control("#inner >> #password", focus=True)
            paste(DEMO_PASSWORD)
            browser_control("#inner >> #login")

            def authorized(s):
                if server.login_attempts and server.login_count == 0:
                    raise AssertionError("Controlled login rejected: " + json.dumps(server.login_attempts))
                return server.login_count == 1

            try:
                wait(authorized, "iframe POST authorization")
            except Exception:
                print("::error::Controlled login checks " + json.dumps(server.login_attempts), flush=True)
                print(
                    "::error::Recorded iframe actions "
                    + json.dumps(
                        [
                            {
                                "type": n["type"],
                                "selector": n["params"].get("selector"),
                                "frame": n["params"].get("frame"),
                                "protected": n["params"].get("sensitive", False),
                            }
                            for n in snapshot()["graph"]["nodes"][-20:]
                        ]
                    ),
                    flush=True,
                )
                raise
            mouse.click(coords=tuple(snapshot()["browser"]["tab_points"][0]))
            browser_control("#main-note", focus=True)
            paste("Returned to main")
            click("record_stop")
            tabs_graph = wait(lambda s: not s["recording"] and not s["busy"], "tabs stopped")["graph"]
            assert "maker_demo" not in tabs_graph["metadata"]
            assert any(n["type"] == "Browser.WaitPopup" for n in tabs_graph["nodes"])
            assert DEMO_PASSWORD not in json.dumps(tabs_graph)
            click("profile_save")
            wait(lambda s: not s["busy"], "encrypted profile saved")
            click("save")
            click("run")
            dialog_yes("Выполнение графа")
            wait(lambda s: s["run_state"] == "SUCCEEDED" and not s["busy"], "tabs replay")
            assert server.login_count == 2
            picture("02-unified-tabs-iframe-profile.png")
            click("stop")
            wait(lambda s: s["run_state"] is None and not s["busy"], "stop tabs")
            fill("browser_url", base + "/account")
            keyboard.send_keys("{ENTER}")
            wait(
                lambda s: s.get("browser", {}).get("title") == "Maker authorized" and not s["busy"],
                "authorized session restored",
            )
            assert server.login_count == 2
            click("stop")
            wait(lambda s: s["run_state"] is None and not s["busy"], "stop restored profile")
            scenarios.append("popup_iframe_login_tab_ids_encrypted_profile_restore")

        click("demo")
        wait(lambda s: s.get("demo_base") and not s["busy"], "demo for loop")
        click("example")
        dialog_yes("Несохранённый проект") if snapshot().get("dialogs") else None
        wait(lambda s: any(n["id"] == "input" for n in s["graph"]["nodes"]), "loop example")["graph"]
        choose_node("input")
        click("breakpoint")
        click("run")
        input_dialog("Параметр проекта: prefix", "Windows unified")
        dialog_yes("Выполнение графа")
        wait(lambda s: s["run_state"] == "PAUSED" and not s["busy"], "loop breakpoint")
        tab("Переменные", tools=True)
        fill("variable_name", "debug_marker")
        fill("variable_value", "native live value")
        click("var_live")
        wait(
            lambda s: s["values"].get("debug_marker") == "native live value" and not s["busy"],
            "live variable edit",
        )
        # A breakpoint is intentionally hit on every loop iteration. Remove it through
        # the UI before asking the debugger to finish the remaining iterations.
        click("breakpoint")
        wait(
            lambda s: not next(n for n in s["graph"]["nodes"] if n["id"] == "input")["breakpoint"],
            "loop breakpoint removed",
        )
        click("run")
        wait(lambda s: s["run_state"] == "SUCCEEDED" and not s["busy"], "loop completed")
        assert snapshot()["values"]["counter"] == 2
        picture("03-unified-loop-variables.png")
        scenarios.append("variables_loops_switch_breakpoint_live_edit")

        # A controlled large graph enters through the real file-open dialog.
        nodes = [Node(id="start", type="Start")]
        nodes += [
            Node(
                id="n" + str(i),
                type="Variables.Set",
                title="Block " + str(i),
                params={"name": "value", "value": i},
                x=(i % 20) * 260,
                y=(i // 20 + 1) * 120,
            )
            for i in range(1198)
        ]
        nodes.append(Node(id="end", type="End"))
        large = Graph(
            entry="start",
            nodes=nodes,
            edges=[Edge(source=a.id, target=b.id) for a, b in zip(nodes, nodes[1:], strict=False)],
            max_steps=2000,
        )
        large_path = output / "large-controlled.json"
        save_graph(large_path, large, Vault())
        click("save")
        click("stop")
        wait(lambda s: s["run_state"] is None and not s["busy"], "close before large project")
        click("open")
        dialog = Desktop(backend="win32").window(title="Открыть граф")
        dialog.wait("visible", timeout=15)
        dialog.set_focus()
        keyboard.send_keys("%n", pause=0.1)
        paste(str(large_path))
        keyboard.send_keys("{ENTER}")
        wait(lambda s: len(s["graph"]["nodes"]) == 1200, "1200 blocks loaded")
        fill("node_search", "Block 1190")
        keyboard.send_keys("{ENTER}")
        wait(lambda s: s["selected"] == ["n1190"], "large graph search")
        click("copy")
        copied = wait(lambda s: len(s["graph"]["nodes"]) == 1201 and len(s["selected"]) == 1, "copy block")[
            "selected"
        ][0]
        click("undo")
        wait(lambda s: len(s["graph"]["nodes"]) == 1200, "undo")
        click("redo")
        wait(lambda s: len(s["graph"]["nodes"]) == 1201, "redo")
        choose_node(copied)
        click("delete")
        wait(lambda s: len(s["graph"]["nodes"]) == 1200, "copied block deleted")
        click("save")
        picture("04-unified-large-graph.png")
        scenarios.append("large_graph_search_copy_delete_undo_redo")

        with serve_readiness() as (_, base):
            tab("Сайт и обучение")
            fill("legacy_url", base + "/compose/topic")
            fill("legacy_project_id", "qt-native-http")
            fill("legacy_title", "Qt HTTP Ж")
            fill("legacy_body", "Настоящий HTTP сценарий Ж")
            click("recognize")
            wait(
                lambda s: s["legacy"]["state"] == "DETECTED" and not s["legacy"]["busy"], "legacy recognition"
            )
            click("publish")
            dialog_yes("Одна тестовая публикация")
            wait(
                lambda s: s["legacy"]["state"] == "HTTP_CANDIDATE" and not s["legacy"]["busy"],
                "legacy browser learning",
            )
            click("http_verify")
            dialog_yes("Отдельная HTTP-проверка")
            wait(
                lambda s: s["legacy"]["state"] == "HTTP_VERIFIED" and not s["legacy"]["busy"],
                "legacy HTTP verify",
            )
            click("legacy_save")
            wait(
                lambda s: (
                    any(p["id"] == "qt-native-http" and p["ready"] for p in s["legacy"]["projects"])
                    and not s["legacy"]["busy"]
                ),
                "HTTP READY project saved",
            )
            tab("Очередь")
            fill("queue_title", "Engine Qt Ж")
            fill("queue_body", "Engine from unified UI Ж")
            click("enqueue")
            wait(lambda s: bool(s["legacy"]["rows"]), "queued")
            click("engine_start")
            wait(lambda s: any(r["status"] == "SUCCEEDED" for r in s["legacy"]["rows"]), "HTTP Engine result")
            click("engine_stop")
            tab("Результаты")
            picture("05-unified-legacy-http-results.png")
            scenarios.append("legacy_recognition_http_ready_queue_engine_results")
        tab("Система")
        click("local_test")
        wait(
            lambda s: s["legacy"].get("local_report") is not None and not s["legacy"]["busy"],
            "one-click legacy local test",
        )
        assert snapshot()["legacy"]["local_report"]["publications"] == 3
        assert not snapshot()["legacy"]["ports"]
        scenarios.append("legacy_local_test_server_shutdown")
        if info.get("stage") == "6":
            click("product_help")
            help_title = "Poster — начало работы, данные и диагностика"
            help_window = Desktop(backend="win32").window(title=help_title)
            help_window.wait("visible", timeout=20)
            assert (
                "help_diagnostics"
                in wait(lambda s: "help_diagnostics" in s.get("positions", {}), "Stage 6 help")["positions"]
            )
            help_window.capture_as_image().save(output / "06-stage6-help.png")
            diagnostic = output / "stage6-safe-diagnostics.json"
            click("help_diagnostics")
            input_dialog("Безопасная диагностика", str(diagnostic))
            wait(lambda s: diagnostic.is_file(), "Stage 6 safe diagnostic saved")
            text = diagnostic.read_text(encoding="utf-8")
            value = json.loads(text)
            assert value["format"] == "poster.diagnostics.v1" and value["frozen"]
            assert value["build"]["commit"] == info["commit"]
            assert DEMO_PASSWORD not in text and str(executable.parent) not in text
            help_window.set_focus()
            keyboard.send_keys("%{F4}", pause=0.12)
            help_window.wait_not("visible", timeout=15)
            scenarios.append("stage6_native_help_safe_diagnostic_exact_frozen_build")
        close()
        scenarios.append("all_owned_processes_exit")
        report = {
            "status": "PASSED",
            "commit": info["commit"],
            "packaged_executable": True,
            "os_mouse_keyboard": True,
            "dialogs_mocked": False,
            "single_window": True,
            "scenarios": scenarios,
        }
        (output / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print("::notice title=Unified Windows GUI E2E::" + json.dumps(report), flush=True)
    except Exception as error:
        try:
            picture("failure.png")
            encoded = base64.b64encode((output / "failure.png").read_bytes()).decode("ascii")
            for index in range(0, len(encoded), 3000):
                print(
                    "::notice title=Unified GUI image "
                    + str(index // 3000)
                    + "::"
                    + encoded[index : index + 3000],
                    flush=True,
                )
        except Exception:
            pass
        print("::error::Unified GUI " + type(error).__name__ + "; phase=" + phase, flush=True)
        state = snapshot()
        print(
            "::error::Unified state "
            + json.dumps(
                {
                    k: state.get(k)
                    for k in [
                        "busy",
                        "recording",
                        "run_state",
                        "error",
                        "status",
                        "dialogs",
                        "panel_sizes",
                        "tools_page",
                    ]
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        print(
            "::error::Unified GUI trace "
            + " > ".join(
                f"{Path(f.filename).name}:{f.lineno}" for f in traceback.extract_tb(error.__traceback__)[-4:]
            ),
            flush=True,
        )
        raise
    finally:
        if process and process.is_running():
            try:
                close()
            except Exception:
                for child in process.children(recursive=True):
                    child.kill()
                process.kill()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--exe", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    check(args.exe.resolve(), args.output.resolve())
