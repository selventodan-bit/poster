# Poster Stage 5C — Google Drive, notebook и независимые запуски

Рабочая ветка: `codex/poster-5c-drive-cloud-multiworker`, исходный принятый
коммит: `74061608c57960bd0679fa9e20e64a84218c3986`. Эта поставка не изменяет main,
PR №2 и стабильные ветки 5A/5B; merge не выполняется.

**Maker создаёт → Google Drive хранит → выбранная среда выполняет → результаты возвращаются.**
Google Drive хранит версии и результаты. Управляемая очередь по-прежнему находится
в `dispatcher.sqlite` работающего Windows Controller. Исходные GraphRunner и
registry из 78 действий общие для Windows, Linux и notebook; второй исполнитель
действий и отдельная несовместимая управляемая очередь не создавались.

## 1. Подключить Google Drive

В Maker откройте меню **Google Drive → Подключить Google Drive**. При первом
подключении выберите конфигурацию OAuth **Desktop app**, затем разрешите доступ
в обычном браузере Google. Коды авторизации и tokens вручную копировать не нужно.
Windows Full ZIP содержит Python и необходимые библиотеки; отдельный Python,
терминал и установка пакетов на Windows не нужны.

Владелец OAuth-приложения один раз включает **Google Drive API** в Google Cloud
Console, настраивает consent screen/test users либо необходимую проверку Google,
создаёт OAuth client типа **Desktop app** и скачивает его JSON. Универсального
production OAuth client внутри Poster нет. Конфигурация web client отклоняется.

Применяются PKCE S256, случайный state, одноразовый callback на 127.0.0.1 со
случайным портом, timeout и отмена. Config/access/refresh tokens на Windows
защищены **DPAPI текущего пользователя и private ACL**, без открытого
`authorized_user.json`. На обычном Linux используется явно обозначенный local
Fernet store с private permissions; это не называется DPAPI.

Desktop запрашивает `drive.file` для записи своих файлов и `drive.readonly` для
чтения результатов, созданных другим OAuth client Colab/Molab. Последнее
разрешение технически даёт чтение Drive, хотя Poster обращается к своему каталогу.
Notebook/native Molab connection должен иметь разрешение записи в папки,
созданные другим приложением; применяется официальный Drive scope. Одного
`drive.file` для всех межклиентских сценариев недостаточно. Неполный grant и 403
не превращаются в успешное подключение/публикацию. Реальные grants конкретных
аккаунтов требуют отдельного provider E2E.

**Отключить аккаунт** удаляет локальную авторизацию и пытается отозвать credential
у Google. Если сеть недоступна, отзыв не объявляется подтверждённым. Независимый
приватный ключ расшифровки прежних результатов сохраняется. Утрата этого ключа
означает невозможность расшифровать адресованные ему файлы; это не Windows queue.key.

## 2. Создать проект в Maker

Используйте обычные кубики и переменные Maker. Ресурсы должны быть явно выбраны:
CSV, XLSX, JSON и другие файлы данных. Нажмите **Добавить CSV/XLSX/JSON…** в окне
Drive. Выбранный внешний файл копируется в ресурсы проекта. Совпадающие абсолютные
пути этого файла заменяются относительным путём только в переносимой версии;
исходник Maker сохраняет свой локальный путь. Не выбранные абсолютные пути
останавливают публикацию. Соседние документы автоматически не отправляются.

Укажите **Возвращаемые файлы**, например `result.txt; reports/table.csv`.
Демо находится в `examples/maker-stage5c/project.postergraph.json`: чтение CSV,
XLSX, JSON и запись result.txt. Защищённых значений и внешних POST в демо нет.

## 3. Сохранить проект в облако

Нажмите кнопку сохранения Google Drive на панели Maker либо **Google Drive →
Сохранить проект на Google Drive**. После подключения сохраняется проверяемая
версия. Сеть работает в отдельном Qt loop; окно продолжает обрабатывать события.
Не закрывайте ноутбук/программу до сообщения о завершённой публикации.

## 4. Где лежит проект

Логическая структура в My Drive:

```text
Poster3/
  Название проекта/             # уникальный folder ID и project ID
    .poster-project.json
    project.postergraph.json   # первый опубликованный снимок для просмотра
    versions/                  # immutable версии и committed manifests
    resources/                 # ресурсы каждой версии с SHA256
    results/
      <уникальный run ID>/
        output-0.box           # только зашифрованные разрешённые outputs
        committed-result.json  # появляется последним
```

