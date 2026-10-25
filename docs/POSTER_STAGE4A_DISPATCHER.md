# Poster 3.0 — Dispatcher 4A

Dispatcher исполняет существующие графы Maker v2 (и v1 через прежнее чтение Graph).
Кубики выполняет только `poster.maker.runtime.GraphRunner`. Maker, Registry из 78
действий и старый Engine не заменены. Новый компонент доступен через установленный
Python на Windows/Linux; кнопка или CLI-режим portable `Poster.exe` в 4A не добавлены.

## Первая проверка

В окружении с установленным Poster (`python -m pip install ".[recorder]"` из checkout):

```text
python -m poster dispatcher enqueue examples/maker-stage3b2/02-lists-loop.postergraph.json --key first-list
python -m poster dispatcher list
python -m poster dispatcher run --once --max-workers 2 --max-per-project 1
python -m poster dispatcher list
python -m poster dispatcher show JOB_ID --report
python -m poster dispatcher history JOB_ID
```

Замените JOB_ID идентификатором из enqueue. `show --report` сообщает путь workdir;
в `lists/result.json` будут ALPHA, BETA, ГАММА. Все команды принимают `--db PATH`
**после подкоманды**, по умолчанию `.poster/dispatcher.sqlite`.

Два проекта параллельно:

```text
python -m poster dispatcher enqueue examples/maker-stage3b2/02-lists-loop.postergraph.json --key list-2
python -m poster dispatcher enqueue examples/maker-stage3b2/03-csv-excel.postergraph.json --key table-1
python -m poster dispatcher run --once --max-workers 2 --max-per-project 1
```

`run` без `--once` ждёт новые задания. Лимиты 1–32 процессов; max_per_project
применяется к Graph.id независимо от версии. Фактическое перекрытие двух процессов
и отсутствие перекрытия при лимите 1 проверяются серверными Event-барьерами.
Очередь — локальная, один активный Dispatcher на базу. Второй экземпляр получает
DISPATCHER_BUSY; SQLite атомарно резервирует каждый job. List/show/history можно
запускать из других процессов одновременно с обслуживанием очереди.

Автоматическая демонстрация четырёх готовых проектов:

```text
python scripts/check_dispatcher.py --output .poster/dispatcher-demo
```

Скрипт владеет локальным HTTP-сервером. Для самостоятельного HTTP-примера сначала
нужен доступный API; `--input api_base=http://127.0.0.1:PORT` переопределяет вход.

## Архитектура и файлы

* `models.py`: состояния и безопасные сообщения.
* `store.py`: новая SQLite, транзакции, история, ограниченные события.
* `snapshots.py`: зашифрованные версии дерева проектов с SHA256.
* `processes.py`: отдельный Python, POSIX process groups / Windows Job Objects.
* `worker.py`: приватный entrypoint, существующий GraphRunner, фильтр отчёта.
* `service.py`: постановка, scheduling, ограничения, stop/cancel/recovery.
* `cli.py`: команды, чувствительные входы через файл, безопасные ошибки.

Рядом с БД создаётся `dispatcher.sqlite.data/`: private key, snapshots и runs.
Другой путь БД даёт другой каталог данных. Старые `.poster/state.sqlite`, HTTP
Engine/READY-проекты, Drive, KnowledgeBase и их схемы не меняются.

SQLite имеет application_id, user_version=1, WAL, synchronous=FULL, busy_timeout,
foreign keys, уникальные job/run/dedup и триггер допустимых переходов. Чужая или
более новая схема отвергается до миграции. Обновление схемы выполняется под
BEGIN IMMEDIATE; миграция начальной пустой БД атомарна. События ограничены 200 на
job / 10000 на БД. История заданий не удаляется автоматически.
Отдельная кратковременная schema-блокировка охватывает также чтение версии и
настройку WAL при одновременном первом открытии; создание локального ключа
сериализуется отдельно. Это не блокировка на всё время работы очереди.

Проект фиксируется до PENDING. Резервирование PENDING → LEASED создаёт execution.
Worker получает разрешение через anonymous stdin pipe **только после** записи
его PID/create_time в SQLite. До разрешения он не исполняет граф. Проверенный
worker атомарно переводит LEASED → RUNNING. Терминальные статусы: SUCCEEDED,
FAILED, CANCELLED, TIMED_OUT, UNKNOWN, REVIEW_REQUIRED. Повторных попыток по
таймеру или автоматического retry нет. Один явно повторённый `--key` возвращает
старый job, даже если исходный проект изменён; ключ в БД хранится как SHA256.

