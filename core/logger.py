# =========================================================
# core/logger.py
# =========================================================

import logging
import logging.handlers
import threading

from core.context import LOGS_DIR


# Имя основного лог-файла
LOG_FILE = LOGS_DIR / "app.log"

# Значение контекста, когда лог не привязан к устройству
DEFAULT_DEVICE = "-"

# Флаг на корневом логгере — защищает от двойной инициализации
_INIT_FLAG = "_nbm_logging_initialized"


# =========================================================
# THREAD-LOCAL DEVICE CONTEXT
# =========================================================
#
# Контекст устройства хранится в thread-local, чтобы разные
# потоки бэкапа не затирали друг другу маркер.
#
# Использование:
#
#     from core.logger import set_device_context, clear_device_context
#     set_device_context("mikrotik/192.168.88.1")
#     try:
#         ...
#     finally:
#         clear_device_context()
#
# =========================================================

class _DeviceContext(threading.local):

    def __init__(self):
        self.device = DEFAULT_DEVICE
        self.debug_connection = False


_device_context = _DeviceContext()


def set_device_context(value: str) -> None:
    _device_context.device = value or DEFAULT_DEVICE


def clear_device_context() -> None:
    _device_context.device = DEFAULT_DEVICE


def get_device_context() -> str:
    return getattr(_device_context, "device", DEFAULT_DEVICE)


def device_log_tag(device_type: str, router: dict) -> str:
    """
    Тег устройства в app.log: '<device_type>/<host>'.

    ЕДИНСТВЕННОЕ место, где он формируется: и запись логов
    (бэкап, массовый бэкап, синхронизация), и фильтр
    /logs/device/... обязаны использовать эту функцию,
    иначе строки не попадут в лог устройства.
    """
    host = (
        router.get("host")
        or router.get("ip")
        or router.get("address")
        or "?"
    )
    return f"{device_type}/{host}"


# =========================================================
# CONNECTION DEBUG CONTEXT
# =========================================================

_debug_lock = threading.Lock()
_debug_sessions: int = 0
_debug_device_global: str | None = None


def set_connection_debug(device_id: str) -> None:
    global _debug_sessions, _debug_device_global
    _device_context.debug_connection = True
    _device_context.device = device_id or DEFAULT_DEVICE
    with _debug_lock:
        _debug_sessions += 1
        _debug_device_global = device_id or DEFAULT_DEVICE


def clear_connection_debug() -> None:
    global _debug_sessions, _debug_device_global
    _device_context.debug_connection = False
    with _debug_lock:
        _debug_sessions = max(0, _debug_sessions - 1)
        if _debug_sessions == 0:
            _debug_device_global = None


def is_connection_debug() -> bool:
    with _debug_lock:
        return _debug_sessions > 0

# =========================================================
# FILTER
# =========================================================

class DeviceContextFilter(logging.Filter):

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "device"):
            # paramiko пишет из своего потока — там thread-local пуст.
            # Если сейчас идёт отладочная сессия, подставим её device.
            if record.name.startswith("paramiko"):
                with _debug_lock:
                    tag = _debug_device_global
                if tag:
                    record.device = tag
                    return True
            record.device = get_device_context()
        return True

# Логгеры, чьи DEBUG-записи считаются «подробным логом подключения».
_DEBUG_LOGGER_PREFIXES = ("paramiko", "nbm.debug")


class ThreadDebugFilter(logging.Filter):
    """
    Пропускает DEBUG-записи от paramiko / nbm.debug
    только из потоков, где включён debug_connection.
    Всё, что >= INFO, пропускается всегда.

    Благодаря этому paramiko можно держать на уровне DEBUG
    глобально: без активной отладочной сессии его DEBUG
    просто не доходит до хендлеров.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno >= logging.INFO:
            return True
        for prefix in _DEBUG_LOGGER_PREFIXES:
            if record.name == prefix or record.name.startswith(prefix + "."):
                return is_connection_debug()
        return True
    
# =========================================================
# SETUP
# =========================================================

_FORMAT = (
    "%(asctime)s [%(levelname)s] "
    "[%(device)s] %(name)s: %(message)s"
)


def setup_logging(level: int = logging.INFO) -> logging.Logger:
    """
    Настраивает корневой логгер. Идемпотентно.

    - RotatingFileHandler на logs/app.log: 5 МБ × 5 копий.
    - StreamHandler в stderr (для консоли и NSSM service_stderr.log).
    - Формат: время [LEVEL] [device] name: сообщение.
    - uvicorn-логи выводятся только в stderr, минуя app.log.
    """

    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()

    if getattr(root, _INIT_FLAG, False):
        root.setLevel(level)
        return root

    root.setLevel(level)

    formatter = logging.Formatter(_FORMAT)
    context_filter = DeviceContextFilter()
    debug_filter = ThreadDebugFilter()

    file_handler = logging.handlers.RotatingFileHandler(
        LOG_FILE,
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    file_handler.addFilter(context_filter)
    file_handler.addFilter(debug_filter)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    console_handler.addFilter(context_filter)
    console_handler.addFilter(debug_filter)

    root.addHandler(file_handler)
    root.addHandler(console_handler)

    setattr(root, _INIT_FLAG, True)

    # ---- Разрешаем DEBUG от paramiko и наших debug-логов ----
    for name in _DEBUG_LOGGER_PREFIXES:
        logging.getLogger(name).setLevel(logging.DEBUG)

    # ---- uvicorn: не пишем access-логи в app.log ----
    for name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        lg = logging.getLogger(name)
        lg.propagate = False
        if not lg.handlers:
            h = logging.StreamHandler()
            h.setFormatter(logging.Formatter(
                "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
            ))
            lg.addHandler(h)

    return root