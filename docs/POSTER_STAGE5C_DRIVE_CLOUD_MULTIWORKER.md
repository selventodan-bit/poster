# Poster Stage 5C — Drive, notebook launcher и независимые экземпляры

Работа ведётся в `codex/poster-5c-drive-cloud-multiworker`, строго от
`74061608c57960bd0679fa9e20e64a84218c3986`. main, PR №2, принятые 5A/5B refs
не изменяются; merge не выполняется. Этот документ пополняется по завершении
блоков. Наличие файла не означает завершения разработки.

## Аудит исходной архитектуры

- `poster/storage/drive.py` уже реализует официальный REST Drive v3, google-auth,
  пагинацию, bounded quota backoff, проверяемый staging download и Legacy backup.
  Legacy использует собственные YAML/READY и HTTP Engine. Это не каталог графов
  Maker и не очередь Dispatcher. Новый storage расширяет этот transport вместо
  второй независимой реализации запросов Google Drive API.
- `poster/maker/model.py`, `storage.py` и registry 78 действий остаются общими.
  Vault/SecretRef отделены от графа; encrypted sidecars и Windows queue.key нельзя
  переносить в публичный project package. Поля metadata поддерживают расширения.
- `poster/dispatcher/snapshots.py` уже даёт immutable, encrypted, hash-verified
  snapshots, traversal/symlink checks и bounded graph dependencies. Сейчас данные
  CSV/XLSX в workdir не переносятся (`Catalog.RESOURCE_NOTICE`). Stage 5C добавляет
  только явно выбранные, проверяемые ресурсные файлы с отдельной копией для run.
- `Store`, `Catalog`, `Host`, `Dispatcher`, Local Worker, Scheduler и Remote Agent
  уже используют общую очередь, лимиты workers/project, отдельный процесс/run,
  cancellation, durable effects fence, UNKNOWN и проверяемую историю. Существующие
  execution/report/lease гарантии сохраняются. В `enqueue_batch` идентичные
  item IDs сейчас разделяют dedup key: независимым копиям нужен стабильный индекс
  внутри одной пользовательской операции, без отказа от дедупликации double click.
- Local и Remote Worker уже вызывают один `dispatcher.worker.execute`, который
  запускает исходный GraphRunner, изолирует FileSandbox/Variables, блокирует GUI
  imports, выполняет watchdog и cleanup. Notebook не получает отдельный executor
  для 78 действий и не поднимает скрытый Agent/SSH server.
- Drive хранит проекты и committed results. Авторитет managed jobs остаётся
  `dispatcher.sqlite`. Автономные notebook reports имеют отдельный режим и identity;
  их импорт не подтверждает и не переводит существующий UNKNOWN managed job.
- Исходные `notebooks/colab_quickstart.ipynb` / `molab_quickstart.ipynb` относятся
  к Legacy Engine. Новые reusable Launchers добавляются отдельно, без notebook
  на каждый проект и без несовместимого Colab drive.mount() в Molab.
- Принятые 18 cross-host сценариев — исторический контроль. Регрессия 5C обязана
  использовать новые Windows binaries и Linux package финального SHA.

## Проверка провайдеров

Дата аудита для пользователя: 27 октября 2026 (Москва). Прямой сетевой proxy среды
отклоняет developers.google.com, research.google.com, molab.marimo.io и docs.marimo.io.
Запрошенное разрешение сети не изменило ограничение proxy. Read-only job
`provider-documentation` получает официальные публичные страницы в стандартном
GitHub runner; он не входит в аккаунты и не запускает платные notebook-сессии.
Источники и bytes SHA256 сохраняются в отдельном безопасном артефакте.

Уже проверены официальные исходники:

- https://github.com/googlecolab/google-colab-cli — CLI поддерживает Linux/macOS;
  Windows официально не поддержан. Есть `new`, `run`, `exec`, `stop`, usage и
  ручной OAuth/ADC. Само наличие CLI не означает entitlement аккаунта или разрешение
  provisioning. В Full Windows автоматическая доступность CLI не заявляется.
- https://github.com/marimo-team/gdrive-fsspec — официальный marimo adapter,
  OAuth/user/service-account/anonymous modes, Shared Drives; нельзя переносить
  cached токены в notebook source или использовать устаревший console OOB flow.
  Production authorization Molab проверяется отдельно от совместимого local marimo.

Живые Google Drive/Colab/Molab credentials/session не предоставлены.
Соответствующий provider E2E до реального исполнения имеет статус
**CLOUD_PROVIDER_NOT_EXECUTED**. Контролируемый Drive fixture и настоящий локальный
Jupyter/marimo не будут называться E2E настоящего Google аккаунта.

## Реализованный storage и переносимый пакет

Transport Google Drive остаётся общим с Legacy. Новые `DriveFiles` и `Library`
используют его для настоящих file IDs, MIME, постраничного каталога, bounded
resumable uploads, проверки размера и SHA256. Recovery journal локальный,
зашифрованный, с приватными правами; он не публикуется на Drive и не является
очередью выполнения. Завершённые transport-записи удаляются, чтобы число проектов
не ограничивалось размером локального журнала.

