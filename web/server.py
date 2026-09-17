import os
import json
import copy
import logging
import re
import threading
import secrets
import hashlib
import base64
import time

from pathlib import Path
from datetime import datetime, timedelta
from collections import defaultdict
from contextlib import asynccontextmanager

from fastapi import (
    FastAPI,
    HTTPException,
    Request,
    Form,
    BackgroundTasks,
    Depends,
)

from fastapi.responses import (
    HTMLResponse,
    RedirectResponse,
    FileResponse,
    Response,
)

from fastapi.templating import (
    Jinja2Templates,
)

from fastapi.staticfiles import (
    StaticFiles,
)

from fastapi.security import (
    HTTPBasic,
    HTTPBasicCredentials,
)


from core.secure_storage import (
    SecureStorage,
)

from core.scheduler import (
    BackupScheduler,
)

from core.storage import (
    StorageManager,
)

from core.service_installer import (
    get_status,
    schedule_restart,
    schedule_stop,
    install_service,
    uninstall_service,
    schedule_uninstall,
    start_service,
    stop_service,
    is_admin,
    is_service_registered,
    is_service_process,
    relaunch_elevated,
    invalidate_status_cache,
)

from core.context import (
    BASE_DIR,
    LOGS_DIR,
    WEB_DIR,
    AUTH_FILE,
    STATE_FILE,
    load_json,
)

from core.logger import (
    setup_logging,
    set_device_context,
    clear_device_context,
)

from core.git_storage import (
    GitStorage,
    is_available as git_available,
)

# =========================================================
# LOGGING
# =========================================================

setup_logging()

# =========================================================
# BACKUP STATE
# =========================================================

backup_lock = threading.Lock()

active_backups_count = 0


def is_backup_running() -> bool:

    with backup_lock:

        return (
            active_backups_count > 0
        )


def backup_started():

    global active_backups_count

    with backup_lock:

        active_backups_count += 1


def backup_finished():

    global active_backups_count

    with backup_lock:

        if active_backups_count > 0:

            active_backups_count -= 1


# =========================================================
# CORE
# =========================================================

DATE_DIR_REGEX = re.compile(
    r"^\d{4}-\d{2}-\d{2}$"
)

storage = SecureStorage(
    str(BASE_DIR)
)

security = HTTPBasic()


# =========================================================
# STORAGE MANAGER
# =========================================================

try:
    storage_manager = StorageManager()
    storage_manager.discover()

    for err in storage_manager.errors():
        logging.warning("storage: %s", err)

    logging.info(
        "storage backends: %s",
        ", ".join(storage_manager.ids()) or "—",
    )

except Exception as exc:
    logging.error(
        "Ошибка загрузки StorageManager: %s",
        exc,
        exc_info=True,
    )
    storage_manager = None


# =========================================================
# PLUGIN MANAGER
# =========================================================

try:

    from core.plugin_manager import (
        PluginManager,
    )

    plugin_manager = PluginManager(
        storage_manager=storage_manager,
    )

    plugin_manager.discover_and_load_plugins()

except Exception as exc:

    logging.error(
        "Ошибка загрузки PluginManager: %s",
        exc,
        exc_info=True,
    )

    plugin_manager = None

# =========================================================
# SCHEDULER
# =========================================================

scheduler = BackupScheduler(
    plugin_manager
)


@asynccontextmanager
async def lifespan(
    app: FastAPI,
):

    logging.info(
        "🚀 Запуск планировщика..."
    )
    
    invalidate_status_cache()

    scheduler.start()

    try:

        yield

    finally:

        logging.info(
            "🛑 Остановка планировщика..."
        )
        
        invalidate_status_cache()
        scheduler.stop()


# =========================================================
# FASTAPI
# =========================================================

app = FastAPI(
    title="Network Backup Manager",
    lifespan=lifespan,
)


app.mount(
    "/static",
    StaticFiles(
        directory=str(
            WEB_DIR / "static"
        )
    ),
    name="static",
)

templates = None

if (
    BASE_DIR
    / "web"
    / "templates"
).exists():

    templates = Jinja2Templates(
        directory=str(
            WEB_DIR
            / "templates"
        )
    )


# =========================================================
# JINJA2 HELPERS
# =========================================================

def _status_meta(status: str) -> dict:
    """Цвета/подписи статуса устройства."""
    return {
        "ok": {
            "dot": "bg-emerald-500",
            "ring": "ring-emerald-500/15",
            "badge": "bg-emerald-100 dark:bg-emerald-900/60 text-emerald-700 dark:text-emerald-300",
            "label": "Свежий",
        },
        "warn": {
            "dot": "bg-amber-500",
            "ring": "ring-amber-500/15",
            "badge": "bg-amber-100 dark:bg-amber-900/60 text-amber-700 dark:text-amber-300",
            "label": "Просрочен",
        },
        "fail": {
            "dot": "bg-rose-500",
            "ring": "ring-rose-500/15",
            "badge": "bg-rose-100 dark:bg-rose-900/60 text-rose-700 dark:text-rose-300",
            "label": "Ошибка",
        },
        "none": {
            "dot": "bg-gray-300 dark:bg-slate-600",
            "ring": "ring-gray-300/20 dark:ring-slate-600/20",
            "badge": "bg-gray-100 dark:bg-slate-800 text-gray-600 dark:text-slate-400",
            "label": "Нет данных",
        },
    }.get(status, {
        "dot": "bg-gray-300 dark:bg-slate-600",
        "ring": "ring-gray-300/20 dark:ring-slate-600/20",
        "badge": "bg-gray-100 dark:bg-slate-800 text-gray-600 dark:text-slate-400",
        "label": "—",
    })


def _age_label(days) -> str:
    if days is None:
        return "нет данных"
    if days == 0:
        return "сегодня"
    if days == 1:
        return "1 день назад"
    if days < 5:
        return f"{days} дня назад"
    return f"{days} дней назад"


def _age_class(days) -> str:
    if days is None:
        return "text-gray-500 dark:text-slate-400"
    if days <= 2:
        return "text-emerald-600 dark:text-emerald-400"
    if days <= 7:
        return "text-gray-500 dark:text-slate-400"
    if days <= 30:
        return "text-amber-600 dark:text-amber-400"
    return "text-rose-600 dark:text-rose-400"


if templates is not None:
    templates.env.globals["status_meta"] = _status_meta
    templates.env.globals["age_label"] = _age_label
    templates.env.globals["age_class"] = _age_class

# =========================================================
# PASSWORD
# =========================================================

def verify_password(
    password: str,
    stored: str,
) -> bool:

    try:

        algorithm, iterations, salt_b64, key_b64 = (
            stored.split("$")
        )

        if algorithm != "pbkdf2_sha256":
            return False

        salt = base64.b64decode(
            salt_b64
        )

        expected = base64.b64decode(
            key_b64
        )

        test = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt,
            int(iterations),
        )

        return secrets.compare_digest(
            test,
            expected,
        )

    except Exception:

        return False


# =========================================================
# AUTH
# =========================================================

def require_auth(
    credentials:
        HTTPBasicCredentials =
        Depends(security),
):

    if not AUTH_FILE.exists():

        raise HTTPException(
            status_code=401,
            detail=(
                "Файл auth.json не найден. "
                "Создайте пользователя командой "
                "--create-user."
            ),
            headers={
                "WWW-Authenticate":
                "Basic"
            },
        )

    try:

        data = json.loads(
            AUTH_FILE.read_text(
                encoding="utf-8"
            )
        )

        username = data[
            "username"
        ]

        password_hash = data[
            "password_hash"
        ]

    except Exception as exc:

        logging.error(
            "Ошибка чтения auth.json: %s",
            exc,
        )

        raise HTTPException(
            status_code=401,
            detail=(
                "Некорректный файл auth.json."
            ),
            headers={
                "WWW-Authenticate":
                "Basic"
            },
        )

    valid = (
        secrets.compare_digest(
            credentials.username,
            username,
        )
        and
        verify_password(
            credentials.password,
            password_hash,
        )
    )

    if not valid:

        raise HTTPException(
            status_code=401,
            detail="Неверный логин или пароль.",
            headers={
                "WWW-Authenticate":
                "Basic"
            },
        )

    return credentials.username


# =========================================================
# SERVICE ACTION HELPERS
# =========================================================

