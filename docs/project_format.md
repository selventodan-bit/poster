# Проект schema_version: 1

Папка с именем ID содержит `site.yaml`, `workflow.yaml`, необязательные `targets.csv`, `session.enc`, `analysis.json`; `READY` публикуется последним. Валидатор — Pydantic (`poster/models.py`), машинные JSON Schema — `schemas/`. Неизвестные поля/операции отклоняются, YAML загружается safe_load.

```yaml
# site.yaml
schema_version: 1
id: classic
base_url: http://127.0.0.1:8765
allowed_hosts: [127.0.0.1]
mode: http
status: HTTP_READY
enabled: true
workflow: workflow.yaml
timeout: 20
get_retries: 2
retry_delay: 0.25
schedule:
  max_concurrent: 1
  min_interval_seconds: 0
```

`auth_file: session.enc` подключает зашифрованные cookies и ограниченные auth-заголовки. `campaign` подключает генерацию targets.csv. В YAML не размещайте пароли, cookies или Authorization. Пути файлов не могут выходить из папки. `allowed_hosts` — точные hostname без схемы/порта/wildcard. HTTPS-проект не переходит на HTTP.

```yaml
# workflow.yaml
schema_version: 1
steps:
  - {id: open, op: request, url: /csrf/new, save_as: page}
  - id: token
    op: extract
    source: page
    kind: css_attribute
    selector: 'input[name="csrf_token"]'
    attribute: value
    save_as: csrf
  - id: submit
    op: request
    method: POST
    url: /csrf/posts
    form: {subject: '{{title}}', message: '{{body}}', csrf_token: '{{csrf}}'}
    save_as: posted
  - id: check
    op: verify
    source: posted
    kind: css_text
    selector: '[data-result-id] h1'
    expected: '{{title}}'
    result_url: true
```

Операции:

| op | Смысл / обязательные поля |
|---|---|
| request | url, save_as; method GET/POST/PUT/PATCH/DELETE/HEAD/OPTIONS; одно из form/json/multipart; headers |
| extract | source, kind, save_as; CSS selector/attribute, json path или явный regex pattern |
| set | values: словарь контекста |
| assert | source/kind/expected, проверка промежуточного результата |
| verify | source/kind/expected, фактическое подтверждение, result_url по желанию |
| wait | seconds, 0–60 |

`kind`: css_text, css_attribute, json_path (например `$.items.0.id`), regex (первая группа, не более 500 символов pattern), contains, status. Status разрешён только assert/extract: один HTTP 200 не подходит для verify. `expected` задаёт точное значение; без него verify требует непустой результат. Для contains expected — подстрока. Multipart в MVP — только текстовые поля, без чтения произвольных локальных файлов. Ответ ограничен 5 MB.

`{{title}}`, `{{body}}`, `{{target_url}}` и извлечённые/заданные скаляры; отсутствующее значение вызывает понятную ошибку. Никаких функций, eval, imports или Jinja expression. `dry-run` проверяет подстановки символически, не выполняет ни GET, ни POST, не утверждает успешность.

Опциональная `recovery:` в workflow — отдельный список read-only шагов с verify. Его можно использовать после неизвестного исхода: GET поиска/объекта, затем сверка уникального идентификатора/контента. Для примера можно вручную задать уникальный title и GET `/api/posts?title=...`; параметры URL должны быть корректно закодированы оператором. При отсутствии однозначной проверки оставляйте UNKNOWN. Встроенный builder не придумывает универсальный recovery для незнакомого сайта.

```bash
python -m poster ready path/to/project
python -m poster validate path/to/project
python -m poster replay path/to/project --title Test --body Text --dry-run
```

HTTP_READY — результат анализа; READY — полнота выгрузки; SUCCEEDED — выполнена семантическая проверка конкретного задания. Это разные состояния.

## Метаданные распознавания 2.0 — совместимое расширение schema v1

```yaml
recognition:
  state: HTTP_VERIFIED
  recognizer: semantic-2.0
  rule_id: form-0123456789abcdef
  browser_verified: true
  http_verified: true
  diagnostics: []
```

Поле optional: существующие проекты без recognition читаются как прежде. Новые HTTP-кандидаты имеют `status: REVIEW`, `recognition.state: HTTP_CANDIDATE` и `http_verified: false`. Registry и Runtime блокируют их даже при ручной смене status на HTTP_READY. После отдельной успешной HTTP-проверки оба evidence-флага true, состояние HTTP_VERIFIED, статус HTTP_READY. Семантика исторического HTTP_READY не меняется задним числом.

Дополнительное поле шага `normalize: whitespace` сравнивает текст без различий в пробелах. Строгие подстановки дополнены ограниченными фильтрами `{{body|html}}` и `{{title|url}}`: только escaping, без функций или произвольного кода. Простой JSON-path по-прежнему поддерживает словари/индексы, не полный JSONPath-язык.

`recognition.json` — диагностические оценки и история состояний без сырых сетевых тел. Индивидуальные cookies остаются в session.enc; глобальные правила knowledge.sqlite в папку проекта не копируются.
