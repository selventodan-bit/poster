# Poster Stage 5D — переносимость исполнения

Ветка `codex/poster-5d-execution-portability` создана строго от принятого
`78971e96952a24202e7b1cbb50d0dd0579b8a0c3`. main, PR №2, принятые 5A/5B/5C
не изменяются; merge не выполняется. Документ развивается вместе с реализацией;
аудит не является заявлением о завершении 5D.

## Аудит и границы расширения

Проверены контракты 3B.1/3B.2, 4B.1/4B.2, 5A/5B, 5C, модули cloud, providers,
profiles, HTTP actions, snapshots, admission, Worker, Remote Agent, signed results,
Scheduler и текущие 18 cross-host сценариев. Принятые 38+7+4 jobs сохраняются.

| Компонент | Уже есть | Windows / Linux | Stage 5D |
|---|---|---|---|
| GraphRunner / 78 actions | Единый исполнитель, изолированные variables/results | Обе ОС; Qt нужен только GUI | Передать подготовленное окружение в этот же Runner |
| Profiles | Create/rename/clear/delete, encrypted snapshot, revision conflict | CDP cookies и localStorage Chromium/Qt | Clone, compatibility, выбор origins, target-bound consent/expiry/revoke, UI Dispatcher |
| Session storage | HttpOnly/Secure/SameSite/domain/path/expiry cookies через CDP; localStorage через supported API | Разрешённые данные, без копирования browser directories | Проверка схемы и ограничений; IndexedDB/sessionStorage/device-bound credentials не обещаются |
| Persistent profile ownership | FileLock сериализует выбранный профиль между очередями | Обе ОС | Не допускать конфликтующих lifecycle операций; новые экземпляры клонируют состояние |
| Remote / Scheduler | Одна native queue, leases, cancellation, UNKNOWN/no resend | Windows Controller + Linux Agent | Шифрованные явные параметры сессии/сети в существующем run envelope |
| Network | HTTPState trust_env=False, TLS, zero write retries; BrowserProvider | Chromium headless и Qt GUI | Private network profiles, HTTP(S)/SOCKS capability checks, run override, safe diagnostics |
| Small resources | Explicit files, 128/20 MB, SHA, relative paths, immutable version | Обе ОС; Drive/Generic notebooks | Directory preview, dependency analysis, bounded larger file streaming |
| Large transfers | Drive resumable upload, но вход целиком bytes; outputs до 14 MB | Drive transport; Remote input 10 MB/output 4 MB | Chunk data manifests, bounded RAM, resume of data only; не увеличивать молча Remote wire limits |
| Drive / Launcher | Catalog, PKCE/DPAPI, Jupyter + marimo, signed/encrypted DETACHED | Local notebook runtimes проверены | Расширить существующие форматы/адаптеры, сохранить 5C compatibility |
| Readiness | Browser/resource/SecretRef checks при admission | Наблюдения текущей среды | Отдельно static compatibility и подтверждённый runtime; ни одного fictitious READY |

Local profile storage не является полным переносимым Chrome/QtWebEngine profile.
Не переносятся автоматически папки браузера, Windows DPAPI/session keys, passkeys,
client certificates, OS-bound credentials, расширения, IndexedDB и sessionStorage.
Даже разрешённый cookie snapshot не гарантирует вход: сервер может потребовать
повторную авторизацию, MFA или привязку к устройству. Partitioned cookies требуют
поддержки API; потеря partition semantics не маскируется обычным cookie.

Для public Colab/Molab чувствительные browser sessions по умолчанию запрещены.
Notebook execution остаётся интерактивным с фактическим максимумом 1. Перенос в
обычный доверенный Agent требует явного согласия, pinned recipient и срока доступа.
Qt embedded provider не получает глобальную proxy настройку, которая затронула бы
Drive/Recorder/другие проекты; неподдерживаемая комбинация отклоняется заранее.

Google Drive/Colab/Molab live accounts по-прежнему не предоставлены:
`CLOUD_PROVIDER_NOT_EXECUTED`. Drive fixtures и реальные локальные Jupyter/marimo
не выдаются за live provider E2E. Платные ресурсы не создаются.

## Реализованные блоки: сессия и сеть

