# Poster 3.0 — Stage 5A: Secure Remote Linux Worker

Одна авторитетная очередь — `dispatcher.sqlite` существующего Dispatcher.
Local Worker и Remote Agent используют одинаковые immutable snapshots и исходный
`poster.dispatcher.worker.execute` / Maker GraphRunner. Scheduler 4B.2 остаётся
единственным генератором плановых заданий. Legacy HTTP Engine и notebooks
Colab/Molab не являются этим Agent и не изменены.

## Соединение и необходимые компоненты

```text
Windows Dispatcher Service ──исходящий HTTPS──> TLS relay <──исходящий HTTPS── Linux Agent
  queue + schedules + trust                  opaque mailboxes                 replay journal
  локальные Worker.exe                                                        отдельные Python workers
```

Для разных сетей нужен отдельно доступный **relay**, например на обычном Linux/VPS.
Открывается только его HTTPS-порт. Домашний Windows Controller и Agent не принимают
сетевые подключения; прежний Controller IPC остаётся локальным AF_PIPE/AF_UNIX.
Коммерческий провайдер, notebook, публичный Windows RPC и общая SQLite не нужны.
Relay хранит исключительно ограниченные зашифрованные envelopes и не решает,
что/когда запускать. Его SQLite отдельная и не совместима с Dispatcher DB.

Windows Full ZIP содержит Controller и новую панель «Удалённые Workers».
На Linux нужны Python 3.11+, пакет Poster и зависимости. Для HTTP/data достаточно
`pip install .`; для Browser-кубиков — `pip install '.[recorder]'` и самостоятельно
установленный Chrome/Chromium. Agent не загружает браузер и не импортирует Qt/Tk.
Запускайте его отдельным непривилегированным пользователем в приватном каталоге.
Это не OS-песочница для враждебных проектов; графы должны быть вашими доверенными
проектами с разрешёнными Maker actions.

## Пошаговое подключение

Используйте код одного проверенного SHA на Controller и Agent. На Linux клонируйте
ветку/нужный commit, создайте venv, установите пакет. Команды выполняются внутри venv.
Все приведённые пути — отдельные приватные каталоги, не папки проектов Maker.
Секретные значения не передаются в командной строке и не выводятся программой.

1. На машине relay подготовьте TLS-сертификат с SAN нужного hostname. Для частного
   тестового/внутреннего развёртывания есть явная генерация self-signed CA/certificate:

   ```bash
   python -m poster.remote tls-init --root /srv/poster-provision --hostname relay.example.org
   python -m poster.remote relay-init --root /srv/poster-provision --url https://relay.example.org:8443 --ca /srv/poster-provision/tls.crt
   ```

   Для публичного CA используйте его server certificate/key и `relay-init` без `--ca`.
   Проверка TLS и hostname обязательна; HTTP, insecure fallback и `verify=False` отсутствуют.
   `relay-init` создаёт `relay.json` с хешами двух разных bearer-токенов и приватные
   `controller-relay.json` / `agent-relay.json`. CA PEM встроен в профили и переносится
   между Windows/Linux без изменения абсолютных путей. Вариант поля `ca` с локальным
   путём поддерживается для управляемых установок. Не передавайте private `tls.key` клиентам.

2. Запустите relay на Linux. Доступность DNS/firewall/HTTPS на этой машине настраивает
   оператор; port forwarding домашнего Windows не используется:

   ```bash
   python -m poster.remote relay --config /srv/poster-provision/relay.json --database /srv/poster-provision/mailbox.sqlite --cert /srv/poster-provision/tls.crt --key-file /srv/poster-provision/tls.key --host 0.0.0.0 --port 8443
   ```

   Для постоянной эксплуатации используйте менеджер процессов и ограничения запросов
   на reverse proxy/firewall. Сам relay ограничивает размер, срок хранения, число
   потоков, время запроса и размер каждой mailbox. Windows `dispatcher.sqlite` ему
   не передаётся. Для локальных тестов те же три роли работают отдельными процессами
   с настоящим loopback HTTPS и своим явно доверенным сертификатом.

3. В Windows Full Dispatcher откройте **Удалённые Workers → Ключ Controller…**.
   Сохраните **публичный** `controller-public.json`, сравните fingerprint при передаче.
   Приватный Controller identity шифруется Windows queue key и не экспортируется.

4. Безопасным файловым каналом передайте на Linux публичный Controller JSON и
   **agent-relay.json**. На Linux создайте новую идентичность Agent:

   ```bash
   python -m poster.remote agent-init --root /home/poster/agent --name linux-worker-1 --controller /home/poster/controller-public.json --relay-profile /home/poster/agent-relay.json
   ```

   Возникают приватный `agent.json`, локальный journal и публичный `registration.json`.
   Передайте в Windows только `registration.json` и свой **controller-relay.json**.
   В GUI загрузите заявку и controller-профиль. Заявка появляется как **PENDING**.
   Сверьте полный ID с Linux-файлом и нажмите **Подтвердить агента…**. Это явное
   доверие машине и разрешение запуска обслуживания общей очереди.

