"""Native Dispatcher client. No queue/worker/SQLite work runs on the Qt thread."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
import os
from pathlib import Path
import sys
import uuid

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QObject, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QDesktopServices
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import (
    QApplication,
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QTabWidget,
    QTableView,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .catalog import RESOURCE_NOTICE
from .host import ensure_service, request
from .models import DispatcherError, MESSAGES
from .workspace import workspace_path

LABELS = {
    "PENDING": "Ожидает",
    "LEASED": "Запускается",
    "RUNNING": "Выполняется",
    "SUCCEEDED": "Успешно",
    "FAILED": "Ошибка",
    "CANCELLED": "Отменено",
    "TIMED_OUT": "Истекло время",
    "UNKNOWN": "Результат неизвестен",
    "REVIEW_REQUIRED": "Нужна ручная проверка",
}


class TableModel(QAbstractTableModel):
    def __init__(self, columns, parent=None):
        super().__init__(parent)
        self.columns, self.rows = columns, []

    def rowCount(self, parent=None):
        return 0 if parent is not None and parent.isValid() else len(self.rows)

    def columnCount(self, parent=None):
        return len(self.columns)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        value = self.rows[index.row()].get(self.columns[index.column()][0], "")
        if role == Qt.ItemDataRole.UserRole:
            return value
        if role == Qt.ItemDataRole.DisplayRole:
            key = self.columns[index.column()][0]
            if key in {"created", "updated", "last_run"} and value:
                return datetime.fromtimestamp(value).strftime("%d.%m %H:%M:%S")
            if key == "status":
                return LABELS.get(value, value)
            if key in {"version", "id"}:
                return str(value)[:12]
            if key == "browser":
                return "Chrome" if value else "Без браузера"
            return "" if value is None else str(value)
        if role == Qt.ItemDataRole.ToolTipRole:
            return str(value)
        return None

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self.columns[section][1]
        return None

    def replace(self, rows):
        if self.rows == rows:
            return
        self.beginResetModel()
        self.rows = rows
        self.endResetModel()


class Replies(QObject):
    ready = Signal(object, object, object)


class InputsDialog(QDialog):
    def __init__(self, item, parent):
        super().__init__(parent)
        self.setObjectName("dispatcher_inputs")
        self.setWindowTitle("Входы — " + item["name"])
        self.resize(560, 400)
        self.fields = []
        layout = QVBoxLayout(self)
        note = QLabel("Типы JSON сохраняются. Защищённые входы шифруются; значения не попадают в журнал.")
        note.setWordWrap(True)
        layout.addWidget(note)
        content = QWidget()
        form = QFormLayout(content)
        self.timeout = QSpinBox()
        self.timeout.setObjectName("job_timeout")
        self.timeout.setRange(1, 86400)
        self.timeout.setValue(parent.timeout.value())
        form.addRow("Время этого задания, с", self.timeout)
        self.profile_path, self.profile_key = QLineEdit(), QLineEdit()
        self.profile_path.setObjectName("job_profile")
        self.profile_key.setObjectName("job_profile_key")
        self.profile_path.setPlaceholderText("Необязательно: profile_….enc; иначе новая сессия")
        self.profile_key.setPlaceholderText("session.key Maker для этого профиля")
        if item.get("browser"):
            for title, edit, pattern in [
                ("Профиль Chrome", self.profile_path, "Профиль (profile_*.enc)"),
                ("Ключ профиля", self.profile_key, "Ключ (session.key *.key)"),
            ]:
                row = QWidget()
                row_layout = QHBoxLayout(row)
                row_layout.addWidget(edit)
                browse = QPushButton("Выбрать…")
                browse.clicked.connect(
                    lambda checked=False, edit=edit, title=title, pattern=pattern: edit.setText(
                        QFileDialog.getOpenFileName(self, title, "", pattern)[0]
                    )
                )
                row_layout.addWidget(browse)
                form.addRow(title, row)
        for definition in item["inputs"]:
            row = QWidget()
            line = QHBoxLayout(row)
            enabled = QCheckBox("Передать")
            enabled.setChecked(definition["required"])
            enabled.setObjectName("input_enabled_" + definition["name"])
            edit = QLineEdit()
            edit.setObjectName("input_" + definition["name"])
            if definition["secret"]:
                edit.setEchoMode(QLineEdit.EchoMode.Password)
            kind = QComboBox()
            kind.addItems(["Текст", "Число", "Да/нет", "JSON"])
            kind.setObjectName("input_type_" + definition["name"])
            boolean = QComboBox()
            boolean.addItems(["Нет (false)", "Да (true)"])
            boolean.setObjectName("input_bool_" + definition["name"])
            boolean.hide()
            kind.currentIndexChanged.connect(
                lambda index, edit=edit, boolean=boolean: (
                    edit.setVisible(index != 2),
                    boolean.setVisible(index == 2),
                )
            )
            if definition["type"] in {"int", "float"}:
                kind.setCurrentIndex(1)
            elif definition["type"] == "bool":
                kind.setCurrentIndex(2)
            elif definition["type"] in {"dict", "list"}:
                kind.setCurrentIndex(3)
            line.addWidget(enabled)
            line.addWidget(edit, 1)
            line.addWidget(boolean, 1)
            line.addWidget(kind)
            form.addRow(definition["name"] + (" *" if definition["required"] else ""), row)
            self.fields.append((definition, enabled, edit, kind, boolean))
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(content)
        layout.addWidget(scroll)
        self.error = QLabel()
        self.error.setWordWrap(True)
        layout.addWidget(self.error)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.setObjectName("inputs_buttons")
        buttons.button(QDialogButtonBox.StandardButton.Ok).setObjectName("confirm_enqueue")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setObjectName("cancel_enqueue")
        buttons.accepted.connect(self.validate)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def validate(self):
        try:
            self.values()
        except (ValueError, TypeError):
            self.error.setText(
                "Некорректный JSON. Строки JSON заключайте в кавычки; логические значения: true/false."
            )
            return
        self.accept()

    def values(self):
        inputs, secrets = {}, {}
        for definition, enabled, edit, kind, boolean in self.fields:
            if enabled.isChecked():
                raw = edit.text()
                if kind.currentIndex() == 2:
                    value = bool(boolean.currentIndex())
                elif kind.currentIndex():
                    value = json.loads(raw)
                    if kind.currentIndex() == 1 and (type(value) not in {int, float}):
                        raise ValueError()
                else:
                    value = raw
                json.dumps(value, allow_nan=False)
                (secrets if definition["secret"] else inputs)[definition["name"]] = value
            elif definition["required"]:
                raise ValueError()
        return inputs, secrets


class DispatcherWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setObjectName("dispatcher_window")
        self.setWindowTitle("Poster Dispatcher — очередь проектов")
        screen = QApplication.primaryScreen().availableGeometry()
        self.resize(min(1220, screen.width() - 30), min(780, screen.height() - 50))
        self.setMinimumSize(min(900, screen.width() - 30), min(620, screen.height() - 50))
        self.root = workspace_path()
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="dispatcher-client")
        self.replies = Replies(self)
        self.replies.ready.connect(self.completed)
        self.busy, self.connected, self.service, self.offset = False, False, {}, 0
        self.last_error, self.last_action, self.selected_report = None, "startup", None
        self.project_sort, self.project_descending = "name", False
        self.job_order, self.job_descending = "created", True
        self.closed = False
        self.pending = None
        self.buttons = {}
        self.build()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(700)
        self.submit("connect", lambda: ensure_service(self.root))

    def button(self, name, title, callback, layout):
        button = QPushButton(title)
        button.setObjectName(name)
        button.clicked.connect(callback)
        layout.addWidget(button)
        self.buttons[name] = button
        return button

    def build(self):
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        self.banner = QLabel("Подключение к самостоятельной службе…")
        self.banner.setObjectName("service_status")
        self.banner.setWordWrap(True)
        layout.addWidget(self.banner)
        actions = QHBoxLayout()
        self.button("add_project", "Добавить проект…", self.add, actions)
        self.button("enqueue_projects", "В очередь", self.enqueue, actions)
        self.button("remove_projects", "Убрать из списка", self.remove, actions)
        self.button("inspect_project", "Сведения о проекте", self.inspect, actions)
        actions.addStretch()
        self.button("reconnect", "Переподключиться", self.reconnect, actions)
        self.button("refresh", "Обновить", self.refresh, actions)
        layout.addLayout(actions)
        filters = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setObjectName("dispatcher_search")
        self.search.setPlaceholderText("Поиск по названию проекта или ID задания")
        self.search.textChanged.connect(self.filters_changed)
        filters.addWidget(self.search, 1)
        self.status_filter = QComboBox()
        self.status_filter.setObjectName("status_filter")
        self.status_filter.addItem("Все статусы", "")
        for key, label in LABELS.items():
            self.status_filter.addItem(label, key)
        self.status_filter.currentIndexChanged.connect(self.filters_changed)
        filters.addWidget(self.status_filter)
        layout.addLayout(filters)
        self.projects_model = TableModel(
            [
                ("name", "Проект"),
                ("project_id", "ID проекта"),
                ("version", "Версия"),
                ("status", "Последний результат"),
                ("executions", "Запуски"),
                ("errors", "Ошибки / неизвестно"),
                ("active", "Процессы"),
                ("last_run", "Последний запуск"),
                ("elapsed", "Всего секунд"),
                ("nodes", "Кубики"),
                ("browser", "Исполнение"),
                ("source", "Исходный файл"),
            ],
            self,
        )
        self.jobs_model = TableModel(
            [
                ("name", "Проект"),
                ("status", "Состояние"),
                ("id", "Задание"),
                ("attempt", "Попытка"),
                ("pid", "Worker PID"),
                ("created", "Создано"),
                ("updated", "Обновлено"),
                ("timeout", "Лимит, с"),
                ("duration", "Секунд"),
                ("code", "Диагностика"),
            ],
            self,
        )
        self.tabs = QTabWidget()
        self.tabs.setObjectName("dispatcher_tabs")
        self.tabs.tabBar().setObjectName("dispatcher_tabbar")
        self.projects = self.table("projects_table", self.projects_model)
        self.jobs = self.table("jobs_table", self.jobs_model)
        self.tabs.addTab(self.projects, "Проекты")
        self.tabs.addTab(self.jobs, "Задания и история")
        self.projects.doubleClicked.connect(self.inspect)
        self.jobs.doubleClicked.connect(self.show_job)
        self.projects.horizontalHeader().sectionClicked.connect(self.sort_projects)
        self.jobs.horizontalHeader().sectionClicked.connect(self.sort_jobs)
        self.projects.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.projects.customContextMenuRequested.connect(lambda p: self.context(self.projects, p))
        self.jobs.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.jobs.customContextMenuRequested.connect(lambda p: self.context(self.jobs, p))
        split = QSplitter(Qt.Orientation.Vertical)
        split.addWidget(self.tabs)
        self.details = QTextEdit()
        self.details.setObjectName("dispatcher_details")
        self.details.setReadOnly(True)
        self.details.setPlainText(RESOURCE_NOTICE)
        split.addWidget(self.details)
        split.setSizes([340, 150])
        layout.addWidget(split, 1)
        pages = QHBoxLayout()
        self.button("previous_jobs", "← Предыдущие 200", lambda: self.page(-200), pages)
        self.page_label = QLabel()
        pages.addWidget(self.page_label)
        self.button("next_jobs", "Следующие 200 →", lambda: self.page(200), pages)
        self.button("job_details", "История / результат", self.show_job, pages)
        self.button("open_workdir", "Открыть рабочую папку", self.open_workdir, pages)
        layout.addLayout(pages)
        settings = QHBoxLayout()
        for name, title, maximum, default in [
            ("max_workers", "Всего workers", 32, 2),
            ("max_per_project", "На один проект", 32, 1),
            ("timeout", "Время задания, с", 86400, 300),
        ]:
            settings.addWidget(QLabel(title))
            spin = QSpinBox()
            spin.setObjectName(name)
            spin.setRange(1, maximum)
            spin.setValue(default)
            setattr(self, name, spin)
            settings.addWidget(spin)
        self.button("save_settings", "Применить", self.settings, settings)
        layout.addLayout(settings)
        run = QHBoxLayout()
        self.button("start_service", "Запустить обслуживание", lambda: self.call("start"), run)
        self.button("stop_service", "Остановить приём", lambda: self.call("stop"), run)
        self.button("cancel_jobs", "Отменить выбранные", self.cancel, run)
        self.button("reconcile_jobs", "Проверить после сбоя", lambda: self.call("reconcile"), run)
        self.button("shutdown_service", "Завершить службу", lambda: self.call("shutdown"), run)
        layout.addLayout(run)
        note = QLabel(
            "Закрытие окна оставляет службу и задания работать. «Остановить приём» завершает текущие задания. "
            "«Завершить службу» также ждёт их окончания. UNKNOWN никогда не повторяется автоматически."
        )
        note.setWordWrap(True)
        layout.addWidget(note)

    def table(self, name, model):
        view = QTableView()
        view.setObjectName(name)
        view.setModel(model)
        view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        view.horizontalHeader().setStretchLastSection(True)
        view.setColumnWidth(0, 230)
        view.setColumnWidth(1, 155)
        return view

    def selected(self, view):
        return [view.model().rows[i.row()] for i in view.selectionModel().selectedRows()]

    def submit(self, label, operation, context=None):
        if self.closed:
            return
        if self.busy:
            if label != "dashboard":
                self.pending = (label, operation, context)
            return
        self.busy, self.last_action = True, label
        if label != "dashboard":
            self.update_buttons()

        def perform():
            try:
                result, error = operation(), None
            except Exception as exc:
                result, error = None, exc.code if isinstance(exc, DispatcherError) else "SERVICE_FAILED"
            if not self.closed:
                self.replies.ready.emit(label, (result, error), context)

        self.pool.submit(perform)

    def call(self, op, **args):
        self.submit(op, lambda: request(self.root, op, **args))

    def completed(self, label, reply, context):
        self.busy = False
        value, error = reply
        if error:
            self.last_error = error
            self.banner.setText(MESSAGES[error])
            self.banner.setStyleSheet("color: #b02828")
            if error in {"SERVICE_UNAVAILABLE", "SERVICE_VERSION"}:
                self.connected = False
            if label == "add" and error == "PROJECT_KEY_REQUIRED":
                key, _ = QFileDialog.getOpenFileName(
                    self, MESSAGES[error], "", "Ключ Maker (session.key *.key);;Все файлы (*)"
                )
                if key:
                    self.submit(
                        "add", lambda: request(self.root, "add", source=context, key_file=key), context
                    )
                    return
        else:
            if label != "dashboard":
                self.last_error = None
            if label == "connect":
                self.connected, self.service = True, value
                for key, number in value["settings"].items():
                    getattr(self, key).setValue(number)
            elif label == "dashboard":
                self.service = value["service"]
                projects = [
                    r for r in value["projects"] if self.search.text().casefold() in r["name"].casefold()
                ]

                def sort_value(row):
                    value = row.get(self.project_sort)
                    return (
                        value is not None,
                        value if isinstance(value, (int, float)) else str(value or "").casefold(),
                    )

                projects.sort(key=sort_value, reverse=self.project_descending)
                self.replace_rows(self.projects, projects)
                self.replace_rows(self.jobs, value["jobs"]["rows"])
                total = value["jobs"]["total"]
                self.page_label.setText(
                    f"{self.offset + 1 if total else 0}–{min(self.offset + 200, total)} из {total}"
                )
                self.buttons["next_jobs"].setProperty("has_next", self.offset + 200 < total)
            elif label == "show":
                self.selected_report = value.get("report")
                self.details.setPlainText(self.format_job(value))
            elif label == "open_workdir":
                path = value.get("workdir")
                if path and Path(path).is_dir():
                    QDesktopServices.openUrl(QUrl.fromLocalFile(path))
                else:
                    self.details.setPlainText("Рабочая папка ещё не создана: задание не запускалось.")
            elif label == "enqueue_batch":
                self.tabs.setCurrentIndex(1)
                self.details.setPlainText(
                    f"Добавлено заданий: {len(value['jobs'])}. "
                    + " ".join(MESSAGES[e["code"]] for e in value["errors"])
                )
            elif label == "shutdown":
                self.connected = False
                self.banner.setText(
                    "Служба завершает текущие задания и отключится. Для нового запуска — «Переподключиться»."
                )
            if self.connected:
                status = (
                    "Останавливается"
                    if self.service.get("stopping")
                    else (
                        "Обслуживание запущено" if self.service.get("running") else "Обслуживание остановлено"
                    )
                )
                counts = " · ".join(
                    f"{LABELS.get(k, k)}: {v}" for k, v in self.service.get("counts", {}).items()
                )
                self.banner.setText(
                    f"{status} · Служба PID {self.service.get('pid')} · {counts}\n{self.root}"
                )
                self.banner.setStyleSheet("")
                if self.service.get("error"):
                    self.banner.setText(MESSAGES[self.service["error"]])
                elif self.last_error:
                    self.banner.setText(MESSAGES[self.last_error])
                    self.banner.setStyleSheet("color: #b02828")
        self.update_buttons()
        self.observe()
        if self.pending:
            pending, self.pending = self.pending, None
            self.submit(*pending)
            return
        if not error and label not in {"dashboard", "show", "shutdown"}:
            self.refresh()

    def replace_rows(self, view, rows):
        selected = {r["id"] for r in self.selected(view)}
        view.model().replace(rows)
        from PySide6.QtCore import QItemSelectionModel

        for i, row in enumerate(rows):
            if row["id"] in selected:
                view.selectionModel().select(
                    view.model().index(i, 0),
                    QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows,
                )

    def refresh(self):
        if not self.busy and self.connected:
            query = {
                "search": self.search.text(),
                "status": self.status_filter.currentData(),
                "offset": self.offset,
                "order": self.job_order,
                "descending": self.job_descending,
            }
            self.submit("dashboard", lambda: request(self.root, "dashboard", **query))

    def filters_changed(self):
        self.offset = 0
        self.refresh()

    def sort_projects(self, index):
        self.project_sort = self.projects_model.columns[index][0]
        self.project_descending = not self.project_descending
        self.refresh()

    def sort_jobs(self, index):
        key = self.jobs_model.columns[index][0]
        if key == "name":
            key = "project_id"
        if key in {"created", "updated", "status", "project_id", "attempt", "timeout"}:
            self.job_order, self.job_descending = key, not self.job_descending
            self.refresh()

    def update_buttons(self):
        available = not self.busy and self.connected
        running = self.service.get("running", False)
        for button in self.buttons.values():
            button.setEnabled(available)
        self.buttons["reconnect"].setEnabled(not self.busy)
        self.buttons["start_service"].setEnabled(available and not running)
        self.buttons["stop_service"].setEnabled(available and running)
        self.buttons["save_settings"].setEnabled(available and not running)
        self.buttons["reconcile_jobs"].setEnabled(available and not running)
        self.buttons["previous_jobs"].setEnabled(available and self.offset > 0)
        self.buttons["next_jobs"].setEnabled(
            available and bool(self.buttons["next_jobs"].property("has_next"))
        )
        for name in ["max_workers", "max_per_project", "timeout"]:
            getattr(self, name).setEnabled(available and not running)

    def reconnect(self):
        self.submit("connect", lambda: ensure_service(self.root))

    def add(self):
        source, _ = QFileDialog.getOpenFileName(
            self, "Добавить сохранённый проект Maker", "", "Poster граф (*.postergraph.json *.json)"
        )
        if source:
            self.submit("add", lambda: request(self.root, "add", source=source), source)

    def inspect(self, *_):
        selected = self.selected(self.projects)
        if selected:
            item = selected[0]
            text = f"{item['name']}\nВерсия снимка: {item['version']}\nИсходник: {item['source']}\nКубиков: {item['nodes']}\n"
            text += "Входы: " + ", ".join(
                v["name"] + (" (защищён)" if v["secret"] else "") for v in item["inputs"]
            )
            self.details.setPlainText(text + "\n\n" + RESOURCE_NOTICE)

    def enqueue(self):
        items = self.selected(self.projects)
        batch = []
        for item in items[:100]:
            inputs, secrets = {}, {}
            dialog = InputsDialog(item, self)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                dialog.deleteLater()
                return
            inputs, secrets = dialog.values()
            batch.append(
                {
                    "id": item["id"],
                    "inputs": inputs,
                    "secrets": secrets,
                    "timeout": dialog.timeout.value(),
                    "profile_root": str(Path(dialog.profile_path.text()).parent)
                    if dialog.profile_path.text()
                    else None,
                    "profile_id": Path(dialog.profile_path.text()).stem
                    if dialog.profile_path.text()
                    else None,
                    "profile_key_file": dialog.profile_key.text() or None,
                }
            )
            dialog.deleteLater()
        if batch:
            self.call("enqueue_batch", items=batch, request_id=uuid.uuid4().hex)

    def remove(self):
        self.call("remove", ids=[r["id"] for r in self.selected(self.projects)])

    def cancel(self):
        self.call("cancel", ids=[r["id"] for r in self.selected(self.jobs)])

    def settings(self):
        self.call(
            "settings", **{k: getattr(self, k).value() for k in ["max_workers", "max_per_project", "timeout"]}
        )

    def show_job(self, *_):
        rows = self.selected(self.jobs)
        if rows:
            self.call("show", id=rows[0]["id"])

    @staticmethod
    def format_job(value):
        lines = [f"Задание {value['id']}: {LABELS.get(value['status'], value['status'])}"]
        if value["status"] in {"UNKNOWN", "REVIEW_REQUIRED"}:
            lines.append("Проверьте фактические внешние действия вручную. Автоматического повтора нет.")
        for item in value["history"]:
            lines.append(
                f"Попытка {item['attempt']} · PID {item['pid']} · {item['status']} · "
                + MESSAGES.get(item["code"], "")
            )
        report = value.get("report")
        if report:
            lines.extend(["\nОтчёт проверен по SHA256.", json.dumps(report, ensure_ascii=False, indent=2)])
            if report.get("node_error_code") in {
                "FILE_MISSING",
                "FILE_NOT_FOUND",
                "PATH_INVALID",
                "PATH_FORBIDDEN",
            }:
                lines.append(RESOURCE_NOTICE)
        lines.append("\nИстория событий:")
        for event in value.get("events", []):
            lines.append(f"{event['status']} — {MESSAGES.get(event['code'], event['code'])}")
        return "\n".join(lines)

    def open_workdir(self):
        # The path is derived from the selected execution, never from graph metadata.
        rows = self.selected(self.jobs)
        if rows:
            self.submit("open_workdir", lambda: request(self.root, "show", id=rows[0]["id"]))
            # Open in completed(), after SHA validation on the service thread.

    def context(self, view, point):
        menu = QMenu(self)
        choices = (
            [("В очередь", self.enqueue), ("Сведения", self.inspect), ("Убрать из списка", self.remove)]
            if view is self.projects
            else [
                ("История / результат", self.show_job),
                ("Отменить", self.cancel),
                ("Открыть рабочую папку", self.open_workdir),
            ]
        )
        for title, callback in choices:
            action = QAction(title, menu)
            action.triggered.connect(callback)
            menu.addAction(action)
        menu.exec(view.viewport().mapToGlobal(point))

    def page(self, delta):
        self.offset = max(0, self.offset + delta)
        self.refresh()

    def observe(self):
        target = os.environ.get("POSTER_DISPATCHER_OBSERVER")
        if not target:
            return
        from ..maker.storage import atomic

        widgets = {}
        for child in self.findChildren(QWidget):
            if child.objectName() and child.isVisible():
                pos = child.mapToGlobal(child.rect().center())
                widgets[child.objectName()] = {"x": pos.x(), "y": pos.y(), "enabled": child.isEnabled()}
        rows = {}
        for name, view in [("projects", self.projects), ("jobs", self.jobs)]:
            values = []
            for index, row in enumerate(view.model().rows):
                rect = view.visualRect(view.model().index(index, 0))
                point = view.viewport().mapToGlobal(rect.center())
                values.append(
                    {
                        **{k: row[k] for k in ["id", "name", "status", "pid", "version"] if k in row},
                        "x": point.x(),
                        "y": point.y(),
                        "visible": view.isVisible() and view.viewport().rect().intersects(rect),
                        "selected": view.selectionModel().isRowSelected(index, QModelIndex()),
                    }
                )
            rows[name] = values
        tabs = []
        for index in range(self.tabs.count()):
            point = self.tabs.tabBar().mapToGlobal(self.tabs.tabBar().tabRect(index).center())
            tabs.append({"index": index, "x": point.x(), "y": point.y()})
        atomic(
            Path(target),
            json.dumps(
                {
                    "format": "poster.dispatcher.observer",
                    "pid": os.getpid(),
                    "busy": self.busy,
                    "connected": self.connected,
                    "service": self.service,
                    "error": self.last_error,
                    "action": self.last_action,
                    "widgets": widgets,
                    "tabs": tabs,
                    "details_sha_verified": "Отчёт проверен по SHA256." in self.details.toPlainText(),
                    **rows,
                }
            ).encode(),
        )

    def closeEvent(self, event):
        self.closed = True
        self.timer.stop()
        self.pool.shutdown(wait=False, cancel_futures=True)
        event.accept()  # No service stop, worker cancellation, or Job Object close here.


def launch():
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("Poster Dispatcher")
    window = DispatcherWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(launch())