Dispatcher хранит временное согласие в своей существующей SQLite: конкретный
доверенный Agent ID, immutable project/version, выбранные origins, срок 5–900 секунд
и число копий. Пакет дополнительно зашифрован для закреплённой identity Agent,
подписан Controller и привязан к run ID. Отзыв до отправки останавливает передачу;
после возможной отправки cookies нельзя отозвать у получателя — требуется выход
на самом сайте. Для каждого перенесённого экземпляра создаётся отдельный профиль.
Очередь, планировщик и GraphRunner остаются прежними; queue.key не передаётся.

Сетевой профиль хранится в приватном SecretStore (Windows DPAPI; на явном Linux
host — Fernet и права пользователя). В публичном графе только logical binding
и разрешение override. HTTP backend поддерживает direct, HTTP/HTTPS и SOCKS5 с
авторизацией. Chromium поддерживает HTTP/HTTPS и SOCKS5 без авторизации; его
SOCKS5 с credentials и proxy для встроенного Qt отклоняются до внешнего действия.
Пользовательский CA HTTPS proxy поддержан HTTP backend, но не Chromium.
Credentials не включаются в server URL/argv и передаются Worker в зашифрованном
payload. Проверка proxy делает один GET, проверяет TLS и возвращает безопасный
код DNS/TLS/auth/connect/timeout без URL, заголовков и тела ответа.

Локальные доказательства: 53 проверки session/network/native store/registry/Drive
PASSED; отдельно реальная цепочка Controller → TLS Relay → Linux Agent → Chromium
восстановила синтетический вход и очистила временный профиль. Это Linux fixture,
не свидетельство Windows↔Linux или live Google Drive. Финальный CI ещё впереди.

## Ограниченные большие данные

Новый portable v2 допускает 128 файлов, 256 MiB на файл и 512 MiB на пакет.
Небольшие пакеты сохраняют формат 5C. Выбранные ресурсы читаются блоками 1 MiB,
immutable native snapshot хранит собственные приватные копии и проверяет SHA256.
Drive передаёт части с проверкой каждой части и целого. Защищённые outputs больше
14 MB шифруются по частям для result recipient; суммарный предел outputs — 256 MiB.
Commit результата публикуется последним. Журнал возобновляет данные с прежними
Drive ID и ciphertext; он не запускает GraphRunner. Чужой run ID/контекст и
перезапись существующего результата запрещены. Незавершённые части можно удалить
после 24 часов только при отсутствии их publication manifest; опубликованные
части очистка не удаляет. Локальные partial-файлы остаются приватными для resume;
удаление сессии/её рабочей папки удаляет их. Свободное место проверяется с резервом
32 MiB; минимально доступная память передачи — 64 MiB. Квота Drive и срок notebook
зависят от внешнего провайдера и не гарантируются.

Files.Copy/Move передают до 256 MiB потоково, бюджет записи — 512 MiB на запуск.
ReadText/таблицы/HTTP multipart сохраняют прежние ограниченные in-memory контракты;
проверка готовности отмечает непригодный большой вход. Remote protocol 1 сохраняет
10 MB входов и 4 MB возвращаемых файлов: большой remote пакет отклоняется до
admission. Через Drive/Generic Jupyter и local Worker работает новый путь.
ZIP автоматически не распаковывается; при выборе проверяются имена, число файлов,
размер распаковки, symlink, encryption и коэффициент сжатия.

Локально: 24 MiB через Drive fixture → оригинальный Worker → encrypted chunks →
archive; обрыв публикации outputs и resume без второго Worker; повреждённая часть,
другой контекст и перезапись отклонены. Копирование 64 MiB проверено отдельным
процессом с peak RSS менее 100 MiB. Это измеренные конечные размеры, не обещание
неограниченных ресурсов или live Google Drive.

## Подготовка в Maker и Dispatcher

В меню **Подготовка проекта → Профили, сеть, ресурсы и готовность** есть четыре
вкладки. Сначала сохраните проект. Выберите чистую сессию либо создайте/клонируйте
профиль. Изменение профиля во время исполнения запрещено; общий профиль
выполняется последовательно, независимые копии имеют отдельные контексты.
Для профилей из другой папки выберите эту папку и её локальный session.key через
файловый диалог — ключ не передаётся в чат или в проект. Местоположение выбранного
профиля закрепляется в приватном локальном хранилище, отдельно от публичного графа.
Сохранение сессии Maker по-прежнему выполняется только явной кнопкой.