5. Запустите Agent. Для постоянной работы можно использовать обычный systemd unit
   текущего пользователя с абсолютным путём к venv Python и приватному root:

   ```bash
   python -m poster.remote agent --root /home/poster/agent
   ```

   Не запускайте копии одной идентичности на разных машинах. PID, OS/version,
   активные run ID и heartbeat идут от реального процесса и подписываются его ключом.
   Online означает свежий проверенный heartbeat за последние 30 секунд при работающем
   обслуживании. Остановка Controller/Agent или TLS/auth failure приводит к offline;
   последнее подтверждённое время и безопасный код остаются в таблице.

6. В форме **В очередь** явно выберите Local Worker либо конкретный доверенный Agent.
   Укажите только те относительные output-пути, которые нужно вернуть, через запятую.
   По умолчанию список пуст; произвольное содержимое машины не выгружается.
   Для расписания выбор находится в его форме и закрепляется вместе с версией и
   входами. Второе окно входов показывает тот же закреплённый target.

7. **Отозвать доступ…** запрещает новые задания и атомарно переводит неопределённые
   активные запуски в UNKNOWN. Agent получает подписанное отключение либо теряет lease.
   Возврат требует новой идентичности/нового подтверждения. Задания не переходят
   автоматически на локальную машину или другой Agent. Relay credentials при
   компрометации отдельно заменяет оператор relay; отзыв Controller уже запрещает
   выдачу execution grants старой идентичности.

## Секреты, snapshots и доверительная граница

TLS аутентифицирует relay, scoped bearer credentials — клиентов relay. Поверх TLS
каждый envelope имеет закреплённые Ed25519 src/dst identities, случайный message ID,
issued/expires и подпись всех полей. Тело зашифровано AES-GCM с новым ephemeral
X25519/HKDF ключом. Неверная подпись, версия, получатель, срок или размер отклоняются.
Нет pickle, сетевого shell, командной строки из сети или Agent-доступа к SQLite Controller.

Controller расшифровывает свой snapshot локально и шифрует **точные прежние bytes**
графов/sidecars новым ключом конкретного run. Этот ключ выдаётся только с grant,
защищённым отдельным envelope. `queue.key` Windows никогда не передаётся. Даже
совпадение Maker project key с queue key запрещает экспорт (REMOTE_MASTER_KEY).
Maker SecretRef использует свой независимый project key; защищённый JSON сохраняет
числа, boolean, null, списки и объекты. Agent получает необходимые project secrets
в памяти своего отдельного worker. Это осознанное доверие Linux-машине и её владельцу.

На Agent нет сохранённого run key: он поступает child через анонимный stdin,
остаётся в памяти, после аварии запуск не возобновляется с потерянными ключами.
Приватный identity/storage key самого Agent находится в owner-only `agent.json`;
journal/outbox зашифрованы этим независимым локальным ключом. Декодированные project
files удаляет прежний worker. После receipt удаляются временный run-каталог и
outbox, остаётся tombstone ID. Утрата/повреждение identity или потеря journal не
создаёт новую идентичность/пустой журнал автоматически.

Не копируйте `agent.json`/journal в другую работающую VM и не откатывайте их на старый
snapshot при живом Controller. Защита предполагает надёжный локальный диск и один
экземпляр Agent, а не противодействие rollback или root-компрометации доверенной машины.

## Admission, риск и аварии

1. Существующий `Store.claim` учитывает **общие** max_workers/max_per_project и
   создаёт один run ID. Local Worker сохраняет прежний путь, Remote — OFFERED lease.
2. Controller посылает offer с ciphertext. Agent транзакционно сохраняет ACCEPTED,
   но без run key исполнить граф не может.
3. Получив accepted, Controller в одной транзакции фиксирует RUNNING, риск внешнего
   эффекта и durable grant outbox. Только после COMMIT разрешение/ключ выходят в сеть.
4. Agent фиксирует STARTED **до** создания worker и открытия permit pipe. Повторный
   offer/grant с тем же run ID не создаёт второй процесс, даже после рестарта.
5. Периодические подписанные pulse содержат действующие run ID. Сам child проверяет
   lease независимо от сетевого цикла Agent; при утрате разрешения срабатывают stop,
   прежний watchdog, Linux process groups и cleanup Chromium descendants.

Срок lease/envelope — 30 секунд, polling около 0.25 секунды, нужен NTP с расхождением
не более 3 секунд. Lease не продлевает индивидуальный timeout задания. Отмена/timeout
используют прежний controls/GraphRunner.stop; браузеры и дочерние процессы освобождаются.
Уже принятые внешним сайтом действия этим не отменяются.

До выдачи ключа Controller может доказать NOT_STARTED и безопасно вернуть lease в
PENDING. После grant потеря Controller/Agent/worker/relay означает UNKNOWN, даже если
worker фактически успел успешно завершиться. Автоматического повторного POST нет.
На рестарте Controller удаляются старые grants; remote PID никогда не используется
для локального terminate_tree. На рестарте Agent STARTED становится LOST/UNKNOWN и
не запускается заново. Подписанный report проверяется по run/job/version/attempt,
SHA256 и разрешённым файлам; повреждение даёт REPORT_INVALID/UNKNOWN.

