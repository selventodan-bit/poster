"""Native property, variable, element and profile panels over existing models."""

import json
import os

from PySide6.QtWidgets import QCheckBox, QComboBox, QFormLayout, QInputDialog, QLabel, QLineEdit, QHBoxLayout
from PySide6.QtWidgets import (
    QMessageBox,
    QPlainTextEdit,
    QScrollArea,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
)
from PySide6.QtWidgets import QVBoxLayout, QWidget
from PySide6.QtWidgets import QToolButton

from .model import GraphError, Node, Variable, secret_refs
from .variables import draft_parameters
from .qt_data import DataToolsMixin


def parse(text):
    try:
        return json.loads(text)
    except ValueError:
        return text


def display(value):
    if isinstance(value, dict) and set(value) == {"secret_ref"}:
        return "••••"
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


class ToolsMixin(DataToolsMixin):
    def build_tools(self):
        self.tools_tabs = QTabWidget()
        self.tools_tabs.setObjectName("tools_tabs")
        self.property_area = QScrollArea()
        self.property_area.setObjectName("property_scroll")
        self.property_area.verticalScrollBar().setObjectName("property_scrollbar")
        self.property_area.setWidgetResizable(True)
        self.tools_tabs.addTab(self.property_area, "Кубик")
        self.property_widgets = {}
        self.show_properties(None)
        variables = QWidget()
        form = QVBoxLayout(variables)
        self.variables_table = QTableWidget(0, 3)
        self.variables_table.setObjectName("variables_table")
        self.variables_table.setHorizontalHeaderLabels(["Имя", "Начальное", "Сейчас"])
        self.variables_table.setSelectionBehavior(self.variables_table.SelectionBehavior.SelectRows)
        self.variables_table.setEditTriggers(self.variables_table.EditTrigger.NoEditTriggers)
        self.variables_table.itemSelectionChanged.connect(self.select_variable)
        form.addWidget(self.variables_table)
        self.variable_name = QLineEdit()
        self.variable_name.setObjectName("variable_name")
        self.variable_value = QPlainTextEdit()
        self.variable_value.setObjectName("variable_value")
        self.variable_value.setMaximumHeight(90)
        form.addWidget(QLabel("Имя и значение (текст или JSON)"))
        form.addWidget(self.variable_name)
        form.addWidget(self.variable_value)
        self.variable_input = QCheckBox("Входной параметр")
        self.variable_required = QCheckBox("Обязательный")
        self.variable_secret = QCheckBox("Защищённое значение")
        for widget in (self.variable_input, self.variable_required, self.variable_secret):
            form.addWidget(widget)
        self.row_buttons(
            form,
            [("var_save", "Задать", self.save_variable), ("var_delete", "Удалить", self.delete_variable)],
        )
        self.row_buttons(form, [("var_live", "Изменить в отладке", self.live_variable)])
        self.tools_tabs.addTab(variables, "Переменные")

        element_area = QScrollArea()
        element_area.setObjectName("element_scroll")
        element_area.verticalScrollBar().setObjectName("element_scrollbar")
        element_area.setWidgetResizable(True)
        element = QWidget()
        element_area.setWidget(element)
        form = QVBoxLayout(element)
        self.row_buttons(
            form,
            [("pick", "Выбрать элемент", self.pick), ("selector_check", "Проверить", self.check_selector)],
        )
        self.selector_mode = QComboBox()
        self.selector_mode.setObjectName("selector_mode")
        self.selector_mode.addItems(["CSS", "XPath"])
        self.selector_mode.currentTextChanged.connect(self.switch_selector)
        form.addWidget(self.selector_mode)
        self.selector = QLineEdit("body")
        self.selector.setObjectName("selector")
        form.addWidget(self.selector)
        form.addWidget(QLabel("Iframe: по одному CSS-селектору на строку"))
        self.frames = QPlainTextEdit()
        self.frames.setMaximumHeight(65)
        self.frames.setObjectName("selector_frames")
        form.addWidget(self.frames)
        self.row_buttons(form, [("selector_apply", "В выбранный кубик", self.apply_selector)])
        self.element_info = QPlainTextEdit("Выберите элемент в браузере")
        self.element_info.setObjectName("element_attributes")
        self.element_info.setReadOnly(True)
        self.element_info.setMinimumHeight(65)
        self.element_info.setMaximumHeight(100)
        form.addWidget(self.element_info)
        self.row_buttons(
            form,
            [
                ("element_click", "Клик", lambda: self.element_action("Browser.Click")),
                ("element_input", "Ввод", lambda: self.element_action("Browser.Input")),
                ("element_wait", "Ждать", lambda: self.element_action("Browser.WaitElement")),
            ],
        )
        self.row_buttons(
            form,
            [
                ("read_text", "Читать текст", lambda: self.element_action("Browser.ReadText")),
                ("read_attribute", "Атрибут", lambda: self.element_action("Browser.ReadAttribute")),
            ],
        )
        form.addStretch()
        self.tools_tabs.addTab(element_area, "Элемент")

        profiles = QWidget()
        form = QVBoxLayout(profiles)
        self.profile_combo = QComboBox()
        self.profile_combo.setObjectName("profile_combo")
        self.profile_combo.currentIndexChanged.connect(self.profile_changed)
        form.addWidget(self.profile_combo)
        self.profile_status = QLabel("Изолированная сессия; сохранение только явно")
        self.profile_status.setWordWrap(True)
        form.addWidget(self.profile_status)
        self.row_buttons(
            form,
            [
                ("profile_create", "Создать", self.create_profile),
                ("profile_rename", "Переименовать", self.rename_profile),
            ],
        )
        self.row_buttons(form, [("profile_save", "Сохранить сессию", self.save_session)])
        self.row_buttons(
            form,
            [
                ("profile_clear", "Очистить", self.clear_profile),
                ("profile_delete", "Удалить", self.delete_profile),
            ],
        )
        label = QLabel(
            "Cookies и localStorage шифруются локально. IndexedDB и sessionStorage пока не включаются в снимок. Ключи не экспортируются в проекты/Drive."
        )
        label.setWordWrap(True)
        form.addWidget(label)
        form.addStretch()
        self.tools_tabs.addTab(profiles, "Профили")
        self.refresh_profiles()
        self.build_data_tools()
        for button in self.tools_tabs.tabBar().findChildren(QToolButton):
            # Qt applies arrowType only after polish/show (initially NoArrow).
            button.setObjectName(
                "tools_scroll_left" if button.objectName() == "ScrollLeftButton" else "tools_scroll_right"
            )
            if os.environ.get("POSTER_GUI_TEST_OUTPUT"):
                button.pressed.connect(
                    lambda b=button: b.setProperty("poster_presses", (b.property("poster_presses") or 0) + 1)
                )
        return self.tools_tabs

    def show_properties(self, node_id):
        self.controls.pop("apply", None)
        body = QWidget()
        form = QFormLayout(body)
        form.setFieldGrowthPolicy(form.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.property_widgets = {}
        self.secret_fields = {}
        self.selected_id = node_id
        if node_id:
            node = self.document.graph.node(node_id)
            spec = self.registry.get(node.type, node.action_version)
            previous = next((r for r in reversed(self.trace) if r["node_id"] == node.id), None)
            self.preview_result(previous) if previous else self.preview_value({})
            form.addRow(QLabel(node.type))
            self.node_title = QLineEdit(node.title)
            self.node_title.setObjectName("node_title")
            form.addRow("Название", self.node_title)
            for prop in spec.properties:
                value = node.params.get(prop.name)
                if value is None:
                    field = spec.params.model_fields[prop.name]
                    value = field.get_default(call_default_factory=True)
                    if type(value).__name__ == "PydanticUndefinedType":
                        value = ""
                secret = bool(secret_refs(value)) and prop.secret
                if prop.kind in {"bool", "boolean"}:
                    widget = QCheckBox()
                    widget.setChecked(bool(value))
                elif prop.name == "column":
                    widget = QComboBox()
                    widget.setEditable(True)
                    table = node.params.get("table", {})
                    if isinstance(table, str) and table.startswith("{{") and table.endswith("}}"):
                        table = self.live_values.get(table[2:-2].strip(), {})
                    if isinstance(table, dict):
                        widget.addItems(table.get("columns", []))
                    widget.setCurrentText(str(value))
                elif prop.choices:
                    widget = QComboBox()
                    for choice in prop.choices:
                        widget.addItem("Tab (\\t)" if choice == "\t" else str(choice), choice)
                    widget.setCurrentIndex(max(0, widget.findData(value)))
                elif prop.kind in {"lines", "json", "multiline", "value"}:
                    widget = QPlainTextEdit(
                        ""
                        if secret
                        else "\n".join(value)
                        if prop.kind == "lines" and isinstance(value, list)
                        else ""
                        if secret
                        else display(value)
                    )
                    widget.setMaximumHeight(90)
                else:
                    widget = QLineEdit("" if secret else display(value))
                    if secret:
                        widget.setEchoMode(QLineEdit.EchoMode.Password)
                        widget.setPlaceholderText("Защищено; пустое поле сохраняет ссылку")
                widget.setObjectName("prop_" + prop.name)
                widget.setToolTip(prop.help or prop.label)
                self.property_widgets[prop.name] = (prop, widget)
                if prop.secret:
                    protected = QCheckBox("Защитить")
                    protected.setChecked(secret)
                    protected.setObjectName("protect_" + prop.name)
                    self.secret_fields[prop.name] = protected
                    if isinstance(widget, QLineEdit):
                        protected.toggled.connect(
                            lambda checked, w=widget: w.setEchoMode(
                                QLineEdit.EchoMode.Password if checked else QLineEdit.EchoMode.Normal
                            )
                        )
                    row = QHBoxLayout()
                    row.addWidget(widget)
                    row.addWidget(protected)
                    form.addRow(prop.label, row)
                elif prop.kind == "file":
                    row = QHBoxLayout()
                    row.addWidget(widget)
                    row.addWidget(
                        self.make_button(
                            "browse_" + prop.name,
                            "…",
                            lambda w=widget, n=node, p=prop: self.choose_property_file(w, n, p),
                        )
                    )
                    form.addRow(prop.label, row)
                else:
                    form.addRow(prop.label, widget)
            self.output_name = QLineEdit(node.save_as or "")
            self.output_name.setObjectName("output_name")
            self.output_path = QLineEdit(node.result_path)
            self.output_secret = QCheckBox("Защитить результат")
            self.output_secret.setChecked(node.protect_result)
            form.addRow("Результат в переменную", self.output_name)
            form.addRow("Поле результата", self.output_path)
            form.addRow(self.output_secret)
            button = self.make_button("apply", "Применить", self.apply_properties)
            form.addRow(button)
        else:
            form.addRow(QLabel("Выберите кубик на холсте"))
        self.property_area.setWidget(body)

    def apply_properties(self):
        if not self.params_editable() or not self.selected_id:
            return False
        node = self.document.graph.node(self.selected_id)
        values = dict(node.params)
        for name, (prop, widget) in self.property_widgets.items():
            if isinstance(widget, QCheckBox):
                value = widget.isChecked()
            elif isinstance(widget, QComboBox):
                value = (
                    widget.currentData()
                    if not widget.isEditable() and widget.currentData() is not None
                    else widget.currentText()
                )
            else:
                value = widget.toPlainText() if isinstance(widget, QPlainTextEdit) else widget.text()
                previous = node.params.get(name)
                if prop.secret and secret_refs(previous) and not value:
                    continue
                if prop.kind == "lines":
                    value = [x for x in value.splitlines() if x]
                elif prop.kind == "json":
                    value = json.loads(value)
                elif prop.kind == "value":
                    value = parse(value)
                elif prop.kind == "integer" and "{{" not in value:
                    value = int(value)
                if self.secret_fields.get(name) and self.secret_fields[name].isChecked():
                    value = self.vault.put_value(value)
            values[name] = value
        if "sensitive" in self.registry.get(node.type).params.model_fields:
            values["sensitive"] = (
                self.secret_fields.get("value").isChecked() if self.secret_fields.get("value") else False
            )
        draft_parameters(self.registry.get(node.type), values)
        updated = Node.model_validate(
            {
                **node.model_dump(),
                "params": values,
                "title": self.node_title.text(),
                "save_as": self.output_name.text() or None,
                "result_path": self.output_path.text(),
                "protect_result": self.output_secret.isChecked(),
            }
        )
        self.document.checkpoint()
        self.document.graph.nodes = [updated if n.id == node.id else n for n in self.document.graph.nodes]
        self.canvas.redraw()
        self.canvas.choose([node.id])
        self.changed()
        return True

    def refresh_variables(self):
        self.variables_table.blockSignals(True)
        definitions = self.document.graph.variables
        names = list(dict.fromkeys([*definitions, *self.live_values]))
        self.variables_table.setRowCount(len(names))
        for row, name in enumerate(names):
            definition = definitions.get(name)
            for column, value in enumerate(
                (
                    name,
                    display(definition.value) if definition else "",
                    display(self.live_values.get(name, "")),
                )
            ):
                self.variables_table.setItem(row, column, QTableWidgetItem(value))
        self.variables_table.blockSignals(False)

    def select_variable(self):
        row = self.variables_table.currentRow()
        if row < 0:
            return
        name = self.variables_table.item(row, 0).text()
        definition = self.document.graph.variables.get(name)
        value = self.live_values.get(name, definition.value if definition else None)
        protected = bool(definition and definition.secret) or value == "••••"
        self.variable_name.setText(name)
        self.variable_value.setPlainText("" if protected else display(value))
        self.variable_secret.setChecked(protected)
        self.variable_input.setChecked(bool(definition and definition.input))
        self.variable_required.setChecked(bool(definition and definition.required))
        self.preview_value(value)

    def save_variable(self):
        if not self.editable():
            return
        name = self.variable_name.text()
        from .variables import NAME

        if not NAME.fullmatch(name):
            raise GraphError("VARIABLE_NAME_INVALID")
        value = parse(self.variable_value.toPlainText())
        if self.variable_secret.isChecked():
            prior = self.document.graph.variables.get(name)
            value = (
                prior.value
                if prior and prior.secret and not self.variable_value.toPlainText()
                else self.vault.put_value(value)
            )
        definition = Variable(
            value=value,
            input=self.variable_input.isChecked(),
            required=self.variable_required.isChecked(),
            secret=self.variable_secret.isChecked(),
        )
        self.document.checkpoint()
        self.document.graph.variables[name] = definition
        self.refresh_variables()
        self.changed()

    def delete_variable(self):
        if self.editable() and self.variable_name.text() in self.document.graph.variables:
            self.document.checkpoint()
            self.document.graph.variables.pop(self.variable_name.text())
            self.refresh_variables()
            self.changed()

    def live_variable(self):
        if self.run_state != "PAUSED":
            raise GraphError("DEBUG_STATE_INVALID")
        name, value = self.variable_name.text(), parse(self.variable_value.toPlainText())

        async def work():
            self.controller.set_variable(name, value)

        self.submit(work(), "variable")

    def pick(self):
        self.submit(self.controller.pick(), "pick")

    def switch_selector(self):
        if self.last_pick:
            self.selector.setText(
                self.last_pick["xpath" if self.selector_mode.currentText() == "XPath" else "css"]
            )

    def selector_params(self):
        return {
            "selector": self.selector.text(),
            "frame": [x for x in self.frames.toPlainText().splitlines() if x],
        }

    def check_selector(self):
        params = self.selector_params()
        self.submit(self.controller.check(params["selector"], params["frame"]), "check")

    def apply_selector(self):
        if not self.selected_id or not self.params_editable():
            return
        node = self.document.graph.node(self.selected_id)
        if "selector" not in self.registry.get(node.type).params.model_fields:
            raise GraphError("PARAMETERS_INVALID")
        self.document.checkpoint()
        node.params.update(self.selector_params())
        if self.last_pick and self.last_pick.get("tab_id"):
            node.extensions["browser_tab"] = self.last_pick["tab_id"]
        self.show_properties(node.id)
        self.canvas.redraw()
        self.canvas.choose([node.id])
        self.changed()

    def element_action(self, action):
        if not self.editable():
            return
        params = self.selector_params()
        if action == "Browser.Input":
            params["value"] = ""
        if action == "Browser.ReadAttribute":
            name, ok = QInputDialog.getText(self, "Атрибут", "HTML-атрибут", text="href")
            if not ok:
                return
            params["attribute"] = name
        node = self.add_action(action, params)
        if self.last_pick and self.last_pick.get("tab_id"):
            node.extensions["browser_tab"] = self.last_pick["tab_id"]
        if action in {"Browser.ReadText", "Browser.ReadAttribute"}:
            name, ok = QInputDialog.getText(self, "Переменная", "Имя результата", text="page_value")
            if ok:
                node.save_as, node.result_path = name, "value"
        self.show_properties(node.id)

    def refresh_profiles(self):
        self.profile_combo.blockSignals(True)
        self.profile_combo.clear()
        self.profile_combo.addItem("Новая изолированная сессия", None)
        for key, name in self.profiles.list():
            self.profile_combo.addItem(name + " · " + key[-6:], key)
        index = self.profile_combo.findData(self.document.graph.metadata.get("browser_profile"))
        self.profile_combo.setCurrentIndex(max(0, index))
        self.profile_combo.blockSignals(False)

    def profile_closed(self):
        if self.busy or self.recording or self.controller.session():
            self.profile_status.setText("Сначала нажмите «Остановить», чтобы закрыть текущую сессию.")
            return False
        return True

    def profile_changed(self):
        if not self.profile_closed():
            self.refresh_profiles()
            return
        key = self.profile_combo.currentData()
        self.document.checkpoint()
        if key:
            self.document.graph.metadata["browser_profile"] = key
        else:
            self.document.graph.metadata.pop("browser_profile", None)
        self.changed()

    def create_profile(self):
        if self.profile_closed():
            name, ok = QInputDialog.getText(self, "Новый профиль", "Название")
            if ok:
                self.document.checkpoint()
                self.document.graph.metadata["browser_profile"] = self.profiles.create(name)
                self.refresh_profiles()
                self.changed()

    def rename_profile(self):
        key = self.profile_combo.currentData()
        if key and self.profile_closed():
            name, ok = QInputDialog.getText(self, "Переименовать профиль", "Название")
            if ok:
                self.profiles.rename(key, name)
                self.refresh_profiles()

    def clear_profile(self):
        key = self.profile_combo.currentData()
        if (
            key
            and self.profile_closed()
            and QMessageBox.question(self, "Очистка", "Очистить только выбранный профиль?")
            == QMessageBox.StandardButton.Yes
        ):
            self.profiles.clear(key)
            self.profile_status.setText("Выбранный профиль очищен")

    def delete_profile(self):
        key = self.profile_combo.currentData()
        if (
            key
            and self.profile_closed()
            and QMessageBox.question(self, "Удаление профиля", "Удалить только выбранный профиль?")
            == QMessageBox.StandardButton.Yes
        ):
            self.profiles.delete(key)
            self.document.graph.metadata.pop("browser_profile", None)
            self.refresh_profiles()
            self.changed()

    def save_session(self):
        session = self.controller.session()
        if not session or not session.profile:
            raise GraphError("PROFILE_MISSING")

        async def work():
            await session.profile.save(session)
            self.events.received.emit(("profile_saved", None))

        self.submit(work(), "profile_save")
