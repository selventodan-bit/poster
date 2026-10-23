from dataclasses import dataclass, field
from enum import StrEnum


class LearningState(StrEnum):
    DETECTED = "DETECTED"
    BROWSER_VERIFIED = "BROWSER_VERIFIED"
    HTTP_CANDIDATE = "HTTP_CANDIDATE"
    HTTP_VERIFIED = "HTTP_VERIFIED"
    LEARNING_REQUIRED = "LEARNING_REQUIRED"
    UNKNOWN = "UNKNOWN"
    FAILED = "FAILED"


MESSAGES = {
    "MISSING_REQUIRED_FIELDS": "В форме есть дополнительные обязательные поля. Заполните их в браузере и повторите распознавание.",
    "DIAGNOSTIC_UNAVAILABLE": "Нет доступа к локальному журналу диагностики. Выберите доступную для записи папку программы.",
    "FIELD_FILL_FAILED": "Не удалось заполнить поле. Отправка не выполнялась; проверьте редактор.",
    "BUTTON_DISAPPEARED": "Кнопка исчезла после обновления страницы. Распознайте форму заново.",
    "CLICK_FAILED": "Не удалось нажать выбранную кнопку. Проверьте состояние страницы.",
    "NETWORK_FAILED": "Ошибка сети при чтении результата. Повторная отправка не выполнялась.",
    "RESULT_READ_FAILED": "Не удалось прочитать подтверждение публикации. Проверьте сайт вручную.",
    "PUBLICATION_UNCERTAIN": "Запрос мог выполниться. Повтор запрещён; проверьте публикацию на сайте.",
    "WORKFLOW_BUILD_FAILED": "Публикация подтверждена, но HTTP-сценарий построить не удалось. Требуется обучение.",
    "OPERATION_CANCELLED": "Операция остановлена до отправки.",
    "STATIC_RESOURCE_BLOCKED": "Редактор может быть недоступен: внешний ресурс заблокирован. Разрешите его точный HTTPS origin в настройках.",
    "NAVIGATION_AMBIGUOUS": "Несколько безопасных путей к форме. Откройте нужную форму в браузере и повторите распознавание.",
    "CONFIRMATION_PENDING": "Время ожидания истекло; результат пока неизвестен. Проверьте сайт перед новой отправкой. Повтор не выполнялся.",
    "CONTENT_REQUIRED": "Для темы укажите разные непустые заголовок и текст; для ответа или комментария — непустой текст.",
    "SESSION_KEY_INVALID": "Ключ сессии недоступен или имеет неверный формат. Исправьте POSTER_SESSION_KEY; отправка не выполнялась.",
    "NO_FORM": "Не найдена однозначная форма публикации. Покажите её в режиме обучения.",
    "AMBIGUOUS_FORM": "Найдено несколько похожих форм. Выберите нужную форму.",
    "AUTH_REQUIRED": "Возможно, требуется вход: обнаружены поля пароля или страница входа.",
    "NO_WRITE": "В записи не найден запрос отправки. Проверьте, была ли форма отправлена.",
    "CONTENT_NOT_MAPPED": "Не удалось связать введённое содержимое с запросами.",
    "UNLINKED_REQUESTS": "Связь запросов публикации не доказана.",
    "UNKNOWN_DYNAMIC": "У изменяемого параметра не установлен источник; HTTP-проект требует обучения.",
    "UNSUPPORTED_ENCODING": "Формат запроса пока не поддерживается или содержит файл.",
    "NO_PERMALINK": "Нет независимого подтверждения по постоянной ссылке; повторная отправка запрещена.",
    "BLOCKED_HOST": "Заблокирован запрос к постороннему домену.",
    "READ_ONLY": "Исследование выполнено без разрешения запросов, изменяющих данные.",
    "ALREADY_ATTEMPTED": "Эта отправка уже выполнялась. Автоматический повтор запрещён.",
    "CONSENT_REQUIRED": "Нужно отдельное явное разрешение на эту реальную отправку.",
    "HTTP_UNVERIFIED": "HTTP-сценарий подготовлен, но ещё не проверен отдельной отправкой.",
    "HTTP_FAILED": "HTTP-повторение не подтверждено. Проект не допущен к исполнению.",
    "BROWSER_ERROR": "Браузерная операция не завершена. Сессия закрыта; можно начать новое распознавание.",
    "PUBLICATION_REJECTED": "Сайт явно отклонил отправку. HTTP 200 не считается подтверждением.",
    "MANUAL_READY": "Заполнены найденные поля. Покажите правильную форму и отправьте один раз вручную.",
    "AUTH_EXPIRED": "Сессия истекла или доступ запрещён. Войдите в браузере и начните новое распознавание.",
    "WRITE_SCOPE": "Заблокирована отправка, не связанная с разрешённой публикацией.",
    "KNOWLEDGE_CONFLICT": "Найденные правила противоречат друг другу. Требуется выбор или ручное обучение.",
    "DISTINCT_CONTENT": "HTTP-проверка требует нового текста; для темы также нужен новый заголовок, отличающийся от браузерного теста.",
    "EDITOR_SYNC": "Редактор не принял введённый текст. Отправка не выполнялась.",
}


