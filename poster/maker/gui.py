"""ProjectMaker panel. Browser/runtime services contain no dependency on this GUI."""

import asyncio
import json
import os
from pathlib import Path
import queue
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk
import traceback
import time
from urllib.parse import urlsplit

from ..recorder.clipboard import ClipboardMixin

from .actions import default_registry
from .canvas import GraphCanvas
from .fixtures import serve_maker
from .model import Document, Graph, GraphError, Node, empty_graph, graph_secret_refs
from .gui_tools import ToolsMixin, parse_value, show_value
from .recorder import ActionRecorder
from .record_script import SELECTOR_FUNCTION
from .runtime import ERRORS, GraphRunner
from .storage import Vault, load_graph, save_graph
from .variables import draft_parameters, has_reference

MESSAGES = {
    **ERRORS,
    "FILE_INVALID": "Это не поддерживаемый файл графа Maker. Старые READY-проекты открывайте в разделе «Проекты».",
    "SECRET_FILE_INVALID": "Зашифрованный файл секретов отсутствует или изменён. Восстановите его рядом с графом.",
    "FILE_TOO_LARGE": "Проект превышает лимит 10 МБ.",
    "RECORD_LIMIT": "Достигнут лимит 2000 кубиков. Остановите запись и разделите проект.",
    "POPUP_UNSUPPORTED": "Первый этап записывает одну вкладку. Действия в новом окне пока не добавляются.",
    "RECORD_EVENT_FAILED": "Одно действие не удалось записать. Проверьте граф перед воспроизведением.",
    "NAVIGATION_NOT_RECORDED": "Переход не удалось записать. Добавьте открытие URL или ожидание вручную.",
    "DEMO_PORT_BUSY": "Демо-порт занят. Закройте другой экземпляр демо-сайта и повторите запуск.",
}


