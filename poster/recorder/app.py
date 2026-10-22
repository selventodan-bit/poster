"""Small cross-platform Tk UI; each browser capture owns a dedicated Chromium profile."""

import asyncio
import queue
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
            ttk.Entry(root, textvariable=var, width=85).pack(fill="x", padx=12, pady=4)
        self.log = tk.Text(root, width=90, height=13, state="disabled")
        self.log.pack(padx=12, pady=8)
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

    def write(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def pump(self):
        while not self.messages.empty():
            kind, value = self.messages.get_nowait()
            if kind == "capture":
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
        self.recording = True
        url = self.url.get()
        profile = Path(".poster/profiles") / self.project_id.get()

        async def work():
            self.stop_event = asyncio.Event()
            recorder = Recorder(url, profile)

            def ready(cap):
                self.messages.put(
                    (
                        "text",
                        f"Маркеры: {cap.title_marker} / {cap.body_marker}\\n"
                        "Без буфера обмена: в браузере выберите поле заголовка и нажмите Ctrl+Alt+1; "
                        "выберите поле сообщения и нажмите Ctrl+Alt+2.\\n"
                        "Проверьте оба поля перед отправкой. Затем нажмите Стоп.",
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
