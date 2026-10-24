# Компоненты редактора и прототип встроенного браузера

Состояние исследования: 24 октября 2026 года. Python Engine, GraphRunner и собственный формат графа сохраняются при любом будущем выборе оболочки.

| Компонент | Проверенные возможности и лицензия | Размер / интеграция | Решение этапа 2 |
|---|---|---|---|
| [React Flow / xyflow](https://github.com/xyflow/xyflow) | [MIT](https://github.com/xyflow/xyflow/blob/main/LICENSE); визуальные nodes/edges, порты, выбор, перемещение, viewport, пользовательские компоненты. Сам не исполняет наши Python-действия | npm `@xyflow/react` 12.12.0: 1 216 196 байт unpacked **без зависимостей и web-host** | Хороший кандидат на большой редактор. Сейчас перенос потребовал бы web-host и адаптера данных v2; Tk не заменяется только ради оформления |
| [FlowGram](https://github.com/bytedance/flowgram.ai) | [MIT](https://github.com/bytedance/flowgram.ai/blob/main/LICENSE); free/fixed layout, формы, variable/type plugins, TS runtime | npm `@flowgram.ai/free-layout-editor` 1.0.16: 188 996 байт **только этого пакета**; множество отдельных пакетов | Оценены README/API/метаданные; в Poster не интегрировался и не считается проверенным исполнителем наших графов. Для UI возможен адаптер, TS runtime не заменит Python |
| [RPAForge](https://github.com/chelslava/rpaforge) | [Apache-2.0](https://github.com/chelslava/rpaforge/blob/main/LICENSE); публично описаны React Flow/Electron studio, Python engine и JSON-RPC bridge | [Release 0.5.0](https://github.com/chelslava/rpaforge/releases): Windows installer 621 802 017 байт, core wheel 134 052 байта. Размер чужого installer не является оценкой нашего ZIP | Целиком не переносится: иные engine/формат/жизненный цикл, Chromium/OCR и дополнительные компоненты. Изучение архитектуры не означает заимствование кода или запуск его тестов |
| Playwright Codegen | [Apache-2.0](https://github.com/microsoft/playwright/blob/main/LICENSE); проверен реальный `codegen --help`, [официальный исходник документации](https://github.com/microsoft/playwright/blob/main/docs/src/codegen-intro.md) | Уже есть в зависимости Playwright; выдаёт Python/JS и другие программы | Не парсим исходник в граф и не используем private Recorder API. Сохраняем DOM binding прежнего Recorder, поскольку он даёт явный контракт секретов/ID/вставки |
| Playwright Trace Viewer | Та же лицензия; проверен `show-trace --help`; API tracing находится в установленном Playwright | Viewer уже в поставке Playwright. Trace может включать DOM, сеть и вводимые данные | Не включается автоматически для реальных сайтов: нужна отдельная явная запись, политика секретов и контролируемая очистка. Пока используется наш безопасный журнал |
| PySide6 / Qt WebEngine | Прототип 6.8.3; [LGPLv3](https://github.com/pyside/pyside-setup/blob/6.8.3/LICENSES/LGPL-3.0-only.txt) / GPLv3 / коммерческая лицензия по используемым модулям; Chromium имеет собственные notices | Полный установленный комплект: Linux около 675 МБ, Windows около 512 МБ. Это размер dev-install, **не измеренный размер минимальной frozen-сборки** | Реальный QWebEngineView/CDP-прототип есть; в portable Poster не включён |

Для MIT при будущей поставке сохраняются copyright/license notices; Apache-2.0 требует license/notice и обозначения изменений применимых исходников. Для Qt до распространения нужны проверка набора реально поставляемых модулей, LGPL-условий динамических библиотек и Chromium third-party notices. В текущий продукт не скопированы React Flow, FlowGram, RPAForge, код или графика ZennoLab. Новые рабочие компоненты используют существующий Playwright и стандартные Python/Tk/Pydantic/Fernet.

## Что показал реальный прототип

`scripts/prototype_embedded_browser.py` создаёт **QMainWindow с QWebEngineView**, временный private-профиль и loopback CDP. Внешнее окно Chrome через SetParent не используется. Тест реально открывает контролируемый сайт, устанавливает DOM binding, нажимает кнопку, вводит Unicode, делает POST, проверяет результат и сохраняет снимок окна. Прототип запускается отдельно в CI на Windows и Ubuntu; его зависимости изолированы от job сборки portable EXE.

Практические ограничения проверены, а не предположены:

1. Обычный `connect_over_cdp` Playwright 1.64.0 на Qt 6.8.3 завершается ошибкой `Browser.setDownloadBehavior: Browser context management is not supported`.
2. Публичный параметр `no_defaults=True` позволяет соединиться, выполнять DOM-действия и получать binding-события.
3. `browser.new_context()` на этом endpoint не поддерживается. Нынешний BrowserSession создаёт отдельный контекст для выполнения, поэтому Qt нельзя подключить простой заменой executable path.
4. Требуются отдельный provider с private-профилем/процессом на выполнение, владение жизненным циклом Qt, сохранение поддержки iframe/записи/вкладок и контракт остановки. Перенос всего рабочего места на Qt также затрагивает существующие вкладки Poster 2.0.

Прототип не объявляется готовым embedded browser продукта. В основной сборке браузер остаётся отдельным управляемым окном. На этапе 3 нужен проверяемый provider-интерфейс `start/page/new_tab/close/stop`, тесты того же графа на обоих провайдерах, управление focus/DPI/popup/download и оценка **реального** ZIP. Только после этого выбирается производственный host и согласованный размер установки. React Flow может подключаться как UI над тем же форматом, без замены Python runtime.

Разработчик может воспроизвести эксперимент отдельно:

```bash
python -m pip install PySide6==6.8.3
python scripts/prototype_embedded_browser.py --output .poster/embedded-check
```

На Linux нужен графический display/Xvfb и системные библиотеки Qt; точный рецепт сохранён в `embedded-prototype` job. Это исследовательская установка разработчика, не автоматическая загрузка при запуске Poster.

## Причина сбоя Ubuntu/xcb и воспроизведение

Сбой коммита `677f9f6` воспроизведён в отдельном чистом Ubuntu 24.04 с теми же `playwright install-deps chromium` и дополнительными пакетами из workflow. Присутствие `libxcb-cursor0` не устраняло ошибку: `ldd` для фактического `PySide6/Qt/plugins/platforms/libqxcb.so` показал **`libxcb-shape.so.0 => not found`**. Общая подсказка Qt про cursor не называла настоящую отсутствующую зависимость. Ранее исправленная `libEGL.so.1` к этому моменту уже разрешалась.

| Проверенный ELF-объект | Не разрешалось в чистом окружении | Проверенный пакет Ubuntu |
|---|---|---|
| `libqxcb.so` и его Qt-зависимости | `libxcb-shape.so.0` | `libxcb-shape0` |
| `libQt6WebEngineCore.so.6` | `libatomic.so.1`, `libXtst.so.6` | `libatomic1`, `libxtst6` |

Библиотеки WebEngine могут уже присутствовать в полном GitHub runner image; рецепт теперь объявляет их явно и работает также в чистом Ubuntu. Имена пакетов сверены с содержимым `.deb`, а не выбраны по общей подсказке Qt. `xauth` также объявлен явно для `xvfb-run`.

Перед запуском Qt прототип сохраняет `dependencies.json` с результатом `ldd` обоих объектов. Неразрешённая библиотека даёт именованную CI-ошибку и останавливает проверку. Linux запускается с `QT_QPA_PLATFORM=xcb` в настоящем Xvfb 1280×1024×24; отчёт проверяет `QApplication.platformName() == "xcb"` и private-профиль. Перехода на offscreen, `continue-on-error` или замены CDP-сценария нет. Артефакт сохраняет ELF-аудит и при неудаче; успешность по-прежнему требует реальных DOM binding, ввода, POST и снимка встроенного окна.
