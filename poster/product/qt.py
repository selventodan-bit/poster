"""Desktop entry points for help and offline maintenance of the same product."""

import json
import os
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTextBrowser,
    QTreeWidget,
    QScrollArea,
    QWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from poster.dispatcher.models import DispatcherError
from poster.dispatcher.security import plain_path
from . import backup, restore
from .diagnostics import build_info, collect
from .locking import registry_path, legacy_folders
from .paths import product_home, checked_workspace, select_workspace


def pending_plans():
    result = []
    directory = registry_path()
    candidates = set(directory.glob("*/restore-transaction.json"))
    makers = [product_home() / "Maker", Path.cwd() / ".poster"]
    if os.environ.get("POSTER_MAKER_WORKSPACE"):
        makers.append(Path(os.environ["POSTER_MAKER_WORKSPACE"]))
    locator = plain_path(product_home() / "maker-location.json")
    if locator.is_file() and locator.stat().st_size < 20000:
        value = json.loads(locator.read_bytes())
        if value.get("format") == "poster.maker.location.v1":
            makers.append(checked_workspace(value["path"]))
    for maker in makers:
        folders = legacy_folders(backup.default_roots(maker).values())
        candidates.update(
            folder / "restore-transaction.json"
            for folder in folders
            if (folder / "restore-transaction.json").is_file()
        )
    for index, path in enumerate(sorted(candidates)):
        if index >= 30000 or path.stat().st_size > 100000:
            raise DispatcherError("RESTORE_RECOVERY")
        value = json.loads(plain_path(path).read_bytes())
        roots = backup.roots_checked({row["scope"]: row["target"] for row in value["entries"]})
        restore.journal_checked(path, roots)
        result.append(roots)
    return result


def initial_roots():
    # Maintenance must remain reachable when maker_root's startup recovery gate
    # correctly refuses normal application launch.
    from .paths import maker_root

    plans = pending_plans()
    if plans:
        return plans[0]
    try:
        return backup.default_roots(maker_root())
    except DispatcherError:
        return backup.default_roots(checked_workspace(product_home() / "Maker"))


class Task(QObject):
    finished = Signal(object)

    def __init__(self, operation):
        super().__init__()
        self.operation = operation

    def run(self):
        try:
            self.finished.emit(dict(ok=True, result=self.operation()))
        except Exception as exc:
            self.finished.emit(
                dict(ok=False, code=exc.code if isinstance(exc, DispatcherError) else "BACKUP_IO")
            )
        finally:
            self.operation = None


