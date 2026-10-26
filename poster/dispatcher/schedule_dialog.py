"""Schedule form; all previews and mutations go through the authenticated host."""

from datetime import datetime, timedelta
import uuid
from zoneinfo import ZoneInfo, available_timezones

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QSpinBox,
    QScrollArea,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .host import request
from .models import DispatcherError, MESSAGES
from .schedule_time import parse_spec
from .remote_widgets import RemoteTarget

MODES = [
    ("Однократно", "once"),
    ("Через интервал", "interval"),
    ("Ежедневно", "daily"),
    ("По дням недели", "weekly"),
]


class ScheduleDialog(QDialog):
    def __init__(self, items, parent, row=None):
        super().__init__(parent)
        self.token = uuid.uuid4().hex
        self.setWindowTitle("Изменить расписание" if row else "Создать расписание")
        self.setObjectName("schedule_dialog")
        self.resize(650, 600)
        self.items = items
        layout = QVBoxLayout(self)
        content = QWidget()
        content_layout = QVBoxLayout(content)
        self.remote_target = RemoteTarget(
            parent.agent_items,
            self,
            agent_id=row.get("agent_id") if row else None,
            outputs=row.get("outputs", []) if row else [],
        )
        content_layout.addWidget(self.remote_target)
        form = QFormLayout()
        self.project = QComboBox()
        self.project.setObjectName("schedule_project")
        for item in items:
            self.project.addItem(item["name"], item["id"])
        self.project.setEnabled(row is None)
        form.addRow("Сохранённый проект", self.project)
        self.version = QLabel()
        self.version.setWordWrap(True)
        self.version.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        form.addRow("Закреплённая версия", self.version)
        self.mode = QComboBox()
        self.mode.setObjectName("schedule_mode")
        for label, code in MODES:
            self.mode.addItem(label, code)
        form.addRow("Режим", self.mode)
        self.zone = QComboBox()
        self.zone.setEditable(True)
        self.zone.setObjectName("schedule_timezone")
        self.zone.addItems(sorted(available_timezones()))
        self.zone.setCurrentText("UTC")
        form.addRow("Часовой пояс IANA", self.zone)
        self.start = QLineEdit(
            (datetime.now(ZoneInfo("UTC")) + timedelta(minutes=2)).strftime("%Y-%m-%d %H:%M:%S")
        )
        self.start.setObjectName("schedule_start")
        form.addRow("Начало (местное YYYY-MM-DD HH:MM:SS)", self.start)
        self.interval = QSpinBox()
        self.interval.setRange(1, 31536000)
        self.interval.setValue(60)
        self.interval.setObjectName("schedule_interval")
        form.addRow("Интервал, секунд (абсолютное UTC)", self.interval)
        self.clock = QLineEdit("09:00:00")
        self.clock.setObjectName("schedule_time")
        form.addRow("Календарное время HH:MM:SS", self.clock)
        days = QWidget()
        day_layout = QHBoxLayout(days)
        self.days = []
        for i, label in enumerate(["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]):
            check = QCheckBox(label)
            check.setObjectName("schedule_day_" + str(i))
            check.setChecked(i < 5)
            self.days.append(check)
            day_layout.addWidget(check)
        form.addRow("Дни недели", days)
        self.policy = QComboBox()
        self.policy.setObjectName("schedule_misfire")
        self.policy.addItem("Пропустить устаревшие события", "skip")
        self.policy.addItem("Догнать только одно последнее событие", "latest")
        form.addRow("После простоя", self.policy)
        self.grace = QSpinBox()
        self.grace.setRange(0, 3600)
        self.grace.setValue(60)
        self.grace.setObjectName("schedule_grace")
        form.addRow("Допустимое опоздание, секунд", self.grace)
        content_layout.addLayout(form)
        note = QLabel(
            "Версия и защищённые входы закрепляются. Исходный файл не перечитывается при запуске. "
            "Несуществующее календарное время DST пропускается; повторяющийся час исполняется один раз. "
            "Сохранение запускает службу; закрытие окна её не останавливает."
        )
        note.setWordWrap(True)
        content_layout.addWidget(note)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(content)
        layout.addWidget(scroll, 1)
        self.events = QTextEdit()
        self.events.setReadOnly(True)
        self.events.setMaximumHeight(100)
        self.events.setObjectName("schedule_preview_text")
        layout.addWidget(self.events)
        self.error = QLabel()
        self.error.setWordWrap(True)
        layout.addWidget(self.error)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setObjectName("save_schedule")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setObjectName("cancel_schedule")
        buttons.accepted.connect(self.validate)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        if row:
            spec = row["spec"]
            self.mode.setCurrentIndex(self.mode.findData(spec["mode"]))
            self.zone.setCurrentText(spec["timezone"])
            self.start.setText(
                datetime.fromtimestamp(spec["start"], ZoneInfo(spec["timezone"])).strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
            )
            self.interval.setValue(spec["interval"])
            self.clock.setText(spec["time"])
            self.policy.setCurrentIndex(self.policy.findData(spec["misfire"]))
            self.grace.setValue(spec["grace"])
            for i, check in enumerate(self.days):
                check.setChecked(i in spec["weekdays"])
        self.pinned = row["version"] if row else None
        self.project.currentIndexChanged.connect(self.changed)
        self.mode.currentIndexChanged.connect(self.changed)
        for field in [self.start, self.clock]:
            field.textChanged.connect(self.changed)
        self.zone.currentTextChanged.connect(self.changed)
        for field in [self.interval, self.grace]:
            field.valueChanged.connect(self.changed)
        self.policy.currentIndexChanged.connect(self.changed)
        for check in self.days:
            check.toggled.connect(self.changed)
        self.debounce = QTimer(self)
        self.debounce.setSingleShot(True)
        self.debounce.timeout.connect(self.preview)
        self.changed()

    def values(self):
        try:
            local = datetime.strptime(self.start.text(), "%Y-%m-%d %H:%M:%S")
            zone = ZoneInfo(self.zone.currentText())
            stamp = int(local.replace(tzinfo=zone, fold=0).timestamp())
            if datetime.fromtimestamp(stamp, zone).replace(tzinfo=None) != local:
                raise ValueError()
            return parse_spec(
                dict(
                    mode=self.mode.currentData(),
                    start=stamp,
                    timezone=self.zone.currentText(),
                    interval=self.interval.value(),
                    time=self.clock.text(),
                    weekdays=[i for i, c in enumerate(self.days) if c.isChecked()],
                    misfire=self.policy.currentData(),
                    grace=self.grace.value(),
                )
            ).model_dump()
        except Exception as exc:
            raise DispatcherError("SCHEDULE_INVALID") from exc

    def changed(self, *_):
        item = self.items[self.project.currentIndex()] if self.items else None
        self.version.setText(self.pinned or (item["version"] if item else "Нет сохранённых проектов"))
        mode = self.mode.currentData()
        self.interval.setEnabled(mode == "interval")
        self.clock.setEnabled(mode in {"daily", "weekly"})
        for check in self.days:
            check.setEnabled(mode == "weekly")
        self.debounce.start(300)

    def preview(self):
        try:
            value = self.values()
            self.error.clear()
            parent = self.parent()
            parent.submit(
                "schedule_preview", lambda: request(parent.root, "schedule_preview", spec=value), self.token
            )
        except DispatcherError as exc:
            self.error.setText(MESSAGES[exc.code])

    def display_preview(self, events):
        self.events.setPlainText(
            "Ближайшие события:\n"
            + "\n".join(
                f"{e['local']} · UTC {datetime.fromtimestamp(e['utc'], ZoneInfo('UTC')).isoformat()}"
                for e in events
            )
        )

    def validate(self):
        try:
            self.values()
            self.remote_target.values()
            if not self.items:
                raise DispatcherError("PROJECT_INVALID")
            self.accept()
        except DispatcherError as exc:
            self.error.setText(MESSAGES[exc.code])
