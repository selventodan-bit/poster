# Poster 3.0 — Stage 5B: установка, подключение и проверка Remote Agent

Windows Full Dispatcher → локальный PosterService.exe → одна `dispatcher.sqlite`
→ исходящий HTTPS → TLS Relay ← исходящий HTTPS ← Linux Agent → исходный
GraphRunner. Scheduler, локальные Workers, snapshots, FileSandbox и протокол 5A
сохранены. Agent хранит replay journal, Relay — opaque mailboxes; собственной
авторитетной очереди у них нет. На Windows новый сетевой порт не открывается.

**Статус межмашинной приёмки: NOT_EXECUTED.** Доступная облачная машина — Linux.
GitHub jobs Windows и Linux по отдельности, перенос графов Windows Maker и
Linux/Linux TLS E2E не доказывают одновременную связь frozen Windows Controller с
Linux Agent. Ни зелёный job `remote-crosshost-harness`, ни наличие harness не
означают прохождения этого критерия. Для него нужны две доверенные машины и
общий доступный Relay; процедура приведена ниже.

## Требования и доверие

Поддерживаемый установщик: Debian/Ubuntu, Linux x86_64, Python 3.11–3.13 с `venv`
и `ensurepip`. Офлайн wheelhouse в CI собран для **CPython 3.12 Linux x86_64**;
его ABI указан в `release.json`. Для другого Python используйте онлайн установку
с закреплёнными хешами или соберите wheelhouse тем же Python. ARM, Alpine/musl,
Windows Agent и другие версии Python этим установщиком не поддерживаются.

Минимум проверки: 64 MiB свободного места; предупреждение при доступной памяти
ниже 256 MiB. Это диагностические пороги, а не обещание достаточной мощности:
два Chromium Worker обычно требуют несколько GiB памяти и существенно больше
места для программы, браузера и проектов. Заложите запас диска для двух версий
venv. Пакет не содержит Qt/Tk/PySide6, браузер и Docker не требуются для HTTP/data.

Оператор отдельно обеспечивает:

- VPS/обычный Linux с постоянным приватным диском, корректными часами/NTP и DNS.
- HTTPS hostname с SAN в сертификате, firewall и доступным TCP-портом Relay
  (например 8443). Только Relay принимает внешние подключения.
- Исходящий HTTPS у Windows и Agent. Корпоративные proxy окружения transport
  автоматически не использует (`trust_env=False`); нужен прямой разрешённый маршрут.
- Установку системных пакетов и включение systemd пользователем-администратором.
- Доверие к Linux оператору: разрешённый Agent получает необходимые секреты
  конкретного запуска в памяти. Это не среда для враждебных графов или хоста.

Постоянные приватные каталоги Agent и Relay должны принадлежать разным обычным
пользователям при размещении на одном VPS. Ниже команды для каждой роли выполняет
её владелец, без root. Не запускайте графы через sudo.

## Получение и проверка Linux-пакета

В Actions нужного SHA скачайте artifact **poster-linux**, содержащий
`poster-linux.tar.gz` и `poster-linux.sha256`. Сначала сверьте SHA коммита и SHA256
с доверенной страницей CI/публикации: соседний checksum защищает от повреждения,
но сам по себе не удостоверяет источник. Не выполняйте скрипт из непроверенного архива.

```bash
sha256sum --check poster-linux.sha256
mkdir -m 700 unpacked
python3.12 -m tarfile -e poster-linux.tar.gz unpacked
cd unpacked/poster-linux
python3.12 install_remote.py --help
```

`release.json` содержит полный Git SHA и SHA256 каждого файла, включая installer,
wheel, hashed requirements и wheelhouse. Installer повторно проверяет manifest,
отклоняет symlink/опасные пути, создаёт versioned venv, устанавливает зависимости
с `--require-hashes --only-binary=:all:`, затем свой wheel без разрешения новых
зависимостей и делает `pip check`. `uv.lock` — источник закреплённых версий.

Сборка оператором из проверенного чистого checkout:

```bash
uv sync --frozen --extra dev --extra recorder
uv run --no-sync python scripts/build_remote_release.py --output .poster/linux-release --wheelhouse
```

Builder берёт исходники через `git archive` точного commit в новый каталог.
Незакоммиченные исходники и старый `build/lib` в release не попадают.

## Установка Relay

Владелец Relay выбирает отдельные абсолютные пути; примеры используют домашний
каталог. Не размещайте state внутри program и program внутри state.