class MaintenanceDialog(QDialog):
    def __init__(self, roots=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Poster — резервные копии и восстановление")
        self.setObjectName("poster_maintenance")
        screen = QApplication.primaryScreen().availableGeometry()
        self.resize(min(900, screen.width() - 30), min(650, screen.height() - 50))
        self.setMinimumSize(min(650, screen.width() - 30), min(380, screen.height() - 50))
        startup_error = None
        try:
            self.roots = dict(roots if roots is not None else initial_roots())
        except Exception:
            self.roots = backup.default_roots(checked_workspace(product_home() / "Maker"))
            startup_error = (
                str(DispatcherError("RESTORE_RECOVERY"))
                + " Сохраните исходные данные и приватные журналы; неизвестный план автоматически не применяется."
            )
        self.preview = None
        self.thread = None
        outer = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)
        form = QVBoxLayout(body)
        notice = QLabel(
            "Закройте Maker и Dispatcher. Остановите Service и его автозапуск перед обслуживанием.\nКопия содержит приватные ключи и OAuth/browser state: храните её и пароль отдельно.\nВосстановление разрешено на исходной машине, под тем же пользователем и в те же папки."
        )
        notice.setWordWrap(True)
        form.addWidget(notice)
        self.paths = QTreeWidget()
        self.paths.setObjectName("maintenance_roots")
        self.paths.setHeaderLabels(["Состояние", "Локальная папка / файл (двойной щелчок для выбора)"])
        self.paths.itemDoubleClicked.connect(self.choose_scope)
        form.addWidget(self.paths)
        row = QHBoxLayout()
        for title, name, callback in [
            ("Выбрать прежний Maker…", "maintenance_maker", self.choose_maker),
            ("Отключить автозапуск и остановить Service", "maintenance_stop", self.stop_service),
        ]:
            button = QPushButton(title)
            button.setObjectName(name)
            button.clicked.connect(callback)
            row.addWidget(button)
        form.addLayout(row)
        fields = QFormLayout()
        self.operation = QComboBox()
        self.operation.setObjectName("maintenance_operation")
        self.operation.addItems(
            [
                "Создать защищённую копию",
                "Проверить копию / предварительный просмотр",
                "Восстановить проверенную копию",
                "Завершить откат после аварии",
            ]
        )
        self.operation.currentIndexChanged.connect(self.changed)
        fields.addRow("Операция", self.operation)
        picker = QHBoxLayout()
        self.archive = QLineEdit()
        self.archive.setObjectName("maintenance_archive")
        self.archive.textChanged.connect(self.clear_preview)
        picker.addWidget(self.archive)
        browse = QPushButton("Обзор…")
        browse.setObjectName("maintenance_browse")
        browse.clicked.connect(self.browse)
        picker.addWidget(browse)
        fields.addRow("Файл .pbackup", picker)
        self.password = QLineEdit()
        self.password.setObjectName("maintenance_password")
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        fields.addRow("Отдельная парольная фраза (от 12 символов)", self.password)
        self.repeat = QLineEdit()
        self.repeat.setObjectName("maintenance_repeat")
        self.repeat.setEchoMode(QLineEdit.EchoMode.Password)
        fields.addRow("Повтор фразы при создании", self.repeat)
        form.addLayout(fields)
        self.consent = QCheckBox(
            "Подтверждаю защищённый экспорт приватного состояния / восстановление проверенной копии"
        )
        self.consent.setObjectName("maintenance_consent")
        form.addWidget(self.consent)
        self.output = QTextBrowser()
        self.output.setObjectName("maintenance_result")
        form.addWidget(self.output)
        self.go = QPushButton("Выполнить")
        self.go.setObjectName("maintenance_execute")
        self.go.clicked.connect(self.execute)
        outer.addWidget(self.go)
        self.refresh_roots()
        self.changed()
        if startup_error:
            self.output.setPlainText(startup_error)

    def refresh_roots(self):
        self.paths.clear()
        for name, path in self.roots.items():
            QTreeWidgetItem(self.paths, [name, str(path)])
        self.paths.resizeColumnToContents(0)

    def clear_preview(self, *_args):
        self.preview = None

    def changed(self, *_args):
        index = self.operation.currentIndex()
        self.password.setEnabled(index != 3)
        self.repeat.setEnabled(index == 0)
        self.archive.setEnabled(index != 3)

    def choose_scope(self, item, _column):
        name = item.text(0)
        if name in {"location", "legacy_key"}:
            selected, _ = QFileDialog.getOpenFileName(self, "Исходный файл " + name)
        else:
            selected = QFileDialog.getExistingDirectory(self, "Исходная папка " + name)
        if selected:
            try:
                roots = backup.roots_checked(dict(self.roots, **{name: selected}))
                self.roots = roots
                self.clear_preview()
                self.refresh_roots()
            except DispatcherError as exc:
                self.output.setPlainText(str(exc))

    def choose_maker(self):
        selected = QFileDialog.getExistingDirectory(self, "Прежняя папка данных Maker (.poster)")
        if selected:
            try:
                self.roots = backup.default_roots(checked_workspace(selected))
                self.clear_preview()
                self.refresh_roots()
            except DispatcherError as exc:
                self.output.setPlainText(str(exc))

    def browse(self):
        callback = (
            QFileDialog.getSaveFileName if self.operation.currentIndex() == 0 else QFileDialog.getOpenFileName
        )
        path, _ = callback(self, "Защищённая копия Poster", "", "Poster backup (*.pbackup)")
        if path:
            self.archive.setText(path if Path(path).suffix else path + ".pbackup")

    def stop_service(self):
        def stop():
            from poster.dispatcher.autostart import configure
            from poster.dispatcher.host import request
            from poster.dispatcher.processes import same_process
            import time

            root = self.roots.get("dispatcher")
            if root is None:
                return dict(status="NO_DISPATCHER")
            if os.name == "nt" and getattr(__import__("sys"), "frozen", False):
                configure(root, enabled=False)
            try:
                info = request(root, "ping")
            except DispatcherError as exc:
                if exc.code != "SERVICE_UNAVAILABLE":
                    raise
                info = None
            if info:
                request(root, "shutdown")
                deadline = time.monotonic() + 20
                import psutil

                def active():
                    process = same_process(info["pid"], info["birth"])
                    try:
                        return process is not None and process.status() != psutil.STATUS_ZOMBIE
                    except psutil.NoSuchProcess:
                        return False

                while active():
                    if time.monotonic() >= deadline:
                        raise DispatcherError("PRODUCT_BUSY")
                    time.sleep(0.1)
            return dict(
                status="SERVICE_STOPPED", message="Закройте остальные окна Poster перед копированием."
            )

        self.start_task(stop)

    def start_task(self, callback):
        if self.thread is not None:
            return
        for widget in [
            self.go,
            self.paths,
            self.operation,
            self.archive,
            self.password,
            self.repeat,
            self.consent,
            *self.findChildren(QPushButton),
        ]:
            widget.setEnabled(False)
        self.output.setPlainText("Выполняется… Не закрывайте окно.")
        self.task = Task(callback)
        self.thread = QThread(self)
        self.task.moveToThread(self.thread)
        self.thread.started.connect(self.task.run)
        self.task.finished.connect(self.done)
        self.task.finished.connect(self.thread.quit)
        self.thread.finished.connect(self.task.deleteLater)
        self.thread.finished.connect(self.thread.deleteLater)
        self.thread.finished.connect(self.released)
        self.thread.start()

    def done(self, value):
        if not value["ok"]:
            self.output.setPlainText(str(DispatcherError(value["code"])))
            return
        result = value["result"]
        if "manifest" in result:
            manifest = result["manifest"]
            self.preview = dict(
                sha=manifest["archive_sha256"], path=self.archive.text(), roots=dict(self.roots)
            )
            self.output.setPlainText(
                "Копия проверена: SHA256 "
                + manifest["archive_sha256"]
                + "\nФайлов: "
                + str(len(manifest["files"]))
                + "\n"
                + "\n".join(s["name"] + ": " + s["path"] for s in manifest["scopes"])
                + "\nПри восстановлении задания с возможным внешним эффектом потребуют ручной проверки; расписания отключатся. Отзыв у удалённого получателя: UNCONFIRMED."
            )
        else:
            self.preview = None
            self.output.setPlainText(json.dumps(result, ensure_ascii=False, indent=2))

    def released(self):
        self.thread = None
        for widget in [
            self.go,
            self.paths,
            self.operation,
            self.archive,
            self.password,
            self.repeat,
            self.consent,
            *self.findChildren(QPushButton),
        ]:
            widget.setEnabled(True)
        self.changed()

    def execute(self):
        if not self.consent.isChecked():
            self.output.setPlainText("Подтвердите операцию с приватными данными.")
            return
        index, path, password, roots = (
            self.operation.currentIndex(),
            self.archive.text(),
            self.password.text(),
            dict(self.roots),
        )
        if index == 0:
            if password != self.repeat.text():
                self.output.setPlainText("Парольные фразы не совпадают.")
                return

            def callback():
                return backup.create(roots, path, password, confirmed=True)
        elif index == 1:

            def callback():
                return dict(manifest=backup.inspect(path, password, roots))
        elif index == 2:
            if not self.preview or self.preview["path"] != path or self.preview["roots"] != roots:
                self.output.setPlainText("Сначала выполните проверку выбранной копии.")
                return
            sha = self.preview["sha"]

            def callback():
                return restore.restore(path, password, roots, expected_sha256=sha, confirmed=True)
        else:

            def callback():
                return dict(recovery=restore.recover(roots, confirmed=True))

        self.start_task(callback)
        self.password.clear()
        self.repeat.clear()

    def closeEvent(self, event):
        if self.thread is not None:
            event.ignore()
        else:
            event.accept()


