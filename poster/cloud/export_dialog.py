"""Prepare/open a reusable notebook: explicit manual workflow, no paid provisioning."""

from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from poster.dispatcher.launcher import build_info
from .providers import colab_automatic_capability


class NotebookExportDialog(QDialog):
    def __init__(self, parent, project_id, version):
        super().__init__(parent)
        self.setObjectName("poster_notebook_export")
        self.setWindowTitle("Подготовить автономный notebook")
        self.resize(640, 420)
        layout = QVBoxLayout(self)
        text = QLabel(
            "Проект будет выбран в общем Launcher. Выполнение начнётся после вашего действия в notebook. Результаты вернутся через Google Drive; Windows может быть выключен."
        )
        text.setWordWrap(True)
        layout.addWidget(text)
        self.backend = QComboBox()
        self.backend.addItems(
            ["Google Colab — интерактивный notebook", "Molab — marimo notebook", "Generic Linux / Jupyter"]
        )
        form = QFormLayout()
        form.addRow("Место выполнения", self.backend)
        layout.addLayout(form)
        selected = QLabel("Проект: " + project_id + "\nЗакреплённая версия: " + version)
        selected.setWordWrap(True)
        selected.setTextInteractionFlags(
            __import__("PySide6.QtCore", fromlist=["Qt"]).Qt.TextInteractionFlag.TextSelectableByMouse
        )
        layout.addWidget(selected)
        capabilities = QLabel(
            "Colab / Molab: максимум 1 одновременно до подтверждения разрешённой параллельности. Локальный Jupyter использует измеренные ресурсы. Автоматическое выделение Colab: "
            + colab_automatic_capability()["reason"]
        )
        capabilities.setWordWrap(True)
        layout.addWidget(capabilities)
        button = QPushButton("Сохранить инструкции и открыть Launcher…")
        button.clicked.connect(lambda: self.export(project_id, version))
        layout.addWidget(button)
        close = QPushButton("Закрыть")
        close.clicked.connect(self.accept)
        layout.addWidget(close)

    def export(self, project_id, version):
        index = self.backend.currentIndex()
        path, _ = QFileDialog.getSaveFileName(
            self, "Инструкция запуска", "Poster notebook.txt", "Текст (*.txt)"
        )
        if not path:
            return
        commit = build_info().get("commit")
        # Source checkouts resolve their own exact commit; frozen Full packages
        # use BUILD_INFO and need neither Git nor Python on the user's Windows.
        if commit == "source":
            import subprocess

            try:
                commit = subprocess.check_output(
                    ["git", "rev-parse", "HEAD"], cwd=Path(__file__).resolve().parents[2], text=True
                ).strip()
            except (OSError, subprocess.SubprocessError):
                QMessageBox.warning(self, "Launcher", "Не удалось определить версию приложения.")
                return
        base = "https://github.com/selventodan-bit/poster/blob/" + commit + "/notebooks/"
        if index == 0:
            url = (
                "https://colab.research.google.com/github/selventodan-bit/poster/blob/"
                + commit
                + "/notebooks/Poster_Launcher.ipynb"
            )
        elif index == 1:
            url = base + "Poster_Molab.py"
        else:
            url = base + "Poster_Launcher.ipynb"
        Path(path).write_text(
            "Poster — автономный notebook\n"
            + url
            + "\nПроект: "
            + project_id
            + "\nВерсия: "
            + version
            + "\nОткройте Launcher, подключите тот же Drive, выберите этот проект и точную версию.\nУкажите обычные входы и имена защищённых secrets. Нажмите «Выполнить».\nMolab: сначала Files → Add remote storage → Google Drive; импортируйте Poster_Molab.py.\nДля возврата откройте Dispatcher → Google Drive projects → Импортировать автономные результаты.\n",
            encoding="utf-8",
        )
        # Only public source references/project/version are exported. Never OAuth,
        # inputs, notebook tokens, filesystem credentials or queue identities.
        QDesktopServices.openUrl(QUrl(url))
        self.accept()
