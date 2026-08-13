from pathlib import Path
import json

# Корень приложения
BASE_DIR = Path(__file__).resolve().parent.parent

# Каталоги
DATA_DIR = BASE_DIR / "data"
LOGS_DIR = BASE_DIR / "logs"
BACKUPS_DIR = BASE_DIR / "backups"
PLUGINS_DIR = BASE_DIR / "plugins"

# Файлы конфигурации
CONFIG_FILE = DATA_DIR / "config.json"
SCHEDULER_FILE = DATA_DIR / "scheduler.json"
STATE_FILE = DATA_DIR / "state.json"
AUTH_FILE = DATA_DIR / "auth.json"


def ensure_directories():
    """Создать необходимые каталоги."""
    for directory in [DATA_DIR, LOGS_DIR, BACKUPS_DIR, PLUGINS_DIR]:
        directory.mkdir(parents=True, exist_ok=True)


def ensure_default_files():
    """Создать конфиги по умолчанию при первом запуске."""

    if not CONFIG_FILE.exists():
        CONFIG_FILE.write_text(
            json.dumps(
                {
                    "host": "0.0.0.0",
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
                    "time": "02:00",
                    "interval_minutes": 60,
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

    if not AUTH_FILE.exists():
        AUTH_FILE.write_text(
            json.dumps(
                {
                    "username": "admin",
                    "password": "admin",
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )


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