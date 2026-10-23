"""Recorder 2.0: recognize first, explicitly authorize each real publication."""

import asyncio
import json
import queue
import re
import threading
import traceback
import uuid
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from urllib.parse import urljoin, urlsplit

from ..auth import ensure_local_key
from ..project import load_project
from ..recognition.knowledge import KnowledgeBase
from ..recognition.service import RecognitionSession
from ..recognition.types import MESSAGES
from ..recognition.policy import ResourcePolicy
from .clipboard import ClipboardMixin
from .preferences import Preferences
from .desktop import DesktopMixin, friendly_error

STATE_TEXT = {
    "DETECTED": "Форма найдена. Проверьте предпросмотр и разрешите одну тестовую публикацию.",
    "BROWSER_VERIFIED": "Браузерная публикация подтверждена.",
    "HTTP_CANDIDATE": "Публикация подтверждена. HTTP-проект подготовлен, но ещё не проверен.",
    "HTTP_VERIFIED": "HTTP-публикация подтверждена. Проект готов к сохранению.",
    "LEARNING_REQUIRED": "Нужно выбрать форму или показать действие вручную.",
    "UNKNOWN": "Результат отправки неизвестен. Повтор запрещён; проверьте сайт вручную.",
    "FAILED": "Тест не подтверждён. Новая отправка автоматически не выполняется.",
}