```bash
python3.12 install_remote.py install --release "$PWD" --role relay --program "$HOME/poster-relay-program" --state "$HOME/poster-relay-state" --offline
export RELAY_PY="$HOME/poster-relay-program/current/bin/python"
```

Создайте сертификат с нужным DNS SAN. Для небольшого частного развёртывания есть
явный self-signed вариант; его публичный сертификат передаётся доверенным каналом
как CA, приватный `tls.key` остаётся только у Relay:

```bash
"$RELAY_PY" -m poster.remote tls-init --root "$HOME/poster-relay-state" --hostname relay.example.org
"$RELAY_PY" -m poster.remote relay-init --root "$HOME/poster-relay-state" --url https://relay.example.org:8443 --ca "$HOME/poster-relay-state/tls.crt"
"$RELAY_PY" -c 'import json,pathlib; p=pathlib.Path.home()/"poster-relay-state/relay-service.json"; p.write_text(json.dumps({"host":"0.0.0.0","port":8443})); p.chmod(0o600)'
"$RELAY_PY" -m poster.remote doctor --root "$HOME/poster-relay-state" --role relay
```

Для публичного CA вместо `tls-init` установите полученные через ACME fullchain и
ключ в приватные `tls.crt`/`tls.key`; `relay-init` вызовите без `--ca`. Для продления
атомарно замените cert/key, проверьте `doctor`, перезапустите Relay. Для смены
частной CA заранее обновите доверенные профили обоих endpoints контролируемой
процедурой; insecure fallback нет. HTTPS→HTTP reverse proxy к Relay не используется:
сам Relay обслуживает TLS. При проксировании сохраняйте TLS до Relay.

`relay-init` создаёт два независимых bearer credential, серверный `relay.json`
с хешами и приватный операторский **pairing-relay.json**. Передайте последний на
Windows безопасным файловым каналом (например SFTP с проверенным SSH host key).
Это намеренная передача credentials с согласия оператора. Не отправляйте их в
чат, git, Actions secrets-echo, QR, URL или argv. Не копируйте `tls.key` на Windows
или Agent. После подготовки уберите лишние копии профилей в защищённое хранилище.

Один канал предназначен ровно для **одной пары Controller/Agent**, внутри Agent
могут параллельно работать несколько Workers. Повторная регистрация другого
Agent на том же Relay/channel отклоняется, включая ранее отозванный канал.
Для нескольких Agent оператор выдаёт отдельные каналы/credentials: данный простой
bootstrap готовит один канал на экземпляр Relay; несколько экземпляров используют
разные state, порты, имена unit, настраиваемые администратором. Нельзя просто
раздать один `agent-relay.json` всем машинам.

## Установка и pairing Agent из Windows

На Linux под отдельным обычным пользователем:

```bash
python3.12 install_remote.py install --release "$PWD" --role agent --program "$HOME/poster-agent-program" --state "$HOME/poster-agent-state" --offline
export AGENT_PY="$HOME/poster-agent-program/current/bin/python"
```

Служба ещё не готова: identity и journal намеренно создаются только при явном
pairing. В Windows Full запустите `PosterDispatcher.exe`, вкладка **Удалённые
Workers → Подключить Linux Agent пошагово…**.

1. Введите `https://relay.example.org:8443`. Controller создаётся локально;
   показывается только его полный публичный fingerprint.
2. Нажмите **Проверить TLS и создать pairing-файл…**, выберите приватный
   `pairing-relay.json` оператора и место для приватного `pairing.json`.
   Подтвердите передачу Agent credential доверенному Linux. Выполняется настоящий
   аутентифицированный TLS probe без потребления сообщений mailbox.
3. Передайте `pairing.json` на Linux приватным каналом. Отдельно сверьте полный
   Controller fingerprint в Windows и в команде ниже. Подставляется **публичный** ID:

   ```bash
   "$AGENT_PY" -m poster.remote pair --root "$HOME/poster-agent-state" --name linux-worker-1 --offer "$HOME/pairing.json" --expect-controller FULL_64_HEX_CONTROLLER_FINGERPRINT
   "$AGENT_PY" -c 'import json,pathlib; print(json.loads((pathlib.Path.home()/"poster-agent-state/registration.json").read_bytes())["peer"]["id"])'
   ```

4. Передайте только публичный `registration.json` обратно в Windows. Введите
   полный Agent fingerprint, полученный отдельно с Linux, **до** импорта заявки.
   Нажмите **Импортировать заявку Agent…**: она становится PENDING.
