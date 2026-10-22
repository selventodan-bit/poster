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
    "CONTENT_REQUIRED": "Для обучения укажите непустые заголовок и текст, отличающиеся друг от друга.",
    "SESSION_KEY_INVALID": "Ключ сессии недоступен или имеет неверный формат. Исправьте POSTER_SESSION_KEY; отправка не выполнялась.",
    "NO_FORM": "Не найдена однозначная форма публикации. Покажите её в режиме обучения.",
    "AMBIGUOUS_FORM": "Найдено несколько похожих форм. Выберите нужную форму.",
    "AUTH_REQUIRED": "Возможно, требуется вход: обнаружены поля пароля или страница входа.",
    "NO_WRITE": "В записи не найден запрос отправки. Проверьте, была ли форма отправлена.",
    "CONTENT_NOT_MAPPED": "Не удалось связать введённые заголовок и текст с запросами.",
    "UNLINKED_REQUESTS": "Заголовок и текст передаются отдельно; связь запросов не доказана.",
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

    def summary(self):
        """User-facing diagnostics contain semantic roles/codes, never page values or raw requests."""
        return {
            "state": str(self.state),
            "engine": self.engine,
            "confidence": dict(self.confidence.__dict__),
            "auth_required": self.auth_required,
            "selected": self.selected,
            "rule_id": self.rule_id,
            "forms": [
                {
                    "id": f.key,
                    "roles": sorted(f.fields),
                    "confidence": f.confidence,
                    "editors": sorted({v.kind for v in f.fields.values() if v.role == "body"}),
                    "evidence": f.evidence,
                }
                for f in self.forms
            ],
            "diagnostics": [{"code": c, "message": MESSAGES[c]} for c in dict.fromkeys(self.diagnostics)],
        }
