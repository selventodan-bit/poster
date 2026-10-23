import asyncio
import json
from dataclasses import replace
import socket
import os
import subprocess
import sys
from pathlib import Path
import time

import pytest

from poster import browser
from poster.recorder.workspace import EngineBridge, Workspace
from test_project_queue import publish


def test_browser_discovery_reuses_existing_and_never_installs(tmp_path, monkeypatch):
    chrome = tmp_path / "Chrome Ж" / "chrome.exe"
    chrome.parent.mkdir()
    chrome.write_bytes(b"existing")
    managed = tmp_path / "managed.exe"
    managed.write_bytes(b"cached")
    monkeypatch.delenv("POSTER_CHROMIUM", raising=False)
    monkeypatch.setattr(browser, "browser_candidates", lambda: [str(chrome)])
    assert browser.find_browser(managed) == str(chrome.resolve())
    monkeypatch.setenv("POSTER_CHROMIUM", str(tmp_path / "missing"))
    with pytest.raises(ValueError, match="BROWSER_PATH_INVALID"):
        browser.find_browser(managed)


def test_cleanup_only_marked_inactive_temp_preserves_user_data(tmp_path):
    workspace = Workspace(tmp_path / ".poster")
    owned = workspace.create_temp()
    (owned / "temporary.txt").write_text("remove me")
    active = workspace.create_temp()
    (active / "current.txt").write_text("active")
    unknown = workspace.temporary / "poster-user-folder"
    unknown.mkdir()
    (unknown / "keep.txt").write_text("user data")
    private = workspace.root / "private"
    private.mkdir()
    (private / "session.key").write_text("never delete")
    project = workspace.projects / "my-site"
    project.mkdir()
    workspace.active.discard(owned)
    assert workspace.clean_temp() > 0
    assert not owned.exists() and active.exists() and unknown.exists()
    assert (private / "session.key").read_text() == "never delete" and project.exists()


def test_cleanup_refuses_symlink_tree(tmp_path):
    workspace = Workspace(tmp_path / ".poster")
    owned = workspace.create_temp()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "valuable.txt").write_text("keep")
    if os.name == "nt":
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(owned / "link"), str(outside)], check=True, capture_output=True
        )
    else:
        (owned / "link").symlink_to(outside, target_is_directory=True)
    workspace.active.clear()
    assert workspace.clean_temp() == 0
    assert owned.exists() and (outside / "valuable.txt").read_text() == "keep"


async def test_desktop_engine_queue_stop_restart_does_not_resend(tmp_path, fixture_site, make_project):
    server, base = fixture_site
    workspace = Workspace(tmp_path / ".poster")
    publish(make_project(base, csrf=True), workspace.projects / "csrf")
    bridge = EngineBridge(workspace)
    job = bridge.enqueue("csrf", "Desktop Ж", "Body & <literal>\n日本語")
    assert bridge.rows()[0]["status"] == "PENDING"
    await bridge.start()
    try:
        for _ in range(100):
            if bridge.rows()[0]["status"] == "SUCCEEDED":
                break
            await asyncio.sleep(0.05)
        assert bridge.rows()[0]["status"] == "SUCCEEDED"
    finally:
        await bridge.stop()
    await bridge.start()
    await asyncio.sleep(0.6)
    await bridge.stop()
    assert len(server.posts) == 1 and bridge.rows()[0]["id"] == job
    pending = bridge.enqueue("csrf", "Cancel", "Never sent")
    bridge.cancel_pending(pending)
    assert bridge.rows()[-1]["status"] == "CANCELLED"


def test_desktop_refuses_review_and_import_retains_manifest(tmp_path, make_project):
    workspace = Workspace(tmp_path / ".poster")
    project = make_project("https://safe.test")
    project = replace(project, site=project.site.model_copy(update={"status": "REVIEW"}))
    source = tmp_path / "external"
    publish(project, source)
    assert workspace.import_project(source) == "classic"
    with pytest.raises(ValueError, match="PROJECT_NOT_READY"):
        EngineBridge(workspace).enqueue("classic", "Title", "Body")
    assert not (workspace.projects / "classic" / "unrelated.txt").exists()


def pump_until(root, predicate, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        root.update()
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("GUI operation timed out")


@pytest.mark.skipif(
    not __import__("os").environ.get("DISPLAY") and __import__("os").name != "nt",
    reason="Native desktop required",
)
def test_gui_local_button_and_cancel_own_and_stop_server(tmp_path, monkeypatch):
    # Tk has process-global native state. Each complete desktop scenario owns
    # its interpreter, just as reopening the portable application does.
    code = """import os,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from test_desktop import local_gui_scenario
os.chdir(sys.argv[2])
local_gui_scenario(Path(sys.argv[2]))
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(Path(__file__).parent), str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=150,
    )
    if result.returncode:
        print(result.stdout, flush=True)
        # A failure before App construction cannot contain website/session data.
        if "_tkinter.TclError" in result.stderr:
            print(
                "\n::error::Fresh Tk startup "
                + result.stderr.split("_tkinter.TclError:")[-1].strip().replace("\n", " ")[:500],
                flush=True,
            )
    assert result.returncode == 0, result.stderr


def local_gui_scenario(tmp_path):
    import tkinter as tk
    from poster.recorder.app import App

    root = tk.Tk()
    app = App(root)
    try:
        root.update()
        app.desktop_widgets["local_test"].invoke()
        pump_until(root, lambda: bool(app.workspace.test_ports))
        ports = set(app.workspace.test_ports)
        pump_until(
            root, lambda: (app.local_report is not None or app.last_error is not None) and not app.busy
        )
        assert app.last_error is None and app.local_report is not None
        assert app.local_report["publications"] == 3 and app.local_report["server_stopped"]
        assert not app.workspace.test_ports and not app.workspace.active
        for port in ports:
            with socket.socket() as client:
                assert client.connect_ex(("127.0.0.1", port)) != 0
        app.desktop_widgets["local_test"].invoke()
        pump_until(root, lambda: bool(app.workspace.test_ports))
        ports = set(app.workspace.test_ports)
        app.desktop_widgets["local_stop"].invoke()
        pump_until(root, lambda: not app.busy and not app.workspace.test_ports)
        for port in ports:
            with socket.socket() as client:
                assert client.connect_ex(("127.0.0.1", port)) != 0
    except Exception:
        print(
            "\n::error::Desktop local GUI "
            + json.dumps(
                {
                    "error": app.last_error,
                    "progress": app.last_progress,
                    "status": app.status.get(),
                    "busy": app.busy,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        raise
    finally:
        app.close()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                root.update()
                if not root.winfo_exists():
                    break
            except tk.TclError:
                break
            time.sleep(0.02)
    assert not app.thread.is_alive()


def test_localization_resources_are_independent_of_windows_ansi(monkeypatch):
    import io
    from poster.recognition.localization import Localizations

    monkeypatch.setattr(io, "text_encoding", lambda encoding, stacklevel=2: encoding or "cp1252")
    words = Localizations()
    assert words.has("publish", "Опубликовать")