5. Сверьте fingerprint и нажмите **Подтвердить fingerprint и доверие**.
   Подтверждение даёт этой машине право получать необходимые секреты запусков и
   включает обслуживание общей очереди. Запустите Agent способом ниже.
6. Дождитесь **подписанного heartbeat ONLINE**. Доступный Relay при выключенном
   Agent оставляет OFFLINE. Диагностика и таблица показывают реальные наблюдения.
7. Нажмите **Безопасное тестовое задание** после ONLINE. Исходный GraphRunner
   через общую очередь создаст `poster-remote-check.txt`; диалог проверит его
   содержимое, покажет SHA256 и откроет папку с вернувшимся файлом на Windows.

Pairing nonce действует 15 минут, одноразовый; заявка подписана Agent и содержит
nonce, срок и оба pin. Controller проверяет срок, собственный ID, Agent pin и
подпись до регистрации, доверие подтверждается отдельно. Ошибочный fingerprint
не создаёт новую identity на Linux. Повторный `pair` существующий journal/identity
не сбрасывает. При истечении срока создайте новый offer; если Linux identity уже
создана, не удаляйте journal: используйте сохранённый ручной workflow 5A с
проверенными публичными pin либо отдельный новый state только после отключения
старой identity. Автоматического переобучения старого Agent нет.

`pairing.json` содержит долгоживущий credential роли Agent: короткий срок nonce
ограничивает регистрацию, а не заменяет отзыв/ротацию credential. После pairing
удалите транспортные копии offer. При компрометации отзовите Agent и замените
канал/relay credentials, а не только удалите файл.

## Реальный запуск, автозапуск и остановка

Для первой проверки в foreground (без администратора):

```bash
"$RELAY_PY" -m poster.remote serve --root "$HOME/poster-relay-state" --role relay
# На машине Agent, в другом терминале:
"$AGENT_PY" -m poster.remote serve --root "$HOME/poster-agent-state" --role agent
```

Обе роли требуют обычный UID. Для постоянного запуска installer создаёт отдельные
`poster-agent.service` и `poster-relay.service` в `~/.config/systemd/user`.
На обычном systemd Linux владелец каждой роли выполняет:

```bash
python3.12 install_remote.py service --role agent --action enable
python3.12 install_remote.py service --role agent --action start
"$AGENT_PY" -m poster.remote status --root "$HOME/poster-agent-state" --role agent
"$AGENT_PY" -m poster.remote doctor --root "$HOME/poster-agent-state" --role agent --network
```

Для Relay замените role на `relay`, PY/state на соответствующие. `start` означает
ответ systemd, **фактическую готовность** проверяют `status`, `doctor --network`
и подписанный heartbeat/безопасный граф из Windows. `status` сверяет PID и время
создания процесса, фазу и свежесть записи; Agent `online` дополнительно требует
недавнего подписанного pulse Controller. Локальная готовность процесса не
подменяет связь с Controller. Коды возврата doctor/status: 0 — готов, 2 — проблема.

Чтобы user services запускались при загрузке без входа пользователя, администратор
однократно включает linger: `sudo loginctl enable-linger USER`. Затем владелец
делает enable/start. В контейнере без systemd/доступного user manager это не
работает: используйте foreground для проверки, постоянный Linux host для boot.

Альтернатива для администратора — системные unit с обычным `User=...`:
вызовите install с `--service-user posteragent --unit-dir /private/staging/units`
под владельцем данных; администратор проверяет unit, копирует в
`/etc/systemd/system`, выполняет `systemd-analyze verify`, `systemctl daemon-reload`
и `systemctl enable --now poster-agent.service`. Для Relay используйте другого
пользователя. Program/state должны быть доступны этому User. Не используйте `/tmp`
для state: unit включает `PrivateTmp=yes`. Все графы по-прежнему исполняются
непривилегированным пользователем. В CI проверяется этот вариант с `User=runner`.

В unit: UMask 0077, NoNewPrivileges, PrivateTmp, ProtectSystem=strict,
ReadWritePaths только state, Restart=on-failure, KillMode=control-group и
TimeoutStopSec=45. Это ограничение службы, не полноценная OS-песочница графов.
Остановку/restart владелец делает через `service --action stop|restart`; для
системных unit администратор добавляет `--system --unit-dir /etc/systemd/system`.
Остановка Agent отправляет STOPPED heartbeat по возможности, отменяет свои
Workers и очищает принадлежащие им процессы. После сетевого обрыва признак
STOPPED может не дойти: через 30 секунд heartbeat станет STALE/OFFLINE.