class HelpDialog(QDialog):
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.setWindowTitle("Poster — начало работы, данные и диагностика")
        self.setObjectName("poster_help")
        screen = QApplication.primaryScreen().availableGeometry()
        self.resize(min(850, screen.width() - 30), min(640, screen.height() - 50))
        form = QVBoxLayout(self)
        text = QTextBrowser()
        text.setObjectName("product_guide")
        text.setPlainText(
            "Poster 3.0 — Release Candidate\n"
            + json.dumps(build_info(), ensure_ascii=False)
            + "\n\nДанные Maker: "
            + str(window.workspace.root)
            + "\nПроекты: "
            + str(window.directory)
            + "\n\n1. Начните с «Демо-сайт», записи действий или меню примеров.\n2. Настройте входы и outputs, сохраните проект.\n3. «Подготовка проекта»: браузер, профили, сеть, ресурсы и готовность.\n4. Google Drive: подключите собственное OAuth-приложение, выберите папку и опубликуйте версию.\n5. Dispatcher: добавьте проект, выберите Local / доверенный Remote Worker и число экземпляров.\n6. Запустите очередь; откройте историю, отчёт и возвращённые файлы. UNKNOWN требует ручной проверки: внешнее действие не повторяется автоматически.\n\nДля обновления распакуйте новый Windows Full в новую папку. Сохраните папки данных и ключи. Прежняя соседняя .poster принимается без перемещения.\n\nДля копирования/восстановления сначала откройте «Помощь и данные → Резервные копии и восстановление…». Архив .pbackup содержит ключи и требует отдельной парольной фразы. После восстановления расписания отключены, разрешения недействительны локально, удалённый отзыв не подтверждён.\n\nЖивые Google Drive / Colab / Molab и публичный WAN требуют отдельной проверки. Контролируемые CI fixtures не подтверждают доступность личного аккаунта."
        )
        form.addWidget(text)
        for title, name, callback in [
            (
                "Открыть проекты",
                "help_projects",
                lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(window.directory))),
            ),
            ("Принять прежнюю папку Maker…", "help_adopt", self.adopt),
            ("Сохранить безопасную диагностику…", "help_diagnostics", self.diagnostics),
            ("Руководство пользователя", "help_manual", self.manual),
        ]:
            button = QPushButton(title)
            button.setObjectName(name)
            button.clicked.connect(callback)
            form.addWidget(button)

    def adopt(self):
        root = QFileDialog.getExistingDirectory(self, "Прежняя папка Maker (.poster)")
        if root:
            try:
                select_workspace(root)
                QMessageBox.information(
                    self,
                    "Папка принята",
                    "Папка сохранена. Закройте Poster и запустите его снова. Явная переменная POSTER_MAKER_WORKSPACE имеет приоритет.",
                )
            except DispatcherError as exc:
                QMessageBox.warning(self, "Poster", str(exc))

    def diagnostics(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Безопасная диагностика", "poster-diagnostics.json", "JSON (*.json)"
        )
        if path:
            from poster.maker.storage import atomic

            atomic(
                Path(path),
                json.dumps(
                    collect(backup.default_roots(self.window.workspace.root)), ensure_ascii=False, indent=2
                ).encode(),
            )

    def manual(self):
        import sys

        base = (
            Path(sys.executable).parent
            if getattr(sys, "frozen", False)
            else Path(__file__).resolve().parents[2]
        )
        path = base / "docs/POSTER_USER_GUIDE.md"
        if path.is_file():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
        else:
            QMessageBox.information(
                self, "Руководство", "Руководство: docs/POSTER_USER_GUIDE.md в полной поставке Poster."
            )