Для приватного file export сначала сохраните открытый ID получателя на другой
машине. Перед экспортом выберите его файл и независимо проверьте полный ID;
укажите origins и срок разрешения. Импорт требует открытый ID доверенного
отправителя и ключ именно получателя. Создаётся новый независимый профиль.
File export уже означает начало передачи: после записи зашифрованного файла
отзыв у получателя невозможен. Для управляемого Linux Agent используйте согласие
Dispatcher: выберите закреплённый Agent ID, origins, срок и количество копий,
затем выберите это согласие в окне запуска. До передачи его можно отозвать.
При истечении срока потребуется новое явное согласие. Ничего не выдаётся навечно
для Scheduler, очереди или notebook. Очистите исходный экспорт, когда он больше
не нужен; перезапись чужого файла запрещена. Удаление не гарантирует физическое
стирание данных с SSD/резервных копий.

На вкладке **Сеть** создайте приватную конфигурацию, выберите её для проекта и
при необходимости разрешите override. Отдельная кнопка проверяет один GET к
нейтральному адресу. Сам тест не подтверждает авторизацию на целевом сайте.
В окне batch enqueue есть выбор сети и режима профиля каждой копии. Нельзя
переопределить сеть, если проект явно запретил это. Сохранённый профиль сети
можно использовать в Scheduler через настройки проекта; credentials фиксируются
в зашифрованном входе вместе с конкретной версией и не меняются посреди запуска.

В notebook используйте существующее поле **Имена защищённых bindings**:
`"Сеть проекта (private JSON)": "POSTER_SECRET_PROXY"`. Значение секрета — JSON
конфигурация с kind/host/port/username/password; оно задаётся только в Secrets
провайдера или private environment. В ячейке и widget остаётся только имя.
Публичный snapshot содержит logical binding, конфигурацию Worker получает в
защищённом IPC. Colab/Molab остаются интерактивными и допускают одну копию;
их аккаунты/доступность/entitlement здесь не подтверждены.

На вкладке **Ресурсы** выберите файлы либо отдельный разрешённый каталог.
Проверьте список, размеры и SHA256 до подтверждения. Личная папка, системный
корень, приватные каталоги, credentials и ссылки запрещены. Внешние файлы
копируются в resources проекта с безопасными именами. Пакет не захватывает
домашнюю папку автоматически. После изменения данных сохраните новую версию;
прежние immutable данные остаются закреплёнными.

На вкладке **Готовность** выберите Windows/Linux/Colab/Molab/Generic Jupyter и
BrowserProvider. Отчёт отдельно показывает статическую совместимость и реальные
наблюдения. Для локальной среды проверяется запуск браузера, доступное место/RAM
и нейтральное соединение. Для Agent выбирается доверенная машина и выполняется
явный безвредный граф через ту же очередь с возвращаемым файлом. Доступ Agent к
сайту проекта требует отдельного подтверждения. Metadata, наличие браузерного
файла или зелёный CI сами по себе готовность внешнего аккаунта не подтверждают.

Демонстрация: `examples/maker-stage5d/project.postergraph.json`. В ней закреплён
CSV, потоковое копирование, чтение таблицы и браузерное чтение example.org без
логина и POST. Сетевой профиль/сохранённый вход выбирайте отдельно приватно.
Папки результатов каждой копии видны в существующем Dispatcher; результаты
Detached импортируются в отдельный архив. UNKNOWN/REVIEW_REQUIRED требуют
проверки последствий; файловый resume не создаёт новый Worker и не делает
повторный POST. Сохраняемые копии и результаты занимают диск до их удаления
оператором; учитывайте общий объём всей серии, а не только одновременных копий.

## Проверки и оставшаяся внешняя приёмка