## Первый свой проект, расписание и результат

В Dispatcher добавьте сохранённый `.postergraph.json`, нажмите **В очередь**,
выберите конкретный Agent, заполните входы и явный allowlist относительных
output-путей. Отправляется уже захваченный immutable snapshot; последующие правки
исходника не меняют задание. Работа остаётся в общей очереди: глобальные
`max_workers` и `max_per_project` совместны с Local Workers и расписаниями.
Offline Agent оставляет PENDING; Local/другой Agent автоматически не выбирается.

Для расписания создайте его в прежней вкладке Scheduler и явно выберите Agent и
output-пути в форме входов. Старые расписания остаются Local. Controller Service
должен работать на Windows; закрытие окна GUI допустимо. Его Windows logon
автозапуск включается отдельно прежней настройкой 4B.2.

В **История / результат** видны запуск, snapshot version, статус и проверенный
отчёт; **Рабочая папка** открывает Windows-копии возвращённых файлов. При UNKNOWN
проверьте внешний сайт вручную перед любым новым запуском. Поздний отчёт
сохраняется для review, не превращает UNKNOWN в разрешение на повторный POST.

## Диагностика и ошибки

Вкладка Workers → **Проверить соединение** либо кнопка диагностики в мастере.
Ниже категории, а не содержимое приватных исключений:

| Наблюдение / код | Что проверить |
| --- | --- |
| Relay=true, TLS=true | Реальный authenticated probe прошёл; это ещё не Agent ONLINE |
| REMOTE_TLS | Доверенную CA, SAN/hostname, срок cert, часы; не отключайте verification |
| REMOTE_NETWORK | DNS, firewall, порт, маршрут, запущенную службу Relay |
| REMOTE_AUTH | Правильный role profile/channel после ротации, не выводя token |
| REMOTE_VERSION | Совместимый protocol; используйте проверенные версии обоих endpoints |
| REMOTE_OFFLINE | Agent ещё не дал подписанного heartbeat либо обслуживание выключено |
| REMOTE_STALE | Подписанный heartbeat старше lease 30 s; живой Relay не продлевает его |
| REMOTE_STOPPED | Последний подписанный heartbeat явно сообщил STOPPED |
| WORKER_UNAVAILABLE | Целостность установленного headless runtime; переустановите программу без удаления state |
| BROWSER_UNAVAILABLE | Установленный Linux Chrome/Chromium либо корректный POSTER_CHROMIUM |
| REMOTE_NO_SLOTS | Общий лимит Workers, активные Local/Remote jobs и project limit |
| REMOTE_DISK / REMOTE_MEMORY | Свободный диск state/venv и память; уменьшите параллельность |
| REMOTE_EXPIRED | Разрешение/lease потеряно; UNKNOWN требует ручной проверки внешних эффектов |
| REMOTE_RESULT | Повреждённый/недоставленный результат; изучите историю, не повторяйте POST автоматически |
| REMOTE_FINGERPRINT / REMOTE_PAIRING | Независимо сверьте pin, срок и одноразовый nonce |
| REMOTE_CHANNEL | Этот mailbox уже закреплён за Agent, нужен отдельный канал |
| REMOTE_REVOKED | Явный отзыв; новым jobs и смене target без согласия отказано |
| KEY_INVALID / REMOTE_CONFIG в doctor | Identity/journal/ключи/настройки повреждены; не инициализируйте поверх |

Worker/browser/resource capabilities подписывает Agent; это его наблюдение, а не
удалённая аттестация честности хоста. Полезен полный тестовый граф с возвратом
файла. Диагностика не содержит bearer, cookies, protected inputs и private keys.
Приватные service/config/journal файлы и сырые crash dumps не публикуйте.

## Браузер и файлы

Agent использует уже установленный **Linux** Chrome/Chromium через прежний
Playwright provider. Браузер Windows Full/Maker на Linux не переносится. Установите
браузер системным менеджером пакетов либо Playwright по процедуре администратора
и проверьте его доступность под пользователем службы. Явный POSTER_CHROMIUM
можно задать администратором через systemd drop-in Environment с обычным путём к
executable; это путь, не секрет. После изменения daemon-reload/restart.

