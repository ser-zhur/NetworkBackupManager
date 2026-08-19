from pathlib import Path
import json
import sys
import shutil

# =========================================================
# Определение корня приложения
# =========================================================

if getattr(sys, "frozen", False):
    # Запуск из PyInstaller EXE
    BASE_DIR = Path(sys.executable).resolve().parent
else:
    # Запуск из исходников
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

def ensure_directories():
    """Создать необходимые каталоги при первом запуске."""

    for directory in [
        DATA_DIR,
        LOGS_DIR,
        BACKUPS_DIR,
        PLUGINS_DIR,
        WEB_DIR / "static",
        WEB_DIR / "templates",
    ]:
        directory.mkdir(parents=True, exist_ok=True)


def ensure_web_files():
    """Распаковать встроенные web-файлы рядом с EXE при первом запуске."""

    if not getattr(sys, "frozen", False):
        return

    source_web = Path(sys._MEIPASS) / "web"

    if not source_web.exists():
        return

    target_templates = WEB_DIR / "templates"
    target_static = WEB_DIR / "static"

    target_templates.mkdir(parents=True, exist_ok=True)
    target_static.mkdir(parents=True, exist_ok=True)

    # Копируем шаблоны, если файлов нет
    for item in (source_web / "templates").glob("*"):
        target = target_templates / item.name
        if not target.exists():
            shutil.copy2(item, target)

    # Копируем статику
    for item in (source_web / "static").glob("*"):
        target = target_static / item.name
        if not target.exists():
            shutil.copy2(item, target)

# =========================================================
# Создание файлов по умолчанию
# =========================================================

def ensure_default_files():
    """Создать конфиги по умолчанию, если их ещё нет."""

    if not CONFIG_FILE.exists():
        CONFIG_FILE.write_text(
            json.dumps(
                {
                    "host": "127.0.0.1",
                    "port": 8080,
                    "open_browser": True,
                    "max_parallel_backups": 5,
                    "backup_timeout_seconds": 300,
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    if not SCHEDULER_FILE.exists():
        SCHEDULER_FILE.write_text(
            json.dumps(
                {
                    "enabled": False,
                    "mode": "daily",
                    "daily_time": "03:00",
                    "interval_hours": 24,
                    "last_run": None,
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    if not STATE_FILE.exists():
        STATE_FILE.write_text(
            json.dumps({}, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )


# =========================================================
# Работа с JSON
# =========================================================

def load_json(path, default=None):
    if default is None:
        default = {}

    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass

    return default


def save_json(path, data):
    path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


# =========================================================
# Инициализация при импорте
# =========================================================

ensure_directories()
ensure_web_files()
ensure_default_files()