def _render_service_action(
    request: Request,
    title: str,
    message: str,
    ok: bool,
):
    """
    Рендер страницы результата действия со службой.
    Убирает дублирование TemplateResponse в каждом endpoint’е.
    """
    return templates.TemplateResponse(
        request,
        "service_action.html",
        {
            "title": title,
            "message": message,
            "ok": ok,
        },
    )


def _schedule_self_exit(delay: float = 5.0) -> None:
    """
    Откладывает завершение текущего процесса.

    Нужно после запроса UAC на install: пока новая служба
    стартует, интерактивная копия должна освободить порт
    8080, иначе служба не сможет забиндиться.
    """
    def _exit():
        time.sleep(delay)
        try:
            os._exit(0)
        except Exception:
            pass

    threading.Thread(
        target=_exit,
        daemon=True,
    ).start()

def _device_filter_tag(device_type: str, index: int) -> str | None:
    """
    Возвращает тег вида '<device_type>/<host>' для фильтрации
    строк app.log, или None, если устройство не найдено.
    """
    if not plugin_manager or device_type not in plugin_manager._plugins:
        return None

    routers = plugin_manager._plugins[device_type].get("routers", [])
    if not (0 <= index < len(routers)):
        return None

    router = routers[index]
    host = router.get("host") or router.get("ip") or router.get("address")
    if not host:
        return None

    return f"{device_type}/{host}"

# =========================================================
# LOG READER
# =========================================================

def read_log_tail(
    file_path: Path,
    limit: str = "100",
    filter_tag: str | None = None,
    focus_date: str | None = None,
):
    """
    Читает хвост файла логов.

    Возвращает:
        (content, lines_shown, has_more, focus_line_index)

    filter_tag — оставить строки с '[filter_tag]'.
    focus_date — индекс первой строки, начинающейся с YYYY-MM-DD,
                 или None.
    """

    if not file_path.exists():
        return ("Лог-файл пока отсутствует...", 0, False, None)

    try:
        with open(
            file_path,
            "r",
            encoding="utf-8",
            errors="ignore",
        ) as f:
            lines = f.readlines()
    except Exception as exc:
        return (f"Ошибка чтения файла логов: {exc}", 0, False, None)

    if filter_tag:
        needle = f"[{filter_tag}]"
        lines = [ln for ln in lines if needle in ln]

    total_lines = len(lines)
    has_more = False

    if limit != "all":
        max_l = int(limit) if limit.isdigit() else 100
        if total_lines > max_l:
            lines = lines[-max_l:]
            total_lines = len(lines)
            has_more = True

    focus_line_index = None
    if focus_date:
        for i, ln in enumerate(lines):
            if ln.startswith(focus_date):
                focus_line_index = i
                break

    return (
        "".join(lines),
        total_lines,
        has_more,
        focus_line_index,
    )

# =========================================================
# BACKUP INFO
# =========================================================

def get_device_backup_info(
    backup_path,
):

    path = Path(
        backup_path
    )

    if not path.is_absolute():

        path = (
            BASE_DIR / path
        )

    if (
        not path.exists()
        or not path.is_dir()
    ):

        return (
            0,
            "Нет бэкапов",
            None,
        )

    dated_dirs = []

    for item in path.iterdir():

        if not item.is_dir():
            continue

        try:

            has_files = any(
                f.is_file()
                for f in item.iterdir()
            )

        except (
            PermissionError,
            OSError,
        ):

            has_files = False

        if not has_files:
            continue

        date_part = item.name[:10]

        try:

            folder_date = (
                datetime.strptime(
                    date_part,
                    "%Y-%m-%d",
                )
            )

            dated_dirs.append(
                (
                    folder_date,
                    item,
                )
            )

        except ValueError:

            continue

    if not dated_dirs:

        return (
            0,
            "Нет бэкапов",
            None,
        )

    latest_date, latest_dir = max(
        dated_dirs,
        key=lambda x: x[0],
    )

    age_days = (
        datetime.now()
        - latest_date
    ).days

    return (
        len(dated_dirs),
        latest_date.strftime(
            "%Y-%m-%d"
        ),
        age_days,
    )

# =========================================================
# DEVICE INFO — расширенный сбор данных
# =========================================================

def get_dir_size(path: Path) -> int:
    """Суммарный размер всех файлов в каталоге, рекурсивно."""
    if not path.exists() or not path.is_dir():
        return 0
    total = 0
    try:
        for f in path.rglob("*"):
            if f.is_file():
                try:
                    total += f.stat().st_size
                except OSError:
                    pass
    except OSError:
        pass
    return total


def format_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes} Б"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} КБ"
    if size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} МБ"
    return f"{size_bytes / (1024 * 1024 * 1024):.2f} ГБ"

def _get_dir_size_excluding_git(path: Path) -> int:
    """
    Размер всех файлов в каталоге, рекурсивно,
    за исключением служебного каталога .git.
    """
    if not path.exists() or not path.is_dir():
        return 0

    git_dir = path / ".git"
    total = 0

    try:
        for f in path.rglob("*"):
            if not f.is_file():
                continue
            # Пропускаем всё, что внутри .git
            try:
                f.relative_to(git_dir)
                continue
            except ValueError:
                pass
            try:
                total += f.stat().st_size
            except OSError:
                pass
    except OSError:
        pass

    return total

# =========================================================
# STORAGE STATE (data/state.json)
# =========================================================

def _load_device_state(device_id: str) -> dict:
    """Последний результат backend'ов для устройства."""
    try:
        state = load_json(STATE_FILE, {})
        if not isinstance(state, dict):
            return {}
        entry = state.get(device_id)
        return entry if isinstance(entry, dict) else {}
    except Exception:
        return {}


def _ensure_storage_section(router: dict) -> None:
    """
    Если у устройства нет секции storage — добавляем
    дефолт в памяти (git включён, остальные выключены).

    На диск НЕ пишем: при первом сохранении формы через
    update_router секция сохранится уже явно.
    """
    if "storage" in router and isinstance(router["storage"], dict):
        return

    if storage_manager is None:
        return

    router["storage"] = storage_manager.defaults_for_new_router()


def _build_storage_rows(router: dict, device_state: dict) -> list:
    """
    Список строк для блока «Хранилища» в карточке устройства.
    """
    if storage_manager is None:
        return []

    storage_cfg = router.get("storage") or {}
    backends_state = device_state.get("backends") or {}
    last_run = device_state.get("last_run")

    rows = []
    for backend in storage_manager.all():
        cfg = storage_cfg.get(backend.id) or {}
        enabled = bool(cfg.get("enabled"))

        st = backends_state.get(backend.id) or {}

        if not enabled:
            status = "disabled"
            ok = None
            message = "Отключено"
            skipped = False
        else:
            ok = st.get("ok")
            message = st.get("message") or ""
            skipped = bool(st.get("skipped"))

            if ok is None:
                status = "idle"
                message = message or "Ещё не запускалось"
            elif skipped:
                status = "skipped"
            elif ok:
                status = "ok"
            else:
                status = "error"

        rows.append({
            "id": backend.id,
            "name": backend.name,
            "icon": backend.icon,
            "description": backend.description,
            "enabled": enabled,
            "status": status,
            "message": message,
            "last_run": last_run if enabled else None,
        })

    return rows


# =========================================================
# STORAGE FORM HELPERS
# =========================================================

def _parse_storage_form(form, old_storage: dict) -> dict:
    """
    Разбирает поля формы storage_<backend>_<field>
    и формирует секцию storage для routers.json.

    Пароли при сохранении шифруются. Если поле-пароль
    пришло пустым — берётся старое значение из old_storage.
    """
    if storage_manager is None:
        return {}

    old_storage = old_storage if isinstance(old_storage, dict) else {}
    result: dict = {}

    for backend in storage_manager.all():
        enabled_key = f"storage_{backend.id}_enabled"
        enabled = form.get(enabled_key) == "true"

        cfg: dict = {"enabled": enabled}
        old_cfg = old_storage.get(backend.id) or {}

        for field in backend.fields:
            key = f"storage_{backend.id}_{field.name}"

            if field.type == "bool":
                cfg[field.name] = form.get(key) == "true"

            elif field.type == "number":
                raw = form.get(key, "")
                if raw == "" or raw is None:
                    cfg[field.name] = field.default
                else:
                    try:
                        cfg[field.name] = int(raw)
                    except (TypeError, ValueError):
                        cfg[field.name] = field.default

            elif field.type == "password":
                raw = form.get(key, "")
                if raw:
                    cfg[field.name] = storage.encrypt(raw)
                else:
                    cfg[field.name] = old_cfg.get(field.name, "")

            else:
                raw = form.get(key, "")
                if raw == "" and field.default != "":
                    cfg[field.name] = field.default
                else:
                    cfg[field.name] = raw

        result[backend.id] = cfg

    return result


