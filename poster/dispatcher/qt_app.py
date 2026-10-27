"""Native Dispatcher client. No queue/worker/SQLite work runs on the Qt thread."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from zoneinfo import ZoneInfo
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
    QMessageBox,
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
from .schedule_dialog import ScheduleDialog
from .remote_widgets import RemoteTarget

LABELS = {
    "ENABLED": "Включено",
    "DISABLED": "Выключено",
    "SCHEDULE_REVIEW": "Пауза: ручная проверка",
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
        row = self.rows[index.row()]
        key = self.columns[index.column()][0]
        value = row.get(key, "")
        if "spec" in row:
            spec = row["spec"]
            if key in {"mode", "timezone"}:
                value = spec[key]
            if key == "timing":
                value = (
                    datetime.fromtimestamp(spec["start"], ZoneInfo(spec["timezone"])).isoformat()
                    if spec["mode"] == "once"
                    else str(spec["interval"]) + " с"
                    if spec["mode"] == "interval"
                    else spec["time"]
                    + (
                        " · " + ",".join(str(d + 1) for d in spec["weekdays"])
                        if spec["mode"] == "weekly"
                        else ""
                    )
                )
        if role == Qt.ItemDataRole.UserRole:
            return value
        if role == Qt.ItemDataRole.DisplayRole:
            key = self.columns[index.column()][0]
            if key in {"created", "updated", "last_run", "next_due", "due", "started", "last_seen"} and value:
                return datetime.fromtimestamp(
                    value, ZoneInfo(row["spec"]["timezone"]) if "spec" in row else None
                ).strftime("%d.%m %H:%M:%S %Z")
            if key in {"paused_code", "status"}:
                return LABELS.get(value, MESSAGES.get(value, value))
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
    def __init__(self, item, parent, target=None):
        super().__init__(parent)
        self.setObjectName("dispatcher_inputs")
        self.setWindowTitle("Входы — " + item["name"])
        self.resize(560, 400)
        self.fields = []
        layout = QVBoxLayout(self)
        note = QLabel(
            "Без флажка «Передать» используется сохранённое значение Maker. Типы JSON сохраняются. "
            "Защищённые входы шифруются; значения не попадают в журнал."
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        self.remote_target = RemoteTarget(
            parent.agent_items,
            self,
            agent_id=target.get("agent_id") if target else None,
            outputs=target.get("remote_outputs", []) if target else item.get("outputs", []),
        )
        if target is not None:
            self.remote_target.setEnabled(False)
        layout.addWidget(self.remote_target)
        content = QWidget()
        form = QFormLayout(content)
        self.timeout = QSpinBox()
        self.timeout.setObjectName("job_timeout")
        self.timeout.setRange(1, 86400)
        self.timeout.setValue(parent.timeout.value())
        form.addRow("Время этого задания, с", self.timeout)
        from poster.portability.network import NetworkProfiles

        self.network_override = QComboBox()
        self.network_override.setObjectName("job_network_override")
        self.network_override.addItem("Сеть из проекта", None)
        for row in NetworkProfiles().rows():
            self.network_override.addItem(row["name"] + " · " + row["kind"], row["id"])
        form.addRow("Явная замена сети", self.network_override)
        self.session_permission = QComboBox()
        self.session_permission.setObjectName("job_session_permission")
        self.session_permission.addItem("Чистая сессия Agent (без переноса)", None)
        import time

        for permit in parent.session_permits:
            if (
                permit["project_id"] == item["project_id"]
                and permit["version"] == item["version"]
                and not permit["revoked"]
                and permit["expires"] > time.time()
                and permit["remaining"] > 0
            ):
                self.session_permission.addItem(permit["agent_id"] + " · " + permit["id"], permit["id"])
        form.addRow("Согласие переноса", self.session_permission)
        self.profile_execution = QComboBox()
        self.profile_execution.setObjectName("job_profile_mode")
        for title, key in [
            ("Чистая независимая сессия", "clean"),
            ("Независимая копия профиля для каждой копии", "isolated"),
            ("Общий профиль, выполнение последовательно", "shared"),
        ]:
            self.profile_execution.addItem(title, key)
        form.addRow("Сессия каждой копии", self.profile_execution)
        self.profile_path, self.profile_key = QLineEdit(), QLineEdit()
        self.profile_rows = []
        self.profile_path.setObjectName("job_profile")
        self.profile_path.textChanged.connect(
            lambda text: (
                self.profile_execution.setCurrentIndex(1)
                if text and self.profile_execution.currentData() == "clean"
                else None
            )
        )
        self.profile_key.setObjectName("job_profile_key")
        self.profile_path.setPlaceholderText("Необязательно: profile_….enc; иначе новая сессия")
        self.profile_key.setPlaceholderText("session.key Maker для этого профиля")

        def profile_mode():
            local = self.remote_target.agent.currentData() is None
            for row in self.profile_rows:
                row.setEnabled(local)
            self.profile_path.setEnabled(local)
            self.profile_key.setEnabled(local)
            if not local:
                self.profile_path.clear()
                self.profile_key.clear()

        self.remote_target.agent.currentIndexChanged.connect(profile_mode)
        from poster.portability.local import profile_binding

        binding = profile_binding(item["source"])
        if binding and binding.get("profile_id"):
            self.profile_path.setText(str(Path(binding["root"]) / (binding["profile_id"] + ".enc")))
            self.profile_key.setText(binding.get("key_file") or "")
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
                self.profile_rows.append(row)
        for definition in item["inputs"]:
            row = QWidget()
            line = QHBoxLayout(row)
            enabled = QCheckBox("Передать")
            enabled.setChecked(definition["required"] and not definition.get("has_default"))
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
                lambda index, edit=edit, boolean=boolean, protected=definition["secret"]: (
                    edit.setVisible(index != 2 or protected),
                    boolean.setVisible(index == 2 and not protected),
                    edit.setPlaceholderText("true / false" if index == 2 and protected else ""),
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
        self.references = {}
        for ref in item.get("secret_refs", []):
            edit = QLineEdit()
            edit.setObjectName("secret_ref_" + ref)
            edit.setEchoMode(QLineEdit.EchoMode.Password)
            form.addRow("Защищённое значение " + ref, edit)
            self.references[ref] = edit
        self.copies, self.parallel = QSpinBox(), QSpinBox()
        self.copies.setObjectName("job_copies")
        self.parallel.setObjectName("job_parallel")
        self.copies.setRange(1, 1000)
        self.parallel.setRange(1, 32)
        self.copies.setValue(1)
        self.parallel.setValue(1)
        if target is None:
            form.addRow("Всего запусков", self.copies)
            form.addRow("Одновременно, не выше лимитов очереди", self.parallel)
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
        profile_mode()
        buttons.accepted.connect(self.validate)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def validate(self):
        try:
            self.values()
            self.reference_values()
            self.remote_target.values()
        except DispatcherError as exc:
            self.error.setText(MESSAGES[exc.code])
            return
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
                    if definition["secret"]:
                        if raw.strip().lower() not in {"true", "false"}:
                            raise ValueError()
                        value = raw.strip().lower() == "true"
                    else:
                        value = bool(boolean.currentIndex())
                elif kind.currentIndex():
                    value = json.loads(raw)
                    if kind.currentIndex() == 1 and (type(value) not in {int, float}):
                        raise ValueError()
                else:
                    value = raw
                json.dumps(value, allow_nan=False)
                (secrets if definition["secret"] else inputs)[definition["name"]] = value
            elif definition["required"] and not definition.get("has_default"):
                raise ValueError()
        return inputs, secrets

    def reference_values(self):
        if any(not edit.text() for edit in self.references.values()):
            raise ValueError()
        return {ref: edit.text() for ref, edit in self.references.items()}


class DispatcherWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setObjectName("dispatcher_window")
        self.setWindowTitle("Poster Dispatcher — очередь проектов")
        screen = QApplication.primaryScreen().availableGeometry()
        self.resize(min(1220, screen.width() - 30), min(780, screen.height() - 50))
        self.setMinimumSize(min(900, screen.width() - 30), min(620, screen.height() - 50))
        self.schedule_dialog = None
        self.schedule_offset = self.history_offset = 0
        self.schedule_order, self.schedule_descending = "next_due", False
        self.project_items = []
        self.agent_items = []
        self.session_permits = []
        self.schedule_total = 0
        self.history_schedule = None
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
        preparation = self.menuBar().addMenu("Подготовка проекта")
        action = preparation.addAction("Профили, сеть, ресурсы и готовность…")
        action.setObjectName("dispatcher_preparation")
        action.setShortcut("Ctrl+Shift+P")
        action.triggered.connect(self.open_preparation)
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
        self.button("cloud_projects", "Google Drive…", self.open_cloud, actions)
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
                ("next_due", "Следующий запуск"),
                ("executions", "Запуски"),
                ("errors", "Ошибки / неизвестно"),
                ("active", "Процессы"),
                ("last_run", "Последний запуск"),
                ("elapsed", "Всего секунд"),
                ("nodes", "Кубики"),
                ("browser", "Исполнение"),
                ("source", "Исходный файл"),
                ("cloud_version", "Версия Google Drive"),
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
                ("last_confirmed_step", "Подтверждённый кубик"),
                ("due", "Плановое время"),
                ("started", "Фактическое время"),
                ("next_due", "Следующий запуск проекта"),
                ("schedule_id", "Расписание"),
                ("agent_id", "Удалённый агент"),
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
        self.build_schedules()
        self.build_remote_agents()
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

    def build_remote_agents(self):
        self.pairing_dialog = None
        panel = QWidget()
        layout = QVBoxLayout(panel)
        self.remote_agents_model = TableModel(
            [
                ("name", "Агент"),
                ("id", "Идентичность"),
                ("state", "Доверие"),
                ("online", "Online"),
                ("os", "ОС"),
                ("version", "Версия"),
                ("protocol", "Протокол"),
                ("last_seen", "Последний heartbeat"),
                ("active", "Активные"),
                ("projects", "Проекты"),
                ("last_error", "Ошибка"),
            ],
            self,
        )
        self.remote_agents = self.table("remote_agents_table", self.remote_agents_model)
        layout.addWidget(self.remote_agents)
        guided = QHBoxLayout()
        self.button("remote_pairing", "Подключить Linux Agent пошагово…", self.remote_pairing, guided)
        self.button("remote_diagnose", "Проверить соединение", self.remote_diagnose, guided)
        self.button("remote_check", "Безопасное тестовое задание", self.remote_check, guided)
        layout.addLayout(guided)
        actions = QHBoxLayout()
        self.button("remote_export", "Ключ Controller…", self.remote_export, actions)
        self.button("remote_register", "Загрузить заявку…", self.remote_register, actions)
        self.button("remote_approve", "Подтвердить агента…", self.remote_approve, actions)
        self.button("remote_revoke", "Отозвать доступ…", self.remote_revoke, actions)
        layout.addLayout(actions)
        note = QLabel(
            "Controller и Linux Agent соединяются исходящими HTTPS-запросами с отдельно развёрнутым relay. SQLite остаётся на Controller. Перед подтверждением сравните полный ID агента с registration.json на Linux. Подтверждение запускает обслуживание общей очереди."
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        self.tabs.addTab(panel, "Удалённые Workers")

    def remote_pairing(self):
        from .pairing_dialog import PairingDialog

        self.pairing_dialog = PairingDialog(self)
        self.pairing_dialog.show()

    def remote_diagnose(self):
        rows = self.selected(self.remote_agents)
        if rows:
            self.call("remote_diagnose", id=rows[0]["id"])

    def remote_check(self):
        rows = self.selected(self.remote_agents)
        if rows:
            self.call("remote_test", id=rows[0]["id"])

    def remote_export(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Экспорт публичной идентичности Controller", "controller-public.json", "JSON (*.json)"
        )
        if path:
            self.call("remote_identity", path=path)

    def remote_register(self):
        registration, _ = QFileDialog.getOpenFileName(
            self, "Публичная заявка Linux Agent", "", "JSON (*.json)"
        )
        if not registration:
            return
        relay, _ = QFileDialog.getOpenFileName(self, "Приватный controller-relay.json", "", "JSON (*.json)")
        if relay:
            self.call("remote_register", registration=registration, relay_profile=relay)

    def remote_approve(self):
        rows = self.selected(self.remote_agents)
        if (
            rows
            and QMessageBox.question(
                self,
                "Доверенный Linux Agent",
                f"Подтвердить {rows[0]['name']}?\nПолный ID: {rows[0]['id']}\nАгент получит необходимые секреты исполняемых проектов. Запустить обслуживание?",
            )
            == QMessageBox.StandardButton.Yes
        ):
            self.call("remote_approve", id=rows[0]["id"])

    def remote_revoke(self):
        rows = self.selected(self.remote_agents)
        if (
            rows
            and QMessageBox.question(
                self,
                "Отзыв доступа",
                "Отозвать доступ? Активные удалённые запуски потребуют проверки UNKNOWN; автоматического повтора не будет. Повторное подключение потребует новой идентичности.",
            )
            == QMessageBox.StandardButton.Yes
        ):
            self.call("remote_revoke", id=rows[0]["id"])

    def build_schedules(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        filters = QHBoxLayout()
        self.schedule_search = QLineEdit()
        self.schedule_search.setObjectName("schedule_search")
        self.schedule_search.setPlaceholderText("Поиск расписаний: проект / ID")
        self.schedule_state = QComboBox()
        self.schedule_state.setObjectName("schedule_state")
        for title, state in [
            ("Все", ""),
            ("Включено", "enabled"),
            ("Пауза проверки", "paused"),
            ("Выключено", "disabled"),
        ]:
            self.schedule_state.addItem(title, state)
        self.schedule_search.textChanged.connect(self.schedule_filters)
        self.schedule_state.currentIndexChanged.connect(self.schedule_filters)
        filters.addWidget(self.schedule_search)
        filters.addWidget(self.schedule_state)
        layout.addLayout(filters)
        self.schedules_model = TableModel(
            [
                ("name", "Проект"),
                ("version", "Версия"),
                ("agent_id", "Агент (пусто — локально)"),
                ("mode", "Режим"),
                ("timing", "Дни / время / интервал"),
                ("timezone", "Часовой пояс"),
                ("status", "Состояние"),
                ("next_due", "Следующий запуск"),
                ("last_run", "Последний запуск"),
                ("last_result", "Результат"),
                ("executions", "Запуски"),
                ("successes", "Успехи"),
                ("errors", "Ошибки"),
                ("missed", "Пропущено"),
                ("paused_code", "Причина паузы"),
                ("id", "ID"),
            ],
            self,
        )
        self.schedules = self.table("schedules_table", self.schedules_model)
        self.schedules.doubleClicked.connect(self.schedule_history)
        self.schedules.horizontalHeader().sectionClicked.connect(self.schedule_sort)
        layout.addWidget(self.schedules)
        actions = QHBoxLayout()
        for name, title, callback in [
            ("create_schedule", "Создать…", self.schedule_create),
            ("edit_schedule", "Изменить…", self.schedule_edit),
            ("enable_schedule", "Включить", lambda: self.schedule_change(enabled=True)),
            ("pause_schedule", "Пауза", lambda: self.schedule_change(enabled=False)),
            ("delete_schedule", "Удалить", self.schedule_delete),
            ("resume_schedule", "Проверено: возобновить", self.schedule_resume),
            ("version_schedule", "Обновить версию…", self.schedule_version),
        ]:
            self.button(name, title, callback, actions)
        layout.addLayout(actions)
        pages = QHBoxLayout()
        self.button("previous_schedules", "← 200", lambda: self.schedule_page_by(-200), pages)
        self.schedule_page = QLabel()
        pages.addWidget(self.schedule_page)
        self.button("next_schedules", "200 →", lambda: self.schedule_page_by(200), pages)
        self.button("schedule_history", "История расписания", self.schedule_history, pages)
        self.button("schedule_history_prev", "История ←", lambda: self.schedule_history_page(-200), pages)
        self.button("schedule_history_next", "История →", lambda: self.schedule_history_page(200), pages)
        layout.addLayout(pages)
        autostart = QHBoxLayout()
        self.autostart_status = QLabel("Автозапуск Windows: не проверен")
        self.autostart_status.setWordWrap(True)
        autostart.addWidget(self.autostart_status)
        self.button(
            "autostart_status", "Проверить автозапуск", lambda: self.call("autostart_status"), autostart
        )
        self.button("autostart_enable", "Включить при входе…", self.autostart_enable, autostart)
        self.button(
            "autostart_disable",
            "Отключить автозапуск",
            lambda: self.call("autostart_set", enabled=False),
            autostart,
        )
        layout.addLayout(autostart)
        self.tabs.addTab(panel, "Расписание")

    def autostart_enable(self):
        if (
            QMessageBox.question(
                self,
                "Автозапуск службы",
                "Разрешить запуск PosterService.exe при вашем входе в Windows? Пароль и права администратора не требуются. Компьютер должен быть включён, пользователь должен войти в систему.",
            )
            == QMessageBox.StandardButton.Yes
        ):
            self.call("autostart_set", enabled=True)

    def schedule_filters(self, *_):
        self.schedule_offset = 0
        self.refresh()

    def schedule_sort(self, index):
        key = self.schedules_model.columns[index][0]
        if key not in {"name", "version", "next_due", "updated", "enabled", "project_id", "created"}:
            return
        self.schedule_order, self.schedule_descending = key, not self.schedule_descending
        self.refresh()

    def schedule_page_by(self, delta):
        self.schedule_offset = max(0, self.schedule_offset + delta)
        self.refresh()

    def schedule_form(self, items, row=None):
        dialog = ScheduleDialog(items, self, row)
        self.schedule_dialog = dialog
        result = dialog.exec()
        dialog.debounce.stop()
        self.schedule_dialog = None
        if result == QDialog.DialogCode.Accepted:
            value = (items[dialog.project.currentIndex()], dialog.values(), dialog.remote_target.values())
        else:
            value = None
        dialog.deleteLater()
        return value

    def schedule_create(self):
        if not self.project_items:
            self.details.setPlainText("Сначала добавьте сохранённый проект Maker.")
            return
        value = self.schedule_form(self.project_items)
        if not value:
            return
        item, spec, target = value
        dialog = InputsDialog(item, self, target)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            inputs, secrets = dialog.values()
            self.call(
                "schedule_create",
                item_id=item["id"],
                spec=spec,
                **target,
                options=dict(
                    inputs=inputs,
                    secrets=secrets,
                    timeout=dialog.timeout.value(),
                    profile_root=str(Path(dialog.profile_path.text()).parent)
                    if dialog.profile_path.text()
                    else None,
                    profile_id=Path(dialog.profile_path.text()).stem if dialog.profile_path.text() else None,
                    profile_key_file=dialog.profile_key.text() or None,
                ),
            )
            self.tabs.setCurrentIndex(2)
        dialog.deleteLater()

    def schedule_edit(self):
        rows = self.selected(self.schedules)
        if rows:
            row = rows[0]
            item = dict(id=row["id"], name=row["name"], version=row["version"])
            value = self.schedule_form([item], row)
            if value:
                self.call("schedule_change", id=row["id"], spec=value[1], target=value[2])

    def schedule_change(self, **options):
        rows = self.selected(self.schedules)
        if rows:
            self.call("schedule_change", id=rows[0]["id"], **options)

    def schedule_delete(self):
        if (
            self.selected(self.schedules)
            and QMessageBox.question(
                self, "Удаление", "Удалить расписание? История, задания и результаты сохранятся."
            )
            == QMessageBox.StandardButton.Yes
        ):
            self.schedule_change(delete=True)

    def schedule_resume(self):
        if (
            self.selected(self.schedules)
            and QMessageBox.question(
                self,
                "Ручная проверка",
                "Вы проверили внешние действия всех UNKNOWN/REVIEW_REQUIRED этого проекта? Возобновить автозапуски без изменения этих статусов?",
            )
            == QMessageBox.StandardButton.Yes
        ):
            self.schedule_change(resume=True, enabled=True)

    def schedule_version(self):
        rows = self.selected(self.schedules)
        if rows:
            row = rows[0]
            items = [i for i in self.project_items if i["project_id"] == row["project_id"]]
            if not items:
                self.details.setPlainText("Сначала явно добавьте новую версию проекта в каталог.")
                return
            item = items[0]
            if (
                QMessageBox.question(
                    self,
                    "Закрепить версию",
                    f"Заменить {row['version']} на {item['version']}? Сохранённые входы будут проверены.",
                )
                == QMessageBox.StandardButton.Yes
            ):
                self.call("schedule_version", id=row["id"], item_id=item["id"])

    def schedule_history(self, *_):
        rows = self.selected(self.schedules)
        if rows:
            self.history_schedule, self.history_offset = rows[0]["id"], 0
            self.call("schedule_history", id=self.history_schedule, offset=0)

    def schedule_history_page(self, delta):
        if self.history_schedule:
            self.history_offset = max(0, self.history_offset + delta)
            self.call("schedule_history", id=self.history_schedule, offset=self.history_offset)

    def table(self, name, model):
        view = QTableView()
        view.setObjectName(name)
        view.setModel(model)
        view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        view.horizontalHeader().setStretchLastSection(True)
        view.horizontalHeader().setSortIndicatorShown(True)
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
        if label == "remote_wizard" and context:
            context.consume(value, error)
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
            if label in {"autostart_status", "autostart_set"}:
                self.autostart_status.setText(
                    "Автозапуск Windows: "
                    + ("включён" if value["enabled"] else "выключен")
                    + (
                        " · путь изменился, включите заново"
                        if value["exists"] and not value["matches"]
                        else ""
                    )
                    + " · "
                    + value["task"]
                )
            elif label == "schedule_preview":
                if self.schedule_dialog and context == self.schedule_dialog.token:
                    self.schedule_dialog.display_preview(value)
            elif label == "schedule_list":
                self.replace_rows(self.schedules, value["rows"])
                self.schedule_total = value["total"]
                self.schedule_page.setText(
                    f"{self.schedule_offset + 1 if value['total'] else 0}–{min(self.schedule_offset + 200, value['total'])} из {value['total']}"
                )
            elif label == "schedule_history":
                self.details.setPlainText(
                    f"История расписания {self.history_schedule} · событий {value['total']} · страница {self.history_offset // 200 + 1}\n"
                    + "\n".join(
                        f"UTC {datetime.fromtimestamp(r['due'], ZoneInfo('UTC')).isoformat()} · {r['status'] or r['outcome']} · {r['occurrences']} событий · job {r['job_id'] or '—'} · PID {r['pid']} · {r['code'] or ''}"
                        for r in value["rows"]
                    )
                )
            elif label == "connect":
                self.connected, self.service = True, value
                for key, number in value["settings"].items():
                    getattr(self, key).setValue(number)
            elif label == "add":
                self.search.clear()
                self.tabs.setCurrentIndex(0)
            elif label == "dashboard":
                self.service = value["service"]
                self.project_items = value["projects"]
                self.agent_items = value["agents"]
                self.session_permits = value.get("session_permits", [])
                self.replace_rows(self.remote_agents, self.agent_items)
                self.replace_rows(self.schedules, value["schedules"]["rows"])
                self.schedule_total = value["schedules"]["total"]
                self.schedule_page.setText(
                    f"{self.schedule_offset + 1 if self.schedule_total else 0}–{min(self.schedule_offset + 200, self.schedule_total)} из {self.schedule_total}"
                )
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
            elif label == "remote_diagnose":
                self.details.setPlainText(
                    "Relay: "
                    + str(value["relay"])
                    + " · TLS: "
                    + str(value["tls_verified"])
                    + " · Agent online: "
                    + str(value["online"])
                    + " · свободно слотов: "
                    + str(value["free_slots"])
                    + "\n"
                    + "\n".join(value["messages"])
                )
            elif label == "remote_test":
                self.tabs.setCurrentIndex(1)
                self.details.setPlainText(
                    "Безопасный тест в общей очереди: "
                    + value["job"]
                    + "\nПосле SUCCEEDED выберите задание и откройте рабочую папку."
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
        if not error and label not in {
            "dashboard",
            "show",
            "shutdown",
            "schedule_preview",
            "schedule_list",
            "schedule_history",
        }:
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
                "schedule_query": dict(
                    search=self.schedule_search.text(),
                    state=self.schedule_state.currentData(),
                    offset=self.schedule_offset,
                    order=self.schedule_order,
                    descending=self.schedule_descending,
                ),
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
        self.projects.horizontalHeader().setSortIndicator(
            index, Qt.SortOrder.DescendingOrder if self.project_descending else Qt.SortOrder.AscendingOrder
        )
        self.refresh()

    def sort_jobs(self, index):
        key = self.jobs_model.columns[index][0]
        self.job_order, self.job_descending = key, not self.job_descending
        self.jobs.horizontalHeader().setSortIndicator(
            index, Qt.SortOrder.DescendingOrder if self.job_descending else Qt.SortOrder.AscendingOrder
        )
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
        self.buttons["previous_schedules"].setEnabled(available and self.schedule_offset > 0)
        self.buttons["next_schedules"].setEnabled(
            available and self.schedule_offset + 200 < self.schedule_total
        )
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

    def open_cloud(self):
        from poster.cloud.qt_panel import CloudDialog

        self.cloud_dialog = CloudDialog(self, opened=self.cloud_project_imported, dispatcher_root=self.root)
        self.cloud_dialog.show()

    def open_preparation(self):
        items = self.selected(self.projects)
        if len(items) != 1:
            self.banner.setText("Выберите один проект для подготовки.")
            return
        from poster.portability.qt_dialog import PreparationDialog
        from poster.maker.profiles import Profiles
        from poster.auth import ensure_local_key

        ensure_local_key(self.root / "private")
        self.preparation_dialog = PreparationDialog(
            self,
            source=items[0]["source"],
            profiles=Profiles(self.root / "browser-profiles"),
            dispatcher_root=self.root,
            item=items[0],
            changed=lambda metadata: self.call("add", source=items[0]["source"]),
        )
        self.preparation_dialog.show()

    def cloud_project_imported(self, value):
        cloud = {k: value[k] for k in ("project_id", "version", "folder_id")}
        self.submit("add", lambda: request(self.root, "add", source=value["source"], cloud=cloud))

    def inspect(self, *_):
        selected = self.selected(self.projects)
        if selected:
            item = selected[0]
            text = f"{item['name']}\nВерсия снимка: {item['version']}\nИсходник: {item['source']}\nКубиков: {item['nodes']}\n"
            if item.get("cloud"):
                text += "Google Drive: " + item["cloud"]["version"] + "\n"
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
                    **dialog.remote_target.values(),
                    "inputs": inputs,
                    "secrets": secrets,
                    "secret_refs": dialog.reference_values(),
                    "count": dialog.copies.value(),
                    "max_concurrent": dialog.parallel.value(),
                    "timeout": dialog.timeout.value(),
                    "network_override": dialog.network_override.currentData(),
                    "session_permission": dialog.session_permission.currentData(),
                    "profile_mode": "isolated"
                    if dialog.profile_execution.currentData() == "clean"
                    else dialog.profile_execution.currentData(),
                    "profile_root": str(Path(dialog.profile_path.text()).parent)
                    if dialog.profile_path.text() and dialog.profile_execution.currentData() != "clean"
                    else None,
                    "profile_id": Path(dialog.profile_path.text()).stem
                    if dialog.profile_path.text() and dialog.profile_execution.currentData() != "clean"
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
                "FILE_OUTSIDE_WORKDIR",
                "FILE_LINK_FORBIDDEN",
                "FILE_PATH_INVALID",
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
        for name, view in [("projects", self.projects), ("jobs", self.jobs), ("schedules", self.schedules)]:
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
        if getattr(self, "preparation_dialog", None):
            self.preparation_dialog.close()
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
