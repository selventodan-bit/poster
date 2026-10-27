# Функциональная матрица Poster 3.0 / Stage 6

Дата проверки: 27 октября 2026. Принятая основа:
`f733210a89a7a30d3d87669f85942bb403e0f9df` (55/55 jobs); новая работа:
`codex/poster-6-product-finalization`. Итоговые CI/evidence Stage 6 публикуются
в [отчёте](POSTER_STAGE6_FINAL_PRODUCT.md). Старые отметки этапа 2 заменены
проверкой текущего кода и пользовательских сценариев.

IMPLEMENTED_AND_TESTED означает указанную контролируемую проверку, с её пределами.
IMPLEMENTED_BUT_PARTIAL означает доступный, ограниченный сценарий.
IMPLEMENTED_NOT_E2E_TESTED требует указанного нового end-to-end подтверждения.
MISSING означает отсутствующую функцию, EXTERNAL_VALIDATION_REQUIRED — необходимое
внешнее окружение/аккаунт. INTENTIONALLY_UNSUPPORTED — вне согласованного объёма.
Число классов и зелёный CI с NOT_EXECUTED не доказывают равенство ZennoPoster.

| Возможность | Проверяемый статус | Проверяемое основание / граница |
|---|---|---|
| Граф, связи, undo/redo, сохранение v1/v2 | IMPLEMENTED_AND_TESTED | test_maker, test_maker_stage2; Windows frozen OS input |
| 78 действий и параметры | IMPLEMENTED_NOT_E2E_TESTED | Все 78 формы: Qt apply/save/reopen; все оригинальные execute измеряются через GraphRunner. Итоговые Windows/Linux CI отчёты Stage 6 обязательны; это не 78 независимых внешних интеграций |
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
| Первый запуск и единая помощь | IMPLEMENTED_NOT_E2E_TESTED | Постоянная кнопка помощи, пути/примеры/setup, принятие прежнего Maker и руководство; frozen Stage 6 приёмка обязательна |
| Защищённый backup/restore продукта | IMPLEMENTED_NOT_E2E_TESTED | AES-GCM, приватные SQLite snapshots, durable staging/recovery, запрет повторного POST; Linux targeted passed, Windows CI нового SHA обязателен |
| SQLite migrations | IMPLEMENTED_AND_TESTED | Schemas 1–7: проверенная копия до изменений, отказ без порчи при ошибке; настоящие предыдущие source producers проверяются отдельно |
| Обновление Windows Full | IMPLEMENTED_NOT_E2E_TESTED | Стабильные per-user данные, legacy adoption без переноса ключей, совместимость Service по SHA, ручное обновление; frozen CI обязателен |
| Безопасный diagnostic export | IMPLEMENTED_NOT_E2E_TESTED | GUI экспорт только allowlist версий/статусов/ёмкостей, без конфигурационных значений; отдельный canary test и новый frozen CI |
| Импорт закрытых проектов ZennoLab | INTENTIONALLY_UNSUPPORTED | Формат .zp/.zppro не реализован |
| Arbitrary Python/JS из импортированного JSON | INTENTIONALLY_UNSUPPORTED | Registry доверенного кода, без eval/import из графа |
| CAPTCHA/antidetect/платные сторонние сервисы | INTENTIONALLY_UNSUPPORTED | Вне согласованного объёма; лицензий/безопасного API нет |
| Безусловная 24/7 бесплатная notebook работа | INTENTIONALLY_UNSUPPORTED | Ресурсы/сроки определяет провайдер |


| Дополнительный сценарий | Статус | Граница |
|---|---|---|
| Несколько редактируемых графов одновременно | IMPLEMENTED_BUT_PARTIAL | Один документ Maker; сохранённые проекты открываются последовательно |
| Приоритеты произвольных заданий | MISSING | FIFO/квоты/лимиты партии; произвольная priority queue не согласована как P0/P1 |
| Подпроекты с inputs/outputs | IMPLEMENTED_AND_TESTED | Original GraphRunner, общий budget/depth, явно экспортируемые значения |
| Произвольные блоки кода | INTENTIONALLY_UNSUPPORTED | Только доверенный Registry; нет eval/import из графа |
| Полный Chrome profile directory | INTENTIONALLY_UNSUPPORTED | Поддерживается валидируемое состояние cookies/localStorage; нет гарантии IndexedDB/extensions |
| Managed UNKNOWN и DETACHED результаты | IMPLEMENTED_AND_TESTED | Разные режимы; signed DETACHED не подтверждает managed автоматически |
| Физический Windows reboot / потеря питания | EXTERNAL_VALIDATION_REQUIRED | Process-kill/SQLite fault tests не подменяют реальную перезагрузку и storage power-cut |
| Цифровая подпись / автоматический updater | INTENTIONALLY_UNSUPPORTED | Нужна инфраструктура; документировано ручное обновление полного пакета |

Основные прочитанные источники:

- [Окно браузера](https://github.com/ZennoLab/docs/blob/main/docs/ZennoPoster/ProjectMaker/Browser_Window/Browser_Window.mdx): вкладки, навигация, запись, DevTools, cookies.
- [Конструктор и XPath](https://github.com/ZennoLab/docs/blob/main/docs/ZennoPoster/ProjectMaker/Browser_Window/Action_Constructor_and_XPath_Search.mdx): выбор элемента, атрибуты, поиск, выполнение и извлечение данных.
- [Редактор проекта](https://github.com/ZennoLab/docs/blob/main/docs/ZennoPoster/ProjectMaker/Project-editor/Project_Editor.mdx): библиотека, свойства, вкладки проектов и статические блоки.
- [Окно переменных](https://github.com/ZennoLab/docs/blob/main/docs/ZennoPoster/ProjectMaker/Program-interface/Variables_Window.mdx), [циклы](https://github.com/ZennoLab/docs/blob/main/docs/ZennoPoster/ProjectMaker/Recording-and-creating-projects/General-information/Loops.mdx), каталог Logic/Http/Data/Tables в том же репозитории.
- [Запуск отладки](https://github.com/ZennoLab/docs/blob/main/docs/ZennoPoster/ProjectMaker/Debugging%20projects/Starting_Debugging.mdx), [актуализация действий](https://github.com/ZennoLab/docs/blob/main/docs/ZennoPoster/ProjectMaker/Debugging%20projects/Re-recording_Problematic_Sections.mdx), [трассировка](https://github.com/ZennoLab/docs/blob/main/docs/ZennoPoster/ProjectMaker/Debugging%20projects/Project_Tracing.mdx).
- [Настройки исполнения ZennoPoster](https://github.com/ZennoLab/docs/blob/main/docs/ZennoPoster/ZennoPoster/Project_delivery/Project_settings/Settings.mdx): повторения, потоки, приоритеты, прокси и изоляция данных.


Документация ZennoLab используется только как описание сценариев. Закрытый код,
графика, плагины и форматы .zp/.zppro не копируются. Все интеграции стороннего
продукта и его антидетект не объявляются реализованными в Poster.