Новые обязательные jobs Stage 5D выполняют одинаковые проверки на Windows и Linux:
scoped session transfer/expiry/revoke, реальные HTTP/HTTPS/SOCKS5 proxy, неверный
пароль, две разные сети у браузеров, приватный notebook network binding, большие
ресурсы/outputs, chunk corruption/resume, закреплённые hashes и readiness.
Отдельные jobs запускают пять настоящих Chromium процессов и пять исходных
Native Workers, затем проверяют cookies/localStorage, отмену одной копии, timeout
другой, повреждённый профиль и независимые output-папки. Qt jobs нажимают реальные
кнопки общего диалога. Frozen workflow дополнительно открывает подготовку из
новых Poster.exe и PosterDispatcher.exe настоящим OS-вводом, затем сохраняет
прежнюю цепочку Maker → Drive fixture → Jupyter → encrypted result → Dispatcher.

Сохранены 38 основных, 7 notebook/Drive и 4 cross-host jobs принятого Stage 5C.
Cross-host берёт Windows/Linux production packages именно текущего SHA,
проверяет BUILD_INFO/SHA256, отдельную Linux OS, реальные PID/TLS/SSH, историю
SQLite и все прежние 18 сценариев. До финального зелёного запуска это не считается
завершённой приёмкой 5D. Реальные аккаунты Google Drive/Colab/Molab не предоставлены:
**CLOUD_PROVIDER_NOT_EXECUTED**. Fixture-проверки не заменяют live API.

На этап 6 остаются только согласуемая финальная продуктовая приёмка, реальные
аккаунты/entitlements и сценарии сайтов, которые требуют ручного входа. Новые
безусловные гарантии переноса чужих браузерных хранилищ, бесконечных notebook-сессий,
неограниченных файлов или exactly-once не вводятся. Расширение protocol 1 для
больших Relay payload потребует отдельного совместимого контракта; текущие лимиты
показаны до admission. Stage 6 этим заданием не начинается.

## Покрытие 30 сценариев исходного задания

Это карта обязательных проверок; итоговый статус определяется запуском CI на
точном release SHA. Unit fault injection для RAM/диска и повреждения данных
проверяет отказ, не имитирует реальное исчерпание ресурсов провайдера.

| № | Проверка | Доказательство |
|---|---|---|
| 1 | Lifecycle профиля | test_portability_sessions + реальные Qt controls |
| 2 | Cookies пяти браузеров | portability_browser_instances, Windows/Linux |
| 3 | Private scoped export | test_portability_sessions, target/signature/ciphertext |
| 4 | Import и несовместимость | настоящий Chromium; другие identity/provider запрещены |
| 5 | Повреждённый профиль | lifecycle и повреждение одной из пяти сессий |
| 6 | Нет session/proxy plaintext | ciphertext/log/argv/artifact boundaries; cross-host scan |
| 7 | HTTP/HTTPS/SOCKS5 | настоящие локальные proxy и TLS endpoints |
| 8 | Неверная proxy auth | HTTP 407 и SOCKS5 auth отказ |
| 9 | Разные сети двух Workers | одна Native queue, два PID и два proxy, разные cookies/outputs |
| 10 | Большой вход | 24 MiB через Drive fixture и оригинальный Worker |
| 11 | Потоковая передача | 1 MiB chunks; 64 MiB copy отдельным процессом, RSS <100 MiB |
| 12 | Обрыв и resume | прерван output upload; прежний Native job/Worker |
| 13 | Повреждение части | hash mismatch до commit/import |
| 14 | Недостаток RAM/диска | проверка фактических counters и unit fault injection |
| 15 | Закреплённые ресурсы | immutable native copy остаётся после изменения source |
| 16 | Конфликт версии | existing Drive atomic slot conflict tests + v2 unchanged |
| 17 | Readiness всех сред | пять target cases; static отдельно от measured runtime |
| 18 | Нет браузера | реальный probe с недоступным executable, fail closed |
| 19 | Неподдерживаемый профиль | provider/public-cloud refusal до передачи |
| 20 | Нет SecretRef | readiness и native validation до admission |
| 21 | Пять настоящих браузеров | Chrome PID/birth, реальные contexts и пять Native Workers |
| 22 | Outputs изолированы | отдельные Native workdirs и Detached result archive |
| 23 | Cancel/timeout | адресная отмена/timeout, startup-gate cancellation, старые regression |
| 24 | UNKNOWN при разрыве | прежний cross-host partition/Agent recovery |
| 25 | Нет второго POST | cross-host POST count=1; отдельный proxy timeout test |
| 26 | Windows frozen GUI | новый Full: OS keyboard/mouse, два preparation dialogs |
| 27 | Linux Worker | TLS Remote E2E и отдельная Linux OS в cross-host |
| 28 | Drive fixtures | прежние Drive/OAuth/atomic commit tests, v1 и v2 |
| 29 | Jupyter/marimo | настоящие kernels/runtimes; private named network binding |
| 30 | Прежние 18 | current-SHA Windows Full и Linux packages, старые 18 без исключений |

