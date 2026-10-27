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
