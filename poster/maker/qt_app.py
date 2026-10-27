"""Single-window Qt Maker; UI on Qt's thread, existing Python services on a worker."""

import json
import os
from pathlib import Path
import tempfile

from PySide6.QtCore import QByteArray, QObject, Qt, QTimer, Signal, qVersion
from PySide6.QtGui import QAction, QKeySequence, QCursor
from PySide6.QtWidgets import QApplication, QDockWidget, QFileDialog, QHBoxLayout, QInputDialog
from PySide6.QtWidgets import QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMessageBox
from PySide6.QtWidgets import (
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTabWidget,
    QToolBar,
    QVBoxLayout,
    QWidget,
    QComboBox,
    QToolButton,
)

from ..auth import ensure_local_key
from ..recorder.workspace import Workspace
from .actions import default_registry
from .controller import MakerController, Worker
from .fixtures import serve_maker
from .model import Document, Graph, GraphError, graph_secret_refs
from .profiles import Profiles
from .qt_browser import BrowserBridge, BrowserPane, EmbeddedProvider, configure_browser
from .qt_canvas import GraphView
from .qt_tools import ToolsMixin, display, parse
from .runtime import BrowserSession, ERRORS
from .storage import Vault, atomic, load_graph, save_graph

TEXT = {
    **ERRORS,
    "FILE_INVALID": "Файл не является поддерживаемым графом Maker. READY-проекты открываются в разделе «Проекты».",
    "TAB_ATTACH_FAILED": "Не удалось записать новую вкладку. Проверьте граф.",
    "POPUP_ANCHOR_MISSING": "Popup открыт без записанного вызывающего действия. Проверьте WaitPopup.",
    "PROFILE_INVALID": "Профиль повреждён или ключ не подходит. Выберите другой профиль.",
    "RUN_BUSY": "Дождитесь завершения операции или остановите исполнение.",
    "DEBUG_STATE_INVALID": "Сначала приостановите исполнение; на паузе доступны параметры, переменные и breakpoint.",
    "RECORD_EVENT_FAILED": "Одно действие не записано. Проверьте граф перед запуском.",
    "NAVIGATION_NOT_RECORDED": "Переход не записан. Добавьте открытие URL или ожидание вручную.",
}


class Events(QObject):
    received = Signal(object)


