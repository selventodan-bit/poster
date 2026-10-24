"""Registry property generation with explicit UI overrides for data actions."""

from typing import get_args, get_origin, Literal

from .actions import Property


LABELS = {
    "url": "URL",
    "method": "HTTP-метод",
    "query": "Query-параметры",
    "headers": "Заголовки",
    "cookies": "Cookies",
    "body_type": "Тип тела",
    "body": "Тело запроса",
    "files": "Файлы multipart",
    "timeout_ms": "Таймаут, мс",
    "follow_redirects": "Перенаправления",
    "expected_status": "Ожидаемые статусы",
    "response_type": "Формат ответа",
    "max_response_bytes": "Лимит ответа, байт",
    "download_path": "Сохранить ответ",
    "overwrite": "Разрешить перезапись",
    "protect_response": "Защитить ответ",
    "name": "Имя",
    "value": "Значение",
    "path": "Путь",
    "secure": "Только HTTPS",
    "text": "Текст",
    "pretty": "Форматировать JSON",
    "new_value": "Новое значение",
    "left": "Левое значение",
    "right": "Правое значение",
    "deep": "Рекурсивно",
    "items": "Массив",
    "item": "Элемент",
    "index": "Индекс с нуля",
    "seed": "Seed / null",
    "encoding": "Кодировка",
    "source": "Источник",
    "destination": "Назначение",
    "missing_ok": "Разрешить отсутствие",
    "pattern": "Маска файлов",
    "include_hidden": "Скрытые файлы",
    "columns": "Столбцы",
    "rows": "Строки",
    "table": "Таблица",
    "row": "Строка",
    "column": "Столбец",
    "operator": "Оператор",
    "delimiter": "Разделитель",
    "has_header": "Есть заголовок",
    "infer_json": "Читать типы JSON",
    "header": "Записать заголовок",
    "sheet": "Лист",
    "update_existing": "Изменять книгу",
    "replace_sheet": "Заменять лист",
    "inputs": "Входы подпроекта",
    "outputs": "Выходы подпроекта",
}


def properties(model, labels=None, *, paths=(), secrets=()):
    result = []
    for name, field in model.model_fields.items():
        annotation = field.annotation
        choices = get_args(annotation) if get_origin(annotation) is Literal else ()
        kind = (
            "choice"
            if choices
            else "bool"
            if annotation is bool
            else "integer"
            if annotation is int
            else "file"
            if name in paths
            else "multiline"
            if annotation is str and name == "text"
            else "text"
            if annotation is str
            else "value"
        )
        result.append(
            Property(
                name,
                (labels or {}).get(name, LABELS.get(name, name)),
                kind,
                choices,
                name in secrets,
                field.description or "",
            )
        )
    return tuple(result)