Имена папок могут совпадать и изменяться: идентичность задают ID. Актуальная версия
выбирается по проверенной цепочке immutable commit records, а не по имени/mtime
первого `project.postergraph.json`. Количество проектов не связано с max_workers;
списки Drive читаются с пагинацией. UI загружает каталог в фоне; тысячи проектов
могут потребовать времени и квоты API.

## 5. Обновить или открыть проект

**Обновить облачную версию** добавляет версию, сохраняя историю. Повтор публикации
того же содержимого идемпотентен. Если другой редактор опубликовал новую версию,
Poster показывает конфликт и предлагает после обновления списка явное сохранение
следующей версии. Скрытой перезаписи чужого обновления нет.

**Открыть проект с Google Drive** создаёт новую локальную рабочую копию, проверяет
manifest/SHA256 и ресурсы. Старые версии остаются доступными. **Открыть папку**
открывает её в браузере по стабильному folder ID.

## 6. Открыть общий Colab / Jupyter Launcher

В Dispatcher: **Google Drive… → Подготовить и открыть notebook… → Google Colab**.
Сохраните предложенную инструкцию, откройте общий `notebooks/Poster_Launcher.ipynb`
и выполните установочную ячейку. Она проверяет SHA256 установщика, архива строго
заданного полного Git SHA и всех зависимостей. `main`, `HEAD` и прежний пакет 5B
не используются. Один Launcher обслуживает все проекты — отдельный notebook на
каждый проект не нужен. В обычном Jupyter используется тот же файл.

Нажмите **Подключить Drive**, подтвердите официальный вход Colab, выберите проект
и конкретную версию. Укажите обычные входы JSON, число запусков и допустимую
параллельность. Для защищённых данных сначала добавьте значения в **Secrets**
провайдера и укажите в Launcher только соответствие `ref/имя входа → имя секрета`.
Значения в ячейки, output или поля обычных входов вставлять не нужно.

Нажмите **Выполнить**. Наличие Chromium, ресурсы, входы и все файлы проверяются
перед разрешением исполнения. Отчёт перечисляет отдельные run IDs/PID/статусы.
Новый независимый запуск разрешается через **Подготовить новый запуск**.
По завершении используйте **Завершить сессию**.

## 7. Открыть общий Molab Launcher

Выберите **Molab — marimo notebook**, загрузите `notebooks/Poster_Molab.py` в Molab.
Это настоящий marimo frontend, не Colab notebook с переименованным заголовком.
В **Files → Add remote storage → Google Drive** настройте официальный native
connection Molab и завершите OAuth в его браузере. После этого нажмите
**Подключить Drive** в Launcher. Используется `gdrive-fsspec` credential cache
native connection; Colab `drive.mount` и устаревший console/OOB flow не применяются.
Если совместимого authenticated connection нет, запуск запрещён с понятной ошибкой.

Установщик фиксирует upstream `gdrive-fsspec` commit
`dd9bb3025b26091d4a29ac4e81cc3d88910203bd` и SHA256 архива. Пакет ещё не имеет
официальной PyPI-поставки; локальная версия отмечена commit suffix, а не выдана за
upstream release. Google Drive file operations идут через прежний REST transport
Poster; native filesystem используется как адаптер авторизации.

Для защищённых значений настройте private environment Molab с именами
`POSTER_SECRET_…`. В Launcher вводятся только имена bindings. Чтение случайных
ambient environment secrets и передача GitHub tokens не выполняются. Выбор
проекта/версии, запуск, отмена и возврат результатов работают через общую backend
логику. Реактивное переисполнение ячеек marimo не отправляет новый запуск: исполнение
требует callback пользовательской кнопки и create-once разрешение.

## 8. Запускать разные проекты одновременно

В Dispatcher добавьте несколько локальных проектов или откройте закреплённые
версии из Drive. Выберите их и поставьте в очередь. До запуска службы настройте
общий `max_workers` и `max_per_project`; они продолжают ограничивать все задания,
включая расписания и Remote Agent. Служба остаётся единственной управляемой очередью.

В обычном Linux/Jupyter независимые проекты могут выполняться в отдельных
разрешённых сессиях; Native Dispatcher исполняет их исходными Worker-процессами.
Ограничение провайдера может быть строже возможностей машины.