Дополнительный cross-host граф 5D одновременно использует закреплённый CSV,
согласованный профиль (HttpOnly cookie + localStorage), authenticated proxy
к контролируемому виртуальному origin и возвращаемые файлы. Эти данные тестовые;
ни пользовательские профили, ни proxy passwords в публичные доказательства
не включаются. Граф выполняет тот же production GraphRunner.

## Исправления, найденные при приёмке

Старое окно typed inputs допускает отсутствие source/project/version и нового
profile-binding cache. Необязательная локальная привязка больше не меняет этот
контракт. Проверка отчёта в GUI отделена от общей подсказки о ресурсах.

Сборка предыдущего Linux release берёт документы и examples только из выбранного
Git snapshot. Если в нём ещё не было 5D, новые файлы в старый пакет не подмешиваются.
Manifest проверяет именно содержимое выбранной версии.

Windows venv launcher может владеть отдельным процессом Python. Раньше Store.start
видел PID launcher вместо Worker и отклонял запуск до первого кубика. Теперь
анонимный pipe сообщает PID/birth, Dispatcher проверяет его по ОС и происхождению
от своего launcher, сохраняет фактический PID и только затем выдаёт gate/key.
Windows Job Object продолжает владеть всем деревом. Неподтверждённая identity
останавливает запуск; queue.key в argv/environment не передаётся.

В диагностике отказа startup gate сохранена совместимость с сокращённым Remote
ExecutionAdapter: ранняя адресная отмена остаётся CANCELLED, а не превращается
в WORKER_INTERNAL_ERROR из-за отсутствующих локальных полей.

Frozen ESC действительно закрывает окно. Проверка теперь ожидает его отсутствие,
не обращается к is_visible уже уничтоженного native window. Все действия
по-прежнему выполняются настоящими OS keyboard/mouse по свежей геометрии.

В Recorder запрос мог появиться в capture до решения асинхронного routing gate.
В управляемой RecognitionSession такой write изначально blocked и становится
допущенным только после существующих проверок scope/click/dependency. Это
устраняет попадание ожидающего фонового запроса в UNLINKED_REQUESTS; требования
к распознаванию, отсутствие второго POST и read-only политика не ослаблены.

## Закреплённая доставка Launcher

Notebook Launcher закреплён на runtime commit
`e828f6c60010d71612e69681ce340eb8a0aeacc7`, а не на main/HEAD.
SHA256 исходного `tar.gz`:
`89bbb4e8755a8e47fce7049877570ea052acd9b2a03f33597796e5ced37bdbfc`.
SHA256 requirements:
`64ed7d46c05c6e408a4a4583b3d7d56e199764e90c88d9fd3489148cc68f18ba`.
Проверка установила поставляемый installer в новую venv, включая закреплённый
Molab gdrive-fsspec, и выполнила оригинальный Native Worker успешно. Финальная
сборка может иметь следующий delivery commit: код runtime остаётся закреплённым
на указанном неизменяемом SHA. Индивидуальные hashes notebook-файлов находятся в
`notebooks/poster-launcher-pins.json`; ZIP/TAR hashes — в GitHub Actions artifacts.

Cookies сохраняют исходные domain/path/SameSite/Secure semantics: выбранный origin
может включать domain cookie, действующий также на его поддомены. Это не даёт
новых browser privileges и не гарантирует вход. Partitioned cookies не переносятся;
IPv6 cookie domains в текущем формате отвергаются. Изменение/очистка исходного
профиля не отзывает уже переданные данные: для этого отзовите сеанс на сайте.
