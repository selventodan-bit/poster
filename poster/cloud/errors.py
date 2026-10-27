"""Payload-free errors shared by desktop and notebook frontends."""

MESSAGES = {
    "MEMORY_LIMIT": "Недостаточно доступной RAM для ограниченной передачи. Освободите память.",
    "DISK_LIMIT": "Недостаточно свободного места для приватного staging и результатов.",
    "AUTH_REQUIRED": "Подключите Google Drive.",
    "AUTH_CONFIG_REQUIRED": "Выберите OAuth Desktop application в настройках подключения.",
    "AUTH_CANCELLED": "Авторизация не завершена. Подключение можно повторить.",
    "DRIVE_UNAVAILABLE": "Google Drive временно недоступен. Выполнение проекта не повторялось.",
    "DRIVE_CONFLICT": "Появилась другая облачная версия. Обновите список и сохраните свою версию отдельно.",
    "DRIVE_AMBIGUOUS": "Найдены противоречивые записи проекта. Требуется проверка; данные не перезаписаны.",
    "INTEGRITY": "Файлы или manifest повреждены. Запуск и импорт остановлены.",
    "PATH_FORBIDDEN": "Выберите ресурс внутри папки проекта с безопасным относительным именем.",
    "PACKAGE_LIMIT": "Пакет превышает допустимое число или размер файлов.",
    "SECRET_REQUIRED": "Настройте защищённые значения для этой сессии; значения в notebook не сохраняются.",
    "PRIVATE_RESOURCE": "Приватные ключи, cookies, профили и авторизацию нельзя включать в облачный пакет.",
    "RUN_UNCERTAIN": "Предыдущий запуск мог выполнить внешнее действие. Автоматический повтор запрещён.",
    "RUN_ALREADY_CLAIMED": "Этот запуск уже принадлежит другой или прежней сессии. Повтор не выполнен.",
    "CAPABILITY": "Выбранная среда не поддерживает запрошенный способ выполнения или параллельность.",
    "BROWSER_UNAVAILABLE": "Совместимый Chromium недоступен. Установите браузер средствами выбранной среды.",
    "RUNTIME_UNAVAILABLE": "Среда выполнения недоступна или завершилась.",
    "RESULT_UNTRUSTED": "Нельзя подтвердить целостность автономного результата.",
    "RECIPIENT_REQUIRED": "Для защищённых результатов сначала опубликуйте проект из подключённого Maker.",
    "INPUT_INVALID": "Проверьте входные значения и параметры запуска.",
}


class CloudError(ValueError):
    def __init__(self, code):
        self.code = code if code in MESSAGES else "DRIVE_UNAVAILABLE"
        super().__init__(self.code)

    @property
    def message(self):
        return MESSAGES[self.code]