## 9. Несколько экземпляров одного проекта

В форме входов Dispatcher есть **Всего запусков** и **Одновременно**. Например:
20 и 5 — двадцать разных заданий, одновременно не более пяти. Для пяти одновременных
копий установите также `max_workers >= 5` и `max_per_project >= 5`. По умолчанию
очередь сохраняет прежний project limit 1. Изменение настроек работающей службы
по-прежнему запрещено: сначала остановите выдачу новых заданий.

Каждая копия имеет отдельные job/run IDs, зашифрованные входы, рабочую папку,
переменные, report, timeout, отмену и browser context. Общий snapshot только читается.
Явно выбранный постоянный browser profile сериализуется прежним execution lock;
для независимых сессий не выбирайте общий профиль. Автоматического переноса
Windows cookies/profile в Linux/notebook нет.

## 10. Фактическая параллельность

| Backend | Количество в заявке | Одновременно | Как запускается |
|---|---:|---:|---|
| Local Windows Worker | 1–1000 суммарно | 1–32, min общих/project/batch limits | автоматически исходным Dispatcher |
| Persistent Linux/VPS Agent | 1–1000 суммарно | те же общие/project/batch limits | автоматически через существующий TLS Relay и pinned Agent |
| Generic Linux/Jupyter | 1–1000 | измеренный максимум ≤32 | вручную из notebook, исходными локальными Worker-процессами |
| Colab interactive | 1–1000 последовательных, без гарантии срока сессии | 1 | вручную из разрешённого notebook UI |
| Molab notebook | 1–1000 последовательных, без гарантии срока сессии | 1 | вручную из marimo UI |

У Generic probe учитывает CPU, доступную RAM, диск и Chromium: для I/O задач
CPU×4, далее ограничение RAM/стоимость процесса и верхний предел 32. Минимум
256 MB RAM/диска; browser execution требует совместимого Chromium. Это проверка
допуска, а не гарантия отсутствия конкурирующих нагрузок/прекращения VM.
Colab/Molab ограничены 1 до подтверждения policy/entitlement и ресурсов аккаунта.
Рекламных обещаний 10–100 параллельных free Colab Workers нет.

Старый Agent 5B не принимает новые resource/SecretRef jobs: они отклоняются до
постановки в очередь с REMOTE_VERSION. Используйте новую поставку 5C. Прежние
5B-compatible HTTP jobs и protocol 1 сохраняются.

## 11. Где результаты

Управляемое выполнение: прежние вкладки заданий, reports, workdirs и SQLite-history
Dispatcher. После **Синхронизировать результаты с Drive** завершённые native runs
закреплённых cloud-проектов выгружаются в уникальные results folders с режимом
**MANAGED_EXPORT**. Это копия истории; Drive не управляет состоянием очереди.
Для Linux Agent разрешённые возвращаемые пути указаны в форме Remote Target
(для открытого cloud проекта подставляется его output allowlist).

Notebook: **DETACHED** results, отдельная папка на run и committed manifest после
всех ciphertext-файлов. Все bytes проверяются SHA256. Ed25519 signature привязана
к public identity в pre-execution claim; выходные файлы X25519/HKDF/AES-GCM
зашифрованы для независимого recipient из опубликованной версии. Лимиты пакета:
128 файлов/20 MB, metadata 200 KB; разрешённые возвращаемые plaintext outputs
суммарно до 14 MB, чтобы зашифрованный JSON/base64 помещался в bounded blob.
Недогруженная папка не является подтверждённым результатом.

## 12. Импортировать в Windows

В Dispatcher откройте **Google Drive… → Синхронизировать результаты с Drive**.
Poster проверяет pre-run claim, подпись, project/version/run IDs, recipient,
SHA256 и разрешённые пути, затем расшифровывает файлы в приватную локальную папку.
Окно **Автономные результаты — DETACHED** показывает каждый run, ОС/PID, статус
и возвращённые файлы; двойное нажатие открывает папку файлов.

Повтор импорта идемпотентен; повреждённый результат отклоняется. Отдельная таблица
`cloud_results` в dispatcher.sqlite не меняет native jobs/executions. Подписанный
DETACHED report никогда не переводит существующее managed UNKNOWN в SUCCEEDED.
Cloud origin конкретной версии закрепляется в encrypted job payload при admission;
последующее редактирование каталога не меняет источник уже поставленного задания.

