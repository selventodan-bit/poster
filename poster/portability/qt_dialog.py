"""Preparation tabs inside the existing Maker/Dispatcher, with private native state."""

import asyncio
import json
import os
from pathlib import Path

from PySide6.QtCore import QUrl, QTimer
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from poster.cloud.crypto import recipient
from poster.cloud.desktop import DesktopCloud
from poster.cloud.qt_panel import AsyncBridge
from poster.dispatcher.host import request
from poster.dispatcher.models import MESSAGES
from poster.dispatcher.security import plain_path
from poster.maker.model import GraphError
from poster.maker.profiles import Profiles
from poster.maker.runtime import ERRORS
from poster.maker.storage import atomic, load_graph, save_graph
from poster.remote.registry import read_json
from .network import NetworkProfiles, probe
from .readiness import inspect
from .transfer import export_file, open_session, seal_session


class PreparationDialog(QDialog):
    def __init__(self, parent, *, source, profiles, changed=None, dispatcher_root=None, item=None):
        super().__init__(parent)
        self.setObjectName("poster_preparation")
        self.setWindowTitle("Подготовка проекта к запуску")
        self.resize(760, 620)
        self.source, self.profiles = Path(source), profiles
        self.graph, self.vault = load_graph(self.source)
        from .local import profile_binding

        binding = profile_binding(self.source)
        if binding:
            selected_key = (
                plain_path(binding["key_file"]).read_bytes().strip() if binding.get("key_file") else None
            )
            self.profiles = Profiles(binding["root"], key=selected_key)
            self.profile_key_file = binding.get("key_file")
        self.changed_callback, self.dispatcher_root, self.item = changed, dispatcher_root, item
        self.service = DesktopCloud()
        self.networks = NetworkProfiles()
        self.bridge = AsyncBridge(self)
        self.bridge.completed.connect(self.completed)
        self.callbacks = {}
        layout = QVBoxLayout(self)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)
        self.status = QLabel("Настройки до запуска. Проверка совместимости не означает вход на сайт.")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.profile_tab()
        self.network_tab()
        self.resource_tab()
        self.readiness_tab()
        self.reload_profiles()
        self.reload_networks()
        self.last_report = None
        self.observer = QTimer(self)
        if os.environ.get("POSTER_PREPARATION_TEST_OUTPUT"):
            self.observer.timeout.connect(self.observe)
            self.observer.start(150)

    def page(self, title):
        widget = QWidget()
        form = QFormLayout(widget)
        self.tabs.addTab(widget, title)
        return form

    def button(self, title, name, callback):
        button = QPushButton(title)
        button.setObjectName(name)
        button.clicked.connect(lambda: self.safe(callback))
        return button

    def row(self, form, actions):
        widget = QWidget()
        line = QHBoxLayout(widget)
        for title, name, callback in actions:
            line.addWidget(self.button(title, name, callback))
        form.addRow(widget)

    def safe(self, callback):
        try:
            callback()
        except Exception as exc:
            code = getattr(exc, "code", "PARAMETERS_INVALID")
            self.status.setText({**MESSAGES, **ERRORS}.get(code, "Проверьте выбранные файлы и настройки."))

    def submit(self, name, coroutine, callback=None):
        self.callbacks[name] = callback
        self.status.setText("Проверка…")
        self.bridge.submit(name, coroutine)

    def completed(self, name, value, error):
        if error:
            self.status.setText(
                {**MESSAGES, **ERRORS}.get(error, "Проверка не завершена. Настройки не подтверждены.")
            )
            return
        callback = self.callbacks.pop(name, None)
        self.status.setText("Готово.")
        if callback:
            self.safe(lambda: callback(value))

    def persist(self):
        save_graph(self.source, self.graph, self.vault)
        if self.changed_callback:
            self.changed_callback(self.graph.metadata)
        self.status.setText("Подготовка сохранена. Для Dispatcher обновите immutable версию проекта.")

    def profile_tab(self):
        form = self.page("Браузерные профили")
        self.profile_choice = QComboBox()
        self.profile_choice.setObjectName("preparation_profile")
        form.addRow("Профиль", self.profile_choice)
        self.row(
            form,
            [
                ("Создать", "prepare_profile_create", self.create_profile),
                ("Переименовать", "prepare_profile_rename", self.rename_profile),
                ("Независимая копия", "prepare_profile_clone", self.clone_profile),
            ],
        )
        self.row(
            form,
            [
                ("Выбрать для проекта", "prepare_profile_select", self.select_profile),
                ("Расположение", "prepare_profile_location", self.profile_location),
                ("Очистить", "prepare_profile_clear", self.clear_profile),
                ("Удалить…", "prepare_profile_delete", self.delete_profile),
            ],
        )
        self.row(
            form,
            [
                ("Другой каталог…", "prepare_profile_folder", self.profile_folder),
                ("Выбрать session.key…", "prepare_profile_key", self.profile_key),
            ],
        )
        self.row(
            form,
            [
                ("Открытый ID получателя…", "prepare_receiver_public", self.public_identity),
                ("Экспорт сессии…", "prepare_profile_export", self.export_session),
                ("Импорт сессии…", "prepare_profile_import", self.import_session),
            ],
        )
        self.origins = QLineEdit()
        self.origins.setObjectName("session_origins")
        self.origins.setPlaceholderText("https://site.example; https://second.example (только эти origins)")
        form.addRow("Разрешённые сайты", self.origins)
        self.ttl = QSpinBox()
        self.ttl.setRange(5, 900)
        self.ttl.setValue(120)
        form.addRow("Разрешение, секунд", self.ttl)
        self.permitted_copies = QSpinBox()
        self.permitted_copies.setRange(1, 1000)
        self.permitted_copies.setValue(1)
        form.addRow("Разрешённых независимых копий", self.permitted_copies)
        self.row(
            form,
            [
                ("Разрешить перенос Agent…", "prepare_agent_consent", self.agent_consent),
                ("Отозвать разрешение…", "prepare_agent_revoke", self.agent_revoke),
            ],
        )
        note = QLabel(
            "Переносятся только поддерживаемые cookies/localStorage. Вход может потребовать повторной авторизации. "
            "IndexedDB, sessionStorage, passkeys и ключи ОС не переносятся. После экспорта/возможной отправки данные отозвать нельзя. "
            "Colab/Molab: чувствительные сессии запрещены. Для Agent используется отдельное явное согласие перед очередью."
        )
        note.setWordWrap(True)
        form.addRow(note)

    def reload_profiles(self):
        self.profile_choice.clear()
        self.profile_choice.addItem("Чистая независимая сессия", None)
        for key, name in self.profiles.list():
            self.profile_choice.addItem(name, key)
        index = self.profile_choice.findData(self.graph.metadata.get("browser_profile"))
        self.profile_choice.setCurrentIndex(max(0, index))
        if self.profiles.unreadable:
            self.status.setText(
                "Есть недоступные профили. Выберите правильный локальный ключ; остальные профили сохранены."
            )

    def selected_profile(self):
        key = self.profile_choice.currentData()
        if not key:
            raise GraphError("PROFILE_MISSING")
        return key

    def create_profile(self):
        name, accepted = QInputDialog.getText(self, "Создать профиль", "Название")
        if accepted:
            key = self.profiles.create(name)
            self.reload_profiles()
            self.profile_choice.setCurrentIndex(self.profile_choice.findData(key))

    def rename_profile(self):
        key = self.selected_profile()
        name, accepted = QInputDialog.getText(
            self, "Переименовать профиль", "Название", text=self.profiles.open(key).data["name"]
        )
        if accepted:
            self.profiles.rename(key, name)
            self.reload_profiles()

    def clone_profile(self):
        key = self.profiles.clone(self.selected_profile())
        self.reload_profiles()
        self.profile_choice.setCurrentIndex(self.profile_choice.findData(key))

    def select_profile(self):
        from .local import profile_binding

        key = self.profile_choice.currentData()
        profile_binding(
            self.source,
            value=dict(
                root=str(self.profiles.root), profile_id=key, key_file=getattr(self, "profile_key_file", None)
            )
            if key
            else None,
        )
        if key:
            self.graph.metadata["browser_profile"] = key
        else:
            self.graph.metadata.pop("browser_profile", None)
        self.persist()

    def profile_location(self):
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.profiles.root)))
        self.status.setText(str(self.profiles.root))

    def confirmed(self, title, text):
        return QMessageBox.question(self, title, text) == QMessageBox.StandardButton.Yes

    def clear_profile(self):
        if self.confirmed(
            "Очистить профиль", "Удалить сохранённые cookies и localStorage выбранного профиля?"
        ):
            self.profiles.clear(self.selected_profile())

    def delete_profile(self):
        if self.confirmed("Удалить профиль", "Удалить выбранный зашифрованный профиль?"):
            self.profiles.delete(self.selected_profile())
            self.reload_profiles()

    def profile_folder(self):
        path = QFileDialog.getExistingDirectory(self, "Локальные профили", str(self.profiles.root))
        if path:
            self.profiles = Profiles(path)
            self.profile_key_file = None
            self.reload_profiles()

    def profile_key(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Локальный ключ этих профилей", "", "Ключ (session.key *.key)"
        )
        if path:
            if plain_path(path).stat().st_size > 1000:
                raise GraphError("PROFILE_INVALID")
            self.profiles = Profiles(self.profiles.root, key=plain_path(path).read_bytes().strip())
            self.profile_key_file = path
            self.reload_profiles()

    def public_identity(self):
        path, _ = QFileDialog.getSaveFileName(self, "Открытый ID этого получателя", "", "JSON (*.json)")
        if path:
            atomic(Path(path), json.dumps(recipient(self.service.store).public()).encode())

    def trusted_peer(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Закреплённый открытый ID другой машины", "", "JSON (*.json)"
        )
        if not path:
            return None
        value = read_json(path)
        peer = value.get("peer", value)
        entered, accepted = QInputDialog.getText(
            self, "Проверка доверия", "Полный ID, проверенный независимым доверенным способом"
        )
        if not accepted or entered.strip() != peer["id"]:
            raise GraphError("PROFILE_PERMISSION")
        return peer

    def export_session(self):
        peer = self.trusted_peer()
        if not peer:
            return
        if not self.confirmed(
            "Передать данные сессии",
            "Разрешить передачу cookies/localStorage только указанных сайтов выбранной доверенной машине? После экспорта отзыв невозможен; файл истечёт по сроку.",
        ):
            return
        packet = seal_session(
            recipient(self.service.store),
            peer,
            self.profiles.open(self.selected_profile()).data["state"],
            origins=[s.strip() for s in self.origins.text().split(";") if s.strip()],
            environment="windows",
            trusted=True,
            ttl=self.ttl.value(),
        )
        path, _ = QFileDialog.getSaveFileName(
            self, "Приватный зашифрованный экспорт", "", "Poster session (*.postersession)"
        )
        if path:
            export_file(path, packet)
            self.status.setText(
                "Экспорт зашифрован для проверенного получателя. Храните файл приватно. Срок ограничен."
            )

    def import_session(self):
        peer = self.trusted_peer()
        if not peer:
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Зашифрованный экспорт сессии", "", "Poster session (*.postersession)"
        )
        if not path or not self.confirmed(
            "Импорт сессии",
            "Импортировать разрешённые cookies/localStorage доверенного отправителя в новый независимый профиль?",
        ):
            return
        if plain_path(path).stat().st_size > 2_000_000:
            raise GraphError("PROFILE_LIMIT")
        packet = json.loads(plain_path(path).read_bytes())
        state = open_session(recipient(self.service.store), peer, packet, environment="windows")
        key = self.profiles.create("Импортированная сессия")
        self.profiles.open(key).commit(state)
        self.reload_profiles()
        self.profile_choice.setCurrentIndex(self.profile_choice.findData(key))
        self.status.setText("Импортирован новый независимый профиль. Проверьте вход на нужном сайте.")

    def agent_consent(self):
        if not self.dispatcher_root or not self.item:
            self.status.setText(
                "В Dispatcher выберите immutable проект и доверенного Linux Agent для согласия."
            )
            return

        async def work():
            agents = await asyncio.to_thread(request, self.dispatcher_root, "remote_agents")
            return [a for a in agents if a["state"] == "TRUSTED"]

        def choose(agents):
            labels = [a["name"] + " · " + a["id"] for a in agents]
            label, accepted = QInputDialog.getItem(
                self, "Получатель сессии", "Проверьте полный Agent ID", labels, 0, False
            )
            if not accepted:
                return
            agent = agents[labels.index(label)]
            if not self.confirmed(
                "Согласие на передачу",
                "Передать выбранные cookies/localStorage этому Agent для одной копии выбранной immutable версии?",
            ):
                return
            origins = [s.strip() for s in self.origins.text().split(";") if s.strip()]
            profile_id, ttl, max_runs = (
                self.selected_profile(),
                self.ttl.value(),
                self.permitted_copies.value(),
            )

            async def grant():
                return await asyncio.to_thread(
                    request,
                    self.dispatcher_root,
                    "session_permission_create",
                    agent_id=agent["id"],
                    project_id=self.item["project_id"],
                    version=self.item["version"],
                    profile_root=str(self.profiles.root),
                    profile_id=profile_id,
                    origins=origins,
                    key_file=getattr(self, "profile_key_file", None),
                    ttl=ttl,
                    max_runs=max_runs,
                    confirmed=True,
                )

            self.submit(
                "consent",
                grant(),
                lambda permit: self.status.setText(
                    "Разрешение для очереди: "
                    + permit["id"]
                    + "; действует ограниченное время и число копий. Выберите его в окне запуска."
                ),
            )

        self.submit("agents", work(), choose)

    def agent_revoke(self):
        if not self.dispatcher_root:
            return

        async def work():
            return await asyncio.to_thread(request, self.dispatcher_root, "session_permission_list")

        def choose(rows):
            rows = [r for r in rows if not r["revoked"]]
            values = [r["id"] + " · " + r["agent_id"] for r in rows]
            value, accepted = QInputDialog.getItem(
                self, "Отзыв разрешения", "Выберите разрешение", values, 0, False
            )
            if accepted:

                async def revoke():
                    return await asyncio.to_thread(
                        request,
                        self.dispatcher_root,
                        "session_permission_revoke",
                        id=rows[values.index(value)]["id"],
                    )

                self.submit(
                    "revoke",
                    revoke(),
                    lambda result: self.status.setText(
                        "Разрешение отозвано. "
                        + (
                            "Данные могли быть переданы; для отзыва входа выйдите на сайте."
                            if result["may_have_been_disclosed"]
                            else "Передача не начиналась."
                        )
                    ),
                )

        self.submit("permits", work(), choose)

    def network_tab(self):
        form = self.page("Сеть")
        self.network_choice = QComboBox()
        self.network_choice.setObjectName("preparation_network")
        form.addRow("Приватный профиль", self.network_choice)
        self.network_name, self.proxy_host, self.proxy_user, self.proxy_password = (
            QLineEdit() for _ in range(4)
        )
        self.proxy_password.setEchoMode(QLineEdit.EchoMode.Password)
        self.proxy_kind = QComboBox()
        self.proxy_kind.addItems(["direct", "http", "https", "socks5"])
        self.proxy_port = QSpinBox()
        self.proxy_port.setRange(1, 65535)
        self.proxy_port.setValue(8080)
        for title, edit in [
            ("Название", self.network_name),
            ("Тип", self.proxy_kind),
            ("Host (без URL/credentials)", self.proxy_host),
            ("Порт", self.proxy_port),
            ("Пользователь", self.proxy_user),
            ("Пароль", self.proxy_password),
        ]:
            form.addRow(title, edit)
        self.override = QCheckBox("Разрешить явную замену при запуске")
        form.addRow(self.override)
        self.row(
            form,
            [
                ("Сохранить профиль", "prepare_network_save", self.save_network),
                ("Выбрать для проекта", "prepare_network_select", self.select_network),
                ("Удалить", "prepare_network_delete", self.delete_network),
            ],
        )
        self.proxy_url = QLineEdit("https://example.org/")
        form.addRow("Нейтральный URL проверки (GET)", self.proxy_url)
        self.row(form, [("Проверить подключение", "prepare_network_probe", self.probe_network)])
        note = QLabel(
            "Credentials приватны и не сохраняются в граф/Drive/notebook. Qt browser: только direct. Chromium SOCKS5: без авторизации. HTTP backend: все четыре режима. TLS проверяется."
        )
        note.setWordWrap(True)
        form.addRow(note)

    def reload_networks(self):
        self.network_choice.clear()
        self.network_choice.addItem("Прямое соединение без binding", None)
        for row in self.networks.rows():
            self.network_choice.addItem(row["name"] + " · " + row["kind"], row["id"])
        self.network_choice.setCurrentIndex(
            max(0, self.network_choice.findData(self.graph.metadata.get("poster.network", {}).get("binding")))
        )

    def save_network(self):
        kind = self.proxy_kind.currentText()
        config = (
            dict(kind=kind)
            if kind == "direct"
            else dict(
                kind=kind,
                host=self.proxy_host.text().strip(),
                port=self.proxy_port.value(),
                username=self.proxy_user.text(),
                password=self.proxy_password.text(),
            )
        )
        key = self.networks.put(self.network_name.text(), config)
        self.proxy_password.clear()
        self.reload_networks()
        self.network_choice.setCurrentIndex(self.network_choice.findData(key))

    def select_network(self):
        key = self.network_choice.currentData()
        if key:
            self.graph.metadata["poster.network"] = dict(
                binding=key, allow_override=self.override.isChecked()
            )
        else:
            self.graph.metadata.pop("poster.network", None)
        self.persist()

    def delete_network(self):
        if self.network_choice.currentData() and self.confirmed(
            "Удалить сетевой профиль",
            "Удалить приватную конфигурацию и credentials? Проекты с этим binding потребуют настройки.",
        ):
            self.networks.delete(self.network_choice.currentData())
            self.reload_networks()

    def probe_network(self):
        config = (
            self.networks.get(self.network_choice.currentData())
            if self.network_choice.currentData()
            else None
        )
        self.submit(
            "network",
            probe(config, self.proxy_url.text()),
            lambda value: self.status.setText(
                "Подключение подтверждено: " + str(value["status"]) + ". Вход на сайт проверяйте отдельно."
            ),
        )

    def resource_tab(self):
        form = self.page("Ресурсы")
        self.resources = QListWidget()
        self.resources.setObjectName("preparation_resources")
        self.resource_names = set(self.graph.metadata.get("poster.resources", {}))
        self.resources.addItems(sorted(self.resource_names))
        form.addRow(self.resources)
        self.row(
            form,
            [
                ("Выбрать файлы…", "prepare_resource_files", lambda: self.add_resources(False)),
                ("Выбрать каталог…", "prepare_resource_directory", lambda: self.add_resources(True)),
                ("Убрать выбранные", "prepare_resource_remove", self.remove_resources),
            ],
        )
        note = QLabel(
            "Только явно выбранные данные: 128 файлов, 256 MiB/файл, 512 MiB/пакет. Приватные каталоги, ключи и ссылки запрещены. Большие Files.Copy/Move потоковые; ReadText/CSV/XLSX сохраняют меньшие лимиты."
        )
        note.setWordWrap(True)
        form.addRow(note)

    def add_resources(self, directory):
        paths = (
            [QFileDialog.getExistingDirectory(self, "Выбрать разрешённый каталог", str(self.source.parent))]
            if directory
            else QFileDialog.getOpenFileNames(
                self, "Явные ресурсы", str(self.source.parent), "Все файлы (*)"
            )[0]
        )
        paths = [p for p in paths if p]
        if not paths:
            return

        def show(report):
            text = "\n".join(
                e["name"] + " — " + str(e["size"]) + " bytes · " + e["sha256"] for e in report["files"]
            )
            if self.confirmed(
                "Предварительный список ресурсов",
                text + "\nВсего " + str(report["total_bytes"]) + " bytes. Включить именно эти файлы?",
            ):
                self.submit(
                    "resources", self.service.include_resources(self.source, paths), self.resources_added
                )

        self.submit("preview", self.service.preview_resources(self.source, paths), show)

    def resources_added(self, names):
        from .blobs import FileBlob

        self.resource_names.update(names)
        self.graph.metadata["poster.resources"] = {
            n: FileBlob.capture(self.source.parent / n).sha256 for n in sorted(self.resource_names)
        }
        self.resources.clear()
        self.resources.addItems(sorted(self.resource_names))
        self.persist()

    def remove_resources(self):
        for selected in self.resources.selectedItems():
            self.resource_names.discard(selected.text())
        self.resources_added([])

    def readiness_tab(self):
        form = self.page("Готовность")
        self.target = QComboBox()
        self.target.setObjectName("readiness_target")
        for title, key in [
            ("Windows", "windows"),
            ("Linux/VPS", "linux"),
            ("Colab", "colab"),
            ("Molab", "molab"),
            ("Generic Jupyter", "generic"),
        ]:
            self.target.addItem(title, key)
        self.target_provider = QComboBox()
        self.target_provider.addItems(["chromium", "qt"])
        self.readiness_agent = QComboBox()
        self.readiness_agent.addItem("Agent ещё не выбран", None)
        form.addRow("Доверенный Linux Agent", self.readiness_agent)
        if self.dispatcher_root:

            async def agents():
                return await asyncio.to_thread(request, self.dispatcher_root, "remote_agents")

            def available(rows):
                for row in rows:
                    if row["state"] == "TRUSTED":
                        self.readiness_agent.addItem(row["name"] + " · " + row["id"], row["id"])

            self.submit("readiness_agents", agents(), available)
        self.actual = QCheckBox("Проверить текущую локальную среду (запуск браузера и GET)")
        self.actual.setChecked(True)
        form.addRow("Место исполнения", self.target)
        form.addRow("BrowserProvider", self.target_provider)
        form.addRow(self.actual)
        self.report = QPlainTextEdit()
        self.report.setReadOnly(True)
        self.report.setObjectName("readiness_report")
        self.row(
            form, [("Проверить готовность проекта к запуску", "prepare_readiness", self.check_readiness)]
        )
        form.addRow(self.report)

    def check_readiness(self):
        profile = (
            self.profiles.open(self.profile_choice.currentData())
            if self.profile_choice.currentData()
            else None
        )
        target, actual = self.target.currentData(), self.actual.isChecked()
        provider, probe_url = self.target_provider.currentText(), self.proxy_url.text()
        agent_id = self.readiness_agent.currentData()

        async def work():
            remote = None
            if target == "linux" and actual and agent_id and self.dispatcher_root:
                remote = await asyncio.to_thread(
                    request, self.dispatcher_root, "remote_diagnose", id=agent_id
                )
                if remote.get("online") and remote.get("tls_verified"):
                    diagnostic = await asyncio.to_thread(
                        request, self.dispatcher_root, "remote_test", id=agent_id, browser=True
                    )
                    for _ in range(300):
                        result = await asyncio.to_thread(
                            request, self.dispatcher_root, "remote_test_status", job=diagnostic["job"]
                        )
                        if result["status"] not in {"PENDING", "LEASED", "RUNNING"}:
                            remote.update(
                                file_returned=result["file_returned"], browser_tested=result["file_returned"]
                            )
                            break
                        await asyncio.sleep(0.1)
            return await inspect(
                self.source,
                target,
                actual=actual,
                provider=provider,
                profile=profile,
                trusted=target == "windows",
                probe_url=probe_url if actual else None,
                remote=remote,
            )

        def show(value):
            self.last_report = value
            labels = dict(
                READY="Готовность подтверждена текущими проверками",
                BLOCKED="Нужно исправить",
                NOT_VERIFIED="Фактическая среда ещё не проверена",
            )
            self.report.setPlainText(
                labels[value["status"]]
                + "\nСтатическая совместимость: "
                + ("да" if value["static_compatible"] else "нет")
                + "\nФактическая готовность: "
                + ("подтверждена" if value["runtime_verified"] else "не подтверждена")
                + "\nКубиков в registry: "
                + str(value["registry_actions"])
                + "\nРесурсы: "
                + str(value["resource_count"])
                + ", bytes: "
                + str(value["resource_bytes"])
                + "\nВерхний предел одновременных копий: "
                + str(value["max_parallel"])
                + "\nСеть: "
                + value["network_kind"]
                + "\n\n"
                + "\n".join(
                    readiness_message(i)
                    for i in value["static_issues"] + value["runtime_issues"] + value["pending"]
                )
            )

        self.submit("readiness", work(), show)

    def closeEvent(self, event):
        self.observer.stop()
        self.bridge.close(self.service)
        super().closeEvent(event)

    def done(self, code):
        self.observer.stop()
        self.bridge.close(self.service)
        super().done(code)

    def observe(self):
        """Opt-in read-only control geometry; no inputs, cookies or credentials."""
        target = os.environ.get("POSTER_PREPARATION_TEST_OUTPUT")
        if not target:
            return
        points = {}
        for widget in self.findChildren(QWidget):
            if widget.objectName() and widget.isVisible():
                points[widget.objectName()] = widget.mapToGlobal(widget.rect().center()).toTuple()
        for index in range(self.tabs.count()):
            points["preparation_tab_" + str(index)] = (
                self.tabs.tabBar().mapToGlobal(self.tabs.tabBar().tabRect(index).center()).toTuple()
            )
        atomic(
            Path(target),
            json.dumps(
                dict(
                    visible=self.isVisible(),
                    pid=os.getpid(),
                    positions=points,
                    runtime_verified=bool(self.last_report and self.last_report["runtime_verified"]),
                    static_compatible=bool(self.last_report and self.last_report["static_compatible"]),
                )
            ).encode(),
        )


