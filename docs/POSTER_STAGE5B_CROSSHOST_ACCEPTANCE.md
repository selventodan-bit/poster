# Stage 5B — финальная межмашинная приёмка

Проверяемый production commit: `f411f806d5d0fa498b556bd4e4d2c6a955bcd992`.
Исправления инфраструктуры/harness находятся только в отдельной ветке
`codex/poster-5b-crosshost-acceptance`. Исходные frozen Controller/Service и
Linux Agent/Relay/GraphRunner должны остаться из принятой сборки 5B.

Статус 18 сценариев: **NOT_EXECUTED** до фактического Windows/Linux прогона.

## Проверка бесплатной инфраструктуры

Репозиторий публичный. Используется стандартный `windows-2022` hosted runner,
без larger runners, аренды VPS и стороннего платного туннеля. Ubuntu WSL2 —
отдельная VM с настоящим Linux-ядром. Это один hosted runner с двумя ОС;
он не изображает два физических компьютера или испытание публичного WAN.

Начальный workflow `crosshost-acceptance` проверяет только доступность WSL2.
Он требует настоящее ядро WSL2 и отключает automount Windows-дисков и interop.
Проба не запускает 18 сценариев и всегда сохраняет
`full_cross_host_acceptance=false`, даже при статусе `WSL2_READY`.
Доступность WSL2 проверяется фактически, а не выводится из документации action.

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
