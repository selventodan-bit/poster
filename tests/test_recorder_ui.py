"""Real Tk widgets under Xvfb in CI; Windows native integration remains a separate check."""

import os
import time
import asyncio
import threading
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("DISPLAY") and os.name != "nt",
    reason="Requires Xvfb on Linux or native Windows desktop",
)


def test_recorder_launch_clipboard_localization_destination_and_close(tmp_path, monkeypatch):
    import tkinter as tk
    from poster.recorder.app import App

    monkeypatch.chdir(tmp_path)
    root = tk.Tk()
    app = App(root)
    root.update()
    assert str(app.buttons["recognize"].cget("state")) == "normal"
    assert str(app.buttons["publish"].cget("state")) == "disabled"
    assert app.output_parent.get() == ""
    app.output_parent.set(str(tmp_path / "projects"))
    app.body.delete("1.0", "end")
    app.body.insert("1.0", "Текст для буфера")
    app._shortcut(SimpleNamespace(keysym="Cyrillic_ef", keycode=0, widget=app.body))
    app._shortcut(SimpleNamespace(keysym="Cyrillic_es", keycode=0, widget=app.body))
    assert root.clipboard_get() == "Текст для буфера"
    app._shortcut(SimpleNamespace(keysym="Cyrillic_che", keycode=0, widget=app.body))
    assert app.body.get("1.0", "end-1c") == ""
    app._shortcut(SimpleNamespace(keysym="Cyrillic_em", keycode=0, widget=app.body))
    assert app.body.get("1.0", "end-1c") == "Текст для буфера"
    # A delayed failure from a stopped generation must not close a new session.
    started, release = threading.Event(), threading.Event()

    class NewSession:
        closed = browser_attempted = http_attempted = manual = False

        async def close(self):
            self.closed = True

    async def stale_operation():
        started.set()
        while not release.is_set():
            await asyncio.sleep(0.01)
        raise RuntimeError("private URL/token must not reach UI")

    app.run(stale_operation)
    assert started.wait(3)
    app.generation += 1
    replacement = NewSession()
    app.session = replacement
    release.set()
    app.future.result(timeout=3)
    assert not replacement.closed
    app.busy = False
    app.snapshot = {"state": "UNKNOWN"}
    replacement.browser_attempted = True
    app.update_buttons()
    assert str(app.buttons["publish"].cget("state")) == "disabled"
    assert str(app.buttons["http"].cget("state")) == "disabled"
    app.snapshot = {"state": "HTTP_CANDIDATE"}
    app.update_buttons()
    assert str(app.buttons["http"].cget("state")) == "normal"
    replacement.http_attempted = True
    app.update_buttons()
    assert str(app.buttons["http"].cget("state")) == "disabled"
    app.close()
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        try:
            root.update()
            if not root.winfo_exists():
                break
        except tk.TclError:
            break
        time.sleep(0.02)
    app.thread.join(timeout=3)
    assert not app.thread.is_alive()
