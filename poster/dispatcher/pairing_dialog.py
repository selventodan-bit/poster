"""Guided private-file pairing. Every operation goes through local Service IPC."""

from PySide6.QtCore import QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QFormLayout,
    QLineEdit,
    QLabel,
    QPushButton,
    QFileDialog,
    QTextEdit,
    QMessageBox,
)

from .host import request
from .models import MESSAGES


class PairingDialog(QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self.setObjectName("remote_pairing_wizard")
        self.setWindowTitle("Подключение Linux Agent — 5B")
        self.resize(760, 700)
        self.window, self.agent_id, self.job, self.workdir = parent, None, None, None
        layout = QVBoxLayout(self)
        note = QLabel(
            "1. Укажите HTTPS relay и выберите приватный pairing-relay.json от его оператора.\n2. Проверьте TLS и экспортируйте приватный pairing-файл на Linux. Срок — 15 минут.\n3. На Linux сверяйте fingerprint Controller, выполните явный pair и верните registration.json.\n4. Сравните полный fingerprint Agent с Linux, подтвердите доверие и дождитесь heartbeat.\n5. Запустите безопасный тест и откройте возвращённый файл."
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        form = QFormLayout()
        self.url = QLineEdit()
        self.url.setPlaceholderText("https://relay.example.org:8443")
        self.url.setObjectName("pairing_relay_url")
        self.controller = QLineEdit()
        self.controller.setReadOnly(True)
        self.controller.setObjectName("pairing_controller_fingerprint")
        self.fingerprint = QLineEdit()
        self.fingerprint.setObjectName("pairing_agent_fingerprint")
        self.fingerprint.setPlaceholderText("Полные 64 hex символа из Linux registration.json")
        form.addRow("HTTPS relay", self.url)
        form.addRow("Fingerprint Controller", self.controller)
        form.addRow("Fingerprint Agent", self.fingerprint)
        layout.addLayout(form)
        self.actions = {}
        for name, text, callback in [
            ("export", "1–2. Проверить TLS и создать pairing-файл…", self.export),
            ("import", "3. Импортировать заявку Agent…", self.register),
            ("approve", "4. Подтвердить fingerprint и доверие", self.approve),
            ("diagnose", "Диагностика relay / Agent / Worker", self.diagnose),
            ("test", "5. Безопасное тестовое задание", self.test),
            ("open", "Открыть папку с возвращённым файлом", self.open_result),
            ("revoke", "Аварийно отозвать Agent", self.revoke),
        ]:
            button = QPushButton(text)
            button.setObjectName("pairing_" + name)
            button.clicked.connect(callback)
            layout.addWidget(button)
            self.actions[name] = button
        self.result = QTextEdit()
        self.result.setReadOnly(True)
        self.result.setObjectName("pairing_result")
        layout.addWidget(self.result, 1)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll)
        self.timer.start(1000)
        self.invoke("remote_public")

    def invoke(self, op, **args):
        self.window.submit("remote_wizard", lambda: (op, request(self.window.root, op, **args)), self)

    def consume(self, value, error):
        if error:
            self.result.setPlainText(MESSAGES[error])
            return
        op, reply = value
        if op == "remote_public":
            self.controller.setText(reply["id"])
        elif op == "remote_pairing_export":
            self.controller.setText(reply["controller"])
            self.result.setPlainText(
                "TLS проверен. Передайте pairing-файл приватным каналом. На Linux:\npython -m poster.remote pair --root /home/poster/agent --name linux-worker --offer /home/poster/pairing.json --expect-controller "
                + reply["controller"]
                + "\nПодключение должно завершиться в течение 15 минут. Команда не содержит секретных ключей."
            )
        elif op == "remote_pairing_finish":
            self.agent_id = reply
            self.result.setPlainText(
                "Заявка зарегистрирована как PENDING. Сверьте полный Agent fingerprint на Linux и подтвердите доверие.\n"
                + reply
            )
        elif op == "remote_test":
            self.job = reply["job"]
            self.result.setPlainText("Задание в общей очереди: " + self.job)
        elif op == "remote_test_status":
            self.workdir = reply["workdir"]
            self.result.setPlainText(
                "Задание: "
                + reply["status"]
                + "\nФайл возвращён: "
                + str(reply["file_returned"])
                + ("\nSHA256: " + reply["sha256"] if reply["sha256"] else "")
            )
        elif op == "remote_diagnose":
            self.result.setPlainText(
                "Relay: "
                + ("доступен; TLS проверен" if reply["relay"] else "недоступен")
                + "\nПодписанный heartbeat: "
                + ("ONLINE" if reply["online"] else "OFFLINE")
                + "\nСвободных слотов: "
                + str(reply["free_slots"])
                + "\n"
                + "\n".join(reply["messages"])
            )
        else:
            self.result.setPlainText("Операция выполнена. Ожидайте реальный heartbeat Agent.")

    def export(self):
        operator, _ = QFileDialog.getOpenFileName(
            self, "Приватный pairing-relay.json от оператора relay", "", "JSON (*.json)"
        )
        if not operator:
            return
        destination, _ = QFileDialog.getSaveFileName(
            self, "Приватный файл для Linux Agent", "pairing.json", "JSON (*.json)"
        )
        if (
            destination
            and QMessageBox.question(
                self,
                "Передача приватных данных",
                "Pairing-файл содержит bearer credential роли Agent. Передайте его только доверенной Linux-машине приватным файловым каналом. Создать файл?",
            )
            == QMessageBox.StandardButton.Yes
        ):
            self.invoke(
                "remote_pairing_export",
                operator_file=operator,
                url=self.url.text().strip(),
                destination=destination,
            )

    def register(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Публичный registration.json с Linux", "", "JSON (*.json)"
        )
        if path:
            self.invoke(
                "remote_pairing_finish", registration=path, fingerprint=self.fingerprint.text().strip()
            )

    def approve(self):
        if (
            self.agent_id
            and QMessageBox.question(
                self,
                "Доверие Linux-машине",
                "Разрешить Agent получать необходимые секреты проектов в памяти своей машины и запустить обслуживание общей очереди?",
            )
            == QMessageBox.StandardButton.Yes
        ):
            self.invoke("remote_approve", id=self.agent_id, fingerprint=self.fingerprint.text().strip())

    def diagnose(self):
        if self.agent_id:
            self.invoke("remote_diagnose", id=self.agent_id)

    def test(self):
        if self.agent_id:
            self.invoke("remote_test", id=self.agent_id)

    def open_result(self):
        if self.workdir:
            QDesktopServices.openUrl(QUrl.fromLocalFile(self.workdir))

    def revoke(self):
        if (
            self.agent_id
            and QMessageBox.question(
                self,
                "Аварийный отзыв",
                "Отозвать Agent? Возможные внешние действия останутся UNKNOWN до ручной проверки.",
            )
            == QMessageBox.StandardButton.Yes
        ):
            self.invoke("remote_revoke", id=self.agent_id)

    def poll(self):
        if self.isVisible() and self.job and not self.window.busy:
            self.invoke("remote_test_status", job=self.job)

    def closeEvent(self, event):
        self.timer.stop()
        super().closeEvent(event)
