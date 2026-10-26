"""Native remote enrollment and explicit routing controls; Service owns all operations."""

from PySide6.QtWidgets import QWidget, QComboBox, QFormLayout, QLineEdit, QLabel
from poster.remote.registry import output_names


class RemoteTarget(QWidget):
    def __init__(self, agents, parent, *, agent_id=None, outputs=()):
        super().__init__(parent)
        layout = QFormLayout(self)
        self.agent = QComboBox()
        self.agent.setObjectName("execution_target")
        self.agent.addItem("Локальный Worker", None)
        for row in agents:
            if row["state"] == "TRUSTED":
                self.agent.addItem(
                    row["name"] + " · " + ("online" if row["online"] else "OFFLINE") + " · " + row["id"][:12],
                    row["id"],
                )
        if agent_id and self.agent.findData(agent_id) < 0:
            self.agent.addItem("Недоступный агент " + agent_id, agent_id)
        self.agent.setCurrentIndex(max(0, self.agent.findData(agent_id)))
        layout.addRow("Исполнение", self.agent)
        self.outputs = QLineEdit(", ".join(outputs))
        self.outputs.setObjectName("remote_outputs")
        self.outputs.setPlaceholderText("Например: output/report.json, results/data.csv")
        layout.addRow("Вернуть только эти файлы", self.outputs)
        note = QLabel(
            "Удалённый агент получает необходимые секреты в памяти своей Linux-машины. Выбирайте только доверенную машину. Недоступный агент не заменяется локальным worker автоматически."
        )
        note.setWordWrap(True)
        layout.addRow(note)

    def values(self):
        return dict(
            agent_id=self.agent.currentData(),
            remote_outputs=output_names([s.strip() for s in self.outputs.text().split(",") if s.strip()]),
        )
