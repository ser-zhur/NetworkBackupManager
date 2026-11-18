# =========================================================
# core/context.py
# =========================================================

from pathlib import Path
import json
import logging
import os
import shutil
import sys
import threading


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
# Путь к бэкапам устройства
# =========================================================

def resolve_backup_path(raw) -> Path:
    """
    Единое правило для backup_path устройства:
    абсолютный — как есть, относительный — от BASE_DIR
    (папка приложения / рядом с EXE).

    Используется везде: загрузка routers.json, формы
    добавления/редактирования, плагины, git, UI. Иначе
    один и тот же относительный путь разрешался бы от
    разных каталогов (папки плагина, текущего каталога
    процесса), и история устройства расползалась.
    """
    path = Path(str(raw).strip())
    if not path.is_absolute():
        path = BASE_DIR / path
    return path.resolve()


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

def _same_file(a: Path, b: Path) -> bool:
    try:
        if a.stat().st_size != b.stat().st_size:
            return False
        return a.read_bytes() == b.read_bytes()
    except OSError:
        return False


def _sync_tree(source: Path, target: Path) -> int:
    """
    Рекурсивно копирует файлы из source в target: недостающие
    и отличающиеся по содержимому. Возвращает число скопированных.

    Отличающиеся перезаписываются: web/ рядом с EXE — копия из
    сборки, и после обновления программы шаблоны должны
    соответствовать новому коду (старый шаблон с новым
    server.py — ошибки на страницах). Лишние файлы в target
    не удаляются.
    """
    if not source.exists():
        return 0

    copied = 0
    for item in source.rglob("*"):
        rel = item.relative_to(source)
        dst = target / rel

        if item.is_dir():
            dst.mkdir(parents=True, exist_ok=True)
            continue

        if dst.is_file() and _same_file(item, dst):
            continue

        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(item, dst)
            copied += 1
        except OSError as exc:
            logger.warning(
                "Не удалось скопировать %s → %s: %s",
                item, dst, exc,
            )
    return copied


def ensure_web_files() -> None:
    """
    Распаковать встроенные web-файлы рядом с EXE и обновить их
    после обновления программы (недостающие и изменённые).
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

    copied = 0
    for sub in ("templates", "static"):
        copied += _sync_tree(source_web / sub, WEB_DIR / sub)
    if copied:
        logger.info("Обновлено web-файлов из сборки: %d", copied)


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
        # Строк тегов в фильтре главной страницы (0 — все)
        "tag_rows": 2,

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
    
    # Чистим .tmp-хвосты от прошлых сбоев.
    # 24 часа — чтобы не задеть файлы, которые
    # может писать другой процесс прямо сейчас.
    cleanup_stale_temp_files(max_age_hours=24)


# =========================================================
# Работа с JSON
# =========================================================

_UNSET = object()

# ---- Per-path lock ----
#
# Параллельные save_json на один и тот же path сериализуются.
# Без этого два потока могут одновременно открыть один tmp
# (теперь он фиксированный) и перетереть друг друга.

_path_locks: dict[str, threading.Lock] = {}
_path_locks_guard = threading.Lock()


def _get_path_lock(key: str) -> threading.Lock:
    with _path_locks_guard:
        lock = _path_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _path_locks[key] = lock
        return lock

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

def save_json(path: Path, data: dict | list) -> None:
    """
    Атомарно записывает JSON (объект или список) в файл.

    Особенности:
      • временный файл имеет фиксированное имя <path>.tmp —
        при прерывании процесса остаётся максимум ОДИН tmp,
        а не 13000 с уникальными суффиксами;
      • per-path lock — параллельные записи на один файл
        сериализуются (без него os.replace на Windows падает
        с PermissionError при конкуренции);
      • except BaseException (не Exception!) — ловит
        KeyboardInterrupt и SystemExit, чтобы удалить tmp;
      • tmp удаляется всегда при любой ошибке.
    """

    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)

    text = json.dumps(data, indent=2, ensure_ascii=False)
    tmp_path = path.parent / (path.name + ".tmp")

    lock = _get_path_lock(str(path))

    with lock:
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())

            os.replace(tmp_path, path)

        except BaseException:
            # BaseException, не Exception — иначе Ctrl+C
            # и SystemExit оставят tmp на диске.
            try:
                if tmp_path.exists():
                    os.unlink(tmp_path)
            except OSError:
                pass
            
            # ДОБАВИТЬ ЭТУ СТРОКУ:
            logger.exception("save_json: не удалось записать %s", path)
            
            raise


def cleanup_stale_temp_files(
    directory: Path | None = None,
    max_age_hours: int = 24,
) -> int:
    """
    Удаляет *.tmp старше max_age_hours в directory.

    Best-effort: ошибки логируются и не пробрасываются.
    Безопасно вызывать при старте — файлы моложе часа
    не трогаются, чтобы не сломать параллельную запись.
    """
    import time

    target = Path(directory) if directory else DATA_DIR
    if not target.exists():
        return 0

    cutoff = time.time() - (max_age_hours * 3600)
    removed = 0

    try:
        for f in target.glob("*.tmp"):
            try:
                if not f.is_file():
                    continue
                if f.stat().st_mtime > cutoff:
                    continue
                f.unlink()
                removed += 1
            except OSError:
                pass
    except Exception as exc:
        logger.warning("cleanup_stale_temp_files: %s", exc)

    if removed:
        logger.info(
            "🧹 Удалено %d устаревших .tmp-файлов в %s",
            removed, target,
        )

    return removed


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