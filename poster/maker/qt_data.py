"""Data previews and host-owned project settings added to the existing Qt tools."""

from pathlib import Path
from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from .model import GraphError
from .variables import NAME


class DataToolsMixin:
    def build_data_tools(self):
        result = QWidget()
        layout = QVBoxLayout(result)
        label = QLabel("Предпросмотр: до 200 строк и 50 столбцов; секреты скрыты")
        label.setWordWrap(True)
        layout.addWidget(label)
        self.result_json = QPlainTextEdit()
        self.result_json.setReadOnly(True)
        self.result_json.setObjectName("result_json")
        self.result_table = QTableWidget()
        self.result_table.setObjectName("result_table")
        self.result_table.setEditTriggers(self.result_table.EditTrigger.NoEditTriggers)
        layout.addWidget(self.result_json)
        layout.addWidget(self.result_table)
        self.tools_tabs.addTab(result, "Результат")
        project = QWidget()
        form = QFormLayout(project)
        self.execution_workdir = QLineEdit(str(self.workspace.root / "maker-data" / self.document.graph.id))
        self.execution_workdir.setObjectName("execution_workdir")
        self.execution_workdir.editingFinished.connect(self.remember_workdir)
        row = QHBoxLayout()
        row.addWidget(self.execution_workdir)
        row.addWidget(self.make_button("choose_workdir", "…", self.choose_workdir))
        form.addRow("Рабочий каталог", row)
        form.addRow(self.make_button("open_workdir", "Открыть каталог", self.open_workdir))
        self.project_outputs = QLineEdit()
        self.project_outputs.setObjectName("project_outputs")
        self.project_outputs.setPlaceholderText("result, count")
        self.project_outputs.editingFinished.connect(self.edit_outputs)
        form.addRow("Выходы подпроекта", self.project_outputs)
        explanation = QLabel(
            "Относительные пути файлов считаются от рабочего каталога. Project.Run — от файла графа. Выходы: имена переменных через запятую."
        )
        explanation.setWordWrap(True)
        form.addRow(explanation)
        self.tools_tabs.addTab(project, "Проект")

    def remember_workdir(self):
        if self.editable():
            self.workdirs[self.document.graph.id] = self.execution_workdir.text()

    def choose_workdir(self):
        if not self.editable():
            return
        path = QFileDialog.getExistingDirectory(self, "Рабочий каталог", self.execution_workdir.text())
        if path:
            self.execution_workdir.setText(path)
            self.remember_workdir()

    def open_workdir(self):
        path = Path(self.execution_workdir.text()).absolute()
        path.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def edit_outputs(self):
        if not self.editable():
            return
        names = [n.strip() for n in self.project_outputs.text().split(",") if n.strip()]
        if len(names) > 1000 or len(set(names)) != len(names) or any(not NAME.fullmatch(n) for n in names):
            self.show_error(GraphError("VARIABLE_NAME_INVALID"))
            return
        if names != self.document.graph.extensions.get("outputs", []):
            self.document.checkpoint()
            self.document.graph.extensions["outputs"] = names
            self.changed()

    def choose_property_file(self, widget, node, prop):
        root = (
            self.path.parent
            if node.type == "Project.Run" and self.path
            else Path(self.execution_workdir.text())
        )
        if node.type in {"Files.CreateDirectory", "Files.ListDirectory"}:
            chosen = QFileDialog.getExistingDirectory(self, "Каталог", str(root))
        elif (
            node.type.endswith(("Write", "WriteText", "AppendText", "Download")) or prop.name == "destination"
        ):
            chosen, _ = QFileDialog.getSaveFileName(self, "Файл результата", str(root), "Все файлы (*)")
        else:
            chosen, _ = QFileDialog.getOpenFileName(
                self,
                "Выбрать файл",
                str(root),
                "Граф Maker (*.json)" if node.type == "Project.Run" else "Все файлы (*)",
            )
        if chosen:
            try:
                relative = Path(chosen).resolve().relative_to(root.resolve())
            except ValueError:
                self.status_label.setText("Сначала выберите рабочий каталог, содержащий этот файл.")
                return
            widget.setText(relative.as_posix())

    def preview_value(self, value):
        if not hasattr(self, "result_json"):
            return
        from .qt_tools import display

        self.result_json.setPlainText(display(value)[:200000])
        data = value.get("value", value) if isinstance(value, dict) else value
        if (
            isinstance(data, dict)
            and isinstance(data.get("columns"), list)
            and isinstance(data.get("rows"), list)
        ):
            columns = data["columns"][:50]
            rows = [r[:50] for r in data["rows"][:200]]
        elif isinstance(data, list):
            columns = ["Индекс", "Значение"]
            rows = [[i, v] for i, v in enumerate(data[:200])]
        else:
            columns = []
            rows = []
        self.result_table.setColumnCount(len(columns))
        self.result_table.setHorizontalHeaderLabels(columns)
        self.result_table.setRowCount(len(rows))
        for i, row in enumerate(rows):
            for j, v in enumerate(row):
                self.result_table.setItem(i, j, QTableWidgetItem(display(v)))
        self.result_table.resizeColumnsToContents()

    def data_examples(self):
        if not self.editable() or not self.discard():
            return
        from PySide6.QtWidgets import QInputDialog
        from .example_projects import EXAMPLES, copy_examples
        from .storage import load_graph
        from .fixtures import serve_maker

        label, ok = QInputDialog.getItem(
            self, "Примеры 3B.2", "Выберите настоящий сохранённый граф", [x[0] for x in EXAMPLES], 0, False
        )
        if not ok:
            return
        directory = copy_examples(self.workspace.root)
        filename = next(path for name, path in EXAMPLES if name == label)
        path = directory / filename
        graph, vault = load_graph(path)

        async def work():
            await self.controller.stop()
            if filename == EXAMPLES[0][1]:
                if not self.demo_context:
                    self.demo_context = serve_maker()
                    self.demo_server, self.demo_base = self.demo_context.__enter__()
                graph.variables["api_base"].value = self.demo_base
                graph.metadata["maker_demo"] = self.demo_base
            self.events.received.emit(("data_example", (graph, vault, path)))

        self.submit(work(), "data_example")
