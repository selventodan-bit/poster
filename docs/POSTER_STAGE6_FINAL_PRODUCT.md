# Poster 3.0 — Stage 6 / Release Candidate

Основа: `f733210a89a7a30d3d87669f85942bb403e0f9df`, принятые 55 jobs и 18 cross-host
сценариев. Рабочая ветка: `codex/poster-6-product-finalization`. Этот документ
отделяет доказательства принятой основы от ещё выполняемой приёмки Stage 6.
main, PR №2 и стабильные ветки не изменяются. Платные ресурсы не создаются.

## Актуальный аудит A

Источники: README, ZENNO_PARITY_MATRIX, Stage 3A/3B, 4A/4B, 5A/5B/5C/5D,
registry/actions и модели параметров, Qt Maker/Dispatcher и их панели,
DesktopOAuth/SecretStore, Store/Host/Worker/Scheduler, Drive/NotebookSession,
release builders и все четыре принятых workflows. Старую матрицу этапа 2
нельзя использовать как описание текущего продукта.

Статусы относятся к указанной основе. Новые функции ниже требуют своих тестов
и CI точного итогового SHA, прежде чем будут отмечены завершёнными.

| Возможность | Статус основы | Проверяемое основание / граница |
|---|---|---|
| Граф, связи, undo/redo, сохранение v1/v2 | IMPLEMENTED_AND_TESTED | test_maker, test_maker_stage2; Windows frozen OS input |
| 78 действий и параметры | IMPLEMENTED_BUT_PARTIAL | Семейства runtime покрыты test_maker_actions3/control; сплошная Qt roundtrip-проверка всех 78 ещё нужна |
| Recorder, embedded Chromium, iframe/popup/вкладки | IMPLEMENTED_AND_TESTED | test_maker_stage3a, unified frozen; closed Shadow DOM/SSO не гарантированы |
| Переменные, условия, циклы, отладка | IMPLEMENTED_AND_TESTED | test_maker_stage2; breakpoint/step/edit, bounded runtime |
| HTTP/JSON/lists/tables/CSV/XLSX/files/subprojects | IMPLEMENTED_AND_TESTED | test_maker_actions3 и frozen data graphs |
| Managed queue, inputs, batches, priorities/limits | IMPLEMENTED_AND_TESTED | dispatcher/admission, реальные Workers; arbitrary priorities отсутствуют |
| Scheduler, restart, отмена, UNKNOWN | IMPLEMENTED_AND_TESTED | schedule recovery, frozen GUI; no automatic second POST |
| Профили и ограниченный перенос сессии | IMPLEMENTED_AND_TESTED | portability и 5 реальных Chromium; cookies/localStorage, не Chrome directory |
| Proxy и private network bindings | IMPLEMENTED_AND_TESTED | настоящие HTTP/HTTPS/SOCKS5 endpoints и Worker tests; backend limits |
| Явные ресурсы и большие encrypted outputs | IMPLEMENTED_AND_TESTED | 24 MiB Worker/output/resume, immutable SHA; bounded quotas |
| Remote Agent/Relay/pairing/TLS | IMPLEMENTED_AND_TESTED | 18/18 Windows Server ↔ Ubuntu WSL2, отдельные OS/kernel/SSH/private TCP |
| Публичный WAN/VPS deployment | EXTERNAL_VALIDATION_REQUIRED | Нужны доверенный публичный адрес, firewall/DNS/TLS и разрешённые машины |
| Drive OAuth GUI import, root/version/conflicts | IMPLEMENTED_AND_TESTED | cloud_qt_e2e, test_cloud_auth/storage через fixtures |
| Google Drive live E2E | EXTERNAL_VALIDATION_REQUIRED | CLOUD_PROVIDER_NOT_EXECUTED; аккаунт не предоставлен |
| Общие Jupyter/marimo Launchers | IMPLEMENTED_AND_TESTED | genuine local kernels, original GraphRunner, fresh pinned installer |
| Colab/Molab live E2E | EXTERNAL_VALIDATION_REQUIRED | CLOUD_PROVIDER_NOT_EXECUTED; interactive concurrency 1 |
| Первый запуск и единая помощь | IMPLEMENTED_BUT_PARTIAL | Разрозненные инструкции; нужен GUI путь к workspace/examples/setup |
| Безопасная backup/restore Maker + Dispatcher | MISSING | State/KnowledgeBase backup не охватывает native queue, snapshots, ключи и настройки |
| SQLite migrations | IMPLEMENTED_BUT_PARTIAL | Additive/transactional v1–7; до миграции нет отдельной проверенной копии |
| Обновление Windows Full | IMPLEMENTED_BUT_PARTIAL | Maker по умолчанию пишет рядом с ZIP; извлечение в новую папку скрывает старое состояние |
| Единый безопасный diagnostic export / build identity | IMPLEMENTED_BUT_PARTIAL | Есть remote/readiness/GUI reports; нужен общий payload-free экспорт |
| Импорт закрытых проектов ZennoLab | INTENTIONALLY_UNSUPPORTED | Формат .zp/.zppro не реализован |
| Arbitrary Python/JS из импортированного JSON | INTENTIONALLY_UNSUPPORTED | Registry доверенного кода, без eval/import из графа |
| CAPTCHA/antidetect/платные сторонние сервисы | INTENTIONALLY_UNSUPPORTED | Вне согласованного объёма; лицензий/безопасного API нет |
| Безусловная 24/7 бесплатная notebook работа | INTENTIONALLY_UNSUPPORTED | Ресурсы/сроки определяет провайдер |

