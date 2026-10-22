# Google Drive v3

Адаптер использует официальный API `https://www.googleapis.com/drive/v3/`, google-auth для авторизации и httpx для запросов. Реальный Drive требует вашего аккаунта; автотесты проверяют протокол через MockTransport, без внешнего доступа.

1. В Google Cloud включите Google Drive API.
2. Используйте OAuth desktop flow со scope `https://www.googleapis.com/auth/drive`, сохранив authorized-user JSON **вне репозитория/Drive**, либо service account JSON вне проекта. Для service account предоставьте доступ к нужному каталогу; для backup upload удобнее Shared Drive, поскольку у service account нет обычной пользовательской квоты хранения. CLI интерактивного OAuth login в MVP нет: можно использовать стандартную библиотеку google-auth-oauthlib отдельно.
3. Задайте `POSTER_GOOGLE_CREDENTIALS=/secure/path/credentials.json` или настройте Application Default Credentials. Ключ сессии POSTER_SESSION_KEY тоже задаётся вне общего проекта.
4. Создайте AUTOMATOR с `projects/<id>/`, `content/`, `backups/`. ID AUTOMATOR берётся из URL папки Drive. Загружайте YAML/словари/зашифрованную сессию, затем READY. READY для content охватывает все dictionaries/templates/campaigns; для каждого проекта — его файлы.

```bash
python -m poster ready examples/content
python -m poster ready path/to/my-project
# Загрузите соответствующие папки на Drive, READY последним.
python -m poster engine --drive-root YOUR_AUTOMATOR_FOLDER_ID --projects .poster/drive/projects --db .poster/state.sqlite --poll 60
```

Локальный кеш: `.poster/drive/projects` и `.poster/drive/content`. Синхронизация каждые 60 секунд по умолчанию; библиотека Drive не используется как очередь. Пагинация включена, transient/quota ошибки имеют bounded exponential backoff. При недоступности API остаётся кеш. Токены и ответы авторизации не выводятся. Нужны HTTPS-домены `www.googleapis.com`, `oauth2.googleapis.com` для refresh; в некоторых ADC-конфигурациях `accounts.google.com`, `iamcredentials.googleapis.com`. Разрешите только используемый способ авторизации в настройках сети среды. Onboarding-настройки этот проект автоматически не меняет.

Адаптер скачивает данные в staging, сверяет SHA-256 и валидирует проект, затем меняет каталог. Недогруженная версия не заменяет предыдущую. Удалённый/отключённый проект перестаёт давать новые задачи. Проектный удалённый Python не запускается; допускаются только ограниченные типы data-файлов. Для нескольких рабочих узлов локальной SQLite недостаточно.

## Согласованные backup/restore

```bash
python -m poster backup .poster/state-snapshot.sqlite --db .poster/state.sqlite
python -m poster drive-backup .poster/state-snapshot.sqlite --folder-id YOUR_BACKUPS_FOLDER_ID
# Команда возвращает ID загруженного файла.
python -m poster drive-restore .poster/downloaded.sqlite --file-id BACKUP_FILE_ID
python -m poster restore .poster/downloaded.sqlite --db .poster/restored.sqlite
python -m poster reconcile --db .poster/restored.sqlite
python -m poster engine --drive-root YOUR_AUTOMATOR_FOLDER_ID --projects .poster/drive/projects --db .poster/restored.sqlite
```

Backup — SQLite backup API с integrity_check. Restore только в несуществующую БД. Выбор снимка/момента переноса явный: остановите старый Engine до запуска копии состояния на другой машине. UNKNOWN проверяется read-only и не ставится автоматически обратно в PENDING. Периодические upload и выбор последнего snapshot пока остаются явными командами оператора; никаких обещаний вечного notebook runtime.
