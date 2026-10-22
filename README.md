# Poster HTTP MVP 0.1

Python 3.11+: **Recorder → декларативный проект → HTTP Engine**, горячая загрузка проектов/словарей, независимый генератор контента, SQLite и восстановление. Браузер нужен только Recorder. Исходное ТЗ: [specifications/](specifications/).

## Первая проверка в подготовленной среде

```bash
cd /workspace/poster
.venv/bin/python -m poster demo --output .poster/my-first-check
.venv/bin/python -m pytest -q
```

Для каждого demo выберите новую пустую папку. Это реальный E2E: Chromium вводит маркеры и отправляет две формы; Recorder автоматически экспортирует `site.yaml/workflow.yaml`; браузер закрывается; работающий Engine подхватывает проекты и отправляет новый контент через `httpx`. Проверяются заголовок **и текст** результата, новый одноразовый CSRF, отсутствие дублей после рестарта и целостность SQLite backup.

Ожидаемый итог: `status: SUCCEEDED`, `browser_writes: 2`, `verified_http_writes: 2`, `hot_load_without_restart: true`, `restart_duplicate_writes: 0`. Отчёт: `.poster/my-first-check/report.json`, проекты: `projects/`, база: `state.sqlite`, backup: `snapshot.sqlite`. Тестовые серверы останавливаются по завершении; URL из отчёта — диагностические артефакты.

## Установка на другом компьютере

```bash
python -m venv .venv
# Linux/macOS:
source .venv/bin/activate
# Windows PowerShell вместо предыдущей строки:
# .\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev,recorder]"
python -m playwright install chromium
python -m poster demo --output demo-output
```

Установка Chromium не нужна при наличии системного `chromium`/`chromium-browser` или `POSTER_CHROMIUM` с путём к нему. Если Linux сообщает о нехватке библиотек: `python -m playwright install --with-deps chromium` (потребуются права установки пакетов). Windows Python с python.org включает `tkinter`.

Для HTTP Engine достаточно `python -m pip install -e .`; Playwright и браузер на VPS/ноутбуке не нужны. Зафиксированные зависимости: `uv.lock`, альтернативная установка — `uv sync --frozen --extra dev --extra recorder`.

## Recorder

```bash
python -m poster fixtures --port 8765
# Во втором терминале:
python -m poster keygen
# Сохраните ключ вне проектов/Drive и задайте POSTER_SESSION_KEY.
python -m poster recorder
```

В UI задайте URL и ID, нажмите «Запись». В отдельном Chromium авторизуйтесь, внесите показанные `REC_TITLE_...`/`REC_BODY_...`, отправьте форму, дождитесь результата: «Стоп → Анализ». Для fixtures селектор подтверждения — `[data-result-id]`; для своего сайта укажите элемент, появляющийся именно при успехе. «Проверить HTTP» выполняет дополнительную настоящую отправку после подтверждения пользователя. «Экспорт» требует пустую папку с именем ID проекта.

Cookies/auth-заголовки сохраняются только в `session.enc` (Fernet); ключ — отдельно в `POSTER_SESSION_KEY`. Профили `.poster/profiles/` тоже содержат сессии и не должны попадать в Git или общий Drive. [Подробнее](docs/recorder.md).

## Engine и горячие обновления

```bash
python -m poster engine --projects .poster/projects --content examples/content --db .poster/state.sqlite --poll 60
```

Добавьте `.poster/projects/<id>/` целиком, **READY последним**. Проект появится без рестарта. После изменения YAML выполните `python -m poster ready .poster/projects/<id>`. Неполная/невалидная версия не заменяет рабочую. `enabled: false` или удаление прекращает новые задания; уже начатые сохраняют свой снимок. Дефолты: 10 задач глобально, 1 HTTP-запрос на хост; параметры `--max-concurrent-http`, `--max-per-host`, `--poll`.

Само обнаружение проекта не создаёт бесконечные отправки. Добавьте явное задание:

```bash
python -m poster enqueue .poster/projects/my-project --key first-check --title "Заголовок" --body "Текст"
python -m poster report --db .poster/state.sqlite
```