## Версии и зависимости

Snapshot включает главный JSON, все `*.postergraph.json` в ограниченном дереве
его каталога, статически указанные относительные Project.Run JSON и связанные
`.secrets.enc`. Пути сохраняются. Динамический путь может выбрать только файл,
попавший в снимок; отсутствующий файл вызывает ошибку, обращения к живому источнику
нет. Используйте отдельный каталог проектов. Скрытые каталоги и собственное
хранилище Dispatcher исключены из автоматического обхода.

Ограничения: 2048 записей обхода, глубина каталогов 8, 128 файлов, 20 MB суммарно;
прежний load_graph дополнительно ограничивает размер каждого графа/sidecar.
Symlink, Windows reparse points и выход за корень запрещены. JSON типов действий
проверяется существующим Registry. Python-код из JSON не выполняется.

Manifest содержит пути и SHA256; его SHA256 — версия проекта в БД. Содержимое
дерева зашифровано существующим Fernet (`poster.auth`), включая обычные графы:
даже случайные plaintext-параметры импортированного графа не лежат открыто в
снимке. Перед запуском worker проверяет manifest, расшифровку и SHA256 всех файлов,
затем создаёт приватную копию с файлами только для чтения. После исполнения эта
расшифрованная копия удаляется. Оригинал можно редактировать после enqueue —
ожидающее задание выполнит закреплённую версию. Подмена снимка запрещает исполнение.
Публикация снимка — атомарное переименование готового staging-каталога; прерванная
запись не оставляет частично опубликованную версию, на которую мог бы сослаться job.

Это защита от изменений проектов и действий графа, не OS-песочница против
враждебного процесса того же пользователя, подменяющего память/файлы/базу.

## Входы и секреты

Обычные входы: `--input name=JSON_OR_TEXT` или `--inputs-file ordinary.json`.
Объявленные защищённые входы нельзя передать как открытое значение через --input.
Для секретов подготовьте приватный JSON-объект и используйте только путь:

```text
python -m poster dispatcher enqueue my-project.postergraph.json --secrets-file private-inputs.json --key account-1
```

Типы JSON сохраняются. Имя должно быть объявленным input графа; `--secrets-file`
также позволяет защитить обычный объявленный вход явно. После чтения весь envelope
входов шифруется в SQLite; worker создаёт собственные SecretRef в своём Vault.
Сохраняется прежняя taint/masking-модель. Исходный пользовательский secrets-file
Dispatcher не удаляет и не публикует — храните его приватно.

Без POSTER_SESSION_KEY Dispatcher создаёт собственный локальный ключ. Для проекта
с Maker sidecar укажите тот же ключ через окружение POSTER_SESSION_KEY либо
`--key-file PATH`, например `.poster/private/session.key`, **на enqueue и run**.
Значение ключа не передаётся аргументом; worker получает его через анонимный pipe.
Ключи, профили и secrets-file не экспортируются в CI/Drive/JSON-графы. В Windows
новый приватный каталог получает ACL текущего SID, в POSIX — 0700/0600.
Граф не может разрешить себе env-секреты или внешний файловый доступ.

Профиль задаётся только явно: `enqueue ... --profile-root PATH --profile profile_ID`.
Его зашифрованное состояние фиксируется в envelope, worker восстанавливает его
в отдельный браузерный контекст. Файловая блокировка профиля удерживается worker
на всё исполнение и действует между разными Dispatcher-базами. Ожидание профиля
входит в timeout и занимает worker-слот. Dispatcher не сохраняет сессию обратно;
при реально занятой блокировке в событиях появляется PROFILE_BUSY. Тест выполняет
пять последовательных циклов двух очередей и ждёт именно подтверждённой блокировки.
Maker использует прежние атомарные versioned snapshots, общий mutable user-data
каталог браузера не появляется. Metadata графа сама не предоставляет профиль.

## Stop, cancel, timeout и авария

```text
python -m poster dispatcher stop
python -m poster dispatcher cancel JOB_ID
python -m poster dispatcher reconcile
```