def readiness_message(issue):
    code = issue["code"]
    text = {
        "ACTION_UNSUPPORTED": "Кубик не поддерживается выбранной версией.",
        "RESOURCE_MISSING_OR_CHANGED": "Ресурс отсутствует или отличается от закреплённого SHA256.",
        "MISSING_DEPENDENCY": "Входной файл не включён в ресурсы проекта.",
        "ABSOLUTE_PATH": "Замените абсолютный путь переносимым относительным путём.",
        "DYNAMIC_DEPENDENCY": "Путь зависит от входов; проверьте его после задания значений.",
        "GENERATED_DEPENDENCY": "Файл создаётся графом; проверьте ветвление и порядок кубиков.",
        "IN_MEMORY_ACTION_LIMIT": "Для этого файла кубик превышает ограничение RAM. Используйте потоковое Copy/Move.",
        "RUNTIME_NOT_VERIFIED": "Текущая среда исполнения ещё не проверена.",
        "CLOUD_PROVIDER_NOT_EXECUTED": "Внешний аккаунт Colab/Molab не проверен; откройте интерактивную сессию.",
        "REMOTE_RUNTIME_NOT_TESTED": "Не подтверждено выполнение диагностического графа на Agent.",
        "REMOTE_PROJECT_NETWORK_UNVERIFIED": "Проверьте доступ Agent к внешнему сайту проекта отдельно.",
        "EXTERNAL_DEPENDENCIES_UNVERIFIED": "Не подтверждено подключение к внешнему сайту.",
        "REAUTHORIZATION_MAY_BE_REQUIRED": "Может потребоваться повторный вход на сайт в выбранной среде.",
        "SECRET_REQUIRED": "Настройте защищённое значение: " + issue.get("ref", ""),
        "SOURCE_CHANGED": "Проект изменён во время проверки. Проверьте готовность заново.",
    }.get(code, {**MESSAGES, **ERRORS}.get(code, code))
    return "• " + text + (" Файл: " + issue["resource"] if issue.get("resource") else "")
