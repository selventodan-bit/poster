"""Native Qt cloud projects window. Network and credential work never block Qt."""

import asyncio
from datetime import datetime
from pathlib import Path
import threading

from PySide6.QtCore import QObject, QUrl, Qt, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from .desktop import DesktopCloud
from .errors import CloudError, MESSAGES


class AsyncBridge(QObject):
    completed = Signal(str, object, object)

    def __init__(self, parent):
        super().__init__(parent)
        self.loop = asyncio.new_event_loop()
        self.closed = False
        self.thread = threading.Thread(target=self.loop.run_forever, name="poster-drive-ui", daemon=True)
        self.thread.start()

    def submit(self, name, coroutine):
        future = asyncio.run_coroutine_threadsafe(coroutine, self.loop)

        def finished(value):
            if self.closed:
                return
            try:
                result = value.result()
                error = None
            except Exception as exc:
                result = None
                error = exc.code if isinstance(exc, CloudError) else "DRIVE_UNAVAILABLE"
            if not self.closed:
                self.completed.emit(name, result, error)

        future.add_done_callback(finished)

    def close(self, service):
        if self.closed:
            return
        self.closed = True

        async def shutdown():
            current = asyncio.current_task()
            others = [t for t in asyncio.all_tasks() if t is not current]
            for task in others:
                task.cancel()
            if others:
                await asyncio.gather(*others, return_exceptions=True)
            await service.close()
            self.loop.call_soon(self.loop.stop)

        asyncio.run_coroutine_threadsafe(shutdown(), self.loop)
        self.thread.join(2)