def install(window):
    menu = window.menuBar().addMenu("Помощь и данные")
    menu.setObjectName("product_help_menu")

    def help_dialog():
        window.product_help = HelpDialog(window)
        window.product_help.show()

    action = menu.addAction("Начало работы, обновление и диагностика…")
    action.setObjectName("product_help_action")
    action.triggered.connect(help_dialog)

    def maintenance():
        import subprocess
        import sys
        from poster.dispatcher.launcher import process_options

        args = (
            [sys.executable, "--maintenance"]
            if getattr(sys, "frozen", False)
            else [sys.executable, "-m", "poster.desktop_entry", "--maintenance"]
        )
        env = dict(os.environ)
        env.pop("POSTER_SESSION_KEY", None)
        subprocess.Popen(args, env=env, **process_options())

    action = menu.addAction("Резервные копии и восстановление…")
    action.setObjectName("product_maintenance_action")
    action.triggered.connect(maintenance)
    button = QPushButton("Начало и помощь")
    button.setObjectName("product_help")
    button.clicked.connect(help_dialog)
    window.statusBar().addPermanentWidget(button)


def launch():
    app = QApplication.instance() or QApplication([])
    app.setApplicationName("Poster — обслуживание")
    app.setStyle("Fusion")
    dialog = MaintenanceDialog()
    dialog.show()
    return app.exec()
