# Poster Maker 3B.2 — универсальные действия

База: `7e660e4baef36707a3e16d1515133a05012322bf` (29 зарегистрированных действий).
Ветка: `codex/poster-3-actions-stage3b2`. Формат `poster.maker.graph`, schema v2 сохраняется.

## Инвентаризация перед разработкой

| Область | Уже существовало | Добавляется в 3B.2 |
|---|---|---|
| Переменные | Set/Get/Delete/Increment/Compare, SecretRef | Повторно используются |
| Строки | Strings.Transform | Повторно используется |
| Логика | If, Loop/ForEach, Switch, Delay, Break/Continue, Start/End | Повторно используется |
| Браузер | URL, click/input/select, ожидания, чтение, вкладки/popup | Сохраняется BrowserProvider |
| HTTP | HTTP Engine Workflow v1, httpx, streaming cap, TLS, запрет повторной записи | Семь HTTP-действий Registry, общий bounded_body |
| JSON | JSON-значения переменных | Parse/Stringify/Get/Set/Delete/Merge |
| Списки | JSON-массивы и ForEach | Create/Add/Insert/Get/Set/Remove/Count/Contains/Clear/Unique/Shuffle |
| Файлы | Сохранение графов и очередь | Рабочий каталог и девять файловых операций |
| Таблицы | SQLite очередь (другая задача) | JSON-модель, операции, CSV и XLSX |
| Подпроекты | Отсутствовали | Project.Run через тот же GraphRunner |

Официальные функциональные ориентиры: https://docs.zennolab.com/,
https://help.zennolab.com/en/v7/zennoposter/ и https://zennolab.atlassian.net/wiki/spaces/EN/overview.
При проверке 24.10.2026 среда вернула `Tunnel connection failed: 403 Forbidden` для всех
этих адресов. Содержимое не выдаётся за изученное. Контракт определяется подробным ТЗ
пользователя; закрытый код и оформление ZennoLab не используются.

## HTTP-контракт

HTTP.Request/Get/Post/Download используют одну реализацию httpx; SetHeader/SetCookie/
ClearCookies управляют изолированной сессией запуска. Браузер для HTTP не запускается.
TLS включён, системные proxy/env не наследуются, автоматических повторов нет.
GET/HEAD перенаправляются до 10 раз; write-preserving 307/308 не повторяют отправку;
перенаправление HTTPS → HTTP запрещено. Пользовательские заголовки не переносятся
на другой origin. Cookie jar соблюдает domain/path/secure.

Типы тела: none/json/form/multipart/text. Multipart files — поле → относительный путь.
Результат: status, headers, cookies, bytes, text, json, необязательный path.
`expected_status=[]` принимает любой статус. Таймаут общий для цепочки redirect,
включая чтение ответа. Ограничивается декодированный размер (включая gzip).
По умолчанию тело ответа защищено; для открытых данных явно снимите protect_response.
Cookies и известные секретные поля дополнительно защищаются. В логах нет payload/URL
с токенами, текстов сетевых исключений или секретных заголовков. Ошибки имеют отдельные
коды HTTP_TIMEOUT/CONNECT/NETWORK/STATUS/DECODE/RESPONSE_LIMIT/WRITE_UNKNOWN.

## Общий контракт выполнения

Новые действия регистрируются в существующем Registry, получают параметры Pydantic,
имеют русские подписи/описания и возвращают JSON-результат. SecretRef раскрывается
только для действия; taint защищает его результат. Индексы начинаются с нуля.
Граф никогда не выполняет Python eval/exec. Лимиты host-owned: время 300 с по умолчанию,
шаги ограничены max_steps родителя, включая подпроекты; значение/результат до 1 MB,
совокупные результаты до 16 MB, файл до 10 MB. Ошибки не содержат исходных payload.
