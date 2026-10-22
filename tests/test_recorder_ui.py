"""Real Tk widgets under Xvfb in CI; Windows native integration remains a separate check."""

import os
import time
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
