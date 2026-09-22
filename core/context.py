# =========================================================
# core/context.py
# =========================================================

from pathlib import Path
import json
import logging
import os
import shutil
import sys
import tempfile


logger = logging.getLogger(__name__)


# =========================================================
# Определение корня приложения
# =========================================================

if getattr(sys, "frozen", False):
    # Запуск из PyInstaller EXE — рядом с .exe
    BASE_DIR = Path(sys.executable).resolve().parent
else:
    # core/context.py → корень проекта
    BASE_DIR = Path(__file__).resolve().parent.parent


# =========================================================
# Каталоги
# =========================================================

DATA_DIR = BASE_DIR / "data"
LOGS_DIR = BASE_DIR / "logs"
BACKUPS_DIR = BASE_DIR / "backups"
PLUGINS_DIR = BASE_DIR / "plugins"
WEB_DIR = BASE_DIR / "web"


# =========================================================
# Файлы конфигурации
# =========================================================

CONFIG_FILE = DATA_DIR / "config.json"
SCHEDULER_FILE = DATA_DIR / "scheduler.json"
STATE_FILE = DATA_DIR / "state.json"
AUTH_FILE = DATA_DIR / "auth.json"


# =========================================================
# Создание каталогов
# =========================================================

def ensure_directories() -> None:
    """Создать необходимые каталоги при первом запуске."""

    for directory in [
        DATA_DIR,
        LOGS_DIR,
        BACKUPS_DIR,
        PLUGINS_DIR,
        WEB_DIR,
        WEB_DIR / "static",
        WEB_DIR / "templates",
    ]:
        directory.mkdir(parents=True, exist_ok=True)


# =========================================================
# Копирование встроенных web-файлов (только для EXE)
# =========================================================

def _copy_tree_missing(source: Path, target: Path) -> None:
    """
    Рекурсивно копирует файлы из source в target,
    не перезатирая уже существующие.
    """
    if not source.exists():
        return

    for item in source.rglob("*"):
        rel = item.relative_to(source)
        dst = target / rel

        if item.is_dir():
            dst.mkdir(parents=True, exist_ok=True)
            continue

        dst.parent.mkdir(parents=True, exist_ok=True)

        if not dst.exists():
            try:
                shutil.copy2(item, dst)
            except OSError as exc:
                logger.warning(
                    "Не удалось скопировать %s → %s: %s",
                    item, dst, exc,
                )


def ensure_web_files() -> None:
    """
    Распаковать встроенные web-файлы рядом с EXE при первом запуске.
    В режиме исходников ничего не делает.
    """

    if not getattr(sys, "frozen", False):
        return

    meipass = getattr(sys, "_MEIPASS", None)
    if meipass is None:
        logger.warning(
            "sys._MEIPASS недоступен — web-файлы не будут скопированы."
        )
        return

    source_web = Path(meipass) / "web"
    if not source_web.exists():
        logger.warning("В бандле нет каталога web/: %s", source_web)
        return

    _copy_tree_missing(
        source_web / "templates",
        WEB_DIR / "templates",
    )
    _copy_tree_missing(
        source_web / "static",
        WEB_DIR / "static",
    )


# =========================================================
# Создание файлов по умолчанию
# =========================================================

def _write_default_json(path: Path, data: dict) -> None:
    if path.exists():
        return
    try:
        path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError as exc:
        logger.warning(
            "Не удалось создать %s: %s", path, exc
        )


def ensure_default_files() -> None:
    """Создать конфиги по умолчанию, если их ещё нет."""

    _write_default_json(CONFIG_FILE, {
        "host": "127.0.0.1",
        "port": 8080,
        "open_browser": True,
        "max_parallel_backups": 5,
        "backup_timeout_seconds": 300,

        # Правила автоматической очистки истории.
        # Подробности — на странице /retention.
        "retention": {
            "enabled": False,
            "max_age_days": 365,
            "keep_last": 20,
            "keep_with_notes": True,
            "auto_sweep": True,
        },
    })
    _write_default_json(SCHEDULER_FILE, {
        "enabled": False,
        "mode": "daily",
        "daily_time": "03:00",
        "interval_hours": 24,
        "last_run": None,
    })

    _write_default_json(STATE_FILE, {})


# =========================================================
# Работа с JSON
# =========================================================

_UNSET = object()


def load_json(path: Path, default=_UNSET) -> dict:
    """
    Читает JSON-объект из файла.

    При любой ошибке (нет файла, битый JSON, не словарь)
    возвращает default (по умолчанию — пустой dict) и пишет
    предупреждение в лог.
    """

    if default is _UNSET:
        default = {}

    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return default
    except OSError as exc:
        logger.warning("Не удалось прочитать %s: %s", path, exc)
        return default

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        logger.warning("Повреждён JSON в %s: %s", path, exc)
        return default

    if not isinstance(data, dict):
        logger.warning(
            "Ожидался объект в %s, получено %s",
            path, type(data).__name__,
        )
        return default

    return data


def save_json(path: Path, data: dict) -> None:
    """
    Атомарно записывает JSON-объект в файл.
    Сначала пишет во временный файл в том же каталоге,
    затем переименовывает.
    """

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    text = json.dumps(data, indent=2, ensure_ascii=False)

    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=str(path.parent),
    )

    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())

        os.replace(tmp_name, path)

    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


# =========================================================
# Инициализация
# =========================================================
#
# FIX: побочные эффекты (создание каталогов, копирование web-файлов,
# запись дефолтных конфигов) больше НЕ выполняются при импорте.
#
# Вызывайте явно из точки входа:
#
#     from core.context import (
#         ensure_directories,
#         ensure_web_files,
#         ensure_default_files,
#     )
#     ensure_directories()
#     ensure_web_files()
#     ensure_default_files()
#
# =========================================================