## 13. Windows может быть выключен

После публикации проекта и явного запуска notebook его локальные Worker-процессы
работают независимо от Windows. Drive получает зашифрованные результаты для уже
опубликованного public recipient. При следующем запуске Windows импортируйте их.
Private identity, queue.key и OAuth tokens Windows для этого в notebook не нужны.

## 14. Когда нужен работающий Controller

Для управляемых Local/Remote jobs, расписаний, lease, отмены Remote Agent и
автоматического учёта Remote reports нужен существующий Windows Controller.
При его потере действуют прежние UNKNOWN/review/no-resend гарантии. DETACHED
notebook — отдельное автономное выполнение, а не способ скрыто восстановить или
повторить неопределённое managed задание.

В бесплатном Colab не запускаются Relay, Agent, SSH server, удалённый controller,
распределённые Workers или keepalive. Foreground notebook владеет ограниченной
локальной службой исходного Dispatcher через закрытый stdin lifetime pipe и
локальный authenticated IPC. EOF/kernel loss прекращает дочерние процессы;
разрешение не превращается в переносимую очередь для другой VM.

## 15. Ограничения провайдеров и автоматизация

По официальному Colab FAQ free managed runtimes без положительного compute balance
ограничивают SSH/remote control, distributed workers и обход notebook UI. Resources,
idle timeout и максимальный срок VM не гарантированы. Сотни последовательных
запусков не обещают успешного завершения до окончания сессии.

Официальный Colab CLI существует для **Linux/macOS**, Windows пока не поддерживает.
Наличие CLI не доказывает доступность аккаунта/entitlement и разрешение затрат.
Poster показывает ручной notebook workflow; автоматическое выделение облачных VM
не включено. Никакие платные VM/compute units этой разработкой не создаются.
Molab требует своего native authenticated storage и совместимого marimo runtime;
лимиты конкретного аккаунта также не предполагаются по умолчанию.

## 16. Потеря сессии и повторный POST

Runtime lifecycle отделён от job status: NOT_CONFIGURED, READY, STARTING,
AVAILABLE, EXECUTING, FINISHED, STOPPING, EXPIRED, UNAVAILABLE, UNKNOWN.
Исчезнувшая VM не считается прежней живой сессией. Уже committed результаты
можно импортировать; неподтверждённое выполнение остаётся неопределённым.

До admission каждый notebook атомарно создаёт **один и тот же заранее выданный
сервером Drive file ID** в цепочке разрешений проекта. Только успешный создатель
получает право enqueue. Это атомарная create по уникальному ID, не lock по имени
файла. При 409/утрате ответа не выполняется ни retry admission, ни GraphRunner.
Повторная доставка и двойное нажатие той же заявки возвращают прежние задания;
изменение её параметров отклоняется. Отчёты/бинарные uploads можно возобновлять
как передачу данных, не как повтор внешнего действия.

Перед новым разрешением проверяются signed committed batch/results. Если сессия
могла выполнить POST и пропала либо есть UNKNOWN/REVIEW_REQUIRED, новый запуск
останавливается. Проверьте внешний сайт и подтверждённые результаты. После явной
ручной проверки оператор может выполнить в доверенной notebook-сессии:

```python
from poster.cloud.admission import claims, resolve

folder = await launcher.backend.connection.library.find(project_id)
descriptor = await launcher.backend.connection.library.descriptor(folder)
history, _ = await claims(launcher.backend.connection.library, descriptor)
await resolve(launcher.backend.connection.library, history[-1], explicitly_reviewed=True)
```

Это операторская процедура после проверки последствий, не автоматическое
восстановление и не обещание exactly-once у внешних сайтов. Для Jupyter объект UI
называется `poster_launcher`, его backend — `poster_launcher.backend`; для Molab
объект — `launcher`. Затем создаётся отдельная новая заявка. До ручной проверки
кнопки/переоткрытие notebook не разрешают повтор.

## 17. Секреты и переносимость

В public project package нет SecretRef values, encrypted sidecars с зависимостью
от Windows key, queue.key, private identities, OAuth/refresh tokens, Relay bearer,
профилей, cookies или session keys. Private filenames/executable resources,
symlinks/reparse points, traversal, Windows device names и несовместимые пути
отклоняются. Защищённые refs передаются только через явно выбранный secret provider
и encrypted per-run inputs; без такого binding запуск запрещён.