class MakerPanel(ToolsMixin, ClipboardMixin):
    def __init__(self, notebook, loop, workspace):
        self.frame = ttk.Frame(notebook, padding=5)
        self.root = self.frame.winfo_toplevel()
        self.loop, self.workspace = loop, workspace
        self.registry = default_registry()
        self.document = Document()
        self.vault = Vault()
        self.path = None
        self.messages = queue.Queue()
        self.closed = False
        self.busy = self.recording = False
        self.operation = None
        self.operation_kind = None
        self.recorder = self.runner = None
        self.demo_context = None
        self.demo_server = None
        self.demo_base = None
        self.trace = []
        self.last_error = None
        self.browser_view = {}
        self.probe_task = None
        self.probing = False
        self.last_probe = 0
        self.epoch = 0
        self.props = {}
        self.buttons = {}
        self.saved_paths = []
        self.project_name = tk.StringVar(value=self.document.graph.name)
        self.url = tk.StringVar()
        self.saved = tk.StringVar()
        self.status = tk.StringVar(
            value="Запишите действия или добавьте кубики из библиотеки. S — Success; E — Error."
        )
        self.selected_label = tk.StringVar(value="Выберите кубик на холсте")
        self.node_title = tk.StringVar()
        self.directory = workspace.root / "maker-projects"
        self.directory.mkdir(exist_ok=True)
        self.settings = workspace.root / "maker-settings.json"
        self.build()
        for widget in (self.name_entry, self.url_entry, self.title_entry, self.trace_widget):
            self._enable_clipboard(widget)
        self.project_name.trace_add("write", self.name_changed)
        self.refresh_saved()
        try:
            previous = json.loads(self.settings.read_text(encoding="utf-8")).get("last_project")
            if isinstance(previous, str) and Path(previous).is_file():
                self.load(Path(previous))
        except (OSError, ValueError):
            pass
        self.pump_after = self.frame.after(80, self.pump)

    def button(self, parent, key, label, command):
        widget = ttk.Button(parent, text=label, command=command)
        widget.pack(side="left", padx=2, pady=2)
        self.buttons[key] = widget
        return widget

    def build(self):
        row = ttk.Frame(self.frame)
        row.pack(fill="x")
        ttk.Label(row, text="Проект:").pack(side="left")
        self.name_entry = ttk.Entry(row, textvariable=self.project_name, width=20)
        self.name_entry.pack(side="left", padx=3)
        self.button(row, "new", "Новый", self.new)
        self.button(row, "save", "Сохранить", self.save)
        self.button(row, "save_as", "Сохранить как…", lambda: self.save(save_as=True))
        self.saved_combo = ttk.Combobox(row, textvariable=self.saved, state="readonly", width=24)
        self.saved_combo.pack(side="left", padx=3, fill="x", expand=True)
        self.button(row, "open", "Открыть", self.open)
        self.button(row, "open_file", "Из файла…", lambda: self.open(from_file=True))
        row = ttk.Frame(self.frame)
        row.pack(fill="x")
        ttk.Label(row, text="URL:").pack(side="left")
        self.url_entry = ttk.Entry(row, textvariable=self.url)
        self.url_entry.pack(side="left", fill="x", expand=True, padx=3)
        self.button(row, "navigate", "Открыть URL", self.navigate)
        self.button(row, "record", "Начать запись", self.start_recording)
        self.button(row, "record_resume", "Продолжить запись", lambda: self.start_recording(continued=True))
        self.button(row, "record_stop", "Остановить запись", self.stop_recording)
        self.button(row, "demo", "Демо-сайт", self.demo)
        row = ttk.Frame(self.frame)
        row.pack(fill="x")
        self.button(row, "run", "Запустить", self.run)
        self.button(row, "step", "Шаг", self.step)
        self.button(row, "stop", "Остановить", self.stop)
        self.button(row, "copy", "Копировать кубик", lambda: self.canvas.duplicate())
        self.button(row, "delete", "Удалить", lambda: self.canvas.delete())
        self.button(row, "undo", "Отменить правку", lambda: self.canvas.undo())
        self.button(row, "fit", "Показать всё", lambda: self.canvas.fit())
        self.button(row, "zoom", "100%", self.reset_zoom)
        row = ttk.Frame(self.frame)
        row.pack(fill="x")
        self.button(row, "breakpoint", "Точка останова", self.toggle_breakpoint)
        self.button(row, "selected_run", "Выполнить кубик", self.run_selected)
        self.button(row, "example", "Пример с циклом", self.example_project)
        ttk.Label(
            self.frame,
            text="Браузер открывается отдельным управляемым окном. Встроенный Chromium запланирован на следующий этап.",
            wraplength=880,
        ).pack(anchor="w", pady=3)
        panes = ttk.Panedwindow(self.frame, orient="horizontal")
        panes.pack(fill="both", expand=True)
        library = ttk.Frame(panes, width=160)
        panes.add(library, weight=0)
        ttk.Label(library, text="Библиотека действий").pack(anchor="w")
        self.library = tk.Listbox(library, width=23, exportselection=False)
        self.action_names = [s.name for s in self.registry.actions.values()]
        for spec in self.registry.actions.values():
            self.library.insert("end", spec.label)
        self.library.pack(fill="both", expand=True)
        self.library.bind("<Double-1>", lambda _: self.add())
        self.button(library, "add", "Добавить кубик", self.add)
        self.canvas = GraphCanvas(
            panes, self.document, selected=self.select, changed=self.changed, editable=self.editable
        )
        panes.add(self.canvas.frame, weight=4)
        tools = ttk.Notebook(panes, width=285)
        panes.add(tools, weight=0)
        self.tools_notebook = tools
        properties = ttk.Frame(tools, width=270)
        self.properties_page = properties
        tools.add(properties, text="Свойства")
        self.build_tools(tools)
        ttk.Label(properties, text="Свойства кубика").pack(anchor="w")
        ttk.Label(properties, textvariable=self.selected_label, wraplength=250).pack(anchor="w")
        ttk.Label(properties, text="Название").pack(anchor="w")
        self.title_entry = ttk.Entry(properties, textvariable=self.node_title)
        self.title_entry.pack(fill="x", pady=3)
        prop_canvas = tk.Canvas(properties, width=255, height=250, highlightthickness=0)
        prop_scroll = ttk.Scrollbar(properties, orient="vertical", command=prop_canvas.yview)
        prop_canvas.configure(yscrollcommand=prop_scroll.set)
        prop_scroll.pack(side="right", fill="y")
        prop_canvas.pack(fill="both", expand=True)
        self.prop_frame = ttk.Frame(prop_canvas)
        prop_canvas.create_window((0, 0), window=self.prop_frame, anchor="nw", width=245)
        self.prop_frame.bind(
            "<Configure>", lambda _: prop_canvas.configure(scrollregion=prop_canvas.bbox("all"))
        )
        self.property_canvas = prop_canvas
        prop_canvas.bind("<MouseWheel>", self.scroll_properties)
        self.prop_frame.bind("<MouseWheel>", self.scroll_properties)
        self.button(properties, "apply", "Применить", self.apply)
        ttk.Label(self.frame, textvariable=self.status, wraplength=880).pack(fill="x", pady=3)
        self.trace_widget = tk.Text(self.frame, height=3, state="disabled", wrap="word")
        self.trace_widget.pack(fill="x")
        self.update_buttons()

    def editable(self):
        return (
            not self.closed
            and not self.busy
            and not self.recording
            and not (self.runner and self.runner.state in {"READY", "PAUSED", "RUNNING"})
        )

    def reset_zoom(self):
        self.document.graph.view.zoom = 1
        self.canvas.draw()

    def name_changed(self, *_):
        if (
            self.editable()
            and self.project_name.get().strip()
            and self.project_name.get() != self.document.graph.name
        ):
            self.document.graph.name = self.project_name.get()[:200]
            self.document.dirty = True

    def changed(self):
        self.document.dirty = True
        runner = self.active_runner()
        if not runner or runner.state != "PAUSED":
            self.canvas.results = {}
        self.status.set(
            "Параметры обновлены в остановленной сессии. Можно продолжить."
            if runner and runner.state == "PAUSED"
            else "Граф изменён. Связь: нажмите S/E источника, затем вход или тело следующего кубика."
        )
        self.update_buttons()

    def replace(self, graph, vault=None, dirty=False):
        self.document = Document(graph)
        self.document.dirty = dirty
        if vault is not None:
            self.vault = vault
        self.canvas.document = self.document
        self.canvas.selected = set()
        self.canvas.selected_edge = None
        self.canvas.results = {}
        self.refresh_variables()
        self.project_name.set(graph.name)
        self.canvas.draw()
        self.select(None)

    def add(self):
        if not self.editable():
            return
        selected = self.library.curselection()
        if not selected:
            return
        name = self.action_names[selected[0]]
        spec = self.registry.get(name)
        count = len(self.document.graph.nodes)
        node = self.document.add(
            name, spec.params().model_dump(mode="json"), 80 + (count % 3) * 220, 80 + (count // 3) * 125
        )
        self.canvas.selected = {node.id}
        self.canvas.draw()
        self.select(node.id)
        self.changed()

    def select(self, node_id):
        for child in self.prop_frame.winfo_children():
            child.destroy()
        self.props.clear()
        if not node_id:
            self.selected_label.set("Выберите кубик на холсте")
            self.node_title.set("")
            self.update_buttons()
            return
        node = self.document.graph.node(node_id)
        self.selected_label.set(node.type + " · " + node.id[:7])
        self.node_title.set(node.title)
        try:
            spec = self.registry.get(node.type, node.action_version)
        except GraphError:
            self.status.set(MESSAGES["ACTION_UNSUPPORTED"])
            return
        defaults = spec.params().model_dump(mode="json")
        if node.type == "Start":
            ttk.Button(
                self.prop_frame, text="Сделать точкой входа", command=lambda: self.set_entry(node.id)
            ).pack(anchor="w", pady=3)
        for prop in spec.properties:
            ttk.Label(self.prop_frame, text=prop.label, wraplength=240).pack(anchor="w")
            value = node.params.get(prop.name, defaults.get(prop.name, ""))
            protected = isinstance(value, dict) and set(value) == {"secret_ref"}
            shown = (
                show_value(value)
                if prop.kind in {"json", "value"}
                else (
                    "@secret:" + value["secret_ref"]
                    if protected
                    else "\n".join(value)
                    if isinstance(value, list)
                    else ""
                    if value is None
                    else str(value)
                )
            )
            if prop.kind == "bool":
                shown = "Да" if value else "Нет"
            if prop.kind in {"multiline", "lines"}:
                widget = tk.Text(
                    self.prop_frame, height=3 if prop.kind == "multiline" else 2, width=28, wrap="word"
                )
                widget.insert("1.0", shown)
            elif prop.kind in {"choice", "bool"}:
                widget = ttk.Combobox(
                    self.prop_frame,
                    values=("Нет", "Да") if prop.kind == "bool" else prop.choices,
                    state="readonly",
                    width=27,
                )
                widget.set(shown)
            else:
                widget = ttk.Entry(self.prop_frame, width=29)
                widget.insert(0, shown)
            widget.pack(fill="x", pady=(0, 3))
            self._enable_clipboard(widget)
            widget.bind("<MouseWheel>", self.scroll_properties)
            secure = tk.BooleanVar(value=protected)
            if prop.secret:
                ttk.Checkbutton(self.prop_frame, text="Защищённое значение", variable=secure).pack(anchor="w")
            self.props[prop.name] = (prop, widget, secure, value)
        self.add_output_properties(node)
        self.update_buttons()

    def apply(self):
        if not self.params_editable() or len(self.canvas.selected) != 1:
            return False
        node = self.document.graph.node(next(iter(self.canvas.selected)))
        try:
            spec = self.registry.get(node.type, node.action_version)
            values = {**spec.params().model_dump(mode="json"), **node.params}
            for name, (prop, widget, secure, _old) in self.props.items():
                text = widget.get("1.0", "end-1c") if isinstance(widget, tk.Text) else widget.get()
                if prop.secret and secure.get():
                    if text.startswith("@secret:"):
                        values[name] = {"secret_ref": text.removeprefix("@secret:")}
                    else:
                        values[name] = self.vault.put(text)
                elif text.startswith("@secret:"):
                    raise GraphError("PASSWORD_REF_REQUIRED")
                elif prop.kind in {"integer", "optional_integer"}:
                    values[name] = (
                        text
                        if has_reference(text)
                        else None
                        if not text and prop.kind == "optional_integer"
                        else int(text)
                    )
                elif prop.kind == "bool":
                    values[name] = text == "Да"
                elif prop.kind == "lines":
                    values[name] = [s for s in text.splitlines() if s]
                elif prop.kind == "json":
                    values[name] = json.loads(text)
                elif prop.kind == "value":
                    values[name] = parse_value(text)
                else:
                    values[name] = text
            if node.type == "Browser.Input":
                values["sensitive"] = isinstance(values.get("value"), dict) and set(values["value"]) == {
                    "secret_ref"
                }
            values = draft_parameters(spec, values)
            updated = Node.model_validate(
                {
                    **node.model_dump(),
                    "params": values,
                    "title": self.node_title.get()[:200],
                    "save_as": self.output_name.get().strip() or None,
                    "result_path": self.output_path.get().strip(),
                    "protect_result": self.output_secret.get(),
                }
            )
            candidate = self.document.graph.model_copy(deep=True)
            candidate.nodes = [updated if n.id == node.id else n for n in candidate.nodes]
            candidate.validate_structure()
            runner = self.active_runner()
            if runner and runner.state == "PAUSED":
                runner.update_node(updated)
            self.document.checkpoint()
            for key in ("params", "title", "save_as", "result_path", "protect_result"):
                setattr(node, key, getattr(updated, key))
            self.canvas.draw()
            self.select(node.id)
            self.changed()
            return True
        except Exception as exc:
            self.show_error(exc)
            return False

    def show_error(self, exc):
        code = (
            exc.code
            if isinstance(exc, GraphError)
            else "PARAMETERS_INVALID"
            if isinstance(exc, ValueError)
            else "BROWSER_ERROR"
        )
        if isinstance(exc, PermissionError):
            text = "Нет доступа к папке. Выберите доступную для записи папку."
        else:
            text = MESSAGES.get(code, "Операция не выполнена. Проверьте проект и установленный браузер.")
        self.status.set(text)
        return code

    def scroll_properties(self, event):
        self.property_canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")
        return "break"

    def set_entry(self, node_id):
        if self.editable():
            self.document.checkpoint()
            self.document.graph.entry = node_id
            self.canvas.draw()
            self.changed()

    def submit(self, kind, operation):
        if self.closed or self.busy:
            return
        self.busy = True
        self.operation_kind = kind
        self.last_error = None
        self.update_buttons()
        messages = self.messages
        epoch = self.epoch

        async def work():
            self.operation = asyncio.current_task()
            try:
                await operation()
            except asyncio.CancelledError:
                messages.put((epoch, "stopped", None))
            except Exception as exc:
                code = exc.code if isinstance(exc, GraphError) else "BROWSER_ERROR"
                messages.put(
                    (
                        epoch,
                        "error",
                        {
                            "code": code,
                            "type": type(exc).__name__,
                            "frames": [
                                f"{Path(f.filename).name}:{f.lineno}"
                                for f in traceback.extract_tb(exc.__traceback__)[-4:]
                            ],
                        },
                    )
                )
            finally:
                self.operation = None
                messages.put((epoch, "idle", None))

        asyncio.run_coroutine_threadsafe(work(), self.loop)

    def pump(self):
        while not self.messages.empty():
            epoch, kind, value = self.messages.get_nowait()
            if epoch != self.epoch:
                continue
            if kind == "graph":
                self.replace(Graph.model_validate(value), dirty=True)
            elif kind == "recording":
                self.recording = value
                self.status.set(
                    "Запись идёт. Выполняйте действия в открытом браузере."
                    if value
                    else "Запись остановлена. Выберите кубик и измените его свойства."
                )
            elif kind == "idle":
                self.busy = False
            elif kind == "error":
                self.last_error = value
                self.status.set(MESSAGES.get(value["code"], MESSAGES["BROWSER_ERROR"]))
            elif kind == "warning":
                if isinstance(value, dict):
                    self.status.set(
                        MESSAGES.get(value["code"], MESSAGES["RECORD_EVENT_FAILED"])
                        + " "
                        + value.get("action", "")
                        + " "
                        + value.get("selector", "")
                    )
                else:
                    self.status.set(MESSAGES.get(value, value))
            elif kind == "picked":
                self.receive_pick(value)
            elif kind == "selector_result":
                self.selector_result = value
                self.status.set(
                    "Найдено элементов: " + str(value["count"]) + ". Совпадения подсвечены в браузере."
                )
            elif kind == "demo":
                self.url.set(value)
                self.status.set("Демо-сайт готов. Нажмите «Начать запись».")
            elif kind == "stopped":
                self.status.set("Остановлено. Уже выполненные действия не отменяются.")
            elif kind == "event":
                if value["kind"] == "node":
                    self.canvas.results[value["node_id"]] = "RUNNING"
                elif value["kind"] == "variables":
                    self.runtime_values = value["values"]
                    if hasattr(self, "refresh_variables"):
                        self.refresh_variables()
                elif value["kind"] == "pause":
                    if value.get("node_id"):
                        self.canvas.results[value["node_id"]] = "BREAKPOINT"
                    self.status.set("Точка останова. Измените параметры или продолжите кнопкой «Запустить».")
                elif value["kind"] == "result":
                    result = value["result"]
                    self.canvas.results[result["node_id"]] = result["status"]
                    self.trace.append(result)
                    self.trace_widget.configure(state="normal")
                    self.trace_widget.insert(
                        "end",
                        f"{result['action']} → {result['status']} / {result['port']} {json.dumps(result['value'], ensure_ascii=False)} {result['message']}\n",
                    )
                    if result.get("details"):
                        self.trace_widget.insert(
                            "end", json.dumps(result["details"], ensure_ascii=False) + "\n"
                        )
                    self.trace_widget.see("end")
                    self.trace_widget.configure(state="disabled")
                    self.status.set(
                        {
                            "PAUSED": "Пауза. Нажмите «Шаг» или «Запустить», чтобы продолжить.",
                            "SUCCEEDED": "Граф выполнен успешно.",
                            "FAILED": "Выполнение остановилось с ошибкой. Смотрите кубик и результат.",
                            "STOPPED": "Остановлено. Уже выполненные действия не отменяются.",
                        }.get(value["state"], value["state"])
                    )
                self.canvas.draw()
            self.update_buttons()
        if (
            not self.closed
            and os.environ.get("POSTER_GUI_TEST_OUTPUT")
            and not self.probing
            and time.monotonic() - self.last_probe > 0.3
        ):
            self.probing = True
            self.last_probe = time.monotonic()
            asyncio.run_coroutine_threadsafe(self.probe_browser(), self.loop)
        if not self.closed:
            self.pump_after = self.frame.after(80, self.pump)

    def update_buttons(self):
        if not self.buttons:
            return
        edit = self.editable()
        for name in ("new", "save_as", "open", "open_file", "add", "copy", "delete", "undo", "apply", "demo"):
            self.buttons[name].configure(state="normal" if edit else "disabled")
        self.buttons["save"].configure(
            state="normal" if not self.busy and not self.recording and not self.closed else "disabled"
        )
        self.buttons["record"].configure(state="normal" if edit else "disabled")
        self.buttons["record_resume"].configure(
            state="normal" if edit and len(self.canvas.selected) == 1 else "disabled"
        )
        self.buttons["apply"].configure(state="normal" if self.params_editable() else "disabled")
        self.buttons["breakpoint"].configure(state="normal" if self.params_editable() else "disabled")
        runner = self.active_runner()
        paused = runner and runner.state == "PAUSED" and not self.busy
        self.buttons["selected_run"].configure(
            state="normal" if self.params_editable() and len(self.canvas.selected) == 1 else "disabled"
        )
        for name in ("var_save", "var_delete", "example", "read_text", "read_attribute"):
            self.buttons[name].configure(state="normal" if edit else "disabled")
        self.buttons["var_live"].configure(state="normal" if paused else "disabled")
        for name in (
            "browser_open",
            "pick",
            "selector_check",
            "selector_apply",
            "tab_new",
            "tab_switch",
            "tab_close",
        ):
            self.buttons[name].configure(
                state="normal" if not self.busy and not self.recording and not self.closed else "disabled"
            )
        self.buttons["record_stop"].configure(
            state="normal" if self.recording and not self.busy else "disabled"
        )
        self.buttons["navigate"].configure(state="normal" if self.recording and not self.busy else "disabled")
        for name in ("run", "step"):
            self.buttons[name].configure(
                state="normal" if not self.closed and not self.busy and not self.recording else "disabled"
            )
        self.buttons["stop"].configure(
            state="normal" if (self.busy or self.runner) and not self.closed else "disabled"
        )

    async def ensure_demo(self, origin=None):
        if origin is None:
            origin = self.document.graph.metadata.get("maker_demo")
        if not origin:
            return
        parsed = urlsplit(origin)
        if (
            parsed.scheme != "http"
            or parsed.hostname != "127.0.0.1"
            or not parsed.port
            or not 1024 <= parsed.port <= 65535
        ):
            raise GraphError("URL_INVALID")
        if self.demo_base == origin:
            return
        if self.demo_context:
            self.demo_context.__exit__(None, None, None)
            self.demo_context = None
        manager = serve_maker(parsed.port)
        try:
            self.demo_server, self.demo_base = manager.__enter__()
        except OSError as exc:
            raise GraphError("DEMO_PORT_BUSY") from exc
        self.demo_context = manager

    def demo(self):
        async def work():
            if not self.demo_context:
                settings = self.workspace.root / "maker-demo-port.json"
                port = 0
                try:
                    port = int(json.loads(settings.read_text(encoding="utf-8"))["port"])
                except (OSError, ValueError, KeyError):
                    pass
                if not 1024 <= port <= 65535:
                    port = 0
                manager = serve_maker(port)
                try:
                    self.demo_server, self.demo_base = manager.__enter__()
                except OSError as exc:
                    raise GraphError("DEMO_PORT_BUSY") from exc
                self.demo_context = manager
                settings.write_text(json.dumps({"port": urlsplit(self.demo_base).port}), encoding="utf-8")
            self.messages.put((self.epoch, "demo", self.demo_base + "/"))

        self.submit("demo", work)

    def confirm_discard(self):
        if not self.document.dirty:
            return True
        answer = messagebox.askyesnocancel(
            "Несохранённый граф",
            "Сохранить изменения графа перед продолжением?",
            parent=self.frame.winfo_toplevel(),
        )
        if answer is None:
            return False
        return self.save() if answer else True

    def new(self):
        if self.editable() and self.confirm_discard():
            self.path = None
            self.replace(empty_graph(), Vault())
            self.trace = []
            self.status.set("Новый граф. Добавьте кубики или начните запись.")

    def start_recording(self, continued=False):
        if not self.editable() or (not continued and not self.confirm_discard()):
            return
        selected = next(iter(self.canvas.selected)) if continued and len(self.canvas.selected) == 1 else None
        if continued and (
            not selected or self.document.graph.node(selected).type in {"End", "End.Success", "End.Failure"}
        ):
            self.status.set("Выберите кубик, после которого нужно продолжить запись.")
            return
        url = self.url.get().strip()
        if url and urlsplit(url).scheme not in {"http", "https"}:
            self.status.set(MESSAGES["URL_INVALID"])
            return
        self.epoch += 1
        messages = self.messages
        epoch = self.epoch
        previous = self.recorder
        reused = previous.session if continued and previous and previous.session.page else None
        if not continued:
            self.vault = Vault()
            self.path = None
        self.trace = []
        recording = ActionRecorder(
            headless=False,
            vault=self.vault,
            on_change=lambda g: messages.put((epoch, "graph", g)),
            on_warning=lambda code: messages.put((epoch, "warning", code)),
            graph=self.document.graph if continued else None,
            after_node=selected,
            session=reused,
        )
        if self.demo_base and url.startswith(self.demo_base + "/"):
            recording.graph.metadata["maker_demo"] = self.demo_base
        self.recorder = recording

        async def work():
            if previous and not reused:
                await previous.close()
            await recording.start()
            messages.put((epoch, "recording", True))
            if url and not reused:
                await recording.navigate(url)

        self.submit("record", work)

    def navigate(self):
        if self.recording and self.recorder:
            url = self.url.get().strip()

            async def work():
                await self.recorder.navigate(url)

            self.submit("navigate", work)

    def stop_recording(self):
        if not self.recording:
            return

        async def work():
            await self.recorder.stop(close=False)
            self.messages.put((self.epoch, "recording", False))

        self.submit("record_stop", work)

    def prepare_secrets(self):
        refs = graph_secret_refs(self.document.graph)
        for ref in sorted(refs):
            try:
                self.vault.get(ref)
            except GraphError:
                value = simpledialog.askstring(
                    "Защищённое значение",
                    "Введите значение для " + ref,
                    show="*",
                    parent=self.frame.winfo_toplevel(),
                )
                if value is None:
                    return False
                self.vault.put(value, ref)
        return True

    def execute(self, step):
        if self.busy or self.recording:
            return
        fresh = not self.runner or self.runner.state in {"SUCCEEDED", "FAILED", "STOPPED"}
        if fresh:
            if not self.prepare_secrets():
                return
            inputs = self.prepare_inputs()
            if inputs is None:
                return
            if not messagebox.askyesno(
                "Выполнение графа",
                "Выполнить действия графа в браузере? Клики могут отправлять формы и изменять данные на сайте.",
                parent=self.frame.winfo_toplevel(),
            ):
                return
            graph = self.document.graph.model_copy(deep=True)
            vault = self.vault
            self.trace = []
            self.canvas.results = {}
            self.canvas.draw()
            self.trace_widget.configure(state="normal")
            self.trace_widget.delete("1.0", "end")
            self.trace_widget.configure(state="disabled")
        messages = self.messages
        epoch = self.epoch

        async def work():
            if fresh:
                if self.recorder:
                    await self.recorder.close()
                    self.recorder = None
                if self.tools_context:
                    await self.tools_context.session.close()
                    self.tools_context = None
                if self.runner:
                    await self.runner.close()
                await self.ensure_demo()
                self.runner = GraphRunner(
                    graph,
                    vault=vault,
                    headless=False,
                    on_event=lambda event: messages.put((epoch, "event", event)),
                    inputs=inputs,
                )
            if step:
                await self.runner.step()
            else:
                await self.runner.run()

        self.submit("step" if step else "run", work)

    def run(self):
        self.execute(False)

    def step(self):
        self.execute(True)

    async def stop_all(self):
        task = self.operation
        if self.runner:
            await self.runner.stop()
        if task and task is not asyncio.current_task() and not task.done():
            if self.operation_kind not in {"run", "step"} and not task.cancelling():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if self.recorder:
            await self.recorder.close()
            self.recorder = None
        if self.runner:
            await self.runner.close()
            self.runner = None
        if self.tools_context:
            await self.tools_context.session.close()
            self.tools_context = None
        self.inspector = None
        self.messages.put((self.epoch, "recording", False))
        self.messages.put((self.epoch, "idle", None))
        self.messages.put((self.epoch, "stopped", None))

    def stop(self):
        if not self.closed:
            asyncio.run_coroutine_threadsafe(self.stop_all(), self.loop)

    def save(self, save_as=False):
        if self.busy or self.recording:
            return False
        path = self.path
        if save_as:
            chosen = filedialog.asksaveasfilename(
                title="Сохранить граф Maker",
                initialdir=self.directory,
                defaultextension=".postergraph.json",
                filetypes=[("Граф Maker", "*.json")],
            )
            if not chosen:
                return False
            path = Path(chosen)
        if path is None:
            path = self.directory / (self.document.graph.id + ".postergraph.json")
        try:
            saved = save_graph(path, self.document.graph, self.vault)
            self.document.graph = saved
            self.document.dirty = False
            self.path = path
            self.settings.write_text(json.dumps({"last_project": str(path)}), encoding="utf-8")
            self.refresh_saved()
            self.status.set(
                "Граф сохранён. Зашифрованные секреты храните рядом с JSON; ключ остаётся в .poster/private."
            )
            return True
        except Exception as exc:
            self.show_error(exc)
            return False

    def refresh_saved(self):
        self.saved_paths = []
        labels = []
        for path in sorted(self.directory.glob("*.postergraph.json")):
            try:
                graph, _ = load_graph(path)
                self.saved_paths.append(path)
                labels.append(graph.name + " · " + graph.id[:7])
            except Exception:
                continue
        self.saved_combo.configure(values=labels)
        if self.path in self.saved_paths:
            self.saved_combo.current(self.saved_paths.index(self.path))

    def load(self, path):
        graph, vault = load_graph(path)
        self.path = Path(path)
        self.replace(graph, vault)
        first = next((n.params.get("url") for n in graph.nodes if n.type == "Browser.OpenURL"), None)
        if isinstance(first, str):
            self.url.set(first)
        self.refresh_saved()
        self.status.set("Граф открыт. Нажмите «Запустить» или «Шаг».")

    def open(self, from_file=False):
        if not self.editable() or not self.confirm_discard():
            return
        index = self.saved_combo.current()
        path = self.saved_paths[index] if not from_file and 0 <= index < len(self.saved_paths) else None
        if path is None:
            chosen = filedialog.askopenfilename(
                title="Открыть граф Maker", initialdir=self.directory, filetypes=[("Граф Maker", "*.json")]
            )
            if not chosen:
                return
            path = Path(chosen)
        try:
            self.load(path)
            self.settings.write_text(json.dumps({"last_project": str(path)}), encoding="utf-8")
        except Exception as exc:
            self.show_error(exc)

    def can_close(self):
        if self.busy or self.recording:
            return messagebox.askyesno(
                "Закрыть Maker",
                "Остановить запись/выполнение и закрыть приложение? Последние несохранённые изменения не сохранятся.",
                parent=self.frame.winfo_toplevel(),
            )
        return self.confirm_discard()

    def prepare_close(self):
        self.closed = True
        self.frame.after_cancel(self.pump_after)

    async def shutdown(self):
        await self.stop_all()
        if self.probe_task and not self.probe_task.done():
            await asyncio.gather(self.probe_task, return_exceptions=True)
        if self.demo_context:
            self.demo_context.__exit__(None, None, None)
            self.demo_context = None
            self.demo_server = None

    async def probe_browser(self):
        """Read-only DOM geometry for OS-input tests; never reads field values."""
        self.probe_task = asyncio.current_task()
        try:
            page = (
                self.recorder.session.page
                if self.recorder
                else self.runner.context.session.page
                if self.runner
                else None
            )
            if not page or page.is_closed():
                self.browser_view = {}
                return
            script = """() => {const selector=SELECTOR;
              return {title:document.title,viewport:{width:innerWidth,height:innerHeight},
                controls:[...document.querySelectorAll('button,a,input:not([type=hidden]),textarea,select')].map(e=>{
                  const r=e.getBoundingClientRect();return {selector:selector(e),x:r.x,y:r.y,width:r.width,height:r.height};
                }).filter(r=>r.width>0&&r.height>0)};}""".replace("SELECTOR", SELECTOR_FUNCTION)
            self.browser_view = await page.evaluate(script)
        except Exception:
            pass
        finally:
            self.probing = False

    def observation(self):
        if self.closed:
            return {}
        widgets = {"maker_" + k: v for k, v in self.buttons.items()}
        widgets.update(
            {
                "maker_url": self.url_entry,
                "maker_name": self.name_entry,
                "maker_library": self.library,
                "maker_properties": self.property_canvas,
                "maker_variable_name": self.variable_name_entry,
                "maker_variable_value": self.variable_value,
                "maker_selector": self.selector_entry,
            }
        )
        widgets.update({"maker_prop_" + k: v[1] for k, v in self.props.items()})
        positions = {}
        for key, widget in widgets.items():
            if not widget.winfo_viewable():
                continue
            x = widget.winfo_rootx() + widget.winfo_width() // 2
            y = widget.winfo_rooty() + widget.winfo_height() // 2
            if (
                key.startswith("maker_prop_")
                and not self.property_canvas.winfo_rooty()
                <= y
                <= self.property_canvas.winfo_rooty() + self.property_canvas.winfo_height()
            ):
                continue
            positions[key] = [x, y]
        return {
            "positions": positions,
            "graph": self.document.graph.model_dump(mode="json"),
            "nodes": self.canvas.positions(),
            "selected": list(self.canvas.selected),
            "recording": self.recording,
            "busy": self.busy,
            "run_state": self.runner.state if self.runner else None,
            "trace": self.trace,
            "error": self.last_error,
            "status": self.status.get(),
            "saved_file": str(self.path) if self.path else None,
            "browser": self.browser_view,
            "runtime_values": self.runtime_values,
            "current_node": self.runner.current if self.runner else None,
            "pause_reason": self.runner.pause_reason if self.runner else None,
            "picked": self.last_pick,
            "selector_result": getattr(self, "selector_result", None),
            "demo": {"origin": self.demo_base, "posts": self.demo_server.posts.copy()}
            if self.demo_server
            else None,
        }

    def dispose(self):
        self.props.clear()
        self.buttons.clear()
        self.canvas.dispose()
        for key, value in list(vars(self).items()):
            if isinstance(value, (tk.Variable, tk.Misc)):
                setattr(self, key, None)