Если Controller успел сохранить SUCCEEDED, но receipt потерялся, повтор отчёта только
восстанавливает receipt. Второй lease/worker не создаётся. Поздние результаты после
UNKNOWN остаются приватным `late-result.enc` для проверки и не меняют статус молча.
Для UNKNOWN/REVIEW_REQUIRED действует прежняя пауза автоматических запусков проекта;
проверяйте сайт и завершение старого worker перед явным возобновлением. Exactly-once
для стороннего сайта не обещается.

## Возвращение результатов и лимиты

После валидного report существующая история Dispatcher содержит исходный job/run ID,
закреплённую версию и remote Agent ID. Разрешённые output-файлы появляются в обычной
приватной рабочей папке Controller; действует кнопка открытия workdir. `results.enc`
перешифровывается ключом Controller и не расшифровывается GUI для показа секретов.
Output может содержать данные, которые сам граф намеренно записал; не публикуйте
workdir целиком. Symlink, абсолютные пути, `..`, Windows drive paths и неразрешённые
имена отклоняются.

Лимиты 5A: до 32 доверенных/ожидающих идентичностей одновременно, прежние 32 глобальных
workers; snapshot до 10 MB, до 32 output-путей и 4 MB возвращаемых файлов/results.
Envelope до 40 MB; mailbox до 128 MB/128 сообщений на пару; expired messages удаляются.
Relay имеет не более 64 HTTP threads и общий request deadline 30 секунд. Медленная
связь приводит к безопасной недоступности/UNKNOWN, а не отключению проверки TLS.

Windows browser profile/cookies из 4B.1 не переносится: remote BrowserProvider
работает с новой изолированной сессией. Для существующего проекта с Windows profile
создайте отдельное расписание/постановку без этого профиля. Сам проект, SecretRef,
inputs и timeout остаются прежними. Resource bundles/входные локальные CSV-файлы
автоматически не переносятся — прежний FileSandbox/workdir contract сохраняется.
Agent identity rotation, distributed execution одного графа, quotas, provisioning
Colab/Molab, автоматическое создание аккаунтов и лицензирование не реализованы.

## Проверки и точная граница доказательств

- `pytest tests/test_remote_protocol.py tests/test_remote_registry.py`: криптография,
  сроки, повреждённые ключи/отчёты, trust/revoke, master-key guard, migrations v2→v4,
  отсутствие маршрутизации offline-заданий в Local Worker, recovery без kill чужого PID.
- `pytest tests/remote_linux_e2e.py`: отдельные **настоящие** Controller, HTTPS relay,
  Linux Agent и child GraphRunner, HTTP fixture, параллельность/лимиты, SecretRef,
  типизированные секреты, точные snapshot bytes, output-файлы, циклы/CSV/Excel/подпроекты,
  повтор grant, cancel, browser timeout/cleanup и аварии всех четырёх ролей.
  Это отдельный обязательный Linux CI target, а не platform skip в Windows pytest.
- Существующий Qt-тест расширен реальной регистрацией/подтверждением/отзывом через
  UI/IPC и постановкой на offline Agent без fallback. В этом GUI-сценарии Agent не
  имитируется online и Linux-выполнение не заявляется.
- `pytest tests/remote_windows_graph.py`: загружает artifacts, действительно сохранённые
  Windows Maker в том же CI, и выполняет точные графы через Linux Controller + TLS
  relay + Linux Agent. Это доказательство переносимости Windows-графов, **не** живой
  межмашинной связи Windows Controller↔Linux Agent.

Прежние 29 jobs сохранены. Добавлены 2 protocol jobs Windows/Linux, Linux E2E и Windows
artifact→remote Linux проверка: всего 33 jobs. Windows Controller/Full ZIP проверяются
прежней полной матрицей, обе стороны remote transport E2E запускаются на Linux.
Одновременный Windows↔Linux сетевой E2E на разных hosts требует общей доступной TLS
relay-инфраструктуры; этот CI его не выполняет и не выдаёт раздельные OS-тесты за такое
доказательство. Зелёный итог, числа tests и ZIP относятся к точному SHA конкретного run.

## Threat model

Недоверенный relay/сеть могут задержать, потерять, повторить, переставить или повредить
envelopes. Подписи, receiver pin, expiry, AEAD и durable run tombstones защищают от
подмены/повторного исполнения; они не гарантируют доступность. Relay видит метаданные
и может устроить DoS. Кража relay token позволяет атаковать mailbox, но не подписывать
Controller grants и не расшифровывать проекты. Кража Agent private keys/доверенный root
на Agent нарушает эту границу и требует отзыва. Долгосрочный box key способен раскрыть
записанные прежде app-envelopes при последующей компрометации: application-layer
forward secrecy здесь не обещается. Не храните сетевые captures с секретными пакетами.

Компрометация Controller/Windows user account или queue key за пределами защиты этого
протокола. Agent journal нельзя клонировать/откатывать. ОС и внешние сайты могут
оставить уже принятый эффект после cancel/lease expiry — поэтому UNKNOWN сохраняется
до проверки, безопасный результат не фабрикуется, внешние публикации не повторяются.