Транспорт Google Drive переиспользует `poster/storage/drive.py` — тот же, что в
Legacy, с отдельными API admission/immutable storage вызовами. Старые YAML,
READY/queue форматы и HTTP Engine не смешиваются с Maker project catalog.
Новые Qt функции не заменяют единое окно Maker/Recorder/Legacy.

## 18. Что подтверждают тесты

Локально выполнены реальные GraphRunner/Worker/Host процессы, пять параллельных
копий, пять одновременных Chromium contexts с разными cookies/outputs/PID,
разные проекты одновременно, отдельные cancel/timeout, crash после POST без
повтора, immutable resources/subprojects/protected refs, encrypted results/import,
отдельный MANAGED_EXPORT, genuine Jupyter kernel и genuine marimo export runtime.
Drive endpoint в новых storage/E2E тестах — контролируемый protocol/HTTP fixture,
а не аккаунт Google. Qt проверяет реальный event loop и HTTP запросы.

Полный локальный прогон базового кандидата: 457 PASSED / 10 существующих
GUI/platform skips; дополненные explicit suites проверяют новые сценарии отдельно.
Fresh venv выполнил **саму установочную ячейку поставляемого Launcher** с pinned
источником и `pip --require-hashes`, установил настоящий upstream gdrive-fsspec и
успешно запустил исходный Worker. Версии: marimo 0.25.2, ipywidgets 8.1.9,
nbclient 0.11.0; полный hash lock — requirements-cloud-runtime.txt.

Существующие **38 CI jobs сохраняются**, Stage 5C добавляет storage/Qt matrix,
notebook/browser execution и проверку установки/поставки. Отдельный crosshost
workflow собирает **новые Windows Full и Linux wheelhouse на github.sha**,
проверяет producer SHA256/BUILD_INFO/release.json и выполняет прежние 18 сценариев
на одновременно работающих Windows Server 2022 и отдельной WSL2 Ubuntu VM.
У VM отдельный kernel, отключены Windows drive mounts и interop, Linux UID non-root;
связь TCP+TLS, pinned SSH key. Это не доказательство маршрутизации через публичный
Интернет. Старый f411 пакет может быть историческим контролем, но не доказательством 5C.

В том же workflow отдельная настоящая frozen проверка: **Maker → HTTP Drive
fixture → genuine Jupyter → исходный GraphRunner → encrypted Drive result →
frozen Dispatcher**, с system-only PATH у приложений, screenshots и безопасным
отчётом. Исходные/native Qt тесты её не подменяют. Статусы берутся из фактических
report/CI на выбранном SHA; зелёная сборка с NOT_EXECUTED не является приёмкой.

| Сценарии задания | Реальные проверки / отдельные обязательные jobs |
|---|---|
| 1–6, 9–15 Drive | test_cloud_storage.py, test_cloud_auth.py: paging, quota, конфликт, SHA, PKCE, private package |
| 7–8 Maker/Dispatcher | cloud_qt_e2e.py, scripts/cloud/check_frozen.py |
| 16–23 Launcher/данные | cloud_notebook_runtimes.py, test_cloud_runtime.py, examples/maker-stage5c |
| 24–28 отказ/сессия/импорт/secrets | test_cloud_runtime.py, pinned installer, frozen pipeline |
| 29–34 native concurrency/contexts/limits | test_dispatcher_host.py, test_dispatcher_service.py, test_cloud_runtime.py |
| 35–36 browser/output isolation | cloud_browser_instances.py: пять настоящих Chromium contexts |
| 37–42 cancel/timeout/crash/Drive/replay/capability | test_cloud_runtime.py, test_cloud_managed.py, storage strict create/lost ACK |
| 43–46 frozen/local/Scheduler | прежние Windows GUI jobs + frozen-drive-notebook + Scheduler 4B.2 suites |
| 47–51 Remote/TLS/pairing/UNKNOWN/18 scenarios | прежние Remote jobs + crosshost-acceptance новых binaries |
| 52 Legacy HTTP Engine | прежние Python/PHPBB/Windows regressions, Legacy notebooks/storage tests |

## 19. Что требует настоящих аккаунтов

