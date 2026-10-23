"""Single-window desktop UI; no replacement HTTP/queue/knowledge implementation."""

import asyncio
import json
import os
from pathlib import Path
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import webbrowser

from ..browser import find_browser, install_browser
from .workspace import EngineBridge, JOB_TEXT, Workspace, local_test

ERROR_TEXT = {
    "NO_READY_PROJECTS": "Нет готовых проектов. Обучите сайт и сохраните проект после HTTP-проверки.",
    "PROJECT_NOT_READY": "Этот проект ещё не готов для Engine. Выполните обучение и HTTP-проверку.",
    "PROJECT_EXISTS": "Проект с таким ID уже есть. Сохраните новую версию с другим ID.",
    "EMPTY_BODY": "Введите текст публикации.",
    "EMPTY_TITLE": "Для темы нужен заголовок.",
    "BROWSER_PATH_INVALID": "Указанный браузер не найден. Выберите установленный Chrome в разделе «Система».",
    "UNSAFE_TEMP": "Папка временных файлов является ссылкой. Очистка и локальный тест остановлены.",
    "BROWSER_INSTALL_FAILED": "Chromium не удалось скачать. Проверьте подключение или выберите установленный браузер.",
}


def friendly_error(exc):
    if isinstance(exc, PermissionError):
        return "Нет доступа к папке. Распакуйте Poster в доступную для записи папку пользователя."
    if isinstance(exc, OSError):
        return "Не удалось прочитать или записать данные. Проверьте свободное место и права доступа."
    if type(exc).__name__ == "Timeout":
        return "Другой Engine уже использует эту очередь. Остановите второй экземпляр приложения."
    code = str(exc) if isinstance(exc, ValueError) else ""
    if code.startswith("LOCAL_"):
        return "Локальная проверка остановилась. Проверьте браузер в разделе «Система» и повторите тест."
    return ERROR_TEXT.get(code, "Операция не выполнена. Проверьте проект и браузер в разделе «Система».")