def _storage_backends_for_template():
    """Список backend'ов для рендера формы."""
    if storage_manager is None:
        return []
    return storage_manager.all()


def _storage_defaults_for_template():
    """Дефолтная секция storage — для формы добавления."""
    if storage_manager is None:
        return {}
    return storage_manager.defaults_for_new_router()


def _resolve_backup_path(backup_path) -> Path:
    path = Path(backup_path)
    if not path.is_absolute():
        path = BASE_DIR / path
    return path


def get_last_attempt(backup_path):
    """
    Возвращает (date_str, ok_bool) последней попытки бэкапа.

    ok=True  — в последнем по дате каталоге есть файлы;
    ok=False — каталог создан, но пуст (бэкап упал).
    """
    path = _resolve_backup_path(backup_path)

    if not path.exists() or not path.is_dir():
        return None, None

    latest = None
    for item in path.iterdir():
        if not item.is_dir():
            continue
        date_part = item.name[:10]
        try:
            folder_date = datetime.strptime(date_part, "%Y-%m-%d")
        except ValueError:
            continue
        if latest is None or folder_date > latest[0]:
            try:
                has_files = any(f.is_file() for f in item.iterdir())
            except OSError:
                has_files = False
            latest = (folder_date, has_files)

    if latest is None:
        return None, None

    return latest[0].strftime("%Y-%m-%d"), latest[1]


def get_device_history(backup_path, days: int = 30):
    """
    Возвращает (history, dates).

    history — список из 'ok' | 'fail' | 'none' длиной days.
    dates   — список YYYY-MM-DD для каждой ячейки.
    Сортировка: слева старые, справа свежие.
    """
    today = datetime.now().date()

    dates = [
        (today - timedelta(days=i)).strftime("%Y-%m-%d")
        for i in range(days - 1, -1, -1)
    ]

    history = ["none"] * days

    path = _resolve_backup_path(backup_path)
    if not path.exists() or not path.is_dir():
        return history, dates

    for item in path.iterdir():
        if not item.is_dir():
            continue
        date_part = item.name[:10]
        if date_part not in dates:
            continue
        idx = dates.index(date_part)
        try:
            has_files = any(f.is_file() for f in item.iterdir())
        except OSError:
            has_files = False
        history[idx] = "ok" if has_files else "fail"

    return history, dates


def get_recent_files(backup_path, limit: int = 3):
    """
    Последние limit файлов из датированных подкаталогов
    (по mtime). Файлы в корне и служебные каталоги
    (.git и прочие) не учитываются.
    """
    path = _resolve_backup_path(backup_path)
    if not path.exists() or not path.is_dir():
        return []

    files = []
    for folder in path.iterdir():

        if not folder.is_dir():
            continue

        # Только датированные подкаталоги
        if not DATE_DIR_REGEX.match(folder.name):
            continue

        try:
            for f in folder.iterdir():
                if f.is_file():
                    files.append(f)
        except OSError:
            continue

    def _mtime(p):
        try:
            return p.stat().st_mtime
        except OSError:
            return 0

    files.sort(key=_mtime, reverse=True)

    result = []
    for f in files[:limit]:
        try:
            size = format_size(f.stat().st_size)
            date = datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
        except OSError:
            continue
        result.append({
            "name": f.name,
            "rel_path": str(f.relative_to(path)),
            "date": date,
            "size": size,
        })
    return result


def classify_device(age_days, last_attempt_ok) -> str:
    """ok / warn / fail / none."""
    if last_attempt_ok is False:
        return "fail"
    if age_days is None:
        return "none"
    if age_days < 15:
        return "ok"
    if age_days <= 30:
        return "warn"
    return "fail"

def _build_device_payload(
    device_type: str,
    index: int,
    router: dict,
) -> dict:
    """Собирает всё, что нужно для карточки устройства."""
    # Гарантируем секцию storage (в памяти)
    _ensure_storage_section(router)

    b_path = router.get("backup_path", "backups/")
    b_path_abs = _resolve_backup_path(b_path)

    storage_cfg = router.get("storage") or {}
    git_cfg = storage_cfg.get("git") or {}
    git_enabled = bool(git_cfg.get("enabled"))

    # ---- Пытаемся прочитать историю из git ----
    git_commits = []
    if git_enabled and git_available():
        try:
            gs = GitStorage(b_path_abs)
            if gs.is_initialized():
                git_commits = gs.get_history(limit=200)
        except Exception as exc:
            logging.warning(
                "git: не удалось прочитать историю %s/%s: %s",
                device_type, index, exc,
            )

    # ---- Определяем статус / последний успех ----
    if git_commits:
        last = git_commits[0]
        try:
            last_ts = last.get("ts") or 0
            last_dt = datetime.fromtimestamp(last_ts)
            last_date = last_dt.strftime("%Y-%m-%d")
            age_days = (datetime.now() - last_dt).days
        except Exception:
            last_date = (last.get("time") or "")[:10]
            age_days = None

        backup_count = len(git_commits)
        last_attempt_date = last_date
        last_attempt_ok = True
    else:
        # Fallback на датированные папки (пока плагины их пишут)
        count, last_date, age_days = get_device_backup_info(b_path)
        last_attempt_date, last_attempt_ok = get_last_attempt(b_path)
        backup_count = count

    # ---- Размер без .git ----
    total_bytes = _get_dir_size_excluding_git(b_path_abs)
    total_size = format_size(total_bytes)

    if backup_count > 0 and total_bytes > 0:
        avg_size = format_size(total_bytes // backup_count)
    else:
        avg_size = "—"

    status = classify_device(age_days, last_attempt_ok)

    ip_addr = (
        router.get("host")
        or router.get("ip")
        or router.get("address", "Не указан")
    )

    if device_type.lower() == "mikrotik":
        version = f"ROS {router.get('version', '7')}"
    else:
        version = "pfSense"

    # ---- Последние коммиты для UI ----
    git_recent = []
    for c in git_commits[:3]:
        git_recent.append({
            "sha": c["sha"],
            "short": c["short"],
            "message": c["message"],
            "time": c["time"],
            "files": c["files"],
        })

    return {
        "id": f"{device_type}/{index}",
        "type": device_type,
        "index": index,
        "host": ip_addr,
        "version": version,
        "port": router.get("port", 22),
        "status": status,
        "last_success": last_date if backup_count > 0 else None,
        "last_success_age_days": age_days,
        "last_attempt": last_attempt_date,
        "last_attempt_ok": last_attempt_ok,
        "backup_count": backup_count,
        "max_backups": router.get("max_backups", 7),
        "total_size": total_size,
        "avg_size": avg_size,
        "backup_path": b_path,
        "ssh_key": (
            router.get("ssh_key")
            or router.get("private_key")
            or "id_rsa.key"
        ),
        "history": [],
        "history_dates": [],
        "recent": get_recent_files(b_path, 3),
        "git_enabled": git_enabled,
        "git_total": len(git_commits),
        "git_recent": git_recent,
        "storage_rows": _build_storage_rows(
            router,
            _load_device_state(f"{device_type}/{index}"),
        ),
    }
    
    
# =========================================================
# BACKUP TASKS
# =========================================================

def execute_device_backup_task(
    device_type: str,
    index: int,
    secure_router: dict,
):
    host = (
        secure_router.get("host")
        or secure_router.get("ip")
        or f"#{index}"
    )

    set_device_context(f"{device_type}/{host}")

    backup_started()
    try:
        plugin = (
            plugin_manager._plugins.get(device_type)
            if plugin_manager else None
        )

        if not plugin:
            return

        backup_func = plugin.get("func")
        if not backup_func:
            return

        ip_disp = (
            secure_router.get("host")
            or secure_router.get("ip")
        )

        logging.info("🚀 [Фон] Запуск бэкапа: %s", ip_disp)
        
        created_files = backup_func(secure_router)

        if created_files:
            storage_cfg = secure_router.get("storage") or {}
            git_enabled = bool(
                (storage_cfg.get("git") or {}).get("enabled")
            )

            # Ротация датированных папок — только если git выключен
            if not git_enabled and hasattr(plugin_manager, "_rotate_local"):
                plugin_manager._rotate_local(
                    secure_router.get("backup_path"),
                    secure_router.get("max_backups", 7),
                )

            # Push в storage backends (git, FTP, SMB, ...)
            if hasattr(plugin_manager, "_push_to_storages"):
                try:
                    plugin_manager._push_to_storages(
                        device_type=device_type,
                        index=index,
                        device_host=ip_disp,
                        router=secure_router,
                        backup_path=Path(
                            secure_router.get("backup_path")
                        ),
                        created_files=created_files,
                    )
                except Exception as exc:
                    logging.error(
                        "storage: ошибка диспетчеризации: %s",
                        exc,
                        exc_info=True,
                    )

        logging.info("✅ [Фон] Бэкап успешно завершен для %s", ip_disp)
        

    except Exception as exc:
        logging.error("❌ [Фон] Ошибка при бэкапе: %s", exc)

    finally:
        backup_finished()
        clear_device_context()

def execute_all_backups_task():

    backup_started()

    try:

        if plugin_manager:

            plugin_manager.run_all_backups()

    except Exception as exc:

        logging.error(
            "❌ [Фон] Ошибка массового "
            "бэкапа: %s",
            exc,
        )

    finally:

        backup_finished()


# =========================================================
# MAIN PAGE
# =========================================================

@app.get("/", response_class=HTMLResponse)
def read_root(
    request: Request,
    user: str = Depends(require_auth),
):
    devices = []

    if plugin_manager and hasattr(plugin_manager, "_plugins"):
        for device_type, plugin_data in plugin_manager._plugins.items():
            routers = plugin_data.get("routers", [])
            for index, router in enumerate(routers):
                devices.append(
                    _build_device_payload(device_type, index, router)
                )

    # Выбор устройства из ?selected=<type>/<index>
    selected_id = request.query_params.get("selected", "")
    selected = None
    if devices:
        if selected_id:
            for d in devices:
                if d["id"] == selected_id:
                    selected = d
                    break
        if selected is None:
            selected = devices[0]

    logs_size_bytes = get_dir_size(LOGS_DIR)

    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "devices": devices,
            "selected": selected,
            "scheduler_status": scheduler.get_status_text(),
            "scheduler_config": scheduler.config,
            "is_backup_running": is_backup_running(),
            "service": get_status(),
            "logs_size": format_size(logs_size_bytes),
        },
    )