**Google Drive OAuth/API, Colab account/runtime и Molab account/native connection:
CLOUD_PROVIDER_NOT_EXECUTED**, пока нет разрешённых credentials/session.
Контролируемый Drive fixture, локальный Jupyter/marimo и hosted Windows/Linux E2E
не выдаются за выполнение в аккаунте провайдера. Потребуются разрешённые аккаунты,
OAuth consent и provider Secrets, доступные по обычным UI/secure environment
settings. Пароли, приватные ключи и tokens в чат или issue отправлять не нужно.

## Поставки и безопасные доказательства

Через GitHub Actions предоставляются Windows Full ZIP, Linux runtime/wheelhouse,
`poster-cloud-launchers.zip` (оба Launcher, demo, guide, lock/installer), отдельные
`.sha256` и release manifests. Notebook source pin —
`830b6024f23f8625e4d0871a0322c4c73cf473ad`, source archive SHA256
`c388fb6640fc8eddc8c8e283ed8a85f0c8a7e95e9ac6f8adb9effdf20dbac9a9`.
Бинарники и CI привязаны к SHA своей финальной сборки; Launcher использует reviewed
полный SHA кода 5C, а не плавающую ветку. Дополнительные коммиты delivery/tests/docs
не требуют смены pin, если исполняемый код не изменён.

Artifacts разрешены только по allowlist: screenshots с synthetic fixture data,
JUnit summaries, public committed-result evidence, safe SQLite history без inputs,
сведений об identity private keys и ciphertext workdirs. Raw workspace, provider
cache, OAuth JSON, notebook credentials и install-private.log не публикуются.
SHA256 каждой конкретной поставки печатается её build job и лежит рядом с архивом.
Итоговый отчёт указывает точный финальный SHA, все jobs, 18/18 cross-host и реальные
артефакты после завершения соответствующего прогона.

Официальные источники проверены read-only CI, без provisioning и credentials:
[Drive create/IDs](https://developers.google.com/workspace/drive/api/guides/create-file),
[Drive errors/quota](https://developers.google.com/workspace/drive/api/guides/handle-errors),
[Desktop OAuth/PKCE](https://developers.google.com/identity/protocols/oauth2/native-app),
[Colab FAQ](https://research.google.com/colaboratory/faq.html),
[официальный Colab CLI](https://github.com/googlecolab/colab-cli),
[Molab remote storage](https://molab.marimo.io/blog/drive-storage),
[gdrive-fsspec](https://github.com/marimo-team/gdrive-fsspec),
[marimo](https://docs.marimo.io/).

## Устойчивость frozen GUI-приёмки после подключения Dispatcher

В `completed(connect)` Dispatcher записывает первый observer до обработки Qt
`LayoutRequest`: появление многострочного статуса службы сдвигает ряд кнопок.
Воспроизведение на настоящем Qt показало смену центра Google Drive с Y=43 на
Y=73. Кнопка при этом существует и активна. Прежний frozen harness брал один
подключённый кадр и мог нажать уже устаревшую координату; последующий timeout
ожидания окна Drive оставался помечен общей фазой «Dispatcher cloud control».

Драйвер теперь сначала активирует окно, затем требует правильный frozen PID,
connected/idle/enabled и одинаковые координаты в двух разных свежих поколениях
observer. После этого выполняется прежний настоящий OS click. Ожидания не
увеличены, внутренние callback/IPC вместо кликов не используются. В public proof
попадают только безопасные PID/координаты/этапы; при ошибке сохраняется screenshot
fixture UI. Дополнительный настоящий Qt/QWindow regression проверяет пересчёт
layout и попадание клика в кнопку после него.

Исполняемые `poster/*`, dependency lock и bootstrap после reviewed runtime pin
`830b6024f23f8625e4d0871a0322c4c73cf473ad` не изменены. Исправление синхронизации затрагивает
драйвер приёмки и его тест. Дополнительно importer требует строгого подмножества
разрешённых outputs закреплённой версии даже при корректной подписи producer.
Отказ проверен настоящим Worker и корректно подписанным/зашифрованным лишним
файлом; архив и локальные outputs не создаются. Runtime pin/source archive hash
пересчитаны для этой проверки, dependency hashes сохраняются.
Fresh-venv установка и genuine Jupyter/marimo проверки остаются обязательными.
