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


_device_context = _DeviceContext()


def set_device_context(value: str) -> None:
    _device_context.device = value or DEFAULT_DEVICE


def clear_device_context() -> None:
    _device_context.device = DEFAULT_DEVICE


def get_device_context() -> str:
    return getattr(_device_context, "device", DEFAULT_DEVICE)


# =========================================================
# FILTER
# =========================================================

class DeviceContextFilter(logging.Filter):
    """
    Гарантирует, что у каждой записи есть атрибут 'device',
    чтобы формат "%(device)s" не падал на записях из чужих
    библиотек (uvicorn, paramiko и т.п.).
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "device"):
            record.device = get_device_context()
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

    file_handler = logging.handlers.RotatingFileHandler(
        LOG_FILE,
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    file_handler.addFilter(context_filter)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    console_handler.addFilter(context_filter)

    root.addHandler(file_handler)
    root.addHandler(console_handler)

    setattr(root, _INIT_FLAG, True)

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