# =========================================================
# API: DEVICE DETAIL (HTML partial)
# =========================================================

@app.get(
    "/api/device/{device_type}/{index}",
    response_class=HTMLResponse,
)
def api_device_detail(
    request: Request,
    device_type: str,
    index: int,
    user: str = Depends(require_auth),
):
    if not plugin_manager or device_type not in plugin_manager._plugins:
        raise HTTPException(status_code=404, detail="Plugin not found")

    routers = plugin_manager._plugins[device_type].get("routers", [])
    if not (0 <= index < len(routers)):
        raise HTTPException(status_code=404, detail="Device not found")

    payload = _build_device_payload(device_type, index, routers[index])

    return templates.TemplateResponse(
        request,
        "_device_detail.html",
        {"d": payload},
    )


# =========================================================
# API: SYSTEM INFO (JSON)
# =========================================================

@app.get("/api/system/info")
def api_system_info(user: str = Depends(require_auth)):
    logs_size_bytes = get_dir_size(LOGS_DIR)
    return {
        "logs_size": format_size(logs_size_bytes),
        "logs_size_bytes": logs_size_bytes,
        "scheduler_config": scheduler.config,
        "scheduler_status": scheduler.get_status_text(),
        "service": get_status(),
        "is_backup_running": is_backup_running(),
    }

# =========================================================
# API: TEST STORAGE BACKEND
# =========================================================

@app.post("/api/storage/test/{backend_id}")
async def api_storage_test(
    backend_id: str,
    request: Request,
    device_type: str = "",
    index: int = -1,
    user: str = Depends(require_auth),
):
    if storage_manager is None:
        raise HTTPException(404, "StorageManager недоступен")

    backend = storage_manager.get(backend_id)
    if backend is None:
        raise HTTPException(404, f"Backend '{backend_id}' не найден")

    try:
        data = await request.json()
    except Exception:
        data = {}

    # Пытаемся найти старое значение паролей из существующего
    # роутера — если тестируется уже настроенное устройство.
    old_cfg: dict = {}
    if device_type and index >= 0:
        try:
            routers = plugin_manager._plugins[device_type].get("routers", [])
            if 0 <= index < len(routers):
                old_cfg = (routers[index].get("storage") or {}).get(backend_id) or {}
        except Exception:
            old_cfg = {}

    # Собираем конфиг из формы + старых паролей
    cfg: dict = {}
    for field in backend.fields:
        value = data.get(field.name, "")

        if field.type == "password":
            if value:
                cfg[field.name] = value           # плейн из формы
            else:
                enc = old_cfg.get(field.name, "")
                cfg[field.name] = storage.decrypt(enc) if enc else ""
        elif field.type == "number":
            try:
                cfg[field.name] = int(value) if value != "" else field.default
            except (TypeError, ValueError):
                cfg[field.name] = field.default
        elif field.type == "bool":
            cfg[field.name] = bool(value)
        else:
            cfg[field.name] = value if value != "" else field.default

    try:
        result = backend.test(cfg)
    except Exception as exc:
        logging.error(
            "storage test [%s]: исключение: %s",
            backend_id, exc, exc_info=True,
        )
        return {"ok": False, "message": f"Исключение: {exc}"}

    return {"ok": result.ok, "message": result.message}

# =========================================================
# SERVICE PAGE
# =========================================================

@app.get(
    "/service",
    response_class=HTMLResponse,
)
def service_page(
    request: Request,
    user: str = Depends(
        require_auth
    ),
):

    return templates.TemplateResponse(
        request,
        "service.html",
        {
            "service":
                get_status(),
        },
    )


# =========================================================
# SERVICE INSTALL
# =========================================================
@app.post(
    "/service/install",
    response_class=HTMLResponse,
)
def service_install_endpoint(
    request: Request,
    user: str = Depends(require_auth),
):
    # -----------------------------------------------------
    # 1. Precondition — уже зарегистрирована
    # -----------------------------------------------------

    if is_service_registered():
        return _render_service_action(
            request,
            "Регистрация службы",
            "Служба уже зарегистрирована.",
            ok=True,
        )

    # -----------------------------------------------------
    # 2. Защита от нонсенса: процесс в режиме службы,
    #    но служба не зарегистрирована.
    # -----------------------------------------------------

    if is_service_process():
        return _render_service_action(
            request,
            "Регистрация службы",
            "Процесс уже запущен в режиме службы. "
            "Регистрация не требуется.",
            ok=False,
        )

    # -----------------------------------------------------
    # 3. Админ — устанавливаем напрямую
    # -----------------------------------------------------

    if is_admin():
        ok, message = install_service()

        logging.info(
            "Регистрация службы пользователем %s: %s",
            user,
            message,
        )
        
        invalidate_status_cache()

        return _render_service_action(
            request,
            "Регистрация службы",
            message,
            ok=ok,
        )

    # -----------------------------------------------------
    # 4. Не админ — UAC
    # -----------------------------------------------------

    ok = relaunch_elevated(["--install-service"])

    if not ok:
        return _render_service_action(
            request,
            "Регистрация службы",
            "Не удалось запросить права администратора.",
            ok=False,
        )

    # Освобождаем порт: после UAC служба стартует и будет
    # биндиться на тот же host:port.
    _schedule_self_exit()

    logging.info(
        "Запрошена регистрация службы через UAC "
        "пользователем %s",
        user,
    )
    
    invalidate_status_cache()

    return _render_service_action(
        request,
        "Регистрация службы",
        "Запрошена регистрация службы с правами администратора. "
        "После подтверждения UAC служба будет зарегистрирована "
        "и запущена.",
        ok=True,
    )

# =========================================================
# SERVICE START
# =========================================================

