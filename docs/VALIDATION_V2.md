# Проверка Poster 2.0 — 22 октября 2026

Работа выполнена на основе актуального main (`3ca4ad7`), в отдельной ветке `codex/poster-2-recognition`. Старые clipboard-исправления сохранены; HTTP Engine, очередь, Drive и генератор не заменены.

## Выполненные проверки

- `uv sync --frozen --extra dev --extra recorder --extra notebook`: успешно, package 0.2.0.
- `ruff check .`, `ruff format --check .`: успешно.
- `python -m pytest -q`: **65 passed**, без skipped/xfail. Все исходные 35 тестов продолжают проходить.
- `poster demo-recognition --output .poster/recognition-final`: **7 browser + 7 HTTP** проверенных публикаций на локальных реальных JS-страницах. Браузер закрывается перед каждым HTTP-повторением.
- Оба notebook smoke-примера повторно выполнены локально: HTTP/CSRF/content и SQLite backup/restore успешно. Это не hosted Colab/Molab.

| Требуемый случай | Фактическая проверка |
|---|---|
| Обычная HTML-форма | Реальный browser submit + HTTP redirect + title/body verify |
| AJAX без перезагрузки | fetch URL-encoded → JSON permalink → независимый GET |
| JSON POST | Вложенный JSON; Unicode/JSON escapes декодируются |
| Multipart | Настоящий FormData, текстовые части; binary uploads отклоняются |
| Contenteditable | Ввод обычного текста, HTML escaping, повтор с другим содержимым |
| Iframe-редактор | Same-origin frame найден, поля заполнены, HTTP-проект проверен |
| Динамический CSRF | Новый GET/token при HTTP; старый токен не фиксируется в YAML |
| Форма после JS-действия | Read-only исследование нажимает New topic и обнаруживает DOM |
| Несколько форм | Search игнорируется; два равноценных кандидата требуют выбора |
| Связанные запросы | draft ID из JSON ответа извлекается для следующего POST |
| Числовой ID | Тип JSON number сохраняется; строгий fixture не принимает строку вместо числа |
| JS без тега form | section + textarea + нестандартная кнопка, реальный fetch |
| HTTP 200 с ошибкой | FAILED, без ложного успеха и автоматического повтора |
| HTTP 200 без permalink | UNKNOWN, повтор теста запрещён |
| Подтверждение | GET постоянной ссылки, точное/whitespace сравнение title/body |
| Обобщение | На втором origin с другим оформлением и двумя несемантическими input без KB — LEARNING_REQUIRED; с изученным правилом — DETECTED, затем browser/HTTP успех |
| Секреты | CSRF отсутствует в YAML/JSON экспорта; session.enc зашифрован; KB запрещает URL/credentials/произвольные поля |
| Неизвестный JS token | Браузер успешен, HTTP — LEARNING_REQUIRED с UNKNOWN_DYNAMIC |
| Согласие | Без confirmed нет POST; HTTP требует второго отдельного согласия |
| Read-only | Browser gate блокирует незапрошенный POST и сторонний hostname |
| Engine без Playwright | Существующий отдельный процесс с import guard по-прежнему публикует через HTTP |
| Совместимость | Старые YAML, hot reload, SQLite/recovery, Drive mock adapter, генератор и notebooks проходят |
| UI lifecycle | Проверен отсев старых ошибок по generation ID и безопасное сохранение настроек |

Артефакты текущей машины: `.poster/recognition-final/report.json`, `projects/v2-*/`, `recognition.json`, `.poster/notebooks/`. Серверы demo конечные и останавливаются; URL из проектов не являются действующими публичными площадками. Сырые capture и ключи не коммитятся.

## Что не утверждается

Публикации на clubelectronicos.com не выполнялись. По старому сообщению Recorder точная причина не устанавливается; новая диагностика различает отсутствие запроса, формат, отсутствие контента, недоказанную связь, неизвестный токен и отсутствие подтверждения. Локальные похожие сценарии проверены, но это не доказывает совместимость конкретного публичного сайта.

Сигнатуры CMS — идентификация возможного семейства, не готовые протестированные адаптеры публикации всех перечисленных продуктов. Графический UI и start_recorder.cmd не исполнялись на настоящем Windows. Реальный Drive и hosted notebooks в этой задаче не тестировались. Подробные ограничения и архитектурные решения — recognition_architecture.md; инструкция Windows — windows_quickstart.md.
