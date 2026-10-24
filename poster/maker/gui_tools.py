"""Maker's variable/debugger/element-constructor widgets; execution stays in services."""

import json
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk
from urllib.parse import urlsplit

from .inspector import Inspector
from .model import GraphError, Variable, secret_refs
from .runtime import Context
from .storage import Vault


def parse_value(text):
    try:
        return json.loads(text)
    except ValueError:
        return text


def show_value(value):
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


class ToolsMixin:
    def active_runner(self):
        return self.runner if self.runner and self.runner.graph.id == self.document.graph.id else None

    def params_editable(self):
        runner = self.active_runner()
        return (
            not self.closed
            and not self.busy
            and not self.recording
            and not (runner and runner.state in {"READY", "RUNNING"})
        )

    def build_tools(self, notebook):
        self.runtime_values = {}
        self.tools_context = None
        self.inspector = None
        self.last_pick = None
        self.variable_name = tk.StringVar()
        self.variable_input = tk.BooleanVar()
        self.variable_required = tk.BooleanVar()
        self.variable_secret = tk.BooleanVar()
        self.output_name = tk.StringVar()
        self.output_path = tk.StringVar()
        self.output_secret = tk.BooleanVar()
        variables = ttk.Frame(notebook, padding=3)
        notebook.add(variables, text="Переменные")
        self.variables_page = variables
        self.variables_tree = ttk.Treeview(
            variables, columns=("default", "current"), height=6, selectmode="browse"
        )
        self.variables_tree.heading("#0", text="Имя")
        self.variables_tree.column("#0", width=80, stretch=True)
        for key, label in [("default", "Начальное"), ("current", "Сейчас")]:
            self.variables_tree.heading(key, text=label)
            self.variables_tree.column(key, width=75, stretch=True)
        self.variables_tree.pack(fill="both", expand=True)
        self.variables_tree.bind("<<TreeviewSelect>>", self.variable_selected)
        ttk.Label(variables, text="Имя переменной").pack(anchor="w")
        self.variable_name_entry = ttk.Entry(variables, textvariable=self.variable_name)
        self.variable_name_entry.pack(fill="x")
        ttk.Label(variables, text="Значение: текст или JSON; списки: [1,2]").pack(anchor="w")
        self.variable_value = tk.Text(variables, height=2, width=25)
        self.variable_value.pack(fill="x")
        for label, variable in [
            ("Входной параметр", self.variable_input),
            ("Обязательный вход", self.variable_required),
            ("Защищённое значение", self.variable_secret),
        ]:
            ttk.Checkbutton(variables, text=label, variable=variable).pack(anchor="w")
        row = ttk.Frame(variables)
        row.pack(fill="x")
        self.button(row, "var_save", "Задать", self.save_variable)
        self.button(row, "var_delete", "Удалить", self.delete_variable)
        self.button(variables, "var_live", "Задать в отладке", self.set_live_variable)
        for widget in (self.variable_name_entry, self.variable_value):
            self._enable_clipboard(widget)

        browser = ttk.Frame(notebook, padding=3)
        notebook.add(browser, text="Элемент")
        self.browser_page = browser
        self.browser_canvas = tk.Canvas(browser, width=260, height=350, highlightthickness=0)
        scrollbar = ttk.Scrollbar(browser, orient="vertical", command=self.browser_canvas.yview)
        self.browser_canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.browser_canvas.pack(fill="both", expand=True)
        browser = ttk.Frame(self.browser_canvas)
        self.browser_body = browser
        self.browser_canvas.create_window((0, 0), window=browser, anchor="nw", width=250)
        browser.bind(
            "<Configure>",
            lambda _: self.browser_canvas.configure(scrollregion=self.browser_canvas.bbox("all")),
        )
        row = ttk.Frame(browser)
        row.pack(fill="x")
        self.button(row, "browser_open", "Открыть браузер / URL", self.open_tool_browser)
        row = ttk.Frame(browser)
        row.pack(fill="x")
        self.button(row, "pick", "Выбрать на странице", self.pick_element)
        self.selector_mode = tk.StringVar(value="CSS")
        mode = ttk.Combobox(
            browser, textvariable=self.selector_mode, values=("CSS", "XPath"), state="readonly", width=12
        )
        mode.pack(anchor="w")
        mode.bind("<<ComboboxSelected>>", self.change_selector_mode)
        self.selector_text = tk.StringVar(value="body")
        self.selector_entry = ttk.Entry(browser, textvariable=self.selector_text, width=28)
        self.selector_entry.pack(fill="x", pady=3)
        ttk.Label(browser, text="Iframe: по одному селектору на строку").pack(anchor="w")
        self.selector_frames = tk.Text(browser, height=2, width=25)
        self.selector_frames.pack(fill="x")
        row = ttk.Frame(browser)
        row.pack(fill="x")
        self.button(row, "selector_check", "Проверить", self.check_selector)
        self.button(row, "selector_apply", "В кубик", self.selector_to_node)
        row = ttk.Frame(browser)
        row.pack(fill="x")
        self.button(row, "read_text", "Читать текст", lambda: self.add_extraction("Browser.ReadText"))
        self.button(row, "read_attribute", "Атрибут", lambda: self.add_extraction("Browser.ReadAttribute"))
        self.element_details = tk.Text(browser, width=25, height=5, state="disabled", wrap="word")
        self.element_details.pack(fill="both", expand=True, pady=3)
        row = ttk.Frame(browser)
        row.pack(fill="x")
        self.tab_index = tk.StringVar(value="0")
        ttk.Spinbox(row, from_=0, to=99, textvariable=self.tab_index, width=3).pack(side="left")
        self.button(row, "tab_switch", "Перейти", lambda: self.manage_tab("switch"))
        row = ttk.Frame(browser)
        row.pack(fill="x")
        self.button(row, "tab_new", "Новая вкладка", lambda: self.manage_tab("new"))
        self.button(row, "tab_close", "Закрыть вкладку", lambda: self.manage_tab("close"))
        for widget in (self.selector_entry, self.selector_frames, self.element_details):
            self._enable_clipboard(widget)

        def scroll(event):
            self.browser_canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")
            return "break"

        def bind_scroll(widget):
            widget.bind("<MouseWheel>", scroll)
            for child in widget.winfo_children():
                bind_scroll(child)

        bind_scroll(self.browser_canvas)

    def add_output_properties(self, node):
        ttk.Label(self.prop_frame, text="Результат → переменная (необязательно)").pack(anchor="w")
        self.output_name.set(node.save_as or "")
        self.output_path.set(node.result_path)
        self.output_secret.set(node.protect_result)
        for variable, label in [
            (self.output_name, None),
            (self.output_path, "Поле результата: value, index…; пусто = весь"),
        ]:
            if label:
                ttk.Label(self.prop_frame, text=label, wraplength=240).pack(anchor="w")
            entry = ttk.Entry(self.prop_frame, textvariable=variable)
            entry.pack(fill="x", pady=2)
            self._enable_clipboard(entry)
        ttk.Checkbutton(self.prop_frame, text="Защитить результат", variable=self.output_secret).pack(
            anchor="w"
        )

    def refresh_variables(self):
        if not hasattr(self, "variables_tree"):
            return
        selected = self.variables_tree.selection()
        self.variables_tree.delete(*self.variables_tree.get_children())
        for name in sorted(set(self.document.graph.variables) | set(self.runtime_values)):
            definition = self.document.graph.variables.get(name)
            default = definition.value if definition else None
            default = "••••" if secret_refs(default) else show_value(default)
            current = show_value(self.runtime_values.get(name, "—"))
            self.variables_tree.insert("", "end", iid=name, text=name, values=(default[:120], current[:120]))
        if selected and self.variables_tree.exists(selected[0]):
            self.variables_tree.selection_set(selected[0])

    def variable_selected(self, _=None):
        selected = self.variables_tree.selection()
        if not selected:
            return
        name = selected[0]
        definition = self.document.graph.variables.get(name, Variable())
        self.variable_name.set(name)
        protected = isinstance(definition.value, dict) and set(definition.value) == {"secret_ref"}
        text = "@secret:" + definition.value["secret_ref"] if protected else show_value(definition.value)
        self.variable_value.delete("1.0", "end")
        self.variable_value.insert("1.0", text)
        self.variable_input.set(definition.input)
        self.variable_required.set(definition.required)
        self.variable_secret.set(definition.secret or protected)

    def read_variable_value(self):
        text = self.variable_value.get("1.0", "end-1c")
        if self.variable_secret.get():
            if text == "null" or text == "":
                return None
            return {"secret_ref": text[8:]} if text.startswith("@secret:") else self.vault.put(text)
        if text.startswith("@secret:"):
            raise GraphError("PASSWORD_REF_REQUIRED")
        return parse_value(text)

    def save_variable(self):
        if not self.editable():
            return
        try:
            name = self.variable_name.get().strip()
            from .variables import NAME

            if not NAME.fullmatch(name):
                raise GraphError("VARIABLE_NAME_INVALID")
            definition = Variable(
                value=self.read_variable_value(),
                input=self.variable_input.get(),
                required=self.variable_required.get(),
                secret=self.variable_secret.get(),
            )
            self.document.checkpoint()
            self.document.graph.variables[name] = definition
            self.changed()
            self.refresh_variables()
        except Exception as error:
            self.show_error(error)

    def delete_variable(self):
        if self.editable() and self.variable_name.get() in self.document.graph.variables:
            self.document.checkpoint()
            del self.document.graph.variables[self.variable_name.get()]
            self.changed()
            self.refresh_variables()

    def set_live_variable(self):
        runner = self.active_runner()
        if not runner or runner.state != "PAUSED":
            return
        try:
            value = self.read_variable_value()
        except Exception as error:
            self.show_error(error)
            return
        name = self.variable_name.get()

        async def work():
            runner.set_variable(name, value)

        self.submit("variable", work)

    def prepare_inputs(self):
        values = {}
        for name, definition in self.document.graph.variables.items():
            if not definition.input:
                continue
            initial = "" if definition.secret or definition.value is None else show_value(definition.value)
            value = simpledialog.askstring(
                "Параметр проекта: " + name,
                "Введите значение"
                + (" (обязательное)" if definition.required else "")
                + (
                    ". Пусто — сохранить защищённое начальное значение."
                    if definition.secret
                    else ". Текст или JSON."
                ),
                initialvalue=initial,
                show="*" if definition.secret else "",
                parent=self.root,
            )
            if value is None:
                return None
            if definition.secret:
                value = self.vault.put(value) if value else definition.value
            else:
                value = parse_value(value)
            if definition.required and value is None:
                self.show_error(GraphError("INPUT_MISSING"))
                return None
            values[name] = value
        return values

    def toggle_breakpoint(self):
        if not self.params_editable() or len(self.canvas.selected) != 1:
            return
        node = self.document.graph.node(next(iter(self.canvas.selected)))
        updated = node.model_copy(deep=True, update={"breakpoint": not node.breakpoint})
        runner = self.active_runner()
        try:
            if runner:
                runner.update_node(updated)
            self.document.checkpoint()
            node.breakpoint = updated.breakpoint
            self.canvas.draw()
            self.status.set("Точка останова включена." if node.breakpoint else "Точка останова снята.")
        except Exception as error:
            self.show_error(error)

    def run_selected(self):
        runner = self.active_runner()
        if len(self.canvas.selected) != 1:
            return
        node_id = next(iter(self.canvas.selected))
        if not messagebox.askyesno(
            "Выполнить кубик",
            "Выполнить выбранное действие в текущей сессии? Оно может повторно отправить форму. Позиция графа сохранится.",
            parent=self.root,
        ):
            return
        inputs = None
        if not runner:
            if not self.prepare_secrets():
                return
            inputs = self.prepare_inputs()
            if inputs is None:
                return
        graph = self.document.graph.model_copy(deep=True)
        session = self.browser_session()
        messages, epoch = self.messages, self.epoch

        async def work():
            nonlocal runner
            if not runner:
                from .runtime import GraphRunner

                runner = GraphRunner(
                    graph,
                    vault=self.vault,
                    headless=False,
                    inputs=inputs,
                    on_event=lambda event: messages.put((epoch, "event", event)),
                )
                if session:
                    runner.context.session = session
                    runner.context.use_page(session.page)
                    if self.recorder and self.recorder.session is session:
                        self.recorder = None
                    if self.tools_context and self.tools_context.session is session:
                        self.tools_context = None
                runner.state, runner.pause_reason = "PAUSED", "selected"
                self.runner = runner
            else:
                runner.update_graph(graph)
            await runner.execute_selected(node_id)

        self.submit("selected", work)

    def browser_session(self):
        if self.recorder and self.recorder.session.page:
            return self.recorder.session
        runner = self.active_runner()
        if runner and runner.context.session.page:
            return runner.context.session
        return self.tools_context.session if self.tools_context else None

    async def get_inspector(self):
        session = self.browser_session()
        if not session:
            raise GraphError("TAB_MISSING")
        if not self.inspector or self.inspector.session is not session:
            messages, epoch = self.messages, self.epoch
            self.inspector = Inspector(session, on_pick=lambda data: messages.put((epoch, "picked", data)))
        return self.inspector

    def open_tool_browser(self):
        url = self.url.get().strip()
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            self.show_error(GraphError("URL_INVALID"))
            return

        async def work():
            await self.ensure_demo()
            session = self.browser_session()
            if not session:
                self.tools_context = Context(self.vault, headless=False)
                await self.tools_context.page()
                session = self.tools_context.session
            await session.page.goto(url, wait_until="domcontentloaded")
            await session.page.bring_to_front()

        self.submit("browser_open", work)

    def pick_element(self):
        async def work():
            await (await self.get_inspector()).enable()

        self.submit("pick", work)

    def change_selector_mode(self, _=None):
        if self.last_pick:
            self.selector_text.set(self.last_pick["xpath" if self.selector_mode.get() == "XPath" else "css"])

    def receive_pick(self, data):
        self.last_pick = data
        self.change_selector_mode()
        self.selector_frames.delete("1.0", "end")
        self.selector_frames.insert("1.0", "\n".join(data["frame"]))
        self.element_details.configure(state="normal")
        self.element_details.delete("1.0", "end")
        self.element_details.insert(
            "end",
            json.dumps({"tag": data["tag"], "attributes": data["attributes"]}, ensure_ascii=False, indent=2),
        )
        self.element_details.configure(state="disabled")
        self.status.set("Элемент выбран. Проверьте селектор или добавьте чтение текста/атрибута.")

    def selector_target(self):
        return {
            "selector": self.selector_text.get(),
            "frame": [s for s in self.selector_frames.get("1.0", "end-1c").splitlines() if s],
        }

    def check_selector(self):
        target = self.selector_target()

        async def work():
            result = await (await self.get_inspector()).check(target["selector"], target["frame"])
            self.messages.put((self.epoch, "selector_result", result))

        self.submit("selector_check", work)

    def selector_to_node(self):
        if not self.params_editable() or len(self.canvas.selected) != 1:
            return
        node = self.document.graph.node(next(iter(self.canvas.selected)))
        if "selector" not in self.registry.get(node.type).params.model_fields:
            return
        target = self.selector_target()
        for key, value in target.items():
            widget = self.props[key][1]
            if isinstance(widget, tk.Text):
                widget.delete("1.0", "end")
                widget.insert("1.0", "\n".join(value))
            else:
                widget.delete(0, "end")
                widget.insert(0, value)
        self.apply()

    def add_extraction(self, action):
        if not self.editable():
            return
        name = simpledialog.askstring(
            "Результат в переменную", "Имя переменной", initialvalue="page_value", parent=self.root
        )
        if not name:
            return
        params = self.selector_target()
        if action == "Browser.ReadAttribute":
            attribute = simpledialog.askstring(
                "Атрибут", "Имя HTML-атрибута", initialvalue="href", parent=self.root
            )
            if not attribute:
                return
            params["attribute"] = attribute
        try:
            from .variables import NAME

            if not NAME.fullmatch(name):
                raise GraphError("VARIABLE_NAME_INVALID")
            node = self.document.add(action, params)
            node.save_as, node.result_path = name, "value"
            self.canvas.selected = {node.id}
            self.canvas.draw()
            self.select(node.id)
            self.tools_notebook.select(self.properties_page)
            self.changed()
        except Exception as error:
            self.show_error(error)

    def manage_tab(self, operation):
        index = self.tab_index.get()

        async def work():
            session = self.browser_session()
            if not session:
                raise GraphError("TAB_MISSING")
            pages = session.context.pages
            if operation == "new":
                page = await session.context.new_page()
            elif operation == "switch":
                number = int(index)
                if not 0 <= number < len(pages):
                    raise GraphError("TAB_MISSING")
                page = pages[number]
            else:
                await session.page.close()
                page = (
                    session.context.pages[-1] if session.context.pages else await session.context.new_page()
                )
            runner = self.active_runner()
            if runner and runner.context.session is session:
                runner.context.use_page(page)
            else:
                session.page = page
            await page.bring_to_front()

        self.submit("tab", work)

    def example_project(self):
        if not self.editable() or not self.confirm_discard():
            return
        if not self.demo_base:
            self.status.set("Нажмите «Демо-сайт», затем «Пример с циклом».")
            return
        from .samples import loop_graph

        self.path = None
        self.replace(loop_graph(self.demo_base), Vault(), dirty=True)
        self.runtime_values = {}
        self.trace = []
        self.refresh_variables()
        self.canvas.fit()
        self.status.set(
            "Пример: For Each, строки, счётчик, Compare, Break/Continue и Switch. Вход prefix задаётся при запуске."
        )