Snapshot переносит прежний manifest с Maker graphs/подпроектами и encrypted
values. Произвольные соседние картинки, CSV, Excel, абсолютные Windows-пути,
downloads и папка пользователя не копируются. Подготовьте данные явными
разрешёнными действиями внутри workdir или заранее на Linux в предусмотренной
модели FileSandbox. Новый перенос input ресурсов в 5B не добавлен. Системный путь
к исходному CSV из Windows не превращается в путь Linux. Windows browser profiles,
cookies/авторизация Maker профиля remote не переносятся: такой target отклоняется.

Output allowlist: до **32** безопасных относительных путей, до **4 000 000 bytes**
суммарно возвращаемых файлов; encrypted results также ограничены 4 MB.
Абсолютные пути, `..`, symlink и выход из sandbox запрещены. Нужные артефакты
выбирайте явно; история и результат возвращаются в исходную Windows queue.

Краткоживущая облачная сессия подходит только при сохранении identity и journal
на постоянном приватном диске. Потерянный journal не создаётся автоматически.
Нельзя клонировать одну identity/journal в два одновременно работающих Agent,
откатывать journal или восстанавливать старую VM во время действующего Controller:
это нарушит историю replay tombstones. Colab/Molab deployment здесь отсутствует.

## Обновление, резервные копии, удаление и авария

1. Остановите приём/расписания для обслуживания, дождитесь завершения jobs;
   остановите Agent service. Активный Agent lock запрещает обновление. Для Relay
   планируйте короткое общее окно: остановка дольше lease даст UNKNOWN активным jobs.
2. После остановки сделайте согласованную закрытую копию **всего state**, отдельно
   от program. Agent: `agent.json` + journal и WAL/SHM при их наличии + остальные
   файлы. Controller: вся постоянная папка, `queue.key`, identity, DB, snapshots,
   settings, schedules. Relay: TLS key/cert, hashed config, profiles, mailbox.
   Резервные копии шифруйте и храните с приватными правами, не в git/artifacts.
   Никогда не отправляйте Windows queue.key на Linux, в том числе для проверки утечек.
3. Проверьте происхождение/checksum нового release; повторите `install` с теми же
   program/state/role/параметрами unit. Новый venv получает каталог полного SHA.
   До атомарного переключения `current` doctor проверяет существующие state и
   journal новой версией. При повреждении отказ, identity/journal не заменяются.
4. daemon-reload/start; status, doctor --network, heartbeat и тестовый job с файлом.
   Старый venv остаётся до ручной уборки только каталога программы. Схему Dispatcher
   5B (v5) нельзя молча откатить на 5A (v4). Восстановление backup после неопределённого
   POST выполняйте только после сверки внешнего результата и отключения старой identity.

Для отключения: `service --role agent --action disable`, затем `--action stop`.
Для удаления unit: `service --role agent --action remove` (для системного unit —
администратор с `--system`). Эта операция disable --now + удаление unit + reload;
**state и program сохраняются**. Удаление данных — отдельное сознательное действие
после отзыва identity и проверки backup. Для Relay порядок аналогичен.

Аварийно нажмите **Отозвать Agent** в GUI; остановите Linux service и при
компрометации смените Relay channel/credentials. Приватные ключи Controller не
экспортируются, роли Agent и Controller имеют разные bearer. Отзыв блокирует
новые выдачи, возможные эффекты остаются UNKNOWN. Даже при пропавшей сети Worker
останавливается по lease watchdog, без автоматической повторной отправки.

## Воспроизводимая межмашинная проверка

Нужны настоящая Windows с Python **для harness**, распакованный frozen Windows Full
того же SHA, обычный Linux с установленным 5B release того же SHA, Chrome/Chromium,
OpenSSH с заранее проверенным host key, прямой маршрут Windows → DNS:порт Relay.
Приложения Windows проверяются с PATH только Windows/System32; Python harness не
является подменой PosterService.exe. Внешний Windows TCP API не создаётся.

На Linux положите проверенный `scripts/crosshost/linux_peer.py` в приватный каталог
под обычным пользователем; сравните SHA256 с checkout Windows. Передайте helper
проверенным файловым каналом. Linux `linux_root` должен быть новым отсутствующим
каталогом на постоянном диске вне production state. Harness создаёт отдельные
тестовые identity, relay и fixture, останавливает только процессы своей session;
production Agent/Relay не трогает. Для теста порт >1024, доступный по DNS. Helper
предназначен только для SSH control, не публикуйте его через HTTP/RPC.

Приватный локальный Windows config (пути замените своими; credentials в нём —
только ссылки на локальные файлы, содержимое ключей не вставляется):