@app.post(
    "/service/start",
    response_class=HTMLResponse,
)
def service_start_endpoint(
    request: Request,
    user: str = Depends(
        require_auth
    ),
):

    if not is_service_registered():

        return templates.TemplateResponse(
            request,
            "service_action.html",
            {
                "title":
                    "Запуск службы",

                "message":
                    "Служба не зарегистрирована.",

                "ok":
                    False,
            },
        )

    if is_admin():

        ok, message = (
            start_service()
        )

        logging.info(
            "Запуск службы "
            "пользователем %s: %s",
            user,
            message,
        )
        
        invalidate_status_cache()

        return templates.TemplateResponse(
            request,
            "service_action.html",
            {
                "title":
                    "Запуск службы",

                "message":
                    message,

                "ok":
                    ok,
            },
        )

    ok = relaunch_elevated(
        [
            "--start-service"
        ]
    )

    return templates.TemplateResponse(
        request,
        "service_action.html",
        {
            "title":
                "Запуск службы",

            "message":
                (
                    "Запрошен запуск службы "
                    "с правами администратора."
                    if ok
                    else
                    "Не удалось запросить "
                    "права администратора."
                ),

            "ok":
                ok,
        },
    )


# =========================================================
# SERVICE RESTART
# =========================================================
@app.post("/service/restart", response_class=HTMLResponse)
def service_restart_endpoint(
    request: Request,
    user: str = Depends(require_auth),
):
    if not is_service_registered():
        return templates.TemplateResponse(
            request,
            "service_action.html",
            {
                "title": "Перезапуск службы",
                "message": "Служба не зарегистрирована.",
                "ok": False,
            },
        )

    # -----------------------------------------------------
    # Если мы сами служба — отложенный перезапуск,
    # чтобы успеть отдать ответ клиенту.
    # -----------------------------------------------------

    if get_status().get("service_mode", False):
        ok, message = schedule_restart()

        logging.info(
            "Перезапуск службы (из службы) "
            "пользователем %s: %s",
            user, message,
        )
        
        invalidate_status_cache()

        return templates.TemplateResponse(
            request,
            "service_action.html",
            {
                "title": "Перезапуск службы",
                "message": message,
                "ok": ok,
            },
        )

    # -----------------------------------------------------
    # Интерактивная копия. Если админ — schedule_restart()
    # сам разберётся (он отложенный и безопасный).
    # -----------------------------------------------------

    if is_admin():
        ok, message = schedule_restart()

        logging.info(
            "Перезапуск службы пользователем %s: %s",
            user, message,
        )
        
        invalidate_status_cache()

        return templates.TemplateResponse(
            request,
            "service_action.html",
            {
                "title": "Перезапуск службы",
                "message": message,
                "ok": ok,
            },
        )

    # -----------------------------------------------------
    # Не админ — UAC. Дочерний процесс вызовет
    # --restart-service.
    # -----------------------------------------------------

    ok = relaunch_elevated(["--restart-service"])

    logging.info(
        "Запрошен перезапуск службы через UAC "
        "пользователем %s: %s",
        user, ok,
    )
    
    invalidate_status_cache()

    return templates.TemplateResponse(
        request,
        "service_action.html",
        {
            "title": "Перезапуск службы",
            "message": (
                "Запрошен перезапуск службы с правами "
                "администратора. После подтверждения UAC "
                "служба будет перезапущена."
                if ok
                else
                "Не удалось запросить права администратора."
            ),
            "ok": ok,
        },
    )
    
# =========================================================
# SERVICE STOP
# =========================================================

@app.post("/service/stop", response_class=HTMLResponse)
def service_stop_endpoint(
    request: Request,
    user: str = Depends(require_auth),
):
    # -----------------------------------------------------
    # Если служба не зарегистрирована — останавливать нечего.
    # -----------------------------------------------------

    if not is_service_registered():
        return templates.TemplateResponse(
            request,
            "service_action.html",
            {
                "title": "Остановка службы",
                "message": "Служба не зарегистрирована.",
                "ok": False,
            },
        )

    # -----------------------------------------------------
    # Мы сами — служба. Нельзя гасить задачу напрямую,
    # иначе web-процесс умрёт до отправки ответа.
    # Используем отложенную остановку.
    # -----------------------------------------------------

    if get_status().get("service_mode", False):
        ok, message = schedule_stop()

        logging.info(
            "Остановка службы (из службы) "
            "пользователем %s: %s",
            user, message,
        )
        
        invalidate_status_cache()

        return templates.TemplateResponse(
            request,
            "service_action.html",
            {
                "title": "Остановка службы",
                "message": message,
                "ok": ok,
            },
        )

    # -----------------------------------------------------
    # Мы интерактивная копия и админ — можно гасить задачу
    # напрямую, текущий процесс не пострадает.
    # -----------------------------------------------------

    if is_admin():
        ok, message = stop_service()

        logging.info(
            "Остановка службы пользователем %s: %s",
            user, message,
        )
        
        invalidate_status_cache()

        return templates.TemplateResponse(
            request,
            "service_action.html",
            {
                "title": "Остановка службы",
                "message": message,
                "ok": ok,
            },
        )

    # -----------------------------------------------------
    # Не админ — запрашиваем UAC. Дочерний процесс вызовет
    # --stop-service и корректно остановит задачу.
    # -----------------------------------------------------

    ok = relaunch_elevated(["--stop-service"])

    logging.info(
        "Запрошена остановка службы через UAC "
        "пользователем %s: %s",
        user, ok,
    )
    
    invalidate_status_cache()

    return templates.TemplateResponse(
        request,
        "service_action.html",
        {
            "title": "Остановка службы",
            "message": (
                "Запрошена остановка службы с правами "
                "администратора. После подтверждения UAC "
                "служба будет остановлена."
                if ok
                else
                "Не удалось запросить права администратора."
            ),
            "ok": ok,
        },
    )

# =========================================================
# SERVICE UNINSTALL
# =========================================================
@app.post(
    "/service/uninstall",
    response_class=HTMLResponse,
)
def service_uninstall_endpoint(
    request: Request,
    user: str = Depends(require_auth),
):
    # -----------------------------------------------------
    # 1. Precondition — нечего удалять
    # -----------------------------------------------------

    if not is_service_registered():
        return _render_service_action(
            request,
            "Удаление службы",
            "Служба не зарегистрирована.",
            ok=True,
        )

    # -----------------------------------------------------
    # 2. Мы сами — служба. Прямое удаление убьёт наш процесс
    #    до отправки ответа, поэтому — отложенно.
    # -----------------------------------------------------

    if is_service_process():
        ok, message = schedule_uninstall()

        logging.info(
            "Удаление службы (из службы) "
            "пользователем %s: %s",
            user,
            message,
        )
        
        invalidate_status_cache()

        return _render_service_action(
            request,
            "Удаление службы",
            message,
            ok=ok,
        )

    # -----------------------------------------------------
    # 3. Админ — удаляем напрямую (мы не служба, нас не убьёт)
    # -----------------------------------------------------

    if is_admin():
        ok, message = uninstall_service()

        logging.info(
            "Удаление службы пользователем %s: %s",
            user,
            message,
        )
        
        invalidate_status_cache()

        return _render_service_action(
            request,
            "Удаление службы",
            message,
            ok=ok,
        )

    # -----------------------------------------------------
    # 4. Не админ — UAC
    # -----------------------------------------------------

    ok = relaunch_elevated(["--uninstall-service"])

    if not ok:
        return _render_service_action(
            request,
            "Удаление службы",
            "Не удалось запросить права администратора.",
            ok=False,
        )

    logging.info(
        "Запрошено удаление службы через UAC "
        "пользователем %s",
        user,
    )
    
    invalidate_status_cache()

    return _render_service_action(
        request,
        "Удаление службы",
        "Запрошено удаление службы с правами администратора. "
        "После подтверждения UAC служба будет удалена.",
        ok=True,
    )

# =========================================================
# ADD ROUTER
# =========================================================

@app.get(
    "/add",
    response_class=HTMLResponse,
)
def add_router_page(
    request: Request,
    user: str = Depends(
        require_auth
    ),
):

    plugins_list = (
        list(
            plugin_manager
            ._plugins
            .keys()
        )
        if plugin_manager
        else []
    )

    return templates.TemplateResponse(
        request,
        "add.html",
        {
            "plugins":
                plugins_list,

            "storage_backends":
                _storage_backends_for_template(),

            "default_storage":
                _storage_defaults_for_template(),
        },
    )