class App(DesktopMixin, ClipboardMixin):
    def __init__(self, root):
        self.root = root
        root.title("Poster 2.0 — рабочее место")
        self.preferences = Preferences(Path(".poster/recorder-settings.json"))
        saved = self.preferences.load()
        ensure_local_key(Path(".poster/private"))
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        self.messages = queue.Queue()
        self.generation = 0
        self.future = self.session = self.knowledge = self.existing_site = None
        self.busy = False
        self.snapshot = {}
        self.url = tk.StringVar(value=saved.get("url", ""))
        self.project_id = tk.StringVar(value=saved.get("project_id", ""))
        self.publication_kind = tk.StringVar(value="auto")
        self.static_origins = tk.StringVar(value="")
        self.title = tk.StringVar(value="Проверка Poster")
        self.status = tk.StringVar(
            value="Нажмите «Локальный тест» или введите адрес сайта и выберите «Проверить распознавание»."
        )
        self.advanced = tk.BooleanVar(value=False)
        self.output_parent = tk.StringVar(value="")
        self.desktop_init(root)
        self.output_parent.set(str(self.workspace.projects))
        root = self.recorder_page
        for label, variable in (("Адрес сайта или формы", self.url), ("Заголовок", self.title)):
            ttk.Label(root, text=label).pack(anchor="w", padx=12)
            widget = ttk.Entry(root, textvariable=variable, width=95)
            widget.pack(fill="x", padx=12, pady=4)
            self._enable_clipboard(widget)
            if variable is self.url:
                self.url_entry = widget
            else:
                self.title_entry = widget
        kind_row = ttk.Frame(root)
        kind_row.pack(fill="x", padx=12)
        ttk.Label(
            kind_row, text="Тип: auto — автоматически; topic — тема; reply — ответ; comment — комментарий"
        ).pack(side="left")
        ttk.Combobox(
            kind_row,
            textvariable=self.publication_kind,
            values=["auto", "topic", "reply", "comment"],
            state="readonly",
            width=12,
        ).pack(side="right")
        ttk.Label(root, text="Текст публикации").pack(anchor="w", padx=12)
        self.body = tk.Text(root, width=90, height=4, wrap="word", undo=True)
        self.body.insert("1.0", "Тестовая публикация для проверки формы.")
        self.body.pack(fill="both", expand=True, padx=12, pady=4)
        self._enable_clipboard(self.body)
        destination = ttk.Frame(root)
        destination.pack(fill="x", padx=12, pady=4)
        ttk.Label(destination, text="Папка для готового проекта:").pack(side="left")
        output_entry = ttk.Entry(destination, textvariable=self.output_parent)
        output_entry.pack(side="left", fill="x", expand=True)
        self._enable_clipboard(output_entry)
        ttk.Button(destination, text="Выбрать…", command=self.choose_output).pack(side="left")
        actions = ttk.Frame(root)
        actions.pack(fill="x", padx=12, pady=8)
        self.buttons = {}
        for index, (key, label, callback) in enumerate(
            (
                ("recognize", "Проверить распознавание", self.recognize),
                ("publish", "Тестовая публикация", self.publish),
                ("http", "Проверить HTTP", self.http_verify),
                ("manual", "Обучить сайт", self.manual),
                ("finish", "Завершить обучение", self.finish_manual),
                ("save", "Сохранить проект", self.save),
                ("stop", "Стоп / закрыть браузер", self.stop),
                ("login", "Разрешить один вход", self.login),
            )
        ):
            button = ttk.Button(actions, text=label, command=callback)
            button.grid(row=index // 4, column=index % 4, padx=2, pady=3, sticky="ew")
            actions.columnconfigure(index % 4, weight=1)
            self.buttons[key] = button
        ttk.Label(root, textvariable=self.status, wraplength=950).pack(anchor="w", padx=12, pady=4)
        self.preview = tk.StringVar(value="Предпросмотр формы появится после распознавания.")
        ttk.Label(root, textvariable=self.preview, wraplength=950).pack(anchor="w", padx=12, pady=4)
        ttk.Checkbutton(
            root,
            text="Расширенные настройки и диагностика",
            variable=self.advanced,
            command=self.toggle_advanced,
        ).pack(anchor="w", padx=12)
        self.details = ttk.Frame(root)
        ttk.Label(self.details, text="ID проекта (пустое поле = автоматически)").pack(anchor="w")
        entry = ttk.Entry(self.details, textvariable=self.project_id)
        entry.pack(fill="x")
        self._enable_clipboard(entry)
        ttk.Label(
            self.details,
            text="Дополнительные доверенные CDN: точные HTTPS origin через пробел (без путей и токенов)",
        ).pack(anchor="w")
        ttk.Entry(self.details, textvariable=self.static_origins).pack(fill="x")
        self.form_choice = ttk.Combobox(self.details, state="readonly")
        self.form_choice.pack(fill="x", pady=4)
        ttk.Button(self.details, text="Выбрать эту форму", command=self.select_form).pack(side="left")
        ttk.Button(self.details, text="Открыть проект для переобучения", command=self.open).pack(side="left")
        self.log = tk.Text(root, height=4, state="disabled", wrap="word")
        self.log.pack(fill="both", padx=12, pady=8)
        self._enable_clipboard(self.log)
        self.desktop_views()
        self.update_buttons()
        self.pump_after = root.after(100, self.pump)
        self.root.protocol("WM_DELETE_WINDOW", self.close)

    def toggle_advanced(self):
        if self.advanced.get():
            self.details.pack(fill="x", padx=12, pady=4, before=self.log)
        else:
            self.details.pack_forget()

    def text(self):
        return self.title.get(), self.body.get("1.0", "end-1c")

    def identity(self):
        value = self.project_id.get().strip()
        if not value:
            value = re.sub(r"[^A-Za-z0-9_-]", "-", urlsplit(self.url.get()).hostname or "project")[:70]
            self.project_id.set(value)
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", value):
            raise ValueError("Invalid project id")
        return value

    def write(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def run(self, operation):
        if self.busy:
            return
        self.busy = True
        generation = self.generation
        self.update_buttons()

        async def work():
            task = asyncio.current_task()
            self.operation_task = task
            self.operation_tasks.add(task)
            try:
                await operation()
                summary = self.session.result.summary() if self.session else {}
                self.messages.put((generation, "result", summary))
            except asyncio.CancelledError:
                self.messages.put((generation, "error", "Операция остановлена."))
            except Exception as exc:
                if generation != self.generation:
                    return
                self.last_error = {
                    "type": type(exc).__name__,
                    "frames": [
                        f"{Path(f.filename).name}:{f.lineno}:{f.name}"
                        for f in traceback.extract_tb(exc.__traceback__)[-5:]
                    ],
                }
                code = str(exc) if isinstance(exc, (PermissionError, ValueError)) else ""
                if code not in MESSAGES:
                    if self.session:
                        await self.session.close()
                        self.session = None
                    self.messages.put((generation, "reset", None))
                self.messages.put((generation, "error", MESSAGES.get(code, friendly_error(exc))))
            finally:
                if self.operation_task is task:
                    self.operation_task = None
                self.operation_tasks.discard(task)
                self.messages.put((generation, "done", None))

        self.future = asyncio.run_coroutine_threadsafe(work(), self.loop)

    def pump(self):
        while not self.messages.empty():
            generation, kind, value = self.messages.get_nowait()
            if generation is not None and generation != self.generation:
                continue  # A stopped/previous run cannot update this run's UI.
            if kind in {"desktop_error", "desktop_snapshot", "local_report", "system_report", "progress"}:
                self.desktop_message(kind, value)
                continue
            if kind == "result":
                self.snapshot = value
                self.status.set(
                    STATE_TEXT.get(value.get("state"), "Браузер закрыт. Можно начать распознавание.")
                )
                self.form_choice.configure(values=[f["id"] for f in value.get("forms", [])])
                if value.get("selected"):
                    self.form_choice.set(value["selected"])
                forms = value.get("forms", [])
                self.preview.set(
                    "Движок: "
                    + value.get("engine", "unknown")
                    + "; форм: "
                    + str(len(forms))
                    + "; роли: "
                    + ", ".join(forms[0]["roles"] if forms else [])
                    + "; уверенность формы: "
                    + str(round(100 * value.get("confidence", {}).get("form", 0)))
                    + "%"
                    + "; сопоставление полей: "
                    + str(round(100 * value.get("confidence", {}).get("fields", 0)))
                    + "%"
                )
                for item in value.get("diagnostics", []):
                    if item["code"] != "READ_ONLY" or value.get("state") == "DETECTED":
                        self.write(item["message"])
                if self.advanced.get():
                    self.write(json.dumps(value, ensure_ascii=False))
            elif kind == "error":
                self.status.set(value)
                self.write(value)
            elif kind == "reset":
                self.snapshot = {}
            elif kind == "saved":
                self.write("Сохранение: " + value)
            elif kind == "done":
                self.busy = False
                self.future = None
            self.update_buttons()
        if not self.closing:
            self.pump_after = self.root.after(100, self.pump)

    def update_buttons(self):
        state = self.snapshot.get("state")
        attempted = bool(self.session and self.session.browser_attempted)
        enabled = {
            "recognize": not self.busy,
            "publish": state == "DETECTED" and not attempted,
            "http": state == "HTTP_CANDIDATE" and not (self.session and self.session.http_attempted),
            "manual": bool(self.session) and not attempted,
            "finish": bool(self.session and self.session.manual)
            and state in {"DETECTED", "LEARNING_REQUIRED"},
            "save": state in {"HTTP_CANDIDATE", "HTTP_VERIFIED"},
            "stop": bool(self.session) or self.busy,
            "login": bool(self.session) and bool(self.snapshot.get("auth_required")) and not attempted,
        }
        for key, button in self.buttons.items():
            button.configure(
                state="normal"
                if not self.closing and enabled[key] and (not self.busy or key == "stop")
                else "disabled"
            )
        self.desktop_buttons()

    def recognize(self):
        if self.busy or not self.ensure_browser():
            return
        try:
            project_id, url = self.identity(), self.url.get().strip()
            self.preferences.save(url, project_id)
        except ValueError:
            self.status.set("Проверьте URL и ID проекта в расширенных настройках.")
            return
        kind, cdns = self.publication_kind.get(), self.static_origins.get().split()
        self.generation += 1
        self.snapshot = {}
        self.status.set("Исследую страницу без отправки публикаций…")

        async def work():
            if (
                self.session
                and not self.session.closed
                and not self.session.browser_attempted
                and self.session.url == url
                and self.session.publication_kind == kind
                and not cdns
            ):
                await self.session.recognize()
                return
            if self.session:
                await self.session.close()
            if not self.knowledge:
                self.knowledge = KnowledgeBase(Path(".poster/knowledge.sqlite"))
            self.session = RecognitionSession(
                url,
                Path(".poster/profiles") / project_id,
                knowledge=self.knowledge,
                publication_kind=kind,
                static_origins=None if not cdns else [*ResourcePolicy.DEFAULT_STATIC, *cdns],
            )
            await self.session.start()
            await self.session.recognize()

        self.run(work)

    def publish(self):
        if not messagebox.askyesno(
            "Одна тестовая публикация",
            "Отправить одну настоящую публикацию с указанными заголовком и текстом?",
        ):
            return
        title, body = self.text()
        project_id = self.identity()

        async def work():
            await self.session.test_publish(
                title, body, project_id, confirmed=True, existing_site=self.existing_site
            )

        self.run(work)

    def login(self):
        if not messagebox.askyesno(
            "Один вход на сайт",
            "Разрешить одну отправку показанной формы входа в течение двух минут? Введите данные в браузере, затем снова нажмите «Распознать сайт».",
        ):
            return

        async def work():
            await self.session.authorize_login(confirmed=True)

        self.run(work)

    def http_verify(self):
        title, body = self.text()
        suffix = uuid.uuid4().hex[:6]
        title = title + " [HTTP " + suffix + "]"
        body = body + "\nHTTP-проверка " + suffix
        if not messagebox.askyesno(
            "Отдельная HTTP-проверка",
            "Создать вторую настоящую публикацию через HTTP с новым заголовком «"
            + title
            + "» и отдельной пометкой в тексте?",
        ):
            return

        async def work():
            await self.session.close()
            await self.session.verify_http(title, body, confirmed=True)

        self.run(work)

    def choose_output(self):
        selected = filedialog.askdirectory(title="Каталог проектов для автоматического сохранения")
        if selected:
            self.output_parent.set(selected)

    def manual(self):
        if not messagebox.askyesno(
            "Ручное обучение",
            "Разрешить ручной вход и одну публикацию в открытом браузере? Найденные поля будут заполнены вашим текстом. После отправки нажмите «Завершить обучение».",
        ):
            return
        title, body = self.text()

        async def work():
            await self.session.begin_manual(title, body, confirmed=True)

        self.run(work)

    def finish_manual(self):
        project_id = self.identity()

        async def work():
            await self.session.finish_manual(project_id, existing_site=self.existing_site)

        self.run(work)

    def select_form(self):
        key = self.form_choice.get()
        if not key:
            return

        async def work():
            self.session.select(key)
            await self.session.highlight()

        self.run(work)

    def save(self):
        if self.snapshot.get("state") == "HTTP_CANDIDATE" and not messagebox.askyesno(
            "Непроверенный кандидат",
            "HTTP ещё не проверен. Сохранить только REVIEW-кандидат? Очередь Engine не будет его выполнять.",
        ):
            return
        parent = self.output_parent.get() or filedialog.askdirectory(
            title="Каталог проектов — внутри будет создана папка с ID"
        )
        if parent:
            output = Path(parent) / self.identity()
            if output.exists() and any(output.iterdir()):
                self.status.set("Папка проекта уже заполнена. Выберите новый каталог для новой версии.")
                return

            async def work():
                self.session.save(output)
                self.workspace.import_project(output)
                self.messages.put((self.generation, "saved", str(output)))

            self.run(work)

    def open(self):
        if self.busy:
            return
        folder = filedialog.askdirectory(title="Проект для переобучения")
        if folder:
            try:
                project = load_project(Path(folder))
                self.existing_site = project.site
                self.project_id.set(project.site.id)
                first = next(s for s in project.workflow.steps if s.op == "request")
                self.url.set(
                    urljoin(project.site.base_url, first.url)
                    if "{{" not in first.url
                    else project.site.base_url
                )
                self.status.set("Настройки проекта загружены. Откройте нужную форму и распознайте её заново.")
            except Exception:
                self.status.set("Не удалось прочитать проект. Проверьте READY и YAML.")

    def stop(self):
        self.generation += 1
        if self.future:
            self.future.cancel()
        self.busy = False
        self.snapshot = {}

        async def work():
            tasks = [t for t in self.operation_tasks if t is not asyncio.current_task() and not t.done()]
            for task in tasks:
                if not task.cancelling():
                    task.cancel()
            if tasks:
                await asyncio.shield(asyncio.gather(*tasks, return_exceptions=True))
            if self.session:
                await self.session.close()
                self.session = None

        self.run(work)

    def close(self):
        if self.closing:
            return
        self.closing = True
        for token in (self.pump_after, self.tick_after):
            self.root.after_cancel(token)
        self.busy = True
        for button in self.buttons.values():
            button.configure(state="disabled")
        self.generation += 1
        if self.future:
            self.future.cancel()
        self.status.set("Закрываю браузер и завершаю начатые HTTP-отправки…")
        self.desktop_buttons()

        async def cleanup():
            tasks = [t for t in self.operation_tasks if not t.done()]
            for task in tasks:
                if not task.cancelling():
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            await self.engine_bridge.stop()
            if self.session:
                await self.session.close()
            if self.knowledge:
                self.knowledge.close()
            await self.loop.shutdown_asyncgens()
            await self.loop.shutdown_default_executor()

        future = asyncio.run_coroutine_threadsafe(cleanup(), self.loop)

        def finish():
            if not future.done():
                self.root.after(50, finish)
                return
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join(timeout=1)
            if not self.thread.is_alive():
                self.loop.close()
            self.root.destroy()
            # Future/coroutine cycles can be collected by a network thread.
            # Release Tcl references here, on their owning Tk thread.
            self.buttons.clear()
            self.desktop_widgets.clear()
            for name, value in list(vars(self).items()):
                if isinstance(value, (tk.Variable, tk.Misc)):
                    setattr(self, name, None)

        finish()


def launch():
    root = tk.Tk()
    App(root)
    root.mainloop()
