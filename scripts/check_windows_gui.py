"""Black-box Windows GUI E2E of Poster.exe using OS mouse/keyboard and real dialogs.

The opt-in observer only supplies geometry and public UI state. It cannot invoke
application actions. Every action below is a native mouse click or key press.
"""

import argparse
import json
import os
from pathlib import Path
import socket
import sys
import time
import traceback

from poster.fixtures_readiness import serve_readiness


def check(executable, output):
    import win32api
    import win32clipboard
    import win32con
    import win32event
    import win32process
    from pywinauto import Desktop, keyboard, mouse

    output.mkdir(parents=True, exist_ok=False)
    observation = output / "observation-private.json"
    os.environ["POSTER_GUI_TEST_OUTPUT"] = str(observation.resolve())
    info = json.loads((executable.parent / "BUILD_INFO.json").read_text(encoding="utf-8"))
    expected = os.environ.get("EXPECTED_POSTER_COMMIT")
    if expected:
        assert info["commit"] == expected, "Executable built from a different commit"
    assert info["browser_bundled"] is False
    window = handle = process = None
    completed = []
    phase = "startup"

    def snapshot():
        try:
            return json.loads(observation.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def wait(predicate, label, timeout=120):
        nonlocal phase
        phase = label
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = snapshot()
            if predicate(value):
                return value
            time.sleep(0.15)
        raise AssertionError("GUI timeout: " + label)

    def start():
        nonlocal window, handle, process
        observation.unlink(missing_ok=True)
        os.startfile(str(executable))  # ShellExecute: the same opening path as a double click.
        desktop = Desktop(backend="win32")
        window = desktop.window(title="Poster 2.0 — рабочее место")
        window.wait("visible", timeout=40)
        handle = window.handle
        pid = win32process.GetWindowThreadProcessId(handle)[1]
        process = win32api.OpenProcess(win32con.SYNCHRONIZE | win32con.PROCESS_QUERY_INFORMATION, False, pid)
        wait(lambda s: bool(s.get("positions", {}).get("local_test")), "initial window")

    def focus():
        window.set_focus()
        time.sleep(0.2)

    def click(name):
        focus()
        point = wait(lambda s: name in s.get("positions", {}), "button " + name)["positions"][name]
        mouse.click(coords=tuple(point))

    def tab(index):
        focus()
        point = wait(lambda s: str(index) in s.get("positions", {}).get("tabs", {}), "tab")["positions"][
            "tabs"
        ][str(index)]
        mouse.click(coords=tuple(point))
        time.sleep(0.6)

    def fill(name, value):
        click(name)
        win32clipboard.OpenClipboard()
        try:
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardText(value, win32con.CF_UNICODETEXT)
        finally:
            win32clipboard.CloseClipboard()
        keyboard.send_keys("^a^v", pause=0.15)

    def confirm(title, yes=True):
        dialog = Desktop(backend="win32").window(title=title)
        dialog.wait("visible", timeout=15)
        dialog.set_focus()
        # Real standard MessageBox buttons: IDYES/IDNO, no dialog mocks.
        dialog.child_window(control_id=6 if yes else 7).click_input()

    def screenshot(name):
        focus()
        window.capture_as_image().save(output / name)

    def close():
        nonlocal process
        if process:
            window.close()
            assert win32event.WaitForSingleObject(process, 30000) == win32con.WAIT_OBJECT_0, (
                "Application did not shut down"
            )
            assert win32process.GetExitCodeProcess(process) == 0, "Application crashed on shutdown"
            win32api.CloseHandle(process)
            process = None

    def port_closed(port):
        with socket.socket() as client:
            client.settimeout(1)
            return client.connect_ex(("127.0.0.1", port)) != 0

    try:
        start()
        click("local_test")
        ports = wait(lambda s: bool(s.get("local_test_ports")), "local server opened")["local_test_ports"]
        result = wait(lambda s: s.get("local_report") and not s.get("busy"), "one-click local test")
        assert result["local_report"]["publications"] == 3
        assert result["local_report"]["server_stopped"] and all(port_closed(p) for p in ports)
        screenshot("01-local-test.png")
        completed.append("one_click_local_browser_http_save_engine_server_shutdown")

        # Exercise the normal real-site controls against an independent loopback server.
        with serve_readiness() as (server, base):
            tab(0)
            fill("url", base + "/compose/topic")
            fill("title", "GUI Ж 日本語")
            fill("body", "GUI & <literal>\n日本語 سطر")
            click("recognize")
            wait(lambda s: s.get("state") == "DETECTED" and not s.get("busy"), "recognition")
            click("publish")
            confirm("Одна тестовая публикация")
            wait(lambda s: s.get("state") == "HTTP_CANDIDATE" and not s.get("busy"), "browser publication")
            click("http")
            confirm("Отдельная HTTP-проверка")
            wait(lambda s: s.get("state") == "HTTP_VERIFIED" and not s.get("busy"), "independent HTTP")
            click("save")
            wait(lambda s: len(s.get("projects", [])) == 1 and not s.get("busy"), "saved project visible")
            tab(1)
            screenshot("02-saved-projects.png")
            tab(2)
            fill("queue_title", "Engine GUI Ж")
            fill("queue_body", "Engine & <literal>\n日本語 سطر")
            click("enqueue")
            confirm("Добавить в очередь")
            wait(lambda s: s.get("jobs") == ["В очереди"], "persistent queue")
            click("engine_start")
            confirm("Запустить Engine")
            wait(lambda s: s.get("jobs") == ["Опубликовано"], "Engine publication")
            click("engine_stop")
            wait(lambda s: not s.get("engine_running"), "graceful Engine stop")
            assert len(server.posts) == 3 and len(server.writes) == 3, "Unexpected or duplicate writes"
            assert all("<literal>" in p["body"] and "\n" in p["body"] for p in server.posts.values())
            click("results")
            screenshot("03-results.png")
            completed.append("real_site_controls_recognition_publish_http_save_queue_engine_results")
            close()
            start()
            wait(
                lambda s: len(s.get("projects", [])) == 1 and s.get("jobs") == ["Опубликовано"],
                "restart restores projects/results",
            )
            click("engine_start")
            confirm("Запустить Engine")
            wait(lambda s: s.get("engine_running"), "Engine restart")
            time.sleep(1.5)
            click("engine_stop")
            wait(lambda s: not s.get("engine_running"), "Engine restart stop")
            assert len(server.posts) == 3, "Restart repeated a publication"
            completed.append("restart_retains_projects_queue_results_and_never_resends")

        tab(4)
        click("system_check")
        time.sleep(1)
        screenshot("04-system.png")
        browser_cache = executable.parent / ".poster/browsers"
        assert not browser_cache.exists(), "Application implicitly downloaded a browser"
        click("download_browser")
        confirm("Загрузка Chromium", yes=False)
        time.sleep(0.5)
        assert not browser_cache.exists(), "Declined download was performed"
        completed.append("installed_browser_reused_and_declined_download_does_nothing")

        click("local_test")
        ports = wait(lambda s: bool(s.get("local_test_ports")), "cancel test server")["local_test_ports"]
        click("local_stop")
        wait(
            lambda s: not s.get("busy") and not s.get("local_test_ports") and not s.get("temporary_active"),
            "cancel cleans server",
        )
        assert all(port_closed(p) for p in ports)
        completed.append("cancel_stops_local_server")
        click("local_test")
        ports = wait(lambda s: bool(s.get("local_test_ports")), "close test server")["local_test_ports"]
        close()
        assert all(port_closed(p) for p in ports)
        completed.append("window_close_stops_local_server_and_exits_cleanly")
        report = {
            "platform": sys.platform,
            "commit": info["commit"],
            "packaged_executable": True,
            "os_mouse_keyboard": True,
            "dialogs_mocked": False,
            "scenarios": completed,
            "status": "PASSED",
        }
        (output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print("::notice title=Windows GUI E2E::" + json.dumps(report), flush=True)
        return report
    except Exception as exc:
        try:
            screenshot("failure.png")
        except Exception:
            pass
        locations = " > ".join(
            f"{Path(f.filename).name}:{f.lineno}" for f in traceback.extract_tb(exc.__traceback__)[-4:]
        )
        print(
            "::error::Windows GUI E2E "
            + type(exc).__name__
            + " at "
            + locations
            + "; phase="
            + phase
            + "; completed="
            + ",".join(completed),
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