# =========================================================
# SAVE ROUTER
# =========================================================

@app.post(
    "/save-router"
)
async def save_router_endpoint(
    request: Request,
    user: str = Depends(
        require_auth
    ),
):
    form = await request.form()

    device_type = (form.get("device_type") or "").strip()
    ip = (form.get("ip") or "").strip()
    version = form.get("version") or "7"
    username = form.get("username") or "admin"
    ssh_pass = form.get("ssh_pass") or ""
    private_key = form.get("private_key") or ""
    backup_path = (form.get("backup_path") or "").strip()

    try:
        max_backups = int(form.get("max_backups") or "5")
    except (TypeError, ValueError):
        max_backups = 5

    if not device_type or not ip or not backup_path:
        raise HTTPException(
            status_code=400,
            detail="Не заполнены обязательные поля",
        )

    if (
        not plugin_manager
        or device_type
        not in plugin_manager._plugins
    ):

        raise HTTPException(
            status_code=400,
            detail="Указанный плагин не найден",
        )

    encrypted_ssh_pass = (
        storage.encrypt(
            ssh_pass
        )
        if ssh_pass
        else ""
    )

    new_router = {
        "host":
            ip,

        "ip":
            ip,

        "username":
            username,

        "ssh_pass":
            encrypted_ssh_pass,

        "backup_path":
            backup_path,

        "max_backups":
            int(max_backups),
    }

    if (
        device_type.lower()
        == "mikrotik"
    ):

        new_router[
            "version"
        ] = version

        new_router[
            "private_key"
        ] = (
            private_key
            if private_key
            else "id_rsa.key"
        )

    else:

        new_router[
            "ssh_key"
        ] = (
            private_key
            if private_key
            else "id_rsa.key"
        )

    new_router["storage"] = _parse_storage_form(form, {})
    
    plugin_data = (
        plugin_manager
        ._plugins[
            device_type
        ]
    )

    plugin_data[
        "routers"
    ].append(
        new_router
    )

    target_dir = (
        BASE_DIR / "plugins"
    )

    for p_folder in (
        target_dir.iterdir()
    ):

        if (
            p_folder.is_dir()
            and
            p_folder.name.lower()
            == device_type.lower()
        ):

            json_file = (
                p_folder
                / "routers.json"
            )

            with open(
                json_file,
                "w",
                encoding="utf-8",
            ) as f:

                json.dump(
                    plugin_data[
                        "routers"
                    ],
                    f,
                    ensure_ascii=False,
                    indent=4,
                )

            break

    logging.info(
        "➕ Добавлено новое устройство "
        "(%s): %s",
        device_type,
        ip,
    )
    
    invalidate_status_cache()

    return RedirectResponse(
        url="/",
        status_code=303,
    )


# =========================================================
# SINGLE BACKUP
# =========================================================

@app.get(
    "/run-progress/{device_type}/{index}"
)
def run_backup(
    device_type: str,
    index: int,
    background_tasks:
        BackgroundTasks,
    user: str = Depends(
        require_auth
    ),
):

    if is_backup_running():

        return RedirectResponse(
            url=(
                f"/logs/device/"
                f"{device_type}/{index}"
            ),
            status_code=303,
        )

    plugin = (
        plugin_manager
        ._plugins
        .get(device_type)
        if plugin_manager
        else None
    )

    if (
        not plugin
        or index < 0
        or index >= len(
            plugin.get(
                "routers",
                []
            )
        )
    ):

        return HTMLResponse(
            "<h3>Устройство не найдено</h3>"
            "<a href='/'>Назад</a>",
            status_code=404,
        )

    router = (
        plugin["routers"][index]
    )

    secure_router = copy.deepcopy(
        router
    )

    if (
        "ssh_pass"
        in secure_router
        and secure_router[
            "ssh_pass"
        ]
    ):

        secure_router[
            "ssh_pass"
        ] = storage.decrypt(
            secure_router[
                "ssh_pass"
            ]
        )

    background_tasks.add_task(
        execute_device_backup_task,
        device_type,
        index,
        secure_router,
    )

    return RedirectResponse(
        url=(
            f"/logs/device/"
            f"{device_type}/{index}"
        ),
        status_code=303,
    )


# =========================================================
# ALL BACKUPS
# =========================================================

@app.post(
    "/run-now"
)
def run_now_endpoint(
    background_tasks:
        BackgroundTasks,
    user: str = Depends(
        require_auth
    ),
):

    if not is_backup_running():

        background_tasks.add_task(
            execute_all_backups_task
        )

    return RedirectResponse(
        url="/logs/main",
        status_code=303,
    )


# =========================================================
# BACKUPS
# =========================================================

@app.get(
    "/backups/{device_type}/{index}",
    response_class=HTMLResponse,
)
def backups_page(
    request: Request,
    device_type: str,
    index: int,
    user: str = Depends(
        require_auth
    ),
):

    plugin = (
        plugin_manager
        ._plugins
        .get(device_type)
        if plugin_manager
        else None
    )

    if (
        not plugin
        or index < 0
        or index >= len(
            plugin.get(
                "routers",
                []
            )
        )
    ):

        return HTMLResponse(
            "<h3>Устройство не найдено</h3>",
            status_code=404,
        )

    router = (
        plugin["routers"][index]
    )

    # Если git включён — переадресуем на историю версий
    storage_cfg = router.get("storage") or {}
    if (storage_cfg.get("git") or {}).get("enabled"):
        return RedirectResponse(
            url=f"/git/{device_type}/{index}",
            status_code=307,
        )

    backup_path = Path(
        router.get(
            "backup_path",
            "backups",
        )
    )

    if not backup_path.is_absolute():

        backup_path = (
            BASE_DIR
            / backup_path
        )

    grouped_backups = (
        defaultdict(list)
    )



    if (
        backup_path.exists()
        and backup_path.is_dir()
    ):

        for item in sorted(
            backup_path.iterdir(),
            reverse=True,
        ):

            # Только датированные подкаталоги.
            # Файлы в корне (config.rsc, config.backup)
            # и служебные каталоги (.git) не показываем.
            if not item.is_dir():
                continue

            if not DATE_DIR_REGEX.match(item.name):
                continue

            folder_name = item.name

            try:
                files_in_dir = sorted(
                    [
                        f
                        for f in item.iterdir()
                        if f.is_file()
                    ],
                    key=lambda x: x.name,
                )
            except OSError:
                continue

            for f in files_in_dir:

                try:
                    size_bytes = f.stat().st_size
                except OSError:
                    continue

                if size_bytes >= 1024:
                    size_str = f"{size_bytes / 1024:.1f} KB"
                else:
                    size_str = f"{size_bytes} B"

                rel_path = f"{folder_name}/{f.name}"

                grouped_backups[folder_name].append({
                    "name": f.name,
                    "rel_path": rel_path,
                    "size": size_str,
                })




    return templates.TemplateResponse(
        request,
        "backups.html",
        {
            "device_type":
                device_type,

            "index":
                index,

            "router":
                router,

            "grouped_backups":
                dict(
                    grouped_backups
                ),

            "backup_path":
                str(
                    backup_path
                ),
        },
    )


# =========================================================
# SCHEDULER
# =========================================================

@app.get(
    "/scheduler",
    response_class=HTMLResponse,
)
def scheduler_page(
    request: Request,
    user: str = Depends(
        require_auth
    ),
):

    return templates.TemplateResponse(
        request,
        "scheduler.html",
        {
            "config":
                scheduler.config
        },
    )


@app.post(
    "/scheduler/save"
)
async def save_scheduler_config(
    user: str = Depends(
        require_auth
    ),
    enabled: bool = Form(False),
    mode: str = Form("daily"),
    daily_time: str = Form("03:00"),
    interval_hours: int = Form(24),
):

    scheduler.save_config({
        "enabled":
            enabled,

        "mode":
            mode,

        "daily_time":
            daily_time,

        "interval_hours":
            interval_hours,
    })

    logging.info(
        "⏰ Изменены настройки "
        "планировщика: %s",
        scheduler.get_status_text(),
    )
    
    invalidate_status_cache()

    return RedirectResponse(
        url="/",
        status_code=303,
    )


# =========================================================
# DOWNLOAD BACKUP
# =========================================================