class CloudDialog(QDialog):
    def __init__(
        self,
        parent,
        *,
        source=None,
        resources=(),
        outputs=(),
        opened=None,
        published=None,
        service=None,
        auto_publish=False,
    ):
        super().__init__(parent)
        self.setObjectName("poster_cloud_projects")
        self.setWindowTitle("Google Drive — проекты Poster")
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.resize(850, 650)
        self.service = service or DesktopCloud()
        self.bridge = AsyncBridge(self)
        self.bridge.completed.connect(self.completed)
        self.source = Path(source) if source else None
        self.auto_publish = auto_publish
        self.preferred_project = None
        self.opened, self.published = opened, published
        self.pending = {}
        self.buttons = {}
        self.busy = False
        self.rows = []
        self.last_error = None
        layout = QVBoxLayout(self)
        self.status = QLabel("Проверка подключения…")
        self.status.setObjectName("cloud_status")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        account = QHBoxLayout()
        self.button(account, "cloud_connect", "Подключить Google Drive", self.connect)
        self.button(account, "cloud_disconnect", "Отключить аккаунт", self.disconnect)
        self.button(
            account,
            "cloud_diagnostics",
            "Диагностика",
            lambda: self.run("diagnostics", self.service.diagnostics(), self.diagnostics),
        )
        self.button(account, "cloud_cancel_auth", "Отменить вход", self.service.auth.cancel)
        layout.addLayout(account)
        self.projects = QTableWidget(0, 3)
        self.projects.setObjectName("cloud_projects")
        self.projects.setHorizontalHeaderLabels(["Проект", "ID проекта", "Версия"])
        self.projects.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.projects.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.projects.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.projects.itemSelectionChanged.connect(self.selected)
        self.projects.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.projects, 1)
        version_row = QFormLayout()
        self.version = QComboBox()
        self.version.setObjectName("cloud_version")
        version_row.addRow("Открываемая версия", self.version)
        layout.addLayout(version_row)
        files = QHBoxLayout()
        self.button(files, "cloud_refresh", "Синхронизировать", self.refresh)
        self.button(files, "cloud_open", "Открыть проект с Google Drive", self.open)
        self.button(files, "cloud_folder", "Открыть папку проекта", self.folder)
        self.button(files, "cloud_versions", "История версий", self.selected)
        layout.addLayout(files)
        if self.source:
            layout.addWidget(QLabel("Файлы данных этого проекта (добавляются только выбранные файлы):"))
            self.resources = QListWidget()
            self.resources.setObjectName("cloud_resources")
            self.resources.addItems(list(resources))
            self.resources.setMaximumHeight(85)
            layout.addWidget(self.resources)
            tools = QHBoxLayout()
            self.button(
                tools, "cloud_add_resource", "Добавить CSV/XLSX/JSON и другие файлы…", self.add_resource
            )
            self.button(tools, "cloud_remove_resource", "Убрать выбранный", self.remove_resource)
            layout.addLayout(tools)
            form = QFormLayout()
            self.outputs = QLineEdit("; ".join(outputs))
            self.outputs.setObjectName("cloud_outputs")
            self.outputs.setPlaceholderText("Например: result.csv; report.txt")
            form.addRow("Возвращаемые файлы", self.outputs)
            layout.addLayout(form)
            save_row = QHBoxLayout()
            self.button(save_row, "cloud_publish", "Сохранить проект на Google Drive", self.publish)
            self.button(save_row, "cloud_update", "Обновить облачную версию", self.publish)
            layout.addLayout(save_row)
        close = QHBoxLayout()
        close.addStretch()
        self.button(close, "cloud_close", "Закрыть", self.accept)
        layout.addLayout(close)
        self.run("status", self.service.status(), self.account_status)

    def button(self, layout, name, title, callback):
        button = QPushButton(title)
        button.setObjectName(name)
        button.clicked.connect(callback)
        self.buttons[name] = button
        layout.addWidget(button)
        return button

    def run(self, name, coroutine, callback):
        if self.busy:
            coroutine.close()
            return
        self.busy = True
        self.last_error = None
        self.pending[name] = callback
        for key, button in self.buttons.items():
            button.setEnabled(key in {"cloud_close", "cloud_cancel_auth"})
        self.status.setText("Подождите… Можно отменить вход или закрыть окно.")
        self.bridge.submit(name, coroutine)

    def completed(self, name, value, error):
        callback = self.pending.pop(name, None)
        self.busy = False
        for button in self.buttons.values():
            button.setEnabled(True)
        if error:
            self.last_error = error
            self.status.setText(MESSAGES.get(error, MESSAGES["DRIVE_UNAVAILABLE"]))
            if error == "DRIVE_CONFLICT" and self.source:
                answer = QMessageBox.question(
                    self,
                    "Конфликт версий",
                    "На Drive уже есть другая версия. Сохранить ваш текущий проект как следующую версию? Прежняя версия останется в истории.",
                )
                if answer == QMessageBox.StandardButton.Yes:
                    self.publish(resolve=True)
            return
        if callback:
            callback(value)

    def account_status(self, value):
        self.status.setText(
            "Google Drive подключён. Выберите проект или сохраните текущий."
            if value["connected"]
            else "Google Drive не подключён. Нажмите «Подключить Google Drive»."
        )
        if value["connected"]:
            if self.auto_publish and self.source:
                self.auto_publish = False
                self.publish()
            else:
                self.refresh()

    def connect(self):
        self.run("status-for-connect", self.service.status(), self.connect_status)

    def connect_status(self, value):
        if not value["configured"]:
            dialog = QMessageBox(self)
            dialog.setWindowTitle("Первое подключение Google Drive")
            dialog.setText(
                "Нужна OAuth Desktop application Google. Один раз создайте её в Google Cloud Console, включите Drive API и скачайте JSON desktop client. Это настройки приложения, а не access token. Файл не встраивается в EXE."
            )
            dialog.setInformativeText(
                "Выберите скачанный desktop-client JSON. Подробные шаги приведены в руководстве Poster. Секреты аккаунта вручную копировать не требуется."
            )
            dialog.setStandardButtons(QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel)
            if dialog.exec() != QMessageBox.StandardButton.Ok:
                return
            name, _ = QFileDialog.getOpenFileName(
                self, "Настройки OAuth Desktop", "", "Google Desktop client (*.json)"
            )
            if not name:
                return
            self.run(
                "configure",
                self.service.configure(name),
                lambda _: self.run("connect", self.service.connect(), self.account_status),
            )
        else:
            self.run("connect", self.service.connect(), self.account_status)

    def disconnect(self):
        self.run("disconnect", self.service.disconnect(), self.disconnected)

    def disconnected(self, value):
        self.rows = []
        self.projects.setRowCount(0)
        self.version.clear()
        self.status.setText(
            "Локальная авторизация удалена."
            + (
                " Доступ Google отозван."
                if value["revoked"]
                else " Сетевой отзыв не подтверждён; проверьте разрешения аккаунта Google."
            )
        )

    def diagnostics(self, value):
        self.status.setText(
            "Google Drive: " + value["drive"] + ". Credentials: " + value["protected_by"] + "."
        )

    def refresh(self):
        self.run("projects", self.service.projects(), self.project_rows)

    def project_rows(self, rows):
        self.rows = sorted(rows, key=lambda r: (r["name"].casefold(), r["project_id"]))
        self.projects.setRowCount(len(self.rows))
        for i, row in enumerate(self.rows):
            for j, key in enumerate(("name", "project_id", "version")):
                value = str(row.get(key, ""))
                item = QTableWidgetItem(value if key == "name" else value[:12])
                item.setToolTip(value)
                self.projects.setItem(i, j, item)
        self.status.setText(
            f"Проектов на Google Drive: {len(rows)}. Частично загруженные версии не показаны как завершённые."
        )
        if rows:
            index = next((i for i, r in enumerate(self.rows) if r["project_id"] == self.preferred_project), 0)
            self.projects.selectRow(index)

    def current(self):
        index = self.projects.currentRow()
        return self.rows[index] if 0 <= index < len(self.rows) else None

    def selected(self):
        row = self.current()
        if self.busy or not row or row.get("status") != "READY":
            return
        self.run("versions", self.service.versions(row["project_id"]), self.version_rows)

    def version_rows(self, rows):
        self.version.clear()
        for row in reversed(rows):
            when = datetime.fromtimestamp(row["created"]).strftime("%d.%m.%Y %H:%M")
            self.version.addItem(when + " — " + row["version"][:12], row["version"])
        self.status.setText("Выбрана закреплённая версия. Другие версии остаются в истории.")

    def open(self):
        row = self.current()
        if not row or row.get("status") != "READY":
            return
        self.run(
            "open", self.service.open(row["project_id"], self.version.currentData()), self.opened_project
        )

    def opened_project(self, value):
        self.status.setText("Проект загружен и проверен.")
        if self.opened:
            self.opened(value)
        self.accept()

    def folder(self):
        row = self.current()
        if row and row.get("status") == "READY":
            QDesktopServices.openUrl(QUrl(row["url"]))

    def add_resource(self):
        names, _ = QFileDialog.getOpenFileNames(
            self,
            "Добавить выбранные файлы проекта",
            str(self.source.parent),
            "Данные (*.csv *.xlsx *.json *.txt *.tsv *.xml *.yaml *.yml);;Все файлы (*)",
        )
        if names:
            self.run("resources", self.service.include_resources(self.source, names), self.resources_added)

    def resources_added(self, names):
        existing = {self.resources.item(i).text() for i in range(self.resources.count())}
        for name in names:
            if name not in existing:
                self.resources.addItem(name)
        self.status.setText("Добавлены только выбранные ресурсы. У каждого запуска будет своя копия.")

    def remove_resource(self):
        for item in self.resources.selectedItems():
            self.resources.takeItem(self.resources.row(item))

    def publish(self, checked=False, *, resolve=False):
        names = [self.resources.item(i).text() for i in range(self.resources.count())]
        outputs = [s.strip() for s in self.outputs.text().replace("\n", ";").split(";") if s.strip()]
        self.run(
            "publish",
            self.service.publish(self.source, resources=names, outputs=outputs, resolve_conflict=resolve),
            self.published_project,
        )

    def published_project(self, value):
        self.preferred_project = value["project_id"]
        self.status.setText("Проект сохранён на Google Drive. Версия " + value["version"][:12] + ".")
        if self.published:
            self.published(value)
        self.refresh()

    def done(self, result):
        self.service.auth.cancel()
        self.bridge.close(self.service)
        super().done(result)
