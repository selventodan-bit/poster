# Stage 5B — финальная межмашинная приёмка

Проверяемый production commit: `f411f806d5d0fa498b556bd4e4d2c6a955bcd992`.
Исправления инфраструктуры/harness находятся только в отдельной ветке
`codex/poster-5b-crosshost-acceptance`. Исходные frozen Controller/Service и
Linux Agent/Relay/GraphRunner должны остаться из принятой сборки 5B.

Статус: **18/18 PASSED**, `NOT_EXECUTED=0`, `full_cross_host_acceptance=true`.
Первый полный прогон исходных f411 пакетов:
https://github.com/selventodan-bit/poster/actions/runs/40374290381
([safe artifact](https://github.com/selventodan-bit/poster/actions/runs/40374290381/artifacts/12674131949)).
Закреплённая запись результата:
[`poster-5b-crosshost-2026-10-26.json`](benchmarks/poster-5b-crosshost-2026-10-26.json).

Фактические ОС: Windows Server 2022 (10.0.20348) и Ubuntu 24.04.5 LTS x86_64,
отдельное настоящее ядро `6.18.54.1-microsoft-standard-WSL2`, Python 3.12.3,
Agent UID 1000. Native Service/GUI PID 980/1432; Linux Agent/Relay PID 7807/7727;
одновременно наблюдались Worker PID 7949/8166. Это две одновременно работающие
ОС на одном стандартном hosted runner с отдельной Linux VM и приватной TCP-сетью.
Испытание двух физических серверов, публичного WAN и личного Windows ПК не проводилось.

Исправлен только тест revoke: он теперь подаёт обязательные typed/protected
входы, чтобы корректное задание доходило до проверки отозванного Agent;
требование ошибки `REMOTE_REVOKED` сохранено, число jobs не увеличивается.
Production код и исходные пакеты f411 не изменялись.

## Проверка бесплатной инфраструктуры

Репозиторий публичный. Используется стандартный `windows-2022` hosted runner,
без larger runners, аренды VPS и стороннего платного туннеля. Ubuntu WSL2 —
отдельная VM с настоящим Linux-ядром. Это один hosted runner с двумя ОС;
он не изображает два физических компьютера или испытание публичного WAN.

Workflow `crosshost-acceptance` скачивает исходные пакеты из принятого run,
проверяет SHA256 архивов, manifest и frozen launchers и запускает существующий
18-сценарный harness. Перед этим он требует настоящее ядро WSL2 и отключает
automount Windows-дисков и interop. Отдельная capability-проба всегда сохраняет
`full_cross_host_acceptance=false`, даже при статусе `WSL2_READY`: доступность VM
ещё не означает прохождения E2E. Kernel-wide WSLInterop также явно выключается
bootstrap root до создания обычного пользователя; он не может включить его обратно.

Для повторения в Actions выберите workflow **crosshost-acceptance → Run workflow →
codex/poster-5b-crosshost-acceptance**. Все входы закреплены в коде; вводить
пользовательские secrets, настраивать личный ПК или покупать VM не требуется.
Job отказывается запускаться для private repository. Он устанавливает Ubuntu,
OpenSSH и browser OS libraries администратором гостя, после чего runtime выполняет
обычный `postercheck`. Windows передаёт через SCP только hash-verified Linux
archive и проверяемый helper. SSH host key получают публичной частью через
доверенный VM control plane и закрепляют до первого SSH подключения.
Private IP гостя имеет DNS alias в hosts обеих ОС; Relay действительно принимает
TCP/TLS с Windows на этом IP, без HTTP-туннелей и переноса заданий через artifacts.

Артефакт **crosshost-safe-evidence** (7 дней) содержит только capability-пробу,
provenance, безопасный harness report, screenshot, проверенные returned files и
`windows-history-safe.sqlite`. Последний — отдельный SQLite export выбранных
столбцов исходной Windows истории. Полная DB с зашифрованными приватными полями,
ключи, профили и raw child logs в artifact не включаются. По завершении guest VM
останавливается и удаляется вместе с ephemeral runner.

setup-wsl закреплён на upstream commit
`4c48163d9f981914dd8caa0fc335fe19cca5bff4` (v7.2.0), cache отключён.
Ни ключи, ни пользовательские данные личного Windows компьютера не используются.

## Закреплённые исходные сборки

Run: https://github.com/selventodan-bit/poster/actions/runs/40355084531

Проверяются внутренние файлы, извлечённые из оболочки GitHub artifact:

| Файл | SHA256 |
| --- | --- |
| poster-maker-windows.zip | `4fabdfd5581133624cdb26e70ae6aeaa9563311bc1754dc825a7736b1af02e54` |
| poster-linux.tar.gz | `bcafbd27f20d8f7f8ddb32222b68efada9870d9c2a67e92c4fe473055859e731` |

Linux wheelhouse предназначен для CPython 3.12 Linux x86_64. Версия пакета
poster-http — 0.2.0, remote — 5B.1, протокол — 1. Основные закреплённые зависимости:
httpx 0.28.1, cryptography 50.0.2, psutil 7.2.2, Playwright 1.64.0,
pydantic 2.14.1. Frozen Full: Qt 6.12.0; для внешнего Windows harness используются
pywinauto 0.6.9 и Pillow 11.3.0. Полный список — requirements-recorder.txt и
requirements-remote.txt внутри hash-verified Linux release.

Локальный архив с manifest другого SHA не допускается независимо от имени папки.
SHA256 архивов выше сверены с аннотациями принятого CI. На фактических машинах
скачанные bytes и BUILD_INFO/release.json проверяются повторно перед запуском.

## Что подтверждают 18 сценариев

| Сценарий | Проверяемое доказательство |
| --- | --- |
| frozen_windows_processes | SHA исходных launchers, native exe path, PID/birth, окно GUI |
| linux_processes | Linux OS/kernel, UID≠0, release commit, PID/birth Agent и Relay |
| tls_hostname_certificate_auth | Настоящий TLS probe; отказ bad token, unknown CA и IP вместо DNS SAN |
| pairing_fingerprints_approval | Отказ неправильного pin, PENDING до явного approval |
| signed_heartbeat | Relay доступен при OFFLINE, ONLINE только после Agent heartbeat |
| exact_maker_snapshot | Расшифрованный per-run snapshot побайтно равен исходному graph |
| typed_and_protected_inputs | HTTP fixture получил ожидаемый hash и int/bool типы protected JSON |
| linux_graphrunner_output | Исходный GraphRunner вернул ожидаемые `out.txt`/`third.txt` на Windows |
| windows_history | Одна execution/history запись на job в исходной Windows SQLite |
| two_parallel_executions | Два одновременно RUNNING job с разными настоящими Linux Worker PID |
| global_and_project_limits | Повтор проекта и третий job остаются PENDING при занятых лимитах |
| cancellation | Отмена работающего Linux графа через Windows queue |
| browser_timeout_cleanup | Реальный Chromium наблюдался, после timeout UNKNOWN и нет живых descendants |
| agent_disconnect_reconnect | STOPPED/OFFLINE, повторный ONLINE, прежняя identity |
| post_partition_unknown_no_resend | Relay аварийно убит после POST; UNKNOWN, одна history и ровно один POST после восстановления |
| secret_and_queue_key_boundary | Canary не попал в публичные данные, Windows master key отсутствует на Linux |
| no_orphan_browsers | Наблюдавшиеся owned Worker/Chromium процессы завершены |
| revoke_without_fallback | После revoke новые выдачи отказаны; локальный fallback не запускается |

## Если hosted VM недоступна: минимальные внешние машины

1. Windows 10/11 x64 с открытой, разблокированной интерактивной сессией,
   Python 3.12 x64, OpenSSH Client и исходящими TCP 22/18443 к Linux. Закладывайте
   4 GiB RAM и 5 GiB свободного диска. Windows не принимает входящие соединения
   от Poster. Сам harness оператор запускает на Windows; удалённый доступ Codex
   не обязателен. При предоставлении доступа нужен рабочий стол, а не только
   SSH/WinRM в Session 0: harness проверяет настоящий GUI и снимает окно.
2. Отдельная доверенная Ubuntu 24.04 x86_64, CPython 3.12, минимум 2 vCPU,
   4 GiB RAM, 10 GiB свободного диска, NTP. Требуются административные права
   только для OS-пакетов/firewall. Все процессы теста запускает обычный
   пользователь `postercheck`, без sudo. Это выделенная тестовая учётная запись;
   production identity/state не используются.
3. DNS A-имя, например `poster-linux.example.org`, разрешается на Linux и Windows
   в доступный IPv4 Linux. Windows должна достигать SSH **22** и TLS Relay
   **18443** на этом адресе; текущий SSH harness использует порт 22. Ограничьте
   входящие соединения адресом Windows/VPN. HTTP fixture слушает только Linux
   loopback, внешний порт ему не нужен. Ни NAT port forwarding, ни firewall
   автоматически не настраиваются. Не направляйте имя на HTTP reverse proxy:
   тест поднимает собственный TLS Relay с DNS SAN.
4. Для установки нужны Ubuntu mirrors, PyPI для Windows harness и официальный
   Playwright browser CDN. Runtime Linux устанавливается из offline wheelhouse.
   Proxy должен пропускать test hostname напрямую (`NO_PROXY`); не отключайте
   проверку TLS. Часы обеих ОС должны быть синхронизированы.

Оператор сообщает только готовность машин, ОС, публичные адреса/DNS, порты и
способ доступа. Пароли, bearer tokens, `queue.key`, приватные SSH/TLS/identity
ключи в чат не передаются. Покупка VPS не требуется этим документом и без
отдельного согласия не выполняется.

## Пошаговый запуск на внешних Windows и Linux

1. На Windows скачайте **poster-maker-windows** и **poster-linux** из закреплённого
   run выше. Сначала извлеките оболочки GitHub artifact. В PowerShell проверьте
   внутренние архивы, не только соседний `.sha256`:

   ```powershell
   if ((Get-FileHash .\poster-maker-windows.zip -Algorithm SHA256).Hash.ToLower() -ne '4fabdfd5581133624cdb26e70ae6aeaa9563311bc1754dc825a7736b1af02e54') { throw 'Windows package mismatch' }
   if ((Get-FileHash .\poster-linux.tar.gz -Algorithm SHA256).Hash.ToLower() -ne 'bcafbd27f20d8f7f8ddb32222b68efada9870d9c2a67e92c4fe473055859e731') { throw 'Linux package mismatch' }
   Expand-Archive .\poster-maker-windows.zip C:\Poster5BFull
   $build = Get-Content C:\Poster5BFull\BUILD_INFO.json -Raw | ConvertFrom-Json
   if ($build.commit -ne 'f411f806d5d0fa498b556bd4e4d2c6a955bcd992') { throw 'Wrong frozen commit' }
   foreach ($role in 'service','gui') {
     $item = $build.launchers.$role
     if ((Get-FileHash (Join-Path C:\Poster5BFull $item.file) -Algorithm SHA256).Hash.ToLower() -ne $item.sha256) { throw 'Launcher hash mismatch' }
   }
   ```

2. Получите checkout `codex/poster-5b-crosshost-acceptance`, зафиксируйте его
   `git rev-parse HEAD`. Harness может содержать исправления теста; frozen
   production пакеты остаются f411. Не запускайте графы из checkout вместо
   frozen Service/установленного Linux wheel. В checkout Windows:

   ```powershell
   py -3.12 -m venv .venv-crosshost
   .\.venv-crosshost\Scripts\python.exe -m pip install ".[recorder]" -c requirements-recorder.txt pywinauto==0.6.9 Pillow==11.3.0
   .\.venv-crosshost\Scripts\python.exe -m pip check
   ```

3. Подготовьте приватный локальный каталог Windows, без синхронизации в облако.
   Его ACL должен разрешать доступ только текущему пользователю. Команды ниже
   передают SID, а не содержимое ключей:

   ```powershell
   New-Item -ItemType Directory C:\Poster5BPrivate
   $sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
   icacls C:\Poster5BPrivate /inheritance:r /grant:r "*$($sid):(OI)(CI)F"
   ssh-keygen -t ed25519 -f C:\Poster5BPrivate\postercheck_ed25519
   ```

   Передайте администратору Linux только `.pub`. Для unattended BatchMode ключ
   должен быть доступен SSH без интерактивного запроса: через локальный ssh-agent
   либо специально созданный ephemeral test key в защищённом файле. Секретную
   часть не пересылайте. Администратор добавляет public key в
   `/home/postercheck/.ssh/authorized_keys` (directory 700, file 600), запрещает
   password login для этой учётной записи и оставляет обычный shell для helper.

4. Администратор Linux устанавливает OS-пакеты:

   ```bash
   sudo apt-get update
   sudo apt-get install openssh-server python3.12-venv ca-certificates
   sudo systemctl enable --now ssh
   sudo ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub -E sha256
   ```

   Последняя команда выводит **публичный** host fingerprint. Сверьте его на
   Windows по отдельному доверенному каналу. Запишите проверенную публичную
   строку host key с именем хоста в `C:/Poster5BPrivate/known_hosts`.
   `ssh-keyscan` сам по себе не подтверждает доверие. Проверьте вход:

   ```powershell
   ssh -T -o BatchMode=yes -o StrictHostKeyChecking=yes -o IdentitiesOnly=yes -o UserKnownHostsFile=C:/Poster5BPrivate/known_hosts -i C:/Poster5BPrivate/postercheck_ed25519 postercheck@poster-linux.example.org id -u
   ```

   UID должен отличаться от 0. Те же pinned options используйте для SCP архива
   Linux и `scripts/crosshost/linux_peer.py`. Helper передавайте **с этого Windows
   checkout**, чтобы bytes и SHA256 совпадали, включая окончания строк.

5. Под `postercheck`, без sudo, проверьте и установите архив:

   ```bash
   umask 077
   cd /home/postercheck
   echo 'bcafbd27f20d8f7f8ddb32222b68efada9870d9c2a67e92c4fe473055859e731  poster-linux.tar.gz' | sha256sum --check
   mkdir -m 700 release checks
   python3.12 -m tarfile -e poster-linux.tar.gz release
   cd release/poster-linux
   python3.12 -c 'import json; assert json.load(open("release.json"))["commit"] == "f411f806d5d0fa498b556bd4e4d2c6a955bcd992"'
   python3.12 install_remote.py install --release "$PWD" --role agent --program /home/postercheck/poster-program --state /home/postercheck/install-placeholder --unit-dir /home/postercheck/units-staging --offline
   /home/postercheck/poster-program/current/bin/python -m pip check
   ```

   Installer проверяет SHA256 **всех** файлов manifest и pinned dependencies.
   `units-staging` не активируется: тест сам управляет отдельными процессами
   Agent/Relay, включая аварийное завершение Relay. Автоматический restart службы
   не должен скрывать этот сбой. Положите переданный helper в
   `/home/postercheck/checks/linux_peer.py`; сравните `sha256sum` с
   `Get-FileHash` на Windows.

6. Администратор устанавливает browser OS libraries, затем пользователь скачивает
   браузер. Этот шаг обязателен для настоящего Chromium timeout:

   ```bash
   sudo /home/postercheck/poster-program/current/bin/python -m playwright install-deps chromium
   # Следующая команда — снова под postercheck, без sudo:
   /home/postercheck/poster-program/current/bin/python -m playwright install chromium
   ```

7. Сохраните приватный `C:/Poster5BPrivate/crosshost.json`:

   ```json
   {
     "windows_bundle": "C:/Poster5BFull",
     "expected_commit": "f411f806d5d0fa498b556bd4e4d2c6a955bcd992",
     "ssh_destination": "postercheck@poster-linux.example.org",
     "ssh_key": "C:/Poster5BPrivate/postercheck_ed25519",
     "known_hosts": "C:/Poster5BPrivate/known_hosts",
     "linux_python": "/home/postercheck/poster-program/current/bin/python",
     "linux_helper": "/home/postercheck/checks/linux_peer.py",
     "linux_root": "/home/postercheck/checks/run-001",
     "relay_url": "https://poster-linux.example.org:18443"
   }
   ```

   `linux_root` и Windows output должны **ещё не существовать**. Для следующей
   попытки выберите новые имена. Windows `queue.key` создаётся только на Windows.
   Harness создаёт TLS key только на Linux, передаёт публичную CA через pinned
   SSH, проверяет DNS SAN, неправильные CA/hostname/token, pins и signed heartbeat.
   Не подготавливайте pairing вручную и не копируйте Windows state в Linux.

8. В разблокированной Windows desktop session запустите:

   ```powershell
   .\.venv-crosshost\Scripts\python.exe scripts/check_remote_crosshost.py --config C:/Poster5BPrivate/crosshost.json --output C:/Poster5BPrivate/run-001
   ```

   Дождитесь выхода команды. Успех требует exit 0, `status=PASSED`,
   `full_cross_host_acceptance=true` и **всех 18** `scenarios=PASSED`.
   FAILED/NOT_EXECUTED/частичный список не принимаются. По умолчанию публичны
   только безопасный `report.json` и проверенный screenshot окна; весь каталог
   результата содержит ключи/профили и публиковаться не должен.

9. До удаления приватных state сохраните локально ОС/версию ядра, SHA пакетов и
   checkout, реальные PID с birth time, HTTP fixture hashes/types/counts,
   returned files и readonly копию Windows SQLite (вместе с WAL/SHM либо через
   SQLite backup). SQLite и Linux journal остаются в приватном хранилище.
   Для публикации используйте только выбранные столбцы jobs/executions:
   ID, status, run_id, pid, birth, timestamps, code, effects_possible; никакие
   inputs_cipher/key_cipher/profile_cipher/credential bytes не включаются.
   После анализа остановите принадлежащие тесту процессы и удалите ephemeral
   SSH key/authorized_keys entry и только тестовые state. Не удаляйте чужие
   процессы по имени. Если cleanup не удался, оператор останавливает свои
   процессы по сохранённым PID **и времени рождения**.