Stop прекращает новые резервирования и дожидается активных заданий в пределах
их индивидуальных timeout. PENDING сохраняются. Ctrl+C/SIGTERM также прекращает
приём; повторный Ctrl+C прерывает ожидание и запускает процедуру отмены активных.
Cancel PENDING сразу даёт CANCELLED. Выполняющемуся worker посылается запрос,
GraphRunner.stop закрывает HTTP/browser. Есть ограниченный grace period, затем
принудительное завершение дерева. Timeout включает запуск процесса и ожидание
профиля; диапазон 0.05–3600 с, по умолчанию 300. Исходные Budget/FileSandbox и
лимиты шагов/файлов/результатов остаются действующими.

До потенциально внешнего действия worker фиксирует effects_possible в БД.
HTTP.Request/Post, Browser.* и Project.Run помечаются консервативно. При отмене
или timeout такого исполнения результат UNKNOWN; без таких действий возможны
CANCELLED/TIMED_OUT. HTTP_WRITE_UNKNOWN и read/write timeout с неопределённой
записью дают UNKNOWN даже при обработанном Error-переходе. Отмена не откатывает
уже выполненные файловые операции или публикации.

При потере Dispatcher Windows Job Object убивает дерево при закрытии handle;
на POSIX worker обнаруживает EOF, останавливает Runner и имеет независимый
watchdog. При рестарте/ reconcile проверяется PID **вместе с create_time** и
останавливаются оставшиеся собственные процессы. LEASED без записанного PID
может вернуться в PENDING: по протоколу gate ещё не открывался. LEASED с PID и
RUNNING не возвращаются автоматически — UNKNOWN. Reconcile переводит такие
задания в REVIEW_REQUIRED и **ничего не исполняет**.
Один вызов обрабатывает до 1000 UNKNOWN; при большем числе повторите reconcile.

После UNKNOWN сначала проверьте внешнюю систему. Повтор возможен только явной
постановкой нового задания с новым ключом; прежний key остаётся дедуплицированным.
Автоматический тест убивает worker/Dispatcher после приёма POST сервером, пока
подтверждение удерживается Event, затем проверяет отсутствие второго POST.

## Отчёты и ограничения 4A

Show/history сохраняют job/run ID, версию, PID, attempt, времена, exit code,
безопасный код ошибки, последний подтверждённый шаг и пути. `show --report`
проверяет SHA256 подтверждения. Публичный отчёт не содержит inputs, variables,
HTTP payload, URL, stdout/stderr или произвольный текст исключений. Полные
результаты сохраняются только в `results.enc`; рабочие файлы графа остаются в
его уникальном workdir. Явная Files.WriteText секрета по намерению графа не
становится зашифрованным файлом автоматически. CI публикует только безопасные
отчёты/счётчики, не SQLite, key, ciphertext inputs или workdir.

Run --once возвращает ненулевой код, если новое исполнение этого запуска не
SUCCEEDED. Бизнес-статус всегда проверяйте через show/history. Нельзя делать
вывод об откате публикации по одному exit code.

HTTP/data/files-only worker запрещает импорт Qt, Tkinter и Playwright. Для
Browser.* используется существующий внешний Chromium provider; браузер сам не
скачивается. На Linux установите браузер/зависимости явно, на Windows используется
найденный Chrome/Chromium. Qt GUI остаётся отдельным Maker.

Не реализованы: portable Dispatcher launcher, новая GUI-панель, distributed/VPS
queue, cron, domain concurrency, CPU/memory quotas и автоматические retry.
CPU/memory quotas не выдаются за одинаково работающие на Windows/Linux.
Workdir нового задания изначально пуст: внешние CSV/XLSX/другие входные файлы
автоматически не копируются. Граф создаёт данные из входов/HTTP либо должен быть
подготовлен к этому контракту; отдельный импорт ресурсов не добавлен в 4A.
GraphRunner сохраняет ограничения подпроектов 3B.2 (без Browser.* в ребёнке).
Для Windows Maker → Linux нужны сам JSON и зависимости; ключи секретов необходимо
предоставлять отдельно по явному безопасному каналу.

CI сохраняет всю прежнюю матрицу. Дополнительно проверяет wheel/CLI Dispatcher
на Linux 3.11/3.13 и Windows 3.11/3.12/3.13, реальные параллельные процессы,
stop/cancel/timeout/crash, профили и Chromium cleanup. Отдельное Linux-задание
скачивает **файлы настоящего Windows Maker** и исполняет четыре data-графа и
браузерный граф через очередь Dispatcher, сверяя неизменность исходных байтов.