class DesktopMixin:
    def desktop_init(self, root):
        self.workspace = Workspace()
        os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(self.workspace.root / "browsers"))
        browser_setting = self.workspace.root / "browser.json"
        try:
            chosen = json.loads(browser_setting.read_text(encoding="utf-8")).get("path")
            if isinstance(chosen, str) and Path(chosen).is_file():
                os.environ.setdefault("POSTER_CHROMIUM", chosen)
        except (OSError, ValueError, AttributeError):
            pass
        self.engine_bridge = EngineBridge(self.workspace)
        self.desktop_tasks = set()
        self.refreshing = False
        self.closing = False
        self.operation_task = None
        self.operation_tasks = set()
        self.local_report = None
        self.desktop_widgets = {}
        self.project_paths = {}
        self.engine_status = tk.StringVar(value="Engine остановлен")
        self.queue_project = tk.StringVar()
        self.queue_title = tk.StringVar(value="Новая публикация")
        self.system_text = tk.StringVar(
            value="Нажмите «Проверить систему» для проверки места и зависимостей."
        )
        self.result_text = tk.StringVar(value="Результатов пока нет.")
        self.result_detail = tk.StringVar(value="Выберите запись для пояснения результата.")
        root.geometry("1000x700")
        root.minsize(940, 640)
        toolbar = ttk.Frame(root, padding=(10, 8))
        toolbar.pack(fill="x")
        for key, label, callback in (
            ("local_test", "Локальный тест", self.local_test),
            ("local_stop", "Отменить тест", self.stop),
            ("engine_start", "Запустить Engine", self.start_engine),
            ("engine_stop", "Остановить Engine", self.stop_engine),
            ("results", "Результаты", lambda: self.notebook.select(self.results_page)),
        ):
            widget = ttk.Button(toolbar, text=label, command=callback)
            widget.pack(side="left", padx=3)
            self.desktop_widgets[key] = widget
        ttk.Label(toolbar, textvariable=self.engine_status).pack(side="right")
        self.notebook = ttk.Notebook(root)
        self.notebook.pack(fill="both", expand=True, padx=8, pady=(0, 6))
        self.recorder_page = ttk.Frame(self.notebook)
        self.projects_page = ttk.Frame(self.notebook, padding=12)
        self.queue_page = ttk.Frame(self.notebook, padding=12)
        self.results_page = ttk.Frame(self.notebook, padding=12)
        self.system_page = ttk.Frame(self.notebook, padding=12)
        for page, label in (
            (self.recorder_page, "Сайт и обучение"),
            (self.projects_page, "Проекты"),
            (self.queue_page, "Очередь"),
            (self.results_page, "Результаты"),
            (self.system_page, "Система"),
        ):
            self.notebook.add(page, text=label)
        self.notebook.bind("<<NotebookTabChanged>>", lambda _: self.refresh_desktop())

    def desktop_views(self):
        ttk.Label(
            self.projects_page,
            text="Готовые проекты можно сразу выбрать для очереди. Кандидат требует HTTP-проверки.",
        ).pack(anchor="w")
        self.projects_tree = ttk.Treeview(
            self.projects_page, columns=("status",), show="tree headings", height=12
        )
        self.projects_tree.heading("#0", text="Проект")
        self.projects_tree.heading("status", text="Готовность")
        self.projects_tree.pack(fill="both", expand=True, pady=8)
        self.projects_tree.bind("<<TreeviewSelect>>", self.project_selected)
        row = ttk.Frame(self.projects_page)
        row.pack(fill="x")
        for key, label, callback in (
            ("import_project", "Добавить сохранённый проект…", self.import_project),
            ("project_queue", "В очередь публикаций", lambda: self.notebook.select(self.queue_page)),
            ("refresh_projects", "Обновить", self.refresh_desktop),
        ):
            self.desktop_widgets[key] = ttk.Button(row, text=label, command=callback)
            self.desktop_widgets[key].pack(side="left", padx=3)
        ttk.Label(self.queue_page, text="Проект").pack(anchor="w")
        self.project_combo = ttk.Combobox(self.queue_page, textvariable=self.queue_project, state="readonly")
        self.project_combo.pack(fill="x", pady=4)
        ttk.Label(self.queue_page, text="Заголовок (для ответа или комментария можно оставить пустым)").pack(
            anchor="w"
        )
        self.queue_title_entry = ttk.Entry(self.queue_page, textvariable=self.queue_title)
        self.queue_title_entry.pack(fill="x", pady=4)
        self._enable_clipboard(self.queue_title_entry)
        self.queue_body = tk.Text(self.queue_page, height=5, wrap="word", undo=True)
        self.queue_body.insert("1.0", "Текст публикации")
        self.queue_body.pack(fill="x", pady=4)
        self._enable_clipboard(self.queue_body)
        self.desktop_widgets["enqueue"] = ttk.Button(
            self.queue_page, text="Добавить публикацию в очередь", command=self.enqueue
        )
        self.desktop_widgets["enqueue"].pack(anchor="w", pady=4)
        self.queue_tree = ttk.Treeview(
            self.queue_page, columns=("project", "status"), show="headings", height=6
        )
        for key, label in (("project", "Проект"), ("status", "Состояние")):
            self.queue_tree.heading(key, text=label)
        self.queue_tree.pack(fill="both", expand=True, pady=4)
        ttk.Button(
            self.queue_page, text="Удалить выбранное ожидающее задание", command=self.cancel_queued
        ).pack(anchor="w")
        ttk.Label(self.results_page, textvariable=self.result_text, wraplength=950).pack(anchor="w", pady=6)
        self.results_tree = ttk.Treeview(
            self.results_page, columns=("project", "status", "url"), show="headings", height=12
        )
        for key, label in (("project", "Проект"), ("status", "Результат"), ("url", "Ссылка")):
            self.results_tree.heading(key, text=label)
        self.results_tree.pack(fill="both", expand=True)
        self.results_tree.bind("<<TreeviewSelect>>", self.result_selected)
        ttk.Label(self.results_page, textvariable=self.result_detail, wraplength=950).pack(anchor="w", pady=5)
        ttk.Button(
            self.results_page, text="Открыть подтверждённую публикацию", command=self.open_result
        ).pack(anchor="w", pady=6)
        ttk.Label(
            self.results_page,
            text="Если результат неизвестен, проверьте сайт вручную. Повторная отправка автоматически не выполняется.",
            wraplength=950,
        ).pack(anchor="w")
        ttk.Label(self.system_page, textvariable=self.system_text, justify="left", wraplength=950).pack(
            anchor="w", pady=8
        )
        for key, label, callback in (
            ("system_check", "Проверить систему", self.check_system),
            ("choose_browser", "Выбрать установленный Chrome/Chromium…", self.choose_browser),
            ("download_browser", "Скачать Chromium с моего согласия…", self.download_browser),
            ("clean_temp", "Очистить временные файлы Poster…", self.clean_temp),
        ):
            self.desktop_widgets[key] = ttk.Button(self.system_page, text=label, command=callback)
            self.desktop_widgets[key].pack(anchor="w", pady=4)
        ttk.Label(
            self.system_page,
            text="Очистка сохраняет проекты, очередь, ключи, знания, браузеры и профили реальных сайтов.",
            wraplength=950,
        ).pack(anchor="w", pady=10)
        ttk.Label(self.root, textvariable=self.status, wraplength=1000).pack(fill="x", padx=12, pady=5)
        self.refresh_desktop()
        self.check_system()
        self.tick_after = self.root.after(500, self.desktop_tick)

    def desktop_submit(self, operation):
        if self.closing:
            return

        async def work():
            try:
                await operation()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.messages.put((None, "desktop_error", friendly_error(exc)))

        future = asyncio.run_coroutine_threadsafe(work(), self.loop)
        self.desktop_tasks.add(future)
        future.add_done_callback(self.desktop_tasks.discard)

    def desktop_message(self, kind, value):
        if kind == "desktop_error":
            self.status.set(value)
            self.write(value)
        elif kind == "progress":
            self.status.set(value)
        elif kind == "local_report":
            self.local_report = value
            self.result_text.set(
                "Локальный тест пройден: браузер, HTTP, сохранение и Engine. Создано 3 записи. Тестовый сервер остановлен."
            )
            self.notebook.select(self.results_page)
        elif kind == "system_report":
            mb = 1024 * 1024
            self.system_text.set(
                f"Python: {value['python']}\nБраузер: {value['browser'] or 'не найден — выберите установленный или разрешите загрузку'}\n"
                f"Свободно: {value['free_bytes'] / mb:.0f} МБ\nДанные Poster: {value['data_bytes'] / mb:.1f} МБ; проекты: {value['projects_bytes'] / mb:.1f} МБ; временные: {value['temp_bytes'] / mb:.1f} МБ\n"
                + "\n".join(f"{k}: {v}" for k, v in value["dependencies"].items())
            )
        elif kind == "desktop_snapshot":
            projects, rows, running, error = value
            self.engine_status.set("Engine работает" if running else "Engine остановлен")
            if error:
                self.engine_status.set("Engine остановлен: проверьте проект или другой экземпляр приложения")
            self.project_paths = {p["id"]: p["path"] for p in projects}
            selected = self.projects_tree.selection()
            self.projects_tree.delete(*self.projects_tree.get_children())
            for p in projects:
                self.projects_tree.insert(
                    "",
                    "end",
                    iid=p["id"],
                    text=p["id"],
                    values=("Готов для Engine" if p["ready"] else "Требуется обучение / проверка",),
                )
            if selected and self.projects_tree.exists(selected[0]):
                self.projects_tree.selection_set(selected)
            self.project_combo.configure(values=[p["id"] for p in projects if p["ready"]])
            if not self.queue_project.get():
                ready = [p["id"] for p in projects if p["ready"]]
                if ready:
                    self.queue_project.set(ready[0])
            for tree in (self.queue_tree, self.results_tree):
                selection = tree.selection()
                tree.delete(*tree.get_children())
                for item in rows:
                    values = (item["project_id"], JOB_TEXT.get(item["status"], "Требуется проверка"))
                    if tree is self.results_tree:
                        values += (item["result_url"] or "",)
                    tree.insert("", "end", iid=item["id"], values=values)
                if selection and tree.exists(selection[0]):
                    tree.selection_set(selection)
            self.refreshing = False
        self.desktop_buttons()

    def desktop_buttons(self):
        if not self.desktop_widgets:
            return
        running = self.engine_bridge.running
        for key in (
            "local_test",
            "engine_start",
            "enqueue",
            "import_project",
            "download_browser",
            "clean_temp",
        ):
            enabled = not self.closing and not self.busy
            if key in {"local_test", "engine_start"}:
                enabled &= not running
            self.desktop_widgets[key].configure(state="normal" if enabled else "disabled")
        self.desktop_widgets["engine_stop"].configure(
            state="normal" if running and not self.closing else "disabled"
        )
        self.desktop_widgets["local_stop"].configure(
            state="normal" if self.busy and not self.closing else "disabled"
        )

    def refresh_desktop(self):
        if self.refreshing or self.closing or not hasattr(self, "projects_tree"):
            return
        self.refreshing = True

        async def work():
            self.messages.put(
                (
                    None,
                    "desktop_snapshot",
                    (
                        self.workspace.projects_list(),
                        self.engine_bridge.rows(),
                        self.engine_bridge.running,
                        self.engine_bridge.error,
                    ),
                )
            )

        self.desktop_submit(work)

    def desktop_tick(self):
        if self.closing:
            return
        self.refresh_desktop()
        self.desktop_buttons()
        self.gui_observation()
        self.tick_after = self.root.after(500, self.desktop_tick)

    def project_selected(self, _=None):
        selection = self.projects_tree.selection()
        if selection:
            self.queue_project.set(selection[0])

    def import_project(self):
        source = filedialog.askdirectory(title="Выберите сохранённый проект с READY")
        if source:

            async def work():
                self.workspace.import_project(Path(source))
                self.messages.put((None, "progress", "Проект добавлен. Выберите его в разделе «Проекты»."))

            self.desktop_submit(work)

    def enqueue(self):
        project, title, body = (
            self.queue_project.get(),
            self.queue_title.get(),
            self.queue_body.get("1.0", "end-1c"),
        )
        if not project or not body.strip():
            self.status.set("Выберите готовый проект и введите текст публикации.")
            return
        if not messagebox.askyesno(
            "Добавить в очередь",
            "Добавить одну настоящую публикацию? Работающий Engine отправит её на выбранный сайт.",
            parent=self.root,
        ):
            return

        async def work():
            self.engine_bridge.enqueue(project, title, body)
            self.messages.put((None, "progress", "Публикация добавлена в очередь."))

        self.desktop_submit(work)

    def cancel_queued(self):
        selection = self.queue_tree.selection()
        if selection:

            async def work():
                self.engine_bridge.cancel_pending(selection[0])

            self.desktop_submit(work)

    def start_engine(self):
        if not messagebox.askyesno(
            "Запустить Engine",
            "Выполнять ожидающие публикации готовых проектов? Запущенные отправки завершаются даже после нажатия «Остановить Engine».",
            parent=self.root,
        ):
            return

        async def work():
            await self.engine_bridge.start()

        self.desktop_submit(work)

    def stop_engine(self):
        self.engine_status.set("Завершаю уже начатые отправки…")
        self.desktop_submit(self.engine_bridge.stop)

    def ensure_browser(self):
        try:
            if find_browser():
                return True
        except ValueError as exc:
            self.status.set(friendly_error(exc))
            self.notebook.select(self.system_page)
            return False
        self.notebook.select(self.system_page)
        self.status.set(
            "Браузер не найден. Выберите установленный Chrome/Chromium или разрешите загрузку в разделе «Система»."
        )
        self.check_system()
        return False

    def local_test(self):
        if self.busy or not self.ensure_browser():
            return
        self.notebook.select(self.results_page)
        self.local_report = None
        generation = self.generation
        self.result_text.set("Выполняется локальный тест. Никакие внешние сайты не изменяются.")

        async def work():
            if self.session:
                await self.session.close()
                self.session = None
            report = await local_test(
                self.workspace, lambda text: self.messages.put((generation, "progress", text))
            )
            self.messages.put((generation, "local_report", report))

        self.run(work)

    def check_system(self):
        async def work():
            report = await asyncio.to_thread(self.workspace.system_report)
            self.messages.put((None, "system_report", report))

        self.desktop_submit(work)

    def choose_browser(self):
        path = filedialog.askopenfilename(
            title="Установленный Chrome/Chromium", filetypes=[("Браузер", "*.exe"), ("Все файлы", "*")]
        )
        if path:
            os.environ["POSTER_CHROMIUM"] = path
            try:
                (self.workspace.root / "browser.json").write_text(
                    json.dumps({"path": path}), encoding="utf-8"
                )
            except OSError as exc:
                self.status.set(friendly_error(exc))
            self.check_system()

    def download_browser(self):
        if self.busy:
            return
        if not messagebox.askyesno(
            "Загрузка Chromium",
            "Скачать дополнительный Chromium? Загрузка может составить до 500 МБ, на диске потребуется до 1.5 ГБ. Установленный Chrome/Chromium можно выбрать без скачивания.",
            parent=self.root,
        ):
            return

        async def work():
            if __import__("shutil").disk_usage(self.workspace.root).free < 1_500_000_000:
                raise OSError("Insufficient space")
            self.messages.put(
                (None, "progress", "Загружаю Chromium с вашего согласия. Это может занять несколько минут…")
            )
            await install_browser()
            self.messages.put(
                (None, "progress", "Chromium установлен. Можно начать локальный тест или распознавание.")
            )

        self.run(work)

    def clean_temp(self):
        if self.busy or not messagebox.askyesno(
            "Очистка Poster",
            "Удалить только помеченные временные файлы завершённых тестов Poster? Проекты, профили, очередь, ключи и знания сохранятся.",
            parent=self.root,
        ):
            return

        async def work():
            count = await asyncio.to_thread(self.workspace.clean_temp)
            self.messages.put(
                (None, "progress", f"Освобождено {count / 1024 / 1024:.1f} МБ временных файлов Poster.")
            )

        self.desktop_submit(work)

    def open_result(self):
        selection = self.results_tree.selection()
        if selection:
            values = self.results_tree.item(selection[0], "values")
            if values[1] == JOB_TEXT["SUCCEEDED"] and values[2].startswith(("http://", "https://")):
                webbrowser.open(values[2])

    def result_selected(self, _=None):
        selected = self.results_tree.selection()
        if not selected:
            return
        status = self.results_tree.item(selected[0], "values")[1]
        explanation = {
            "SUCCEEDED": "Публикация подтверждена. Кнопка ниже открывает её постоянную ссылку.",
            "AUTH_EXPIRED": "Сайт отклонил авторизацию. Откройте проект для переобучения и выполните вход в браузере.",
            "UNKNOWN": "Отправка могла состояться. Проверьте сайт вручную перед любыми следующими действиями.",
            "FAILED": "Не удалось выполнить проверку. Проверьте доступность сайта и заново обучите проект при изменении формы.",
            "REVIEW_REQUIRED": "Нужна проверка проекта или его ключа авторизации. Выполните обучение и отдельную HTTP-проверку.",
            "PENDING": "Задание будет выполнено после запуска Engine. Его ещё можно удалить из очереди.",
            "CANCELLED": "Ожидавшее задание удалено из очереди; отправка не начиналась.",
        }
        self.result_detail.set(
            next(
                (text for code, text in explanation.items() if JOB_TEXT[code] == status),
                "Engine выполняет задание. Остановка дождётся уже начатой отправки.",
            )
        )

    def gui_observation(self):
        """Opt-in, read-only geometry/state for external OS-mouse E2E of the packaged exe."""
        output = os.environ.get("POSTER_GUI_TEST_OUTPUT")
        if not output:
            return
        widgets = {
            **self.desktop_widgets,
            **self.buttons,
            "url": self.url_entry,
            "title": self.title_entry,
            "body": self.body,
            "queue_title": self.queue_title_entry,
            "queue_body": self.queue_body,
        }
        positions = {}
        for key, widget in widgets.items():
            if widget.winfo_viewable():
                positions[key] = [
                    widget.winfo_rootx() + widget.winfo_width() // 2,
                    widget.winfo_rooty() + widget.winfo_height() // 2,
                ]
        # ttk has no portable tab bbox; identify tab centers by hit testing.
        tabs = {}
        for x in range(0, self.notebook.winfo_width(), 4):
            try:
                index = self.notebook.index(f"@{x},10")
                tabs.setdefault(str(index), []).append(x)
            except tk.TclError:
                continue
        positions["tabs"] = {
            i: [self.notebook.winfo_rootx() + (min(xs) + max(xs)) // 2, self.notebook.winfo_rooty() + 10]
            for i, xs in tabs.items()
        }
        data = {
            "positions": positions,
            "busy": self.busy,
            "state": self.snapshot.get("state"),
            "engine_running": self.engine_bridge.running,
            "projects": list(self.project_paths),
            "jobs": [self.queue_tree.item(i, "values")[1] for i in self.queue_tree.get_children()],
            "local_report": self.local_report,
            "local_test_ports": list(self.workspace.test_ports),
            "temporary_active": len(self.workspace.active),
            "status": self.status.get(),
            "thread": threading.get_ident(),
            "observed_at": time.time(),
        }
        try:
            path = Path(output)
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            temporary.replace(path)
        except OSError:
            pass