## Найденные P0/P1/P2

| ID | Приоритет | Факт и необходимое исправление | Статус |
|---|---|---|---|
| S6-01 | P0 | Maker state привязан к каталогу EXE: новое место распаковки теряет видимость проектов/ключей. Нужны стабильный default, сохранение legacy и GUI выбор прежнего workspace | IN_PROGRESS: stable paths/legacy leases implemented; 16 targeted tests passed; GUI adoption pending |
| S6-02 | P1 | Нет согласованного защищённого backup/restore native product state; нельзя повторять восстановленные внешние jobs | IN_PROGRESS: encrypted offline export/verification tested; transactional restore and GUI pending |
| S6-03 | P1 | Перед schema migration нет проверенного SQLite snapshot | FIXED: private SQLite backup before any schema/journal change; 19 targeted tests passed |
| S6-04 | P1 | First-run, инструкции OAuth/updates и diagnostics разнесены и недостаточны для обычного пользователя | OPEN |
| S6-05 | P1 | Нет непрерывной проверки GUI edit/save/reopen всех 78 действий; нужно выявить и закрыть реальные несоответствия | OPEN |
| S6-06 | P1 | Доставка должна включать актуальные user guide, RC identity, совместимые pinned Launchers и финальные evidence | OPEN |
| S6-07 | P2 | Несколько редактируемых документов/расширенный trace viewer, импорт ZennoLab, arbitrary code blocks | DEFERRED: не обязательные P0/P1 согласованного runtime |
| S6-08 | P2 | Signing/auto-updater | DEFERRED: signing infrastructure отсутствует; безопасное ручное обновление |

Сравнение с ProjectMaker/ZennoPoster проводится по пользовательским сценариям:
визуальный граф/запись/данные/отладка/изолированное выполнение/расписания доступны;
закрытый формат ZennoLab, его плагины, все интеграции и антидетект не воспроизводятся.
Число 78 не является доказательством полного эквивалента.

## Внешняя приёмка

Google Drive/Colab/Molab: CLOUD_PROVIDER_NOT_EXECUTED. Public WAN отдельно не
подтверждён. Эти проверки не заменяются fixtures или статусом READY. Они не
блокируют исправление локальных P0/P1. RC не равен окончательной пользовательской
приёмке; необходимые действия аккаунта будут перечислены в пользовательской инструкции.

## Реализованный защищённый backup (блок E, восстановление ещё в работе)

Экспорт требует явного согласия на включение private keys/identities и отдельной
парольной фразы (12–1024 символа). Scrypt и потоковый AES-256-GCM защищают весь
архив и manifest. Пароль/ключи не входят в CLI, заголовок или отчёт. Содержимое
разбирается только после полной проверки тега. Файлы включаются только из
подтверждённых app workspaces; SQLite снимается через backup API с integrity и
foreign-key check, включая зафиксированный WAL. Кеш браузера, tmp и IPC/locks
не включаются. Внешние папки не добавляются автоматически.

Backup привязан к исходным путям и тому же пользователю/машине. DPAPI не
объявляется переносимым. Предел: 16 GiB зашифрованного содержимого, 4 GiB на файл,
20 000 файлов/каталогов, 16 MiB manifest, потоковые блоки 1 MiB; требуется место
на временные копии и резерв 64 MiB. Symlinks/reparse/hardlinks, traversal,
дубликаты, compressed inner ZIP, чужие пути/identity и повреждение отклоняются.
Создание не перезаписывает существующий файл. Проверены реальный SQLite WAL,
правильный и неверный пароль, изменение header/ciphertext/tag, сохранность
существующего файла при ошибке, реальные hardlink и directory link.