@app.get(
    "/download-backup/"
    "{device_type}/{index}/{filepath:path}"
)
def download_backup(
    device_type: str,
    index: int,
    filepath: str,
    user: str = Depends(
        require_auth
    ),
):

    plugin = (
        plugin_manager
        ._plugins
        .get(device_type)
        if plugin_manager
        else None
    )

    if (
        not plugin
        or index < 0
        or index >= len(
            plugin.get(
                "routers",
                []
            )
        )
    ):

        raise HTTPException(
            status_code=404,
            detail="Устройство не найдено",
        )

    router = (
        plugin["routers"][index]
    )

    backup_path = Path(
        router.get(
            "backup_path",
            "backups",
        )
    )

    if not backup_path.is_absolute():

        backup_path = (
            BASE_DIR
            / backup_path
        )

    backup_root = (
        backup_path.resolve()
    )

    file_full_path = (
        backup_path
        / filepath
    ).resolve()

    try:

        file_full_path.relative_to(
            backup_root
        )

    except ValueError:

        raise HTTPException(
            status_code=403,
            detail="Отказано в доступе",
        )

    if (
        not file_full_path.exists()
        or not file_full_path.is_file()
    ):

        raise HTTPException(
            status_code=404,
            detail="Файл бэкапа не найден",
        )

    return FileResponse(
        path=file_full_path,
        filename=file_full_path.name,
        media_type=(
            "application/octet-stream"
        ),
    )


# =========================================================
# GIT: HISTORY
# =========================================================

def _get_git_storage_for(device_type: str, index: int):
    """
    Возвращает (router, backup_path, GitStorage) или
    (None, None, None), если устройство не найдено.
    """
    if not plugin_manager or device_type not in plugin_manager._plugins:
        return None, None, None

    routers = plugin_manager._plugins[device_type].get("routers", [])
    if not (0 <= index < len(routers)):
        return None, None, None

    router = routers[index]
    backup_path = _resolve_backup_path(
        router.get("backup_path", "backups/")
    )

    return router, backup_path, GitStorage(backup_path)


@app.get(
    "/git/{device_type}/{index}",
    response_class=HTMLResponse,
)
def git_history_page(
    request: Request,
    device_type: str,
    index: int,
    limit: int = 100,
    user: str = Depends(require_auth),
):
    router, backup_path, storage = _get_git_storage_for(
        device_type, index
    )

    if router is None:
        return HTMLResponse(
            "<h3>Устройство не найдено</h3>",
            status_code=404,
        )

    commits = []
    notes = {}
    available = git_available()
    initialized = storage.is_initialized() if available else False

    if initialized:
        try:
            commits = storage.get_history(limit=limit)
            notes = storage.get_notes()
        except Exception as exc:
            logging.error(
                "git: ошибка чтения истории %s/%s: %s",
                device_type, index, exc, exc_info=True,
            )

    for c in commits:
        c["note"] = notes.get(c["sha"], "")

    host = (
        router.get("host")
        or router.get("ip")
        or router.get("address")
        or f"устройство #{index}"
    )

    return templates.TemplateResponse(
        request,
        "git_history.html",
        {
            "device_type": device_type,
            "index": index,
            "host": host,
            "backup_path": str(backup_path),
            "commits": commits,
            "total": len(commits),
            "limit": limit,
            "git_available": available,
            "git_initialized": initialized,
        },
    )


# =========================================================
# GIT: DOWNLOAD FILE AT COMMIT
# =========================================================

@app.get(
    "/git/{device_type}/{index}/download/{sha}/{filename}",
)
def git_download_file(
    device_type: str,
    index: int,
    sha: str,
    filename: str,
    user: str = Depends(require_auth),
):
    router, backup_path, storage = _get_git_storage_for(
        device_type, index
    )
    if router is None:
        raise HTTPException(404, "Устройство не найдено")

    if not git_available() or not storage.is_initialized():
        raise HTTPException(404, "Репозиторий git недоступен")

    # Защита от path traversal
    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(400, "Некорректное имя файла")

    data = storage.get_file_at(sha, filename)
    if data is None:
        raise HTTPException(
            404,
            f"Файл {filename!r} не найден в коммите {sha[:7]}",
        )

    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition":
                f'attachment; filename="{filename}"',
        },
    )


# =========================================================
# GIT: SET NOTE
# =========================================================

@app.post(
    "/git/{device_type}/{index}/note/{sha}",
)
async def git_set_note(
    device_type: str,
    index: int,
    sha: str,
    request: Request,
    user: str = Depends(require_auth),
):
    router, backup_path, storage = _get_git_storage_for(
        device_type, index
    )
    if router is None:
        raise HTTPException(404, "Устройство не найдено")

    if not git_available() or not storage.is_initialized():
        raise HTTPException(404, "Репозиторий git недоступен")

    form = await request.form()
    note = (form.get("note") or "").strip()

    try:
        storage.set_note(sha, note)
        logging.info(
            "git[%s/%s]: заметка %s для коммита %s",
            device_type, index,
            "обновлена" if note else "удалена",
            sha[:7],
        )
    except Exception as exc:
        logging.error(
            "git: ошибка сохранения заметки: %s",
            exc, exc_info=True,
        )

    return RedirectResponse(
        url=f"/git/{device_type}/{index}",
        status_code=303,
    )

# =========================================================
# GIT: DIFF
# =========================================================

@app.get(
    "/git/{device_type}/{index}/diff/{sha_a}/{sha_b}",
    response_class=HTMLResponse,
)
def git_diff_page(
    request: Request,
    device_type: str,
    index: int,
    sha_a: str,
    sha_b: str,
    user: str = Depends(require_auth),
):
    router, backup_path, storage = _get_git_storage_for(
        device_type, index
    )
    if router is None:
        return HTMLResponse(
            "<h3>Устройство не найдено</h3>",
            status_code=404,
        )

    if not git_available() or not storage.is_initialized():
        return HTMLResponse(
            "<h3>Репозиторий git недоступен</h3>",
            status_code=404,
        )

    # Разбираем короткие SHA в полные
    history = storage.get_history(limit=500)
    by_short = {c["short"]: c["sha"] for c in history}
    by_full = {c["sha"]: c["sha"] for c in history}

    full_a = by_short.get(sha_a) or by_full.get(sha_a) or sha_a
    full_b = by_short.get(sha_b) or by_full.get(sha_b) or sha_b

    commit_a = next(
        (c for c in history if c["sha"] == full_a), None
    )
    commit_b = next(
        (c for c in history if c["sha"] == full_b), None
    )

    if commit_a is None or commit_b is None:
        return HTMLResponse(
            "<h3>Коммит не найден</h3>",
            status_code=404,
        )

    # Список файлов, которые вообще могут быть в diff.
    # Берём объединение файлов из обоих коммитов.
    filenames = sorted(set(commit_a["files"]) | set(commit_b["files"]))

    try:
        diffs = storage.get_diff(full_a, full_b, filenames)
    except Exception as exc:
        logging.error(
            "git: ошибка построения diff %s..%s: %s",
            sha_a[:7], sha_b[:7], exc, exc_info=True,
        )
        diffs = {"(ошибка)": f"Не удалось построить diff: {exc}"}

    # Парсим diff на строки для подсветки
    parsed_diffs = {}
    for name, text in diffs.items():
        lines = []
        for line in text.splitlines():
            if line.startswith("+++") or line.startswith("---"):
                kind = "header"
            elif line.startswith("@@"):
                kind = "hunk"
            elif line.startswith("+"):
                kind = "add"
            elif line.startswith("-"):
                kind = "del"
            else:
                kind = "ctx"
            lines.append({"kind": kind, "text": line})
        parsed_diffs[name] = lines

    host = (
        router.get("host")
        or router.get("ip")
        or router.get("address")
        or f"устройство #{index}"
    )

    stats = {
        "files": len([n for n, lines in parsed_diffs.items()
                      if any(l["kind"] in ("add", "del") for l in lines)]),
        "add": sum(
            1 for lines in parsed_diffs.values()
            for l in lines if l["kind"] == "add"
        ),
        "del": sum(
            1 for lines in parsed_diffs.values()
            for l in lines if l["kind"] == "del"
        ),
    }

    return templates.TemplateResponse(
        request,
        "git_diff.html",
        {
            "device_type": device_type,
            "index": index,
            "host": host,
            "commit_a": commit_a,
            "commit_b": commit_b,
            "parsed_diffs": parsed_diffs,
            "stats": stats,
        },
    )

