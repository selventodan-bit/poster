"""Qt views over Poster 2.0 services; no duplicate Engine/HTTP/SQLite implementation."""

import asyncio
import json
from pathlib import Path
from urllib.parse import urljoin, urlsplit
import uuid

from PySide6.QtCore import QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QComboBox, QFileDialog, QFormLayout, QLabel, QLineEdit
from PySide6.QtWidgets import (
    QMessageBox,
    QPlainTextEdit,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QScrollArea,
    QWidget,
)

from ..browser import find_browser, install_browser
from ..project import load_project
from ..recognition.knowledge import KnowledgeBase
from ..recognition.policy import ResourcePolicy
from ..recognition.service import RecognitionSession
from ..recorder.workspace import EngineBridge, JOB_TEXT, local_test


class LegacyPanel:
    def __init__(self, window):
        self.window = window
        self.workspace = window.workspace
        self.bridge = EngineBridge(self.workspace)
        self.session = self.knowledge = self.existing_site = None
        self.future = None
        self.snapshot, self.rows, self.projects = {}, [], []
        self.busy = self.refreshing = False
        self.local_report = self.error = None
        self.selected_result = None
        page, form = self.page("Сайт и обучение")
        self.site_page = page
        form.addWidget(
            QLabel(
                "Существующий HTTP Recorder использует установленный Chrome. Maker — встроенный браузер в первой вкладке."
            )
        )
        fields = QFormLayout()
        self.url = QLineEdit()
        self.url.setObjectName("legacy_url")
        self.title = QLineEdit("Проверка Poster")
        self.title.setObjectName("legacy_title")
        self.project_id = QLineEdit()
        self.project_id.setObjectName("legacy_project_id")
        self.kind = QComboBox()
        self.kind.addItems(["auto", "topic", "reply", "comment"])
        self.cdns = QLineEdit()
        self.output = QLineEdit(str(self.workspace.projects))
        for label, widget in (
            ("URL", self.url),
            ("Заголовок", self.title),
            ("ID проекта", self.project_id),
            ("Тип публикации", self.kind),
            ("Доверенные CDN origin", self.cdns),
            ("Каталог проектов", self.output),
        ):
            fields.addRow(label, widget)
        form.addLayout(fields)
        self.body = QPlainTextEdit("Тестовая публикация для проверки формы.")
        self.body.setObjectName("legacy_body")
        self.body.setMaximumHeight(110)
        form.addWidget(self.body)
        window.row_buttons(
            form,
            [
                ("recognize", "Проверить распознавание", self.recognize),
                ("publish", "Тестовая публикация", self.publish),
                ("http_verify", "Проверить HTTP", self.http_verify),
            ],
        )
        window.row_buttons(
            form,
            [
                ("manual", "Обучить сайт", self.manual),
                ("finish_manual", "Завершить обучение", self.finish_manual),
                ("login", "Разрешить один вход", self.login),
            ],
        )
        self.forms = QComboBox()
        self.forms.setObjectName("legacy_forms")
        form.addWidget(self.forms)
        window.row_buttons(
            form,
            [
                ("form_select", "Выбрать форму", self.select_form),
                ("legacy_save", "Сохранить HTTP-проект", self.save),
                ("legacy_retrain", "Открыть для переобучения", self.retrain),
                ("legacy_stop", "Остановить", self.stop),
            ],
        )
        self.info = QPlainTextEdit()
        self.info.setReadOnly(True)
        form.addWidget(self.info)

        page, form = self.page("Проекты")
        self.projects_page = page
        self.projects_table = self.table(["ID", "Готовность"], "legacy_projects")
        form.addWidget(self.projects_table)
        window.row_buttons(
            form,
            [
                ("import_project", "Добавить READY-проект…", self.import_project),
                ("refresh_projects", "Обновить", self.refresh),
                ("project_queue", "В очередь", self.choose_queue),
            ],
        )
        page, form = self.page("Очередь")
        self.queue_page = page
        self.queue_project = QComboBox()
        self.queue_project.setObjectName("queue_project")
        self.queue_title = QLineEdit("Новая публикация")
        self.queue_title.setObjectName("queue_title")
        self.queue_body = QPlainTextEdit("Текст публикации")
        self.queue_body.setObjectName("queue_body")
        self.queue_body.setMaximumHeight(100)
        for widget in (self.queue_project, self.queue_title, self.queue_body):
            form.addWidget(widget)
        window.row_buttons(
            form,
            [
                ("enqueue", "Добавить публикацию", self.enqueue),
                ("engine_start", "Запустить Engine", self.start_engine),
                ("engine_stop", "Остановить Engine", self.stop_engine),
                ("cancel_queued", "Удалить ожидающее", self.cancel_queued),
            ],
        )
        self.queue_table = self.table(["ID", "Проект", "Состояние", "Заголовок"], "queue_table")
        form.addWidget(self.queue_table)
        self.engine_label = QLabel("Engine остановлен")
        form.addWidget(self.engine_label)
        page, form = self.page("Результаты")
        self.results_page = page
        self.results_table = self.table(["ID", "Проект", "Состояние", "URL", "Заголовок"], "results_table")
        form.addWidget(self.results_table)
        self.result_detail = QLabel(
            "Неизвестный результат требует ручной проверки. Автоматической повторной отправки нет."
        )
        self.result_detail.setWordWrap(True)
        form.addWidget(self.result_detail)
        window.row_buttons(form, [("open_result", "Открыть публикацию", self.open_result)])
        page, form = self.page("Система")
        self.system_page = page
        window.row_buttons(
            form, [("local_test", "Локальный тест", self.local), ("local_stop", "Отменить тест", self.stop)]
        )
        window.row_buttons(
            form,
            [
                ("system_check", "Проверить систему", self.system_check),
                ("choose_browser", "Выбрать Chrome", self.choose_browser),
            ],
        )
        window.row_buttons(
            form,
            [
                ("download_browser", "Скачать с согласия…", self.download_browser),
                ("clean_temp", "Очистить свои временные файлы", self.clean_temp),
            ],
        )
        self.system_info = QPlainTextEdit()
        self.system_info.setReadOnly(True)
        form.addWidget(self.system_info)
        self.timer = QTimer(window)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(700)
        self.refresh()
        self.system_check()

    def page(self, title):
        page = QWidget()
        layout = QVBoxLayout(page)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        form = QVBoxLayout(body)
        scroll.setWidget(body)
        layout.addWidget(scroll)
        self.window.pages.addTab(page, title)
        return page, form

    def table(self, labels, name):
        table = QTableWidget(0, len(labels))
        table.setObjectName(name)
        table.setHorizontalHeaderLabels(labels)
        table.setSelectionBehavior(table.SelectionBehavior.SelectRows)
        table.setEditTriggers(table.EditTrigger.NoEditTriggers)
        return table

    def emit(self, kind, data=None):
        self.window.events.received.emit(("legacy", (kind, data)))

    def submit(self, coroutine, name="legacy"):
        if self.busy:
            coroutine.close()
            return
        self.busy = True
        self.error = None

        async def work():
            try:
                value = await coroutine
                if self.session:
                    self.emit("recognition", self.session.result.summary())
                return value
            except asyncio.CancelledError:
                self.emit("status", "Операция остановлена; отправленные публикации не отменяются.")
                raise
            except Exception as error:
                # Do not send exception text, request bodies or token-bearing URLs.
                self.emit(
                    "error",
                    {
                        "type": type(error).__name__,
                        "local_test": getattr(error, "safe_details", None),
                        "code": str(error)
                        if isinstance(error, ValueError) and str(error).isupper()
                        else "LEGACY_FAILED",
                    },
                )
            finally:
                self.emit("idle")

        self.future = self.window.worker.submit(work(), name=name, busy=False)

    def identity(self):
        import re

        name = (
            self.project_id.text().strip()
            or re.sub(r"[^A-Za-z0-9_-]", "-", urlsplit(self.url.text()).hostname or "project")[:70]
        )
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", name):
            raise ValueError("INVALID_PROJECT_ID")
        self.project_id.setText(name)
        return name

    def recognize(self):
        name, url = self.identity(), self.url.text().strip()
        kind, cdns = self.kind.currentText(), self.cdns.text().split()

        async def work():
            if self.session:
                await self.session.close()
            if not self.knowledge:
                self.knowledge = KnowledgeBase(self.workspace.root / "knowledge.sqlite")
            self.session = RecognitionSession(
                url,
                self.workspace.root / "profiles" / name,
                knowledge=self.knowledge,
                publication_kind=kind,
                static_origins=[*ResourcePolicy.DEFAULT_STATIC, *cdns] if cdns else None,
            )
            await self.session.start()
            await self.session.recognize()

        self.submit(work())

    def consent(self, title, message):
        return QMessageBox.question(self.window, title, message) == QMessageBox.StandardButton.Yes

    def publish(self):
        if not self.session or not self.consent(
            "Одна тестовая публикация", "Отправить одну настоящую публикацию с указанным текстом?"
        ):
            return
        title, body, name = self.title.text(), self.body.toPlainText(), self.identity()
        self.submit(
            self.session.test_publish(title, body, name, confirmed=True, existing_site=self.existing_site)
        )

    def http_verify(self):
        if not self.session or not self.consent(
            "Отдельная HTTP-проверка", "Создать вторую настоящую публикацию через HTTP с отдельной пометкой?"
        ):
            return
        suffix = uuid.uuid4().hex[:6]
        title, body = (
            self.title.text() + " [HTTP " + suffix + "]",
            self.body.toPlainText() + "\nHTTP " + suffix,
        )

        async def work():
            await self.session.close()
            await self.session.verify_http(title, body, confirmed=True)

        self.submit(work())

    def manual(self):
        if self.session and self.consent(
            "Ручное обучение", "Разрешить ручной вход и одну публикацию? После отправки завершите обучение."
        ):
            self.submit(self.session.begin_manual(self.title.text(), self.body.toPlainText(), confirmed=True))

    def finish_manual(self):
        if self.session:
            self.submit(self.session.finish_manual(self.identity(), existing_site=self.existing_site))

    def login(self):
        if self.session and self.consent(
            "Один вход", "Разрешить одну отправку формы входа в течение двух минут?"
        ):
            self.submit(self.session.authorize_login(confirmed=True))

    def select_form(self):
        key = self.forms.currentText()

        async def work():
            self.session.select(key)
            await self.session.highlight()

        if self.session and key:
            self.submit(work())

    def save(self):
        if not self.session:
            return
        if self.snapshot.get("state") == "HTTP_CANDIDATE" and not self.consent(
            "Непроверенный кандидат", "Сохранить REVIEW-кандидат? Engine не будет его выполнять."
        ):
            return
        path = Path(self.output.text()) / self.identity()
        if path.exists() and any(path.iterdir()):
            raise ValueError("PROJECT_EXISTS")

        async def work():
            self.session.save(path)
            self.workspace.import_project(path)
            self.emit("status", "HTTP-проект сохранён")

        self.submit(work())

    def retrain(self):
        folder = QFileDialog.getExistingDirectory(self.window, "READY-проект для переобучения")
        if folder:
            project = load_project(Path(folder))
            self.existing_site = project.site
            self.project_id.setText(project.site.id)
            first = next(s for s in project.workflow.steps if s.op == "request")
            self.url.setText(
                urljoin(project.site.base_url, first.url) if "{{" not in first.url else project.site.base_url
            )

    def import_project(self):
        folder = QFileDialog.getExistingDirectory(self.window, "Добавить READY-проект")
        if folder:

            async def work():
                self.workspace.import_project(folder)

            self.submit(work())

    def choose_queue(self):
        row = self.projects_table.currentRow()
        if row >= 0:
            self.queue_project.setCurrentText(self.projects_table.item(row, 0).text())
        self.window.pages.setCurrentWidget(self.queue_page)

    def enqueue(self):
        project, title, body = (
            self.queue_project.currentText(),
            self.queue_title.text(),
            self.queue_body.toPlainText(),
        )

        async def work():
            self.bridge.enqueue(project, title, body)

        self.submit(work())

    def start_engine(self):
        self.submit(self.bridge.start(), "engine_start")

    def stop_engine(self):
        self.submit(self.bridge.stop(), "engine_stop")

    def cancel_queued(self):
        row = self.queue_table.currentRow()
        if row >= 0:
            name = self.queue_table.item(row, 0).text()

            async def work():
                self.bridge.cancel_pending(name)

            self.submit(work())

    def open_result(self):
        row = self.results_table.currentRow()
        if row >= 0:
            record = next((r for r in self.rows if r["id"] == self.results_table.item(row, 0).text()), None)
            if record and record["status"] == "SUCCEEDED" and record.get("result_url"):
                QDesktopServices.openUrl(QUrl(record["result_url"]))

    def local(self):
        async def work():
            self.local_report = await local_test(self.workspace, lambda text: self.emit("status", text))
            self.emit("local", self.local_report)

        self.submit(work(), "local_test")

    def stop(self):
        if self.future:
            self.future.cancel()

        async def work():
            if self.session:
                await self.session.close()
                self.session = None

        self.window.worker.submit(work(), name="legacy_stop", busy=False)

    def system_check(self):
        async def work():
            self.emit("system", self.workspace.system_report())

        self.window.worker.submit(work(), name="system", busy=False)

    def choose_browser(self):
        path, _ = QFileDialog.getOpenFileName(self.window, "Chrome / Chromium / Edge")
        if path:
            import os

            find_browser(path)
            os.environ["POSTER_CHROMIUM"] = path
            (self.workspace.root / "browser.json").write_text(json.dumps({"path": path}))
            self.system_check()

    def download_browser(self):
        if self.consent(
            "Скачать Chromium",
            "Скачать дополнительный браузер? Это занимает место и не требуется для встроенного Maker.",
        ):
            self.submit(install_browser(), "download")

    def clean_temp(self):
        if self.consent("Очистка", "Удалить только собственные неиспользуемые временные файлы Poster?"):

            async def work():
                count = self.workspace.clean_temp()
                self.emit("status", "Очищено временных папок: " + str(count))

            self.submit(work())

    def refresh(self):
        if self.refreshing or self.window.closing:
            return
        self.refreshing = True

        async def work():
            try:
                self.emit(
                    "rows",
                    {
                        "rows": self.bridge.rows(),
                        "projects": self.workspace.projects_list(),
                        "running": self.bridge.running,
                    },
                )
            finally:
                self.emit("refresh_done")

        self.window.worker.submit(work(), name="refresh", busy=False)

    def fill_table(self, table, rows):
        selected = (
            table.item(table.currentRow(), 0).text()
            if table.currentRow() >= 0 and table.item(table.currentRow(), 0)
            else None
        )
        table.setRowCount(len(rows))
        for row, values in enumerate(rows):
            for column, value in enumerate(values):
                table.setItem(row, column, QTableWidgetItem(str(value or "")))
            if str(values[0]) == selected:
                table.selectRow(row)

    def handle(self, packet):
        kind, data = packet
        if kind == "idle":
            self.busy = False
        elif kind == "refresh_done":
            self.refreshing = False
        elif kind == "error":
            self.error = data
            self.window.status_label.setText(
                "Сервис Poster 2.0 остановил операцию: "
                + data["code"]
                + ". Проверьте проект и установленный Chrome."
            )
        elif kind == "status":
            self.window.status_label.setText(data)
        elif kind == "system":
            self.system_info.setPlainText(json.dumps(data, ensure_ascii=False, indent=2))
        elif kind == "local":
            self.local_report = data
            self.window.status_label.setText(
                "Локальный тест: браузер, HTTP и Engine подтверждены; сервер остановлен"
            )
        elif kind == "recognition":
            self.snapshot = data
            self.forms.clear()
            self.forms.addItems([f["id"] for f in data.get("forms", [])])
            if data.get("selected"):
                self.forms.setCurrentText(data["selected"])
            self.info.setPlainText(json.dumps(data, ensure_ascii=False, indent=2))
        elif kind == "rows":
            self.rows, self.projects = data["rows"], data["projects"]
            current = self.queue_project.currentText()
            self.queue_project.clear()
            self.queue_project.addItems([p["id"] for p in self.projects if p["ready"]])
            self.queue_project.setCurrentText(current)
            self.fill_table(
                self.projects_table,
                [(p["id"], "HTTP READY" if p["ready"] else "REVIEW") for p in self.projects],
            )
            self.fill_table(
                self.queue_table,
                [
                    (r["id"], r["project_id"], JOB_TEXT.get(r["status"], r["status"]), r["title"])
                    for r in self.rows
                    if r["status"] in {"PENDING", "LEASED", "RUNNING"}
                ],
            )
            self.fill_table(
                self.results_table,
                [
                    (
                        r["id"],
                        r["project_id"],
                        JOB_TEXT.get(r["status"], r["status"]),
                        r.get("result_url"),
                        r["title"],
                    )
                    for r in self.rows
                    if r["status"] not in {"PENDING", "LEASED", "RUNNING"}
                ],
            )
            self.engine_label.setText("Engine работает" if data["running"] else "Engine остановлен")
        state = self.snapshot.get("state")
        enabled = {
            "recognize": not self.busy,
            "publish": state == "DETECTED" and not (self.session and self.session.browser_attempted),
            "http_verify": state == "HTTP_CANDIDATE" and not (self.session and self.session.http_attempted),
            "manual": bool(self.session and not self.session.browser_attempted),
            "finish_manual": bool(self.session and self.session.manual),
            "legacy_save": state in {"HTTP_CANDIDATE", "HTTP_VERIFIED"},
        }
        for name, allowed in enabled.items():
            self.window.controls[name].setEnabled(bool(allowed and not self.busy))

    async def shutdown(self):
        if self.future and not self.future.done():
            self.future.cancel()
            try:
                await asyncio.wrap_future(self.future)
            except asyncio.CancelledError:
                pass
        if self.session:
            await self.session.close()
        await self.bridge.stop()
        if self.knowledge:
            self.knowledge.close()

    def observation(self):
        return {
            "busy": self.busy,
            "error": self.error,
            "state": self.snapshot.get("state"),
            "snapshot": self.snapshot,
            "rows": self.rows,
            "projects": self.projects,
            "local_report": self.local_report,
            "ports": list(self.workspace.test_ports),
            "engine_running": self.bridge.running,
        }