Обновления проекта — append-only цепочка immutable commit manifest. Конкуренты
создают **один и тот же заранее выданный сервером Drive file ID**. Успешная atomic
create — точка публикации; 409 и несовпадающее содержимое означают конфликт.
Это опирается на уникальность серверного ID, а не на имя lock-файла или условно
поддерживаемый ETag. Недогруженные blobs не попадают в историю до commit manifest.
Старые версии и отдельная results-папка не перезаписываются. Rename использует
folder ID; одинаковые отображаемые имена допустимы для разных project IDs.

Portable package сохраняет один исходный формат GraphRunner, отдельный manifest
с SHA256 и только достижимые графы плюс явно выбранные зависимости/ресурсы.
Динамический Project.Run требует явного списка разрешённых зависимостей.
Не отправляются все соседние файлы, encrypted secret sidecars, private keys,
cookies, browser profiles или приватное состояние Dispatcher. Защищённые refs
сохраняются как ссылки без значений. Не настроенные секреты впоследствии должны
останавливать запуск, а не заменяться открытым текстом.

Каждый original Worker получает собственные копии задекларированных ресурсов
в FileSandbox. Существующий GraphRunner и исходные действия не дублируются.

Подтверждено локально: **36 tests PASSED** (Drive protocol fixtures + Legacy Drive +
Dispatcher Store/Worker). Среди новых проверок — два синхронных конкурента одного
commit slot, имена/rename/pagination, immutable history, прерванный resumable upload,
потерянный ACK run claim без unsafe retry, checksum corruption, symlink/traversal,
недопустимые Windows-пути и отсутствие secret sidecars в portable package.
Живой Google Drive E2E: **CLOUD_PROVIDER_NOT_EXECUTED**.

## Подключение Google Drive и интерфейс Windows

В Maker появилось меню **Google Drive**: подключить, сохранить, открыть, обновить,
открыть папку, история версий и синхронизация. Кнопка сохранения на верхней панели
после подключения начинает публикацию текущего сохранённого проекта. В окне
Google Drive можно явно добавить файлы данных, выбрать возвращаемые файлы,
проект и точную версию. Ресурс вне папки проекта копируется приложением только
после явного выбора; всю папку приложение не загружает. Каждое открытие создаёт
новую локальную рабочую копию, не изменяя опубликованный snapshot.

В Dispatcher кнопка **Google Drive…** открывает тот же каталог, загружает и
проверяет выбранную версию, затем регистрирует её через существующий Catalog и
Store. Происхождение и cloud version сохраняются отдельно от внутреннего
snapshot fingerprint. Новый сетевой код работает в отдельном Qt worker loop;
публикация и OAuth не блокируют Qt event loop.

Для первой настройки владелец приложения:

1. Создаёт проект в Google Cloud Console, включает **Google Drive API**.
2. Настраивает OAuth consent screen и пользователей тестового приложения либо
   проходит требуемую Google проверку перед публичным распространением.
3. Создаёт OAuth client типа **Desktop app**, скачивает его JSON.
4. В Poster нажимает **Подключить Google Drive**, выбирает этот файл и разрешает
   доступ в обычном браузере Google. Access/refresh tokens вручную не копируются.

Собственного универсального production OAuth client в EXE нет. Web-client
configuration отклоняется. Используется официальный desktop loopback flow с
PKCE S256, случайным state и одноразовым callback на 127.0.0.1. Client configuration
и tokens защищены **Windows DPAPI текущего пользователя**, затем private ACL.
Открытого authorized-user JSON на Windows не создаётся. На явно выбранных обычных
Linux-хостах доступен private local Fernet store; он не называется Windows DPAPI.

Scope `drive.file` разрешает запись app-created файлов. Для чтения результатов,
созданных **другими OAuth clients Colab/Molab**, дополнительно нужен
`drive.readonly`: одного `drive.file` для такого импорта недостаточно. Приложение
использует чтение только каталога Poster3, но Google permission технически даёт
чтение Drive; это отражается на экране согласия. Legacy credentials/scopes не
меняются. При частично выданных разрешениях подключение не считается готовым.

**Отключить аккаунт** удаляет локальную авторизацию и пытается отозвать Google
credential. Если сеть недоступна, приложение честно сообщает, что сетевой отзыв
не подтверждён; локальные tokens всё равно удаляются. Независимый приватный ключ
для расшифровки уже полученных результатов не стирается при отключении Drive.
Он никогда не является Windows queue.key и не отправляется в notebook.

Проверки текущего блока: **31 backend tests PASSED**, отдельно **2 genuine Qt tests
PASSED** с actual HTTP Drive fixture и продолжающим работать Qt event loop.
Loopback OAuth callback, проверка state/PKCE, ciphertext credentials, offline
X25519/AES-GCM result encryption и Ed25519 signatures проверены настоящим кодом.
Google OAuth token endpoint в этих тестах контролируемый: Google account E2E
остаётся **CLOUD_PROVIDER_NOT_EXECUTED**. Добавлены обязательные Windows/Linux CI
matrix jobs для OS key store и Qt. Реальный frozen Windows E2E всего Stage 5C
будет выполнен после завершения notebook/runtime блока.
