# Фактическая проверка — 22 октября 2026

Проверено в текущей Linux cloud-среде, Python 3.12.14, системный Chromium, зависимости из uv.lock. Проверки конечные, внешние сайты для публикации не использовались.

| Проверка | Результат |
|---|---|
| `uv sync --frozen --extra dev --extra recorder --extra notebook` | Успешно |
| `python -m pytest -q` | 35 passed, без skipped/xfail |
| `ruff check .` | All checks passed |
| `ruff format --check .` | Все Python-файлы отформатированы |
| `poster demo --output .poster/final-demo` | SUCCEEDED |
| Настоящая запись обычной HTML-формы | HTTP_READY, автоматический экспорт |
| Настоящая запись формы с одноразовым CSRF | HTTP_READY, автоматическое извлечение свежего токена |
| HTTP после закрытия Recorder | 2 SUCCEEDED; сверены title и body на сервере |
| Добавление проектов работающему Engine | Обнаружены без рестарта |
| Рестарт с теми же targets и SQLite | 0 повторных отправок |
| SQLite backup/restore | integrity_check: ok, результаты сохранены |
| Отдельный процесс Engine с запретом импорта Playwright | HTTP-публикация успешно выполнена |
| `colab_quickstart.ipynb`, локальный kernel | HTTP/CSRF/генератор и SQLite restore: SUCCEEDED |
| `molab_quickstart.ipynb`, локальный kernel | HTTP/CSRF/генератор и SQLite restore: SUCCEEDED |

Локальные артефакты demo: `.poster/final-demo/report.json`, `projects/classic/`, `projects/csrf/`, `state.sqlite`, `snapshot.sqlite`. Тестовые серверы уже остановлены. Ключ сессии хранится отдельно в `private/session.key`, не в папках проектов; не публикуйте всю output-папку.

Тесты дополнительно проверяют stale-CSRF отказ, HTTP 200 без подтверждения, запрет чужих redirects/cookies, отсутствие секретов экспорта, dry-run без сети, read retries, UNKNOWN без повторного POST, read-only recovery после таймаута, JSON/multipart, глобальные/похостовые лимиты, горячее обновление словарей и шаблонов, сохранение рабочей версии при плохом READY, дедупликацию очереди, OS-lock и interrupted-job recovery.

Drive проверен через контролируемый HTTP-транспорт: pagination, 403 quota/429/503 backoff, cache, atomic download, плохие checksums, удаление проекта, отказ 401 и запрет удалённого кода. Реальная синхронизация пользовательского Drive требует ID папки и подходящей авторизации и в этой сессии не выполнялась. Google backup upload/restore реализованы, но live-операции не проверены. Графический UI на Windows и размещённые Colab/Molab не запускались. CI для Python 3.11/3.13 добавлен; удалённый GitHub Actions здесь не выполнялся.

Команды повторной проверки находятся в README. Неподдерживаемые сценарии и принятые решения — в DECISIONS.md. Неизвестные исходы и непротестированные внешние платформы не считаются успешными.
