"""Dispatcher state machine and payload-free diagnostic codes."""

from enum import StrEnum


class Status(StrEnum):
    PENDING = "PENDING"
    LEASED = "LEASED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    TIMED_OUT = "TIMED_OUT"
    UNKNOWN = "UNKNOWN"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"


ACTIVE = {Status.LEASED, Status.RUNNING}
TERMINAL = set(Status) - ACTIVE - {Status.PENDING}
TRANSITIONS = {
    Status.PENDING: {Status.LEASED, Status.CANCELLED},
    Status.LEASED: {Status.PENDING, Status.RUNNING, *TERMINAL},
    Status.RUNNING: TERMINAL,
    Status.UNKNOWN: {Status.REVIEW_REQUIRED},
    Status.SUCCEEDED: {Status.UNKNOWN},  # Invalid/missing confirmation is never success.
}

MESSAGES = {
    "SCHEDULE_INVALID": "Проверьте режим, дату, время и часовой пояс расписания.",
    "SCHEDULE_MISSING": "Расписание отсутствует или удалено.",
    "SCHEDULE_REVIEW": "Автозапуски проекта приостановлены: проверьте UNKNOWN/REVIEW_REQUIRED и явно возобновите.",
    "AUTOSTART_FAILED": "Windows отказала в изменении автозапуска. Проверьте права и путь к PosterService.exe.",
    "AUTOSTART_UNSUPPORTED": "Автозапуск доступен только в Windows frozen Full/Lite сборке.",
    "BUILD_INVALID": "Версия программы не совпадает с BUILD_INFO.json. Распакуйте полный исходный ZIP заново.",
    "BROWSER_UNAVAILABLE": "Установленный Chrome/Chromium/Edge не найден или задан неверный путь. Установите браузер самостоятельно либо исправьте путь. Автоматическая загрузка не выполняется.",
    "WORKER_UNAVAILABLE": "Служба или worker отсутствует либо повреждён. Распакуйте полный ZIP; Python устанавливать не нужно.",
    "SERVICE_UNAVAILABLE": "Служба не отвечает. Переподключитесь; уже выполняющиеся задания автоматически не повторяются.",
    "SERVICE_VERSION": "Работает служба другой версии. Остановите обслуживание в прежнем приложении перед обновлением.",
    "SERVICE_FAILED": "Ошибка службы. Проверьте историю и доступ к рабочей папке; автоматического повтора нет.",
    "PROJECT_KEY_REQUIRED": "Ключ Maker не найден или не подходит. Выберите существующий session.key от этого проекта; новый ключ не создаётся.",
    "CATALOG_INVALID": "Каталог проектов повреждён или зашифрован другим ключом. Существующие данные сохранены.",
    "DISPATCHER_BUSY": "Эту очередь уже обслуживает другой Dispatcher.",
    "STORE_INVALID": "Это не поддерживаемая база Dispatcher; прежняя база не изменена.",
    "PARAMETERS_INVALID": "Проверьте параметры Dispatcher.",
    "JOB_MISSING": "Задание отсутствует.",
    "SNAPSHOT_INVALID": "Снимок повреждён или изменён; исполнение запрещено.",
    "SNAPSHOT_LIMIT": "Превышен лимит снимка проектов.",
    "PROJECT_INVALID": "Проект или его зависимости не поддерживаются.",
    "PATH_FORBIDDEN": "Ссылка, специальный файл или выход за разрешённый каталог запрещены.",
    "KEY_INVALID": "Ключ отсутствует, повреждён или не подходит к защищённым данным.",
    "INPUT_INVALID": "Укажите объявленные входы и корректные защищённые значения.",
    "WORKER_CRASH": "Worker завершился без подтверждённого результата; автоматического повтора нет.",
    "WORKER_INTERNAL_ERROR": "Внутренняя ошибка worker. Возможные внешние действия требуют проверки; автоматического повтора нет.",
    "DISPATCHER_LOST": "Связь с Dispatcher потеряна; проверьте внешние действия перед повтором.",
    "CANCEL_REQUESTED": "Исполнение остановлено. Уже выполненные действия не откатываются.",
    "TIMEOUT": "Превышено время задания. Уже выполненные действия не откатываются.",
    "NOT_STARTED": "Канал разрешения запуска не открывался; исполнение не начиналось.",
    "REPORT_INVALID": "Подтверждение результата повреждено; требуется проверка.",
    "PROFILE_BUSY": "Другой запуск использует этот профиль.",
    "EXECUTION_FAILED": "Граф завершился ошибкой; смотрите безопасный код шага.",
    "PROCESS_CONTROL_FAILED": "Не удалось безопасно создать или завершить дерево worker.",
    "REVIEW_REQUIRED": "Требуется ручная проверка; задание не будет повторено автоматически.",
}


class DispatcherError(ValueError):
    def __init__(self, code):
        self.code = code if code in MESSAGES else "PARAMETERS_INVALID"
        super().__init__(MESSAGES[self.code])