class MakerWindow(QMainWindow, ToolsMixin):
    def __init__(self, port, *, workspace=None):
        super().__init__()
        self.setWindowTitle("Poster 3.0 — ProjectMaker")
        self.setObjectName("poster_maker")
        screen = QApplication.primaryScreen().availableGeometry()
        # A fixed 1050px minimum placed the right dock's scroll button beyond a
        # 1024px desktop. Keep every native control reachable on smaller screens.
        self.setMinimumSize(min(1050, screen.width() - 30), min(650, screen.height() - 50))
        self.resize(min(1450, screen.width() - 30), min(940, screen.height() - 50))
        from ..product.paths import maker_root

        self.workspace = workspace or Workspace(maker_root())
        ensure_local_key(self.workspace.root / "private")
        self.settings = self.workspace.root / "maker-qt-layout.json"
        self.directory = self.workspace.root / "maker-projects"
        self.directory.mkdir(exist_ok=True)
        self.registry = default_registry()
        self.document, self.vault = Document(), Vault()
        self.path = None
        self.workdirs = {}
        self.busy = self.recording = self.closing = False
        self.close_ready = False
        self.run_state = self.current_node = self.pause_reason = None
        self.last_error = self.last_pick = self.selected_id = None
        self.live_values, self.trace, self.controls = {}, [], {}
        self.demo_context = self.demo_server = self.demo_base = None
        self.events = Events()
        self.events.received.connect(self.handle)
        self.worker = Worker(lambda kind, data: self.events.received.emit((kind, data)))
        self.browser = BrowserPane()
        self.bridge = BrowserBridge(self.browser, port)
        self.controller = MakerController(
            lambda profile: BrowserSession(False, provider=EmbeddedProvider(self.bridge), profile=profile),
            lambda kind, data: self.events.received.emit((kind, data)),
        )
        self.profiles = Profiles(self.workspace.root / "browser-profiles")
        self.pages = QTabWidget()
        self.pages.setObjectName("main_pages")
        self.setCentralWidget(self.pages)
        self.maker_page = QWidget()
        form = QVBoxLayout(self.maker_page)
        form.setContentsMargins(0, 0, 0, 0)
        self.project_name = QLineEdit(self.document.graph.name)
        self.project_name.setObjectName("project_name")
        self.project_name.editingFinished.connect(self.rename_project)
        form.addWidget(self.project_name)
        self.splitter = QSplitter(Qt.Orientation.Vertical)
        self.splitter.setObjectName("browser_graph_splitter")
        self.canvas = GraphView(self.document, self.editable)
        self.canvas.selected.connect(self.selection_changed)
        self.canvas.changed.connect(self.changed)
        self.canvas.problem.connect(self.show_error)
        self.splitter.addWidget(self.browser)
        self.splitter.addWidget(self.canvas)
        self.splitter.setSizes([400, 320])
        form.addWidget(self.splitter)
        self.pages.addTab(self.maker_page, "ProjectMaker")
        self.build_toolbar()
        library = QWidget()
        form = QVBoxLayout(library)
        self.action_category = QComboBox()
        self.action_category.setObjectName("action_category")
        for title, prefixes in [
            ("Все", ""),
            ("HTTP", "HTTP."),
            ("JSON", "JSON."),
            ("Списки", "Lists."),
            ("Таблицы", ("Tables.", "CSV.", "Excel.")),
            ("Файлы", "Files."),
            ("Проекты", "Project."),
            ("Браузер", "Browser."),
            ("Переменные", "Variables."),
            ("Логика", ("Logic.", "Start", "End")),
            ("Строки", "Strings."),
        ]:
            self.action_category.addItem(title, prefixes)
        self.action_category.currentIndexChanged.connect(
            lambda: self.filter_actions(self.action_search.text())
        )
        form.addWidget(self.action_category)
        self.action_search = QLineEdit()
        self.action_search.setObjectName("action_search")
        self.action_search.setPlaceholderText("Быстро добавить действие…")
        self.action_search.textChanged.connect(self.filter_actions)
        form.addWidget(self.action_search)
        self.action_list = QListWidget()
        self.action_list.setObjectName("action_library")
        for action in self.registry.actions.values():
            item = QListWidgetItem(action.label)
            item.setData(Qt.ItemDataRole.UserRole, action.name)
            item.setToolTip(action.name)
            self.action_list.addItem(item)
        self.action_list.itemDoubleClicked.connect(
            lambda item: self.guard(lambda: self.add_action(item.data(Qt.ItemDataRole.UserRole)))
        )
        form.addWidget(self.action_list)
        self.row_buttons(form, [("add", "Добавить", self.add_selected_action)])
        self.library_dock = self.dock("library", "Библиотека", library, Qt.DockWidgetArea.LeftDockWidgetArea)
        self.tools_dock = self.dock(
            "tools", "Свойства и инструменты", self.build_tools(), Qt.DockWidgetArea.RightDockWidgetArea
        )
        self.log = QPlainTextEdit()
        self.log.setObjectName("execution_log")
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(1500)
        self.log_dock = self.dock(
            "log", "Журнал выполнения", self.log, Qt.DockWidgetArea.BottomDockWidgetArea
        )
        self.resizeDocks([self.library_dock, self.tools_dock], [175, 315], Qt.Orientation.Horizontal)
        self.resizeDocks([self.log_dock], [130], Qt.Orientation.Vertical)
        self.status_label = QLabel(
            "Откройте сайт или нажмите «Демо-сайт». Браузер и граф работают в одном окне."
        )
        self.status_label.setObjectName("status")
        self.statusBar().addWidget(self.status_label, 1)
        from .qt_legacy import LegacyPanel

        self.legacy = LegacyPanel(self)
        self.pages.currentChanged.connect(self.page_changed)
        self.browser.navigate_requested.connect(lambda url: self.guard(self.open_url))
        self.refresh_variables()
        self.restore_settings()
        self.resize(min(self.width(), screen.width() - 30), min(self.height(), screen.height() - 50))
        self.update_controls()
        QTimer.singleShot(0, self.canvas.fit)
        self.observer = QTimer(self)
        if os.environ.get("POSTER_GUI_TEST_OUTPUT"):
            self.observer.timeout.connect(self.observe)
            self.observer.start(100)
        self.observation_browser = {}
        self.probe_busy = False
        self.probe_serial = 0
        self.build_cloud_menu()
        preparation = self.menuBar().addMenu("Подготовка проекта")
        action = preparation.addAction("Профили, сеть, ресурсы и готовность…")
        action.setObjectName("maker_preparation")
        action.setShortcut("Ctrl+Shift+P")
        action.triggered.connect(self.open_preparation)
        from ..product.qt import install

        install(self)

    def guard(self, operation):
        try:
            return operation()
        except Exception as error:
            self.show_error(error)

    def make_button(self, name, label, operation):
        button = QPushButton(label)
        button.setObjectName(name)
        button.clicked.connect(lambda checked=False: self.guard(operation))
        self.controls[name] = button
        return button

    def row_buttons(self, form, specs):
        row = QHBoxLayout()
        for name, label, callback in specs:
            row.addWidget(self.make_button(name, label, callback))
        form.addLayout(row)

    def dock(self, name, title, widget, area):
        dock = QDockWidget(title, self)
        dock.setObjectName(name + "_dock")
        dock.setFeatures(
            QDockWidget.DockWidgetFeature.DockWidgetMovable | QDockWidget.DockWidgetFeature.DockWidgetClosable
        )
        dock.setWidget(widget)
        self.addDockWidget(area, dock)
        self.menuBar().addAction(dock.toggleViewAction())
        return dock

    def toolbar(self, name, specs):
        toolbar = QToolBar(name)
        toolbar.setObjectName(name)
        self.addToolBar(toolbar)
        for key, text, callback, shortcut in specs:
            action = QAction(text, self)
            action.setObjectName(key)
            action.triggered.connect(lambda checked=False, cb=callback: self.guard(cb))
            if shortcut:
                action.setShortcut(QKeySequence(shortcut))
            toolbar.addAction(action)
            self.controls[key] = action
        return toolbar

    def open_dispatcher(self):
        import subprocess
        from ..dispatcher.launcher import command, environment, process_options

        try:
            subprocess.Popen(
                command("gui"),
                env=environment(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **process_options(),
            )
        except Exception:
            self.statusBar().showMessage("Не удалось открыть Dispatcher. Распакуйте полный ZIP этой версии.")

    def build_toolbar(self):
        self.toolbar(
            "project_toolbar",
            [
                ("new", "Новый", self.new, "Ctrl+N"),
                ("open", "Открыть…", self.open, "Ctrl+O"),
                ("save", "Сохранить", self.save, "Ctrl+S"),
                ("save_as", "Сохранить как…", lambda: self.save(True), "Ctrl+Shift+S"),
                ("demo", "Демо-сайт", self.demo, ""),
                ("example", "Пример с циклом", self.example, ""),
                ("data_examples", "Примеры 3B.2…", self.data_examples, ""),
                ("dispatcher", "Dispatcher", self.open_dispatcher, ""),
                ("cloud_save", "Сохранить на Google Drive", lambda: self.open_cloud(publish=True), ""),
            ],
        )
        self.addToolBarBreak()
        self.toolbar(
            "execution_toolbar",
            [
                ("record", "Начать запись", self.record, ""),
                ("record_resume", "Продолжить запись", lambda: self.record(True), ""),
                ("record_stop", "Остановить запись", self.stop_recording, ""),
                ("run", "Запуск / продолжить", self.run, "F5"),
                ("step", "Шаг", lambda: self.run(step=True), "F10"),
                ("pause", "Пауза", self.pause, "F6"),
                ("stop", "Остановить", self.stop, "Shift+F5"),
            ],
        )
        self.addToolBarBreak()
        toolbar = self.toolbar(
            "edit_toolbar",
            [
                ("undo", "Отменить", self.undo, "Ctrl+Z"),
                ("redo", "Повторить", self.redo, "Ctrl+Y"),
                ("copy", "Копировать", lambda: self.canvas.copy(), "Ctrl+D"),
                ("delete", "Удалить", lambda: self.canvas.delete(), ""),
                ("fit", "Весь граф", lambda: self.canvas.fit(), ""),
                ("breakpoint", "Breakpoint", self.breakpoint, "F9"),
                ("selected_run", "Выполнить кубик", self.run_selected, ""),
            ],
        )
        self.node_search = QLineEdit()
        self.node_search.setObjectName("node_search")
        self.node_search.setPlaceholderText("Найти кубик: название / тип")
        self.node_search.returnPressed.connect(lambda: self.canvas.find(self.node_search.text()))
        toolbar.addWidget(self.node_search)

    def build_cloud_menu(self):
        menu = self.menuBar().addMenu("Google Drive")
        menu.setObjectName("google_drive_menu")
        for key, title, callback in [
            ("connect", "Подключить Google Drive", lambda: self.open_cloud()),
            ("publish", "Сохранить проект на Google Drive", lambda: self.open_cloud(publish=True)),
            ("open", "Открыть проект с Google Drive", lambda: self.open_cloud(browse=True)),
            ("update", "Обновить облачную версию", lambda: self.open_cloud(publish=True)),
            ("folder", "Открыть папку проекта…", lambda: self.open_cloud()),
            ("history", "История версий…", lambda: self.open_cloud()),
            ("sync", "Синхронизировать", lambda: self.open_cloud()),
        ]:
            action = menu.addAction(title)
            action.setObjectName("maker_cloud_" + key)
            action.triggered.connect(lambda checked=False, cb=callback: self.guard(cb))

    def open_cloud(self, *, publish=False, browse=False):
        if not self.params_editable() or browse and not self.discard():
            return
        from poster.cloud.qt_panel import CloudDialog

        if not browse:
            self.save()
        graph = self.document.graph
        outputs = graph.metadata.get("poster.outputs", [])
        if not outputs and hasattr(self, "project_outputs"):
            outputs = [
                v.strip() for v in self.project_outputs.text().replace(",", ";").split(";") if v.strip()
            ]
        self.cloud_dialog = CloudDialog(
            self,
            source=None if browse else self.path,
            resources=list(graph.metadata.get("poster.resources", {})),
            outputs=outputs,
            opened=self.cloud_opened,
            published=self.cloud_published,
            auto_publish=publish,
        )
        self.cloud_dialog.show()

    def cloud_opened(self, value):
        graph, vault = load_graph(value["source"])
        self.stop()
        self.replace(graph, vault=vault)
        self.path = Path(value["source"])
        self.canvas.fit()
        self.status_label.setText("Открыта облачная версия " + value["version"][:12])

    def open_preparation(self):
        if not self.editable() or not self.profile_closed():
            return
        self.save()
        if not self.path:
            return
        from poster.portability.qt_dialog import PreparationDialog

        def changed(metadata):
            self.document.graph.metadata = dict(metadata)
            self.refresh_profiles()
            self.changed()
            self.save()

        self.preparation_dialog = PreparationDialog(
            self, source=self.path, profiles=self.profiles, changed=changed
        )
        self.preparation_dialog.show()

    def cloud_published(self, value):
        self.document.graph.metadata["poster.resources"] = value["resources"]
        self.document.graph.metadata["poster.outputs"] = value["outputs"]
        self.save()
        self.status_label.setText("Google Drive: сохранено, версия " + value["version"][:12])

    def page_changed(self):
        maker = self.pages.currentWidget() is self.maker_page
        for dock in (self.library_dock, self.tools_dock):
            dock.setVisible(maker)
        for toolbar in self.findChildren(QToolBar):
            if toolbar.objectName().endswith("_toolbar"):
                toolbar.setVisible(maker)

    def editable(self):
        return not self.busy and not self.recording and not self.closing and self.run_state != "PAUSED"

    def params_editable(self):
        return not self.busy and not self.recording and not self.closing

    def update_controls(self):
        if not hasattr(self, "controls"):
            return
        if hasattr(self, "execution_workdir"):
            self.execution_workdir.setReadOnly(not self.editable())
            self.project_outputs.setReadOnly(not self.editable())
            self.controls["choose_workdir"].setEnabled(self.editable())
            self.controls["data_examples"].setEnabled(self.editable())
        for key in (
            "new",
            "open",
            "add",
            "delete",
            "copy",
            "undo",
            "redo",
            "example",
            "record",
            "record_resume",
            "demo",
        ):
            if key in self.controls:
                self.controls[key].setEnabled(self.editable())
        for key in (
            "save",
            "save_as",
            "apply",
            "breakpoint",
            "pick",
            "selector_check",
            "selector_apply",
            "selected_run",
            "profile_save",
        ):
            if key in self.controls:
                self.controls[key].setEnabled(self.params_editable())
        for key in ("run", "step"):
            self.controls[key].setEnabled(not self.busy and not self.recording and not self.closing)
        self.controls["pause"].setEnabled(self.busy and self.run_state == "RUNNING")
        self.controls["record_stop"].setEnabled(self.recording and not self.busy)

    def changed(self):
        self.document.dirty = True
        self.status_label.setText(
            "Проект изменён. Выход → вход соединяет кубики; Ctrl+колесо — масштаб, средняя кнопка — перемещение."
        )
        self.update_controls()

    def rename_project(self):
        if self.params_editable() and self.project_name.text().strip():
            self.document.graph.name = self.project_name.text().strip()
            self.changed()

    def replace(self, graph, *, vault=None, dirty=False):
        if graph.id != self.document.graph.id:
            self.live_values = {}
            self.trace = []
        self.document = Document(graph)
        self.document.dirty = dirty
        if vault is not None:
            self.vault = vault
        self.canvas.document = self.document
        self.canvas.redraw()
        self.selected_id = None
        self.show_properties(None)
        self.project_name.setText(graph.name)
        self.execution_workdir.setText(
            self.workdirs.get(graph.id) or str(self.workspace.root / "maker-data" / graph.id)
        )
        self.project_outputs.setText(", ".join(graph.extensions.get("outputs", [])))
        self.refresh_variables()
        self.refresh_profiles()
        self.update_controls()

    def selection_changed(self, ids):
        self.show_properties(ids[0] if len(ids) == 1 else None)

    def filter_actions(self, text):
        for row in range(self.action_list.count()):
            item = self.action_list.item(row)
            item.setHidden(
                text.casefold() not in (item.text() + " " + item.data(Qt.ItemDataRole.UserRole)).casefold()
                or not item.data(Qt.ItemDataRole.UserRole).startswith(self.action_category.currentData())
            )

    def add_selected_action(self):
        item = self.action_list.currentItem()
        if item and not item.isHidden():
            self.add_action(item.data(Qt.ItemDataRole.UserRole))
        else:
            for row in range(self.action_list.count()):
                item = self.action_list.item(row)
                if not item.isHidden():
                    self.add_action(item.data(Qt.ItemDataRole.UserRole))
                    break

    def add_action(self, name, params=None):
        if not self.editable():
            raise GraphError("DEBUG_STATE_INVALID")
        spec = self.registry.get(name)
        defaults = spec.params.model_construct().model_dump(mode="json")
        point = self.canvas.mapToScene(self.canvas.viewport().rect().center())
        node = self.document.add(name, defaults if params is None else params, point.x(), point.y())
        self.canvas.redraw()
        self.canvas.choose([node.id])
        self.tools_tabs.setCurrentIndex(0)
        self.changed()
        return node

    def undo(self):
        if self.editable():
            self.document.undo()
            self.canvas.redraw()
            self.refresh_variables()

    def redo(self):
        if self.editable():
            self.document.redo()
            self.canvas.redraw()
            self.refresh_variables()

    def breakpoint(self):
        if self.params_editable() and self.selected_id:
            self.document.checkpoint()
            node = self.document.graph.node(self.selected_id)
            node.breakpoint = not node.breakpoint
            self.canvas.redraw()
            self.canvas.choose([node.id])
            self.changed()

    def profile(self):
        key = self.document.graph.metadata.get("browser_profile")
        if key and self.path:
            from poster.portability.local import profile_binding

            binding = profile_binding(self.path)
            if binding and binding["profile_id"] == key:
                from poster.dispatcher.security import plain_path

                selected_key = (
                    plain_path(binding["key_file"]).read_bytes().strip() if binding.get("key_file") else None
                )
                return Profiles(binding["root"], key=selected_key).open(key)
        return self.profiles.open(key) if key else None

    def submit(self, coroutine, name):
        if self.busy or self.closing:
            coroutine.close()
            return
        self.busy = True
        self.last_error = None
        self.update_controls()
        self.worker.submit(coroutine, name=name)

    def address_opened(self):
        # BrowserPane performs native navigation. The controller attaches scripts
        # before recording; manual navigation is recorded via page events.
        self.status_label.setText("Открытие страницы…")

    def open_url(self):
        self.submit(
            self.controller.open_browser(self.browser.address.text(), self.vault, self.profile()),
            "browser_open",
        )

    def record(self, continued=False):
        if not self.editable() or not self.discard():
            return
        after = self.selected_id if continued else None
        if continued and not after:
            raise GraphError("NODE_MISSING")
        if not continued:
            self.vault, self.path = Vault(), None
        url = self.browser.address.text().strip()
        if self.demo_base and url.startswith(self.demo_base + "/"):
            self.document.graph.metadata["maker_demo"] = self.demo_base
        elif not continued:
            self.document.graph.metadata.pop("maker_demo", None)
        self.trace = []
        self.run_state = None
        self.submit(
            self.controller.record(
                self.document.graph.model_copy(deep=True),
                self.vault,
                self.profile(),
                url if url.startswith(("http://", "https://")) else "",
                after,
            ),
            "record",
        )

    def stop_recording(self):
        self.submit(self.controller.stop_recording(), "record_stop")

    def inputs(self):
        result = {}
        for name, definition in self.document.graph.variables.items():
            if definition.input:
                value, ok = QInputDialog.getText(
                    self,
                    "Параметр проекта: " + name,
                    "Значение (текст/JSON)",
                    echo=QLineEdit.EchoMode.Password if definition.secret else QLineEdit.EchoMode.Normal,
                    text="" if definition.secret else display(definition.value),
                )
                if not ok:
                    return None
                result[name] = self.vault.put_value(parse(value)) if definition.secret else parse(value)
        return result

    def prepare_secrets(self):
        for ref in graph_secret_refs(self.document.graph):
            try:
                self.vault.get(ref)
            except GraphError:
                value, ok = QInputDialog.getText(
                    self,
                    "Защищённое значение",
                    "Задайте отсутствующий секрет",
                    echo=QLineEdit.EchoMode.Password,
                )
                if not ok:
                    return False
                self.vault.put_value(parse(value), ref)
        return True

    def run(self, *, step=False):
        if not self.params_editable() or not self.prepare_secrets():
            return
        inputs = self.inputs() if self.run_state != "PAUSED" else {}
        if inputs is None:
            return
        if (
            self.run_state != "PAUSED"
            and QMessageBox.question(
                self,
                "Выполнение графа",
                "Выполнить проект? Кубики могут обращаться к сети, отправлять формы и изменять файлы.",
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        graph, vault, profile = self.document.graph.model_copy(deep=True), self.vault, self.profile()
        workdir, project_path = self.execution_workdir.text(), self.path

        async def work():
            await self.ensure_demo(graph)
            await self.controller.execute(
                graph, vault, profile, step=step, inputs=inputs, workdir=workdir, project_path=project_path
            )

        self.run_state = "RUNNING"
        self.submit(work(), "step" if step else "run")

    def run_selected(self):
        if not self.selected_id or not self.params_editable():
            return
        if (
            QMessageBox.question(
                self,
                "Выполнить кубик",
                "Выполнить выбранный кубик в текущей сессии? Он может отправить форму.",
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        self.submit(
            self.controller.execute(
                self.document.graph.model_copy(deep=True),
                self.vault,
                self.profile(),
                selected=self.selected_id,
                workdir=self.execution_workdir.text(),
                project_path=self.path,
            ),
            "selected",
        )

    def pause(self):
        self.worker.loop.call_soon_threadsafe(self.controller.pause)

    def stop(self):
        async def work():
            await self.controller.stop()

        self.worker.submit(work(), name="stop")

    def discard(self):
        return (
            not self.document.dirty
            or QMessageBox.question(self, "Несохранённый проект", "Продолжить без сохранения изменений?")
            == QMessageBox.StandardButton.Yes
        )

    def new(self):
        if self.editable() and self.discard():
            self.stop()
            self.replace(Document().graph, vault=Vault())
            self.path = None

    def open(self):
        if not self.editable() or not self.discard():
            return
        name, _ = QFileDialog.getOpenFileName(
            self, "Открыть граф", str(self.directory), "Граф Maker (*.json)"
        )
        if name:
            graph, vault = load_graph(name)
            self.stop()
            self.replace(graph, vault=vault)
            self.path = Path(name)
            self.canvas.fit()

    def save(self, save_as=False):
        if not self.params_editable():
            return
        path = self.path
        if save_as:
            name, _ = QFileDialog.getSaveFileName(
                self, "Сохранить граф", str(self.directory), "Граф Maker (*.json)"
            )
            if not name:
                return
            path = Path(name)
            if path.suffix != ".json":
                path = path.with_suffix(".json")
        path = path or self.directory / (self.document.graph.id + ".postergraph.json")
        self.document.graph = save_graph(path, self.document.graph, self.vault)
        self.canvas.document = self.document
        self.path = path
        self.document.dirty = False
        self.show_properties(self.selected_id)
        self.status_label.setText("Проект сохранён: " + str(path.name))
        self.save_settings()

    def demo(self):
        async def work():
            if not self.demo_context:
                self.demo_context = serve_maker()
                self.demo_server, self.demo_base = self.demo_context.__enter__()
            self.events.received.emit(("demo", self.demo_base))

        self.submit(work(), "demo")

    def example(self):
        if not self.demo_base:
            self.status_label.setText("Сначала нажмите «Демо-сайт».")
            return
        if self.discard():
            from .samples import loop_graph

            self.stop()
            self.replace(loop_graph(self.demo_base), vault=Vault(), dirty=True)
            self.path = None
            self.canvas.fit()

    async def ensure_demo(self, graph):
        origin = graph.metadata.get("maker_demo")
        if origin and not self.demo_context:
            from urllib.parse import urlsplit

            self.demo_context = serve_maker(port=urlsplit(origin).port)
            self.demo_server, self.demo_base = self.demo_context.__enter__()

    def handle(self, packet):
        kind, data = packet
        if kind == "data_example":
            graph, vault, path = data
            self.replace(graph, vault=vault, dirty=True)
            self.path = path
            self.canvas.fit()
        elif kind == "graph":
            self.replace(Graph.model_validate(data), dirty=True)
        elif kind == "recording":
            self.recording = data
            self.status_label.setText(
                "Запись идёт в браузере над графом"
                if data
                else "Запись остановлена; кубики доступны для редактирования"
            )
        elif kind == "idle":
            self.busy = False
        elif kind == "error":
            self.last_error = data
            self.status_label.setText(
                TEXT.get(data["code"], "Операция не выполнена. Проверьте параметры, браузер и профиль.")
            )
            self.log.appendPlainText(self.status_label.text())
        elif kind == "warning":
            code = data.get("code") if isinstance(data, dict) else data
            self.log.appendPlainText(TEXT.get(code, str(code)))
        elif kind == "runtime":
            event = data.get("kind")
            if event == "node":
                self.current_node, self.run_state = data["node_id"], "RUNNING"
                self.canvas.follow(self.current_node)
            elif event == "result":
                result = data["result"]
                self.trace.append(result)
                self.preview_result(result)
                self.canvas.follow(result["node_id"], result["status"])
                self.log.appendPlainText(
                    result["action"]
                    + " — "
                    + result["status"]
                    + (": " + result["message"] if result.get("message") else "")
                )
            elif event == "pause":
                self.run_state, self.pause_reason, self.current_node = (
                    "PAUSED",
                    data["reason"],
                    data["node_id"],
                )
                if self.current_node:
                    self.canvas.follow(self.current_node)
            elif event == "variables":
                self.live_values = data["values"]
                self.refresh_variables()
        elif kind == "run_state":
            self.run_state, self.current_node, self.pause_reason = (
                data["state"],
                data["current"],
                data["reason"],
            )
            self.status_label.setText(
                {
                    "SUCCEEDED": "Проект выполнен",
                    "PAUSED": "Пауза. Измените параметры или переменную и продолжите",
                    "FAILED": "Ошибка выполнения; смотрите журнал",
                }.get(self.run_state, "Остановлено")
            )
        elif kind == "picked":
            self.last_pick = data
            self.switch_selector()
            self.frames.setPlainText("\n".join(data["frame"]))
            self.element_info.setPlainText(
                json.dumps(
                    {
                        "tab": data.get("tab_id"),
                        "frame": data["frame"],
                        "tag": data["tag"],
                        "attributes": data["attributes"],
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            self.tools_tabs.setCurrentIndex(2)
        elif kind == "selector_result":
            self.selector_result = data
            self.status_label.setText("Совпадений: " + str(data["count"]))
        elif kind == "demo":
            self.browser.address.setText(data + "/")
        elif kind == "browser_probe":
            self.observation_browser = data
            self.probe_serial += 1
            self.probe_busy = False
        elif kind == "profile_saved":
            self.profile_status.setText("Сессия зашифрована и сохранена. Автоматического сохранения нет.")
        elif kind == "closed":
            self.close_ready = True
            self.close()
            return
        elif kind == "legacy":
            self.legacy.handle(data)
        self.update_controls()

    def show_error(self, error):
        code = error.code if isinstance(error, GraphError) else "PARAMETERS_INVALID"
        if hasattr(self, "status_label"):
            self.status_label.setText(TEXT.get(code, "Проверьте параметры выбранного действия."))

    def save_settings(self):
        data = {
            "last_page": self.pages.tabText(self.pages.currentIndex()),
            "workdirs": {**self.workdirs, self.document.graph.id: self.execution_workdir.text()},
            "geometry": bytes(self.saveGeometry()).hex(),
            "layout": bytes(self.saveState()).hex(),
            "splitter": bytes(self.splitter.saveState()).hex(),
            "last_project": str(self.path) if self.path else None,
        }
        atomic(self.settings, json.dumps(data).encode())

    def restore_settings(self):
        try:
            data = json.loads(self.settings.read_text())
            self.workdirs = data.get("workdirs", {})
            if data.get("last_project"):
                graph, vault = load_graph(data["last_project"])
                self.replace(graph, vault=vault)
                self.path = Path(data["last_project"])
            for key, restore in (
                ("geometry", self.restoreGeometry),
                ("layout", self.restoreState),
                ("splitter", self.splitter.restoreState),
            ):
                if data.get(key):
                    restore(QByteArray.fromHex(data[key].encode()))
            # Startup selects Maker. A layout saved from a legacy page has its
            # Maker toolbars/docks hidden; restore visibility for the actual page.
            if data.get("last_page") != "ProjectMaker":
                self.page_changed()
        except (OSError, ValueError):
            pass

    def closeEvent(self, event):
        if getattr(self, "preparation_dialog", None):
            self.preparation_dialog.close()
        if self.close_ready:
            self.observer.stop()
            self.browser.dispose()
            self.worker.loop.call_soon_threadsafe(self.worker.loop.stop)
            self.worker.thread.join(timeout=5)
            event.accept()
            return
        event.ignore()
        if self.closing or not self.discard():
            return
        self.save_settings()
        self.closing = True
        self.legacy.timer.stop()
        self.update_controls()

        async def cleanup():
            await self.controller.stop()
            await self.legacy.shutdown()
            if self.demo_context:
                self.demo_context.__exit__(None, None, None)
                self.demo_context = None
            self.events.received.emit(("closed", None))

        self.worker.submit(cleanup(), name="close", busy=False)

    def dialog_observations(self):
        result = []
        for widget in QApplication.topLevelWidgets():
            if widget is self or not widget.isVisible():
                continue
            buttons = []
            for button in widget.findChildren(QPushButton):
                if button.isVisible():
                    point = button.mapToGlobal(button.rect().center())
                    role = widget.buttonRole(button).name if isinstance(widget, QMessageBox) else None
                    buttons.append({"text": button.text(), "role": role, "point": point.toTuple()})
            combos = []
            for combo in widget.findChildren(QComboBox):
                if combo.isVisible():
                    combos.append(
                        {
                            "point": combo.mapToGlobal(combo.rect().center()).toTuple(),
                            "items": [combo.itemText(i) for i in range(combo.count())],
                        }
                    )
            result.append({"title": widget.windowTitle(), "buttons": buttons, "combos": combos})
        return result

    def observe(self):
        """Opt-in, read-only geometry/state for real OS input tests; no command API."""
        target = os.environ.get("POSTER_GUI_TEST_OUTPUT")
        if not target or self.closing:
            return
        try:
            if not self.probe_busy and self.controller.session() and not self.busy:
                self.probe_busy = True

                async def probe():
                    try:
                        await self.controller.probe()
                    except Exception:
                        self.events.received.emit(("browser_probe", {}))

                self.worker.submit(probe(), name="probe", busy=False)
            points = {}
            for widget in self.findChildren(QWidget):
                if widget.objectName() and widget.isVisible() and not widget.visibleRegion().isEmpty():
                    p = widget.mapToGlobal(widget.visibleRegion().boundingRect().center())
                    points[widget.objectName()] = [p.x(), p.y()]
            for toolbar in self.findChildren(QToolBar):
                for action in toolbar.actions():
                    widget = toolbar.widgetForAction(action)
                    if action.objectName() and widget and widget.isVisible():
                        p = widget.mapToGlobal(widget.visibleRegion().boundingRect().center())
                        points[action.objectName()] = [p.x(), p.y()]
            nodes = {}
            for name, block in self.canvas.blocks.items():
                p = self.canvas.viewport().mapToGlobal(
                    self.canvas.mapFromScene(block.sceneBoundingRect().center())
                )
                nodes[name] = [p.x(), p.y()]
            state = {
                "ui": "qt",
                "qt": qVersion(),
                "frozen": bool(getattr(__import__("sys"), "frozen", False)),
                "positions": points,
                "main_page": self.pages.tabText(self.pages.currentIndex()),
                "tools_page": self.tools_tabs.tabText(self.tools_tabs.currentIndex()),
                "panel_sizes": {
                    "window": [self.width(), self.height()],
                    "element": [self.tools_tabs.width(), self.tools_tabs.height()],
                },
                "nodes": nodes,
                "canvas_frame": self.canvas.render_serial,
                "canvas_rect": [
                    *self.canvas.viewport().mapToGlobal(self.canvas.viewport().rect().topLeft()).toTuple(),
                    self.canvas.viewport().width(),
                    self.canvas.viewport().height(),
                ],
                "graph": self.document.graph.model_dump(mode="json"),
                "selected": self.canvas.selection(),
                "recording": self.recording,
                "busy": self.busy,
                "dirty": self.document.dirty,
                "run_state": self.run_state,
                "pause_reason": self.pause_reason,
                "current_node": self.current_node,
                "values": self.live_values,
                "variable_flags": {
                    "secret": self.variable_secret.isChecked(),
                    "input": self.variable_input.isChecked(),
                    "required": self.variable_required.isChecked(),
                },
                "trace": self.trace,
                "workdir": self.execution_workdir.text(),
                "runner_browser_started": bool(
                    self.controller.runner and self.controller.runner.context.session.page
                ),
                "preview_rows": self.result_table.rowCount(),
                "preview_columns": self.result_table.columnCount(),
                "browser": {**self.browser.state(), **self.observation_browser},
                "browser_frame": self.probe_serial,
                "status": self.status_label.text(),
                "error": self.last_error,
                "saved_file": str(self.path) if self.path else None,
                "picked": self.last_pick,
                "selector_result": getattr(self, "selector_result", None),
                "main_tabs": [
                    [
                        self.pages.tabText(i),
                        self.pages.tabBar().mapToGlobal(self.pages.tabBar().tabRect(i).center()).toTuple(),
                    ]
                    for i in range(self.pages.count())
                ],
                "dialogs": self.dialog_observations(),
                "tools_tabs": [
                    [
                        self.tools_tabs.tabText(i),
                        self.tools_tabs.tabBar()
                        .mapToGlobal(self.tools_tabs.tabBar().tabRect(i).center())
                        .toTuple(),
                    ]
                    for i in range(self.tools_tabs.count())
                    if self.tools_tabs.tabBar().rect().contains(self.tools_tabs.tabBar().tabRect(i).center())
                    and not isinstance(
                        self.tools_tabs.tabBar().childAt(self.tools_tabs.tabBar().tabRect(i).center()),
                        QToolButton,
                    )
                ],
                "tools_tab_titles": [self.tools_tabs.tabText(i) for i in range(self.tools_tabs.count())],
                "tools_bar": {
                    "cursor": QCursor.pos().toTuple(),
                    "cursor_widget": type(QApplication.widgetAt(QCursor.pos())).__name__,
                    "dpr": self.devicePixelRatioF(),
                    "rect": self.tools_tabs.tabBar().rect().getRect(),
                    "visible_rect": self.tools_tabs.tabBar().visibleRegion().boundingRect().getRect(),
                    "tabs": [
                        {
                            "title": self.tools_tabs.tabText(i),
                            "rect": self.tools_tabs.tabBar().tabRect(i).getRect(),
                            "covered_by": type(
                                self.tools_tabs.tabBar().childAt(self.tools_tabs.tabBar().tabRect(i).center())
                            ).__name__,
                        }
                        for i in range(self.tools_tabs.count())
                    ],
                    "buttons": [
                        {
                            "name": b.objectName(),
                            "arrow": b.arrowType().name,
                            "enabled": b.isEnabled(),
                            "visible": b.isVisible(),
                            "rect": b.geometry().getRect(),
                            "point": b.mapToGlobal(b.rect().center()).toTuple(),
                            "hit_widget": type(
                                QApplication.widgetAt(b.mapToGlobal(b.rect().center()))
                            ).__name__,
                            "presses": b.property("poster_presses") or 0,
                        }
                        for b in self.tools_tabs.tabBar().findChildren(QToolButton)
                    ],
                },
                "demo_base": self.demo_base,
                "demo_posts": self.demo_server.posts if self.demo_server else {},
                "demo_api_posts": self.demo_server.api_posts if self.demo_server else 0,
                "legacy": self.legacy.observation(),
            }
            atomic(Path(target), json.dumps(state, ensure_ascii=False).encode("utf-8"))
        except (OSError, RuntimeError):
            pass


def launch():
    from ..product.paths import maker_root
    from ..product.locking import application_lease

    with tempfile.TemporaryDirectory(prefix="poster-maker-") as temporary:
        port = configure_browser(temporary)
        app = QApplication.instance() or QApplication([])
        app.setApplicationName("Poster Maker")
        app.setStyle("Fusion")
        root = maker_root()
        with application_lease(root, "maker"):
            window = MakerWindow(port, workspace=Workspace(root))
            window.show()
            return app.exec()


if __name__ == "__main__":
    raise SystemExit(launch())