# =========================================================
# MAIN LOG
# =========================================================

@app.get(
    "/logs/main",
    response_class=HTMLResponse,
)
def logs_main_endpoint(
    request: Request,
    limit: str = "100",
    raw: bool = False,
    user: str = Depends(
        require_auth
    ),
):

    log_file = (
        LOGS_DIR
        / "app.log"
    )

    content, lines_shown, has_more, _ = (
        read_log_tail(
            log_file,
            limit,
        )
    )

    if raw:

        return HTMLResponse(
            content,
            media_type="text/plain",
        )

    return templates.TemplateResponse(
        request,
        "logs.html",
        {
            "log_type":
                "Main Service Log",

            "content":
                content,

            "lines_shown":
                int(lines_shown),

            "has_more":
                has_more,

            "limit":
                limit,
        },
    )


# =========================================================
# SERVICE LOGS (stdout/stderr NSSM)
# =========================================================

@app.get(
    "/logs/service",
    response_class=HTMLResponse,
)
def logs_service_endpoint(
    request: Request,
    stream: str = "stderr",
    limit: str = "100",
    raw: bool = False,
    user: str = Depends(require_auth),
):
    filename = (
        "service_stdout.log"
        if stream == "stdout"
        else "service_stderr.log"
    )

    log_file = LOGS_DIR / filename

    content, lines_shown, has_more, _ = read_log_tail(log_file, limit)

    if raw:
        return HTMLResponse(content, media_type="text/plain")

    label = (
        "Service stdout (NSSM)"
        if stream == "stdout"
        else "Service stderr (NSSM)"
    )

    return templates.TemplateResponse(
        request,
        "logs.html",
        {
            "log_type": label,
            "content": content,
            "lines_shown": int(lines_shown),
            "has_more": has_more,
            "limit": limit,
            "stream": stream,
        },
    )

# =========================================================
# DEVICE LOG
# =========================================================

@app.get(
    "/logs/device/"
    "{device_type}/{index}",
    response_class=HTMLResponse,
)
def device_logs(
    request: Request,
    device_type: str,
    index: int,
    limit: str = "100",
    raw: bool = False,
    focus_date: str = "",
    user: str = Depends(
        require_auth
    ),
):

    device_name = (
        f"{device_type} #{index}"
    )

    if (
        plugin_manager
        and device_type
        in plugin_manager._plugins
    ):

        routers = (
            plugin_manager
            ._plugins[
                device_type
            ].get(
                "routers",
                [],
            )
        )

        if (
            0 <= index
            < len(routers)
        ):

            router = (
                routers[index]
            )

            device_name = (
                router.get("host")
                or router.get("ip")
                or router.get("address")
                or device_name
            )

    log_file = (
        LOGS_DIR
        / "app.log"
    )

    filter_tag = _device_filter_tag(
        device_type,
        index,
    )

    content, lines_shown, has_more, focus_line_index = (
        read_log_tail(
            log_file,
            limit,
            filter_tag=filter_tag,
            focus_date=focus_date or None,
        )
    )

    if raw:

        return HTMLResponse(
            content,
            media_type="text/plain",
        )

    return templates.TemplateResponse(
        request,
        "logs.html",
        {
            "log_type":
                f"Устройство {device_name}",

            "content":
                content,

            "lines_shown":
                int(lines_shown),

            "has_more":
                has_more,

            "limit":
                limit,

            "focus_date":
                focus_date or None,

            "focus_line_index":
                focus_line_index,
        },
    )


# =========================================================
# EDIT ROUTER
# =========================================================

@app.get(
    "/edit/{device_type}/{index}",
    response_class=HTMLResponse,
)
def edit_router(
    request: Request,
    device_type: str,
    index: int,
    user: str = Depends(
        require_auth
    ),
):

    plugin = (
        plugin_manager
        ._plugins
        .get(device_type)
        if plugin_manager
        else None
    )

    if (
        not plugin
        or index < 0
        or index >= len(
            plugin.get(
                "routers",
                []
            )
        )
    ):

        return HTMLResponse(
            "<h3>Устройство не найдено</h3>",
            status_code=404,
        )
    router = copy.deepcopy(
        plugin["routers"][index]
    )

    # Гарантируем секцию storage в памяти
    _ensure_storage_section(router)

    return templates.TemplateResponse(
        request,
        "edit.html",
        {
            "device_type":
                device_type,

            "index":
                index,

            "router":
                router,

            "storage_backends":
                _storage_backends_for_template(),
        },
    )

# =========================================================
# UPDATE ROUTER
# =========================================================

@app.post(
    "/update-router/"
    "{device_type}/{index}"
)
async def update_router(
    device_type: str,
    index: int,
    request: Request,
    user: str = Depends(
        require_auth
    ),
):
    form = await request.form()

    ip = (form.get("ip") or "").strip()
    version = form.get("version") or "7"
    username = form.get("username") or "admin"
    ssh_pass = form.get("ssh_pass") or ""
    private_key = form.get("private_key") or ""
    backup_path = (form.get("backup_path") or "").strip()
    max_backups_raw = form.get("max_backups") or "5"
    port_raw = form.get("port") or "22"

    try:
        max_backups = int(max_backups_raw)
    except (TypeError, ValueError):
        max_backups = 5

    try:
        port = int(port_raw)
    except (TypeError, ValueError):
        port = 22

    if not ip or not backup_path:
        raise HTTPException(
            status_code=400,
            detail="Не заполнены обязательные поля",
        )

    if (
        not plugin_manager
        or device_type
        not in plugin_manager._plugins
    ):

        raise HTTPException(
            status_code=400,
            detail="Плагин не найден",
        )

    routers_list = (
        plugin_manager
        ._plugins[
            device_type
        ].get(
            "routers",
            [],
        )
    )

    if (
        index < 0
        or index >= len(
            routers_list
        )
    ):

        raise HTTPException(
            status_code=404,
            detail="Устройство не найдено",
        )

    # Сохраняем старый роутер — из него берём пароли,
    # если пользователь не ввёл новые (оставил поле пустым).
    old_router = routers_list[index]

    if ssh_pass:
        ssh_pass_encrypted = storage.encrypt(ssh_pass)
    else:
        ssh_pass_encrypted = old_router.get("ssh_pass", "")

    updated_router = {
        "host":
            ip,

        "ip":
            ip,

        "username":
            username,

        "ssh_pass":
            ssh_pass_encrypted,

        "backup_path":
            backup_path,

        "max_backups":
            int(max_backups),
    }

    if (
        device_type.lower()
        == "mikrotik"
    ):

        updated_router[
            "version"
        ] = version

        updated_router[
            "private_key"
        ] = (
            private_key
            if private_key
            else "id_rsa.key"
        )

        if port != 22:

            updated_router[
                "port"
            ] = port

    else:

        updated_router[
            "ssh_key"
        ] = (
            private_key
            if private_key
            else "id_rsa.key"
        )

        updated_router[
            "port"
        ] = port

    # Хранилища (git, ftp, smb, ...)
    old_storage = old_router.get("storage") or {}
    updated_router["storage"] = _parse_storage_form(form, old_storage)
    
    routers_list[
        index
    ] = updated_router

    target_dir = (
        BASE_DIR / "plugins"
    )

    for p_folder in (
        target_dir.iterdir()
    ):

        if (
            p_folder.is_dir()
            and
            p_folder.name.lower()
            == device_type.lower()
        ):

            json_file = (
                p_folder
                / "routers.json"
            )

            with open(
                json_file,
                "w",
                encoding="utf-8",
            ) as f:

                json.dump(
                    routers_list,
                    f,
                    ensure_ascii=False,
                    indent=4,
                )

            break

    logging.info(
        "⚙ Обновлены настройки "
        "устройства [%s] #%s: %s",
        device_type,
        index,
        ip,
    )
    
    invalidate_status_cache()

    return RedirectResponse(
        url="/",
        status_code=303,
    )


# =========================================================
# RUN DIRECTLY
# =========================================================

if __name__ == "__main__":

    import uvicorn

    uvicorn.run(
        "web.server:app",
        host="127.0.0.1",
        port=8080,
        reload=False,
        log_config=None,
    )