@dataclass
class Confidence:
    engine: float = 0
    form: float = 0
    fields: float = 0
    publication: float = 0
    result: float = 0
    http_verified: bool = False


@dataclass
class FieldRef:
    role: str
    selector: str
    frame: int = 0
    kind: str = "input"
    confidence: float = 0
    position: int = 0
    editor: str = "native"
    features: list[str] = field(default_factory=list)


@dataclass
class FormCandidate:
    key: str
    fields: dict[str, FieldRef]
    confidence: float
    frame: int = 0
    method: str = ""
    action: str = ""
    evidence: list[str] = field(default_factory=list)
    text_inputs: list[FieldRef] = field(default_factory=list)
    alternatives: dict[str, list[FieldRef]] = field(default_factory=dict)
    structure: list[str] = field(default_factory=list)
    publication_kind: str = "topic"
    required_fields: list[str] = field(default_factory=list)

    @property
    def required_roles(self):
        return {"body", "submit"} | ({"title"} if self.publication_kind == "topic" else set())

    @property
    def complete(self):
        return (
            all(
                selector in {f.selector for role, f in self.fields.items() if role in {"title", "body"}}
                for selector in self.required_fields
            )
            and self.publication_kind in {"topic", "reply", "comment"}
            and self.required_roles <= self.fields.keys()
        )


@dataclass
class Recognition:
    state: LearningState = LearningState.LEARNING_REQUIRED
    engine: str = "unknown"
    confidence: Confidence = field(default_factory=Confidence)
    forms: list[FormCandidate] = field(default_factory=list)
    selected: str | None = None
    auth_required: bool = False
    diagnostics: list[str] = field(default_factory=list)
    rule_id: str | None = None
    technologies: list[dict] = field(default_factory=list)
    navigation: list[dict] = field(default_factory=list)
    outcome: str = "not_attempted"
    transfer: dict = field(default_factory=dict)

    def summary(self):
        """User-facing diagnostics contain semantic roles/codes, never page values or raw requests."""
        return {
            "state": str(self.state),
            "engine": self.engine,
            "confidence": dict(self.confidence.__dict__),
            "auth_required": self.auth_required,
            "selected": self.selected,
            "rule_id": self.rule_id,
            "technologies": self.technologies,
            "navigation": self.navigation,
            "outcome": self.outcome,
            "transfer": self.transfer,
            "forms": [
                {
                    "id": f.key,
                    "roles": sorted(f.fields),
                    "publication_kind": f.publication_kind,
                    "confidence": f.confidence,
                    "editors": sorted({v.kind for v in f.fields.values() if v.role == "body"}),
                    "evidence": f.evidence,
                    "alternatives": {
                        role: [
                            {"position": r.position, "score": r.confidence, "evidence": r.features}
                            for r in refs
                        ]
                        for role, refs in f.alternatives.items()
                    },
                }
                for f in self.forms
            ],
            "diagnostics": [{"code": c, "message": MESSAGES[c]} for c in dict.fromkeys(self.diagnostics)],
        }