```json
{
  "windows_bundle": "C:/PosterFull",
  "expected_commit": "FULL_40_HEX_STAGE5B_SHA",
  "ssh_destination": "postercheck@linux.example.org",
  "ssh_key": "C:/Private/postercheck_ed25519",
  "known_hosts": "C:/Private/known_hosts",
  "linux_python": "/home/postercheck/poster-agent-program/current/bin/python",
  "linux_helper": "/home/postercheck/checks/linux_peer.py",
  "linux_root": "/home/postercheck/checks/run-unique-001",
  "relay_url": "https://linux.example.org:18443"
}
```

Linux helper запускается тем же установленным Python как Agent и Relay; путь
`current` должен указывать на release SHA. SSH destination поддерживает стандартный
порт 22, обычный username@DNS, ключ с доступом через ваш agent/приватный файл,
BatchMode и StrictHostKeyChecking=yes. Не используйте trust-on-first-use без
внешней сверки host fingerprint. Нужен открытый test Relay port и правильный DNS;
firewall/cloud configuration harness не меняет. В fixture создаётся отдельная
частная CA с DNS SAN, доставляемая по уже доверенному SSH каналу.

На Windows из checkout того же SHA:

```powershell
python -m pip install ".[recorder]" -c requirements-recorder.txt pywinauto==0.6.9 Pillow==11.3.0
python scripts/check_remote_crosshost.py --config C:/Private/crosshost.json --output C:/Private/poster-crosshost-unique-001
```

Harness запускает настоящие frozen GUI/Service, проверяет SHA256 launchers и Linux
release SHA, реально подключает/одобряет Agent, проверяет плохую CA/hostname/auth,
immutable graph bytes, typed protected payload на HTTP fixture Linux, две параллельные
работы и общие/project limits, cancel, настоящий Chromium timeout и отсутствие
живых наблюдавшихся descendants, stop/reconnect, обрыв Relay после POST и UNKNOWN
без повторной отправки, output/Windows history, отзыв. Windows queue.key остаётся
на Windows: bounded encrypted/private evidence с Linux сравнивается на Windows,
ключ не посылается в SSH. Типы и SHA payload сохраняются вместо plaintext секретов.

В папке результата приватны все файлы кроме **report.json** и проверенного вручную
`windows-controller.png`: не загружайте весь каталог, config, observer, профили,
DB, key или raw tracebacks в CI. Linux test state также содержит приватные данные;
после анализа удалите его отдельно, после остановки процессов. Подробности ошибок
в публичном отчёте ограничены фазой и классом ошибки. При провале нет частичного
PASSED. Вызов на Linux отказывает в запуске Windows acceptance.

Без машин выполняется только:

```bash
python scripts/check_remote_crosshost.py --not-executed --output .poster/remote-crosshost
```

Это записывает 18 сценариев **NOT_EXECUTED** и `full_cross_host_acceptance=false`.
Никаких fake remote jobs или mock GraphRunner для замены межмашинного E2E нет.

## Проверки 5B и границы доказательств

- `test_remote_pairing.py`: реальные TLS probes (обе ОС), CA/hostname/auth/version,
  одноразовость/expiry, pins/signature, channel isolation, Relay≠Agent online.
- `remote_deployment_e2e.py`: actual offline venv Agent+Relay, настоящий graph/file,
  non-root systemd на Ubuntu VM, enable, stop/start, обновление **между двумя реальными
  коммитами 5B**, неизменные identity/journal, отказ при активном/повреждённом journal,
  удаление unit с сохранением данных. В контейнере — процессы без systemd.
- Прежние 33 jobs сохранены. Добавлены pairing matrix (2), release (1), deployment
  (1), публикация cross-host NOT_EXECUTED harness (1): всего **38 jobs**.
- Исходный Qt event loop на Windows/Linux проходит новый wizard с настоящим TLS
  и неверным fingerprint; native frozen Windows harness открывает мастер через OS
  mouse/keyboard и сохраняет screenshot. Это GUI evidence, не Linux Agent E2E.
- Прежний real-process `remote_linux_e2e.py` повторяет дубли grant, receipt loss,
  падения Controller/Relay/Agent/Worker, lease, invalid report, revoke, journal,
  таймаут браузера и запрет повторного POST. Ещё один Linux тест проверяет реальные
  процессы/helper/fixture приватного cross-host control канала, не заявляя Windows.
- Финальные SHA, CI counts, ссылки на артефакты/скриншоты и Full Windows ZIP SHA256
  приведены в отчёте задачи. До настоящего запуска harness Windows/Linux полная
  межмашинная приёмка остаётся открытой.