Или укажите `campaign: italian_info` в site.yaml и добавьте `targets.csv` в проект:

```csv
id,target_url,seed
first,http://127.0.0.1:8765/classic/new,42
```

Одна строка — одна задача. Ключ: проект + id строки + кампания + seed. Сканирование, новая ревизия и рестарт не повторяют её; новое действие требует нового id/seed. Проект и готовый текст фиксируются при постановке. Добавьте `--once`, чтобы однократно обработать очередь. Для локальных fixture-проектов сначала запустите fixture-сервер.

## Генератор без HTTP и AI API

```bash
python -m poster generate --content examples/content --campaign italian_info --seed 42
python -m poster generate --content examples/content --campaign french_info --seed 42
```

CSV/YAML таблицы продуктов и описаний связаны по product_id/локали; словари — TXT/YAML. Собственный парсер поддерживает `{{product.name}}`, `{{description}}`, `{{intro}}`, `{{keywords:3}}`, `{{cities:5}}`, `[[optional:0.5]]…[[/optional]]`. N значений выбираются без повторов. Это не Jinja и не исполнение кода. Недостаток значений вызывает ошибку.

Примеры без READY перечитываются при генерации. Для согласованных обновлений нескольких файлов: `python -m poster ready examples/content`; затем записывайте новый READY после каждого законченного изменения. При плохом обновлении работающий генератор сохраняет последний рабочий снимок. SQLite хранит готовый текст, seed, шаблон, выбранные значения и хеш источников. Примеры информационные; реклама из приложенных references в генератор не перенесена.

## Drive, SQLite и notebooks

- [Google Drive: авторизация, запуск и backup](docs/google_drive_setup.md).
- [Формат проекта и DSL](docs/project_format.md), [диагностика](docs/troubleshooting.md).
- [Решения и ограничения](DECISIONS.md).
- [Фактические результаты проверки](docs/VALIDATION.md).
- [Colab](notebooks/colab_quickstart.ipynb), [Molab](notebooks/molab_quickstart.ipynb): конечная HTTP-проверка без браузера; остановка runtime допустима, постоянная работа не предполагается.

```bash
python -m poster backup .poster/snapshot.sqlite --db .poster/state.sqlite
python -m poster restore .poster/snapshot.sqlite --db .poster/restored.sqlite
python -m poster reconcile --db .poster/restored.sqlite
```

Backup использует SQLite backup API, а не копирование живого WAL. Один Engine на локальную БД защищён OS-lock. После падения `LEASED → PENDING`, `RUNNING → UNKNOWN`; успешные задачи не повторяются. `reconcile` выполняет только заданные read-only recovery-шаги. Без подтверждения задача остаётся UNKNOWN; автоматического повторного POST нет.

Для локального выполнения notebooks: установите `python -m pip install -e ".[notebook]"`, затем `python scripts/check_notebooks.py` в окружении с Python-kernel этой venv (`python -m ipykernel install --user --name poster`, `POSTER_NOTEBOOK_KERNEL=poster`). Исполненные копии сохраняются в `.poster/notebooks/`. Установка kernel в пользовательский каталог выполняется на вашем компьютере; в текущей cloud-проверке использован отдельный writable prefix.

## Проверки и ограничения

```bash
python -m pytest -q
ruff check .
ruff format --check .
```

Тесты: настоящий Chromium capture → hot load → словари → HTTP → проверка обеих форм; CSRF, таймаут после записи, редиректы/cookies, dry-run без сети, атомарные обновления, Drive pagination/backoff, SQLite recovery/backup. Drive проверяется контролируемым транспортом. Настоящий аккаунт Google, графический Windows и размещённые Colab/Molab здесь не тестировались.

Автоматический Recorder рассчитан на одну URL-encoded HTML-форму. JSON API и текстовый multipart есть в ручном DSL; сложные цепочки, бинарные uploads, повторяющиеся поля, JS-подписи, CAPTCHA, browser fallback и автоматический ремонт произвольного сайта пока не реализованы. HTTP_READY означает ограниченный анализ; надёжность нового проекта подтверждайте явным тестом. HTTP 200 отдельно не считается успехом.
