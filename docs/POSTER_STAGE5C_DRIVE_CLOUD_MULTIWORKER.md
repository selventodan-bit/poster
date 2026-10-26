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
