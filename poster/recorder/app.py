"""Small cross-platform Tk UI; each browser capture owns a dedicated Chromium profile."""

import asyncio
import queue
import sys
import threading
import tkinter as tk
from pathlib import Path
from urllib.parse import urljoin
from tkinter import filedialog, messagebox, simpledialog, ttk

from ..project import load_project
from ..runtime import Runtime
from .builder import analyze, export
from .capture import Recorder


class App:
    def __init__(self, root):
        self.root = root
        root.title("Poster Recorder — HTML forms MVP")
        self.messages = queue.Queue()
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        self.url = tk.StringVar(value="http://127.0.0.1:8765/classic/new")
        self.project_id = tk.StringVar(value="my-project")
        self.selector = tk.StringVar(value="[data-result-id]")
        for label, var in (
            ("URL формы", self.url),
            ("ID проекта", self.project_id),
            ("CSS подтверждения", self.selector),
        ):
            ttk.Label(root, text=label).pack(anchor="w", padx=12)
            entry = ttk.Entry(root, textvariable=var, width=85)
            entry.pack(fill="x", padx=12, pady=4)
            self._enable_clipboard(entry)
        self.log = tk.Text(root, width=90, height=13, state="disabled")
        self.log.pack(padx=12, pady=8)
        self._enable_clipboard(self.log)
        self.markers = {}
        marker_buttons = ttk.Frame(root)
        marker_buttons.pack(padx=12, pady=4)
        self.marker_buttons = {}
        for key, label in (
            ("title", "Копировать заголовок"),
            ("body", "Копировать текст"),
        ):
            button = ttk.Button(
                marker_buttons,
                text=label,
                state="disabled",
                command=lambda name=key: self.copy_marker(name),
            )
            button.pack(side="left", padx=3)
            self.marker_buttons[key] = button
        buttons = ttk.Frame(root)
        buttons.pack(padx=12, pady=8)
        for text, command in (
            ("Новый проект", self.new),
            ("Открыть", self.open),
            ("Запись", self.record),
            ("Стоп", self.stop),
            ("Анализ", self.analyze),
            ("Проверить HTTP", self.verify),
            ("Экспорт", self.export),
        ):
            ttk.Button(buttons, text=text, command=command).pack(side="left", padx=3)
        self.capture = self.previous = self.build = self.stop_event = None
        self.recording = False
        self.existing_site = None
        self.root.after(100, self.pump)
        root.protocol("WM_DELETE_WINDOW", self.close)

    def _enable_clipboard(self, widget):
        """Keyboard and right-click editing, including with a Russian keyboard layout."""
        widget.bind("<Control-KeyPress>", self._shortcut, add="+")
        menu = tk.Menu(widget, tearoff=0)
        for caption, action in (
            ("Вырезать", "cut"),
            ("Копировать", "copy"),
            ("Вставить", "paste"),
            ("Выделить всё", "all"),
        ):
            menu.add_command(
                label=caption,
                command=lambda a=action, w=widget: self._edit(w, a),
            )
        widget.bind("<Button-3>", lambda event, m=menu: self._popup(event, m), add="+")

    @staticmethod
    def _popup(event, menu):
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        return "break"

    def _shortcut(self, event):
        key = event.keysym.lower()
        actions = {
            "c": "copy",
            "v": "paste",
            "x": "cut",
            "a": "all",
            "cyrillic_es": "copy",
            "cyrillic_em": "paste",
            "cyrillic_che": "cut",
            "cyrillic_ef": "all",
        }
        action = actions.get(key)
        if sys.platform == "win32":
            # Windows virtual-key codes identify the physical C/V/X/A keys.
            action = {67: "copy", 86: "paste", 88: "cut", 65: "all"}.get(event.keycode, action)
        if action:
            return self._edit(event.widget, action)
        return None

    @staticmethod
    def _selection(widget):
        if isinstance(widget, (ttk.Entry, tk.Entry)):
            if widget.selection_present():
                return widget.get()[widget.index("sel.first"):widget.index("sel.last")]
        elif isinstance(widget, tk.Text) and widget.tag_ranges("sel"):
            return widget.get("sel.first", "sel.last")
        return None

    @staticmethod
    def _editable(widget):
        return str(widget.cget("state")) == "normal"

    def _edit(self, widget, action):
        entry = isinstance(widget, (ttk.Entry, tk.Entry))
        text_widget = isinstance(widget, tk.Text)
        if action == "all":
            if entry:
                widget.selection_range(0, "end")
                widget.icursor("end")
            elif text_widget:
                widget.tag_add("sel", "1.0", "end-1c")
            return "break"
        if action in {"copy", "cut"}:
            selected = self._selection(widget)
            if selected is None:
                return "break"
            self.root.clipboard_clear()
            self.root.clipboard_append(selected)
            self.root.update_idletasks()
            if action == "copy" or not self._editable(widget):
                return "break"
            if entry or text_widget:
                widget.delete("sel.first", "sel.last")
            return "break"
        if action == "paste":
            if not self._editable(widget):
                return "break"
            try:
                value = self.root.clipboard_get()
            except tk.TclError:
                return "break"
            if entry:
                selected = widget.selection_present()
                index = widget.index("sel.first") if selected else widget.index("insert")
                if selected:
                    widget.delete("sel.first", "sel.last")
                widget.insert(index, value)
            elif text_widget:
                selected = bool(widget.tag_ranges("sel"))
                index = widget.index("sel.first" if selected else "insert")
                if selected:
                    widget.delete("sel.first", "sel.last")
                widget.insert(index, value)
            return "break"
        return None

    def copy_marker(self, name):
        marker = self.markers.get(name)
        if marker:
            self.root.clipboard_clear()
            self.root.clipboard_append(marker)
            self.root.update_idletasks()
            self.write("Маркер скопирован: " + name)

    def _set_markers(self, title=None, body=None):
        self.markers = {"title": title, "body": body} if title and body else {}
        for name, button in self.marker_buttons.items():
            button.configure(state="normal" if name in self.markers else "disabled")

    def write(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def pump(self):
        while not self.messages.empty():
            kind, value = self.messages.get_nowait()
            if kind == "markers":
                self._set_markers(*value)
            elif kind == "capture":
                self.capture, self.recording = value, False
                self.write("Запись остановлена. Нажмите «Анализ».")
            elif kind == "error":
                self.recording = False
                self.write(value)
            else:
                self.write(value)
        self.root.after(100, self.pump)

    def new(self):
        if self.recording:
            return
        self.capture = self.previous = self.build = None
        self._set_markers()
        self.existing_site = None
        name = simpledialog.askstring("Новый проект", "ID (буквы, цифры, дефис):")
        if name:
            self.project_id.set(name)
        self.write("Новый проект. Укажите URL и начните запись.")

    def open(self):
        if self.recording:
            return
        folder = filedialog.askdirectory(title="Проект для повторного обучения")
        if not folder:
            return
        try:
            project = load_project(Path(folder))
            self.existing_site = project.site
            self.project_id.set(project.site.id)
            first = next(s for s in project.workflow.steps if s.op == "request")
            self.url.set(
                urljoin(project.site.base_url, first.url) if "{{" not in first.url else project.site.base_url
            )
            self.capture = self.previous = self.build = None
            self.write("Проект открыт. Новая запись сохранит настройки; экспортируйте в новую пустую папку.")
        except Exception:
            self.write("Не удалось открыть проект: проверьте YAML и READY.")

    def record(self):
        if self.recording:
            return
        self.previous = self.capture
        self.build = None
        self._set_markers()
        self.recording = True
        url = self.url.get()
        profile = Path(".poster/profiles") / self.project_id.get()

        async def work():
            self.stop_event = asyncio.Event()
            recorder = Recorder(url, profile)

            def ready(cap):
                self.messages.put(("markers", (cap.title_marker, cap.body_marker)))
                self.messages.put(
                    (
                        "text",
                        f"Маркеры: {cap.title_marker} / {cap.body_marker}\n"
                        "Кнопки копирования находятся под журналом Recorder. "
                        "Вставляйте маркеры в форму через Ctrl+V. "
                        "Проверьте оба поля, затем отправьте форму и нажмите Стоп.",
                    )
                )

            try:
                cap = await recorder.record(self.stop_event, ready)
                self.messages.put(("capture", cap))
            except Exception:
                self.messages.put(("error", "Не удалось записать. Проверьте URL и установку Chromium."))

        asyncio.run_coroutine_threadsafe(work(), self.loop)

    def stop(self):
        if self.stop_event:
            self.loop.call_soon_threadsafe(self.stop_event.set)

    def analyze(self):
        if not self.capture or self.recording:
            return
        try:
            self.build = analyze(
                self.capture,
                self.project_id.get(),
                self.selector.get() or None,
                self.previous,
                self.existing_site,
            )
            self.write(self.build.site["status"] + ": " + "; ".join(self.build.diagnostics))
        except Exception:
            self.write("Анализ не выполнен: проверьте ID и CSS-селектор.")

    def export(self):
        if not self.build:
            self.analyze()
        if not self.build:
            return
        folder = filedialog.askdirectory(title="Выберите ПУСТУЮ папку с именем ID проекта")
        if folder:
            try:
                export(self.build, Path(folder))
                self.write("Экспорт завершён. READY записан последним.")
            except Exception:
                self.write("Экспорт не выполнен: нужна пустая папка и POSTER_SESSION_KEY для сессии.")

    def verify(self):
        if not self.build or self.build.site["status"] != "HTTP_READY":
            self.write("Сначала нужен результат анализа HTTP_READY.")
            return
        if not messagebox.askyesno("HTTP replay", "Будет отправлена ЕЩЁ ОДНА настоящая форма. Продолжить?"):
            return
        build = self.build

        async def work():
            import tempfile

            try:
                with tempfile.TemporaryDirectory() as temp:
                    export(build, Path(temp))
                    result = await Runtime(load_project(Path(temp))).run(
                        {"title": "Recorder HTTP check", "body": "Explicitly requested HTTP replay"}
                    )
                self.messages.put(("text", f"HTTP: {result.status} {result.reason}"))
            except Exception:
                self.messages.put(("error", "HTTP проверка не выполнена; проверьте ключ сессии и проект."))

        asyncio.run_coroutine_threadsafe(work(), self.loop)

    def close(self):
        self.stop()
        self.root.destroy()


def launch():
    root = tk.Tk()
    App(root)
    root.mainloop()
