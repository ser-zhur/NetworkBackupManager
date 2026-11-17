import os
import json
import copy
import logging
import threading
import secrets
import hashlib
import base64
import time
import urllib.parse

from pathlib import Path
from datetime import datetime
from contextlib import asynccontextmanager
from core import rclone as rclone_mod

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
    BACKUPS_DIR,
    DATA_DIR,
    LOGS_DIR,
    WEB_DIR,
    PLUGINS_DIR,
    AUTH_FILE,
    STATE_FILE,
    CONFIG_FILE,
    save_json,
    load_json,
    resolve_backup_path,
)

from core.logger import (
    setup_logging,
    set_device_context,
    clear_device_context,
    set_connection_debug,
    clear_connection_debug,
    device_log_tag,
)

from core.git_storage import (
    GitStorage,
    is_available as git_available,
)

from core.maintenance import (
    apply_retention,
    mark_deleted as maint_mark_deleted,
    unmark_deleted as maint_unmark_deleted,
    sweep_repo,
    sweep_all_devices,
)

# =========================================================
# LOGGING
# =========================================================

setup_logging()


# =========================================================
# BACKUP STATE
# =========================================================
#
# Состояние живёт в core.backup_state — общее с
# планировщиком, чтобы плановый прогон и ручные запуски
# видели друг друга.

from core.backup_state import (
    try_start_mass,
    finish_mass,
    try_start_device,
    finish_device,
    is_backup_running,
    is_backup_running_for,
    get_backup_status,
)

# =========================================================
# SYNC STATE (git → storages)
# =========================================================

sync_lock = threading.Lock()
active_syncs: set[str] = set()


def is_sync_running_for(device_id: str) -> bool:
    with sync_lock:
        return device_id in active_syncs


def get_sync_status() -> dict:
    with sync_lock:
        return {"devices": sorted(active_syncs)}


def sync_started(device_id: str) -> None:
    with sync_lock:
        active_syncs.add(device_id)


def sync_finished(device_id: str) -> None:
    with sync_lock:
        active_syncs.discard(device_id)


# =========================================================
# CORE
# =========================================================

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
    # debug=True отдаёт полный traceback (пути, фрагменты
    # кода и данных) любому клиенту. Ошибки и так пишутся
    # в app.log.
    debug=False,
)


# =========================================================
# CSRF: проверка источника изменяющих запросов
# =========================================================
#
# Авторизация — HTTP Basic: браузер сам подставляет
# сохранённые логин/пароль в ЛЮБОЙ запрос к нашему адресу,
# в том числе в форму, отправленную со стороннего сайта.
# Без проверки чужая страница могла бы удалить устройство
# или историю.
#
# Для POST/PUT/PATCH/DELETE сверяем Origin (или Referer,
# если Origin нет) с Host запроса. Браузеры всегда шлют
# Origin в кросс-сайтовых POST. Запросы без обоих
# заголовков (curl, скрипты) пропускаются — это не браузер,
# и CSRF к ним неприменим.

_CSRF_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})


def _csrf_source_ok(request: Request) -> bool:
    host = (request.headers.get("host") or "").lower()

    origin = request.headers.get("origin")
    if origin is not None:
        # "null" — sandbox/file:// и т.п.: источник неизвестен
        if origin.strip().lower() == "null":
            return False
        return urllib.parse.urlsplit(origin).netloc.lower() == host

    referer = request.headers.get("referer")
    if referer:
        return urllib.parse.urlsplit(referer).netloc.lower() == host

    return True


@app.middleware("http")
async def csrf_protect(request: Request, call_next):
    if (
        request.method not in _CSRF_SAFE_METHODS
        and not _csrf_source_ok(request)
    ):
        logging.warning(
            "CSRF: отклонён %s %s (Origin=%r, Referer=%r, Host=%r)",
            request.method,
            request.url.path,
            request.headers.get("origin"),
            request.headers.get("referer"),
            request.headers.get("host"),
        )
        return Response(
            "Запрос отклонён: отправлен с другого сайта.",
            status_code=403,
            media_type="text/plain; charset=utf-8",
        )
    return await call_next(request)


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
            "label": "Актуальный",
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
# PASSWORD (verify only — hash делается в main.py)
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

    device_type приводится к нижнему регистру: ключи в
    plugin_manager._plugins хранятся в lower-case (см.
    discover_and_load_plugins), а URL может прийти с любым
    регистром — например, /logs/device/Mikrotik/0.
    """
    if not plugin_manager:
        return None

    device_type = device_type.lower()

    if device_type not in plugin_manager._plugins:
        return None

    routers = plugin_manager._plugins[device_type].get("routers", [])
    if not (0 <= index < len(routers)):
        return None

    return device_log_tag(device_type, routers[index])


# =========================================================
# LOG READER
# =========================================================

def read_log_tail(
    file_path: Path,
    limit: str = "100",
    filter_tag: str | None = None,
    focus_date: str | None = None,
):

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
# SIZE UTILS
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
        enabled = (
            backend.always_enabled
            or backend.is_authority
            or bool(cfg.get("enabled"))
        )

        st = backends_state.get(backend.id) or {}

        if not enabled:
            status = "disabled"
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
            "always_enabled": backend.always_enabled,
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

        # always_enabled (git) — игнорируем значение из формы
        if backend.always_enabled:
            enabled = True

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


def _check_storage_conflicts(
    device_type: str,
    router: dict,
    exclude: dict | None = None,
) -> None:
    """
    HTTP 400, если включённое внешнее хранилище устройства
    пишет в тот же каталог того же сервера, что и другое
    устройство: имена файлов фиксированные (config.rsc, ...),
    и устройства перезаписывали бы друг друга.

    exclude — старая запись редактируемого устройства
    (с собой не сравниваем).
    """
    if storage_manager is None or not plugin_manager:
        return

    def _enabled_cfg(r: dict, backend) -> dict | None:
        cfg = (r.get("storage") or {}).get(backend.id)
        if not isinstance(cfg, dict) or not cfg.get("enabled"):
            return None
        return cfg

    def _host(r: dict) -> str:
        return r.get("host") or r.get("ip") or r.get("address") or ""

    for backend in storage_manager.all():
        if backend.is_authority:
            continue

        cfg = _enabled_cfg(router, backend)
        if cfg is None:
            continue

        key = backend.target_key(
            cfg,
            host=_host(router),
            name=router.get("name") or "",
            device_type=device_type,
        )
        if key is None:
            continue

        for other_type, plugin_data in plugin_manager._plugins.items():
            for other in plugin_data.get("routers", []):
                if other is exclude or other is router:
                    continue
                other_cfg = _enabled_cfg(other, backend)
                if other_cfg is None:
                    continue
                other_key = backend.target_key(
                    other_cfg,
                    host=_host(other),
                    name=other.get("name") or "",
                    device_type=other_type,
                )
                if other_key == key:
                    other_label = other.get("name") or _host(other)
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            f"{backend.name}: каталог «{key[2]}» на этом "
                            f"сервере уже использует устройство "
                            f"«{other_label}». Файлы перезаписывали бы "
                            f"друг друга. Укажите другой каталог, "
                            f"например с подстановкой {{host}}."
                        ),
                    )


def _storage_backends_for_template():
    if storage_manager is None:
        return []
    return storage_manager.all()


def _storage_defaults_for_template():
    if storage_manager is None:
        return {}
    return storage_manager.defaults_for_new_router()


# =========================================================
# PATH UTILS
# =========================================================

def _raise_routers_save_error(where: str, exc: Exception):
    """Лог + HTTP 500 при неудачной записи routers.json."""
    logging.error(
        "%s: не удалось сохранить routers.json: %s",
        where, exc, exc_info=True,
    )
    raise HTTPException(
        500, f"Не удалось сохранить routers.json: {exc}",
    )


def _resolve_backup_path(backup_path) -> Path:
    # Единое правило — core.context.resolve_backup_path
    return resolve_backup_path(backup_path)

# =========================================================
# TAGS
# =========================================================

def _parse_tags(raw) -> list[str]:
    """
    Парсит строку тегов из формы в нормализованный список.

    Правила:
      • разделитель — запятая;
      • trim пробелов;
      • отсев пустых;
      • длина тега обрезается до 32 символов;
      • максимум 20 тегов на устройство;
      • дубликаты сравниваются без учёта регистра;
      • регистр сохраняется как ввёл пользователь.
    """
    if not raw:
        return []

    seen = set()
    result = []

    for part in str(raw).split(","):
        tag = part.strip()
        if not tag:
            continue
        if len(tag) > 32:
            tag = tag[:32]
        key = tag.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(tag)
        if len(result) >= 20:
            break

    return result


def _safe_router_tags(router: dict) -> list[str]:
    """
    Возвращает гарантированно список строк — даже если
    пользователь вручную испортил routers.json (например,
    записал строку или число вместо массива).
    """
    raw = router.get("tags")
    if not isinstance(raw, list):
        return []
    return [t for t in raw if isinstance(t, str) and t.strip()]


def _all_existing_tags() -> list[str]:
    """
    Уникальные теги по всем устройствам — для datalist
    в формах добавления/редактирования.

    Дубликаты сравниваются case-insensitive; сохраняется
    регистр первого встреченного варианта. Результат
    отсортирован по алфавиту (case-insensitive).
    """
    if not plugin_manager:
        return []

    seen: dict[str, str] = {}
    for plugin_data in plugin_manager._plugins.values():
        for router in plugin_data.get("routers", []):
            for tag in _safe_router_tags(router):
                tag = tag.strip()
                key = tag.lower()
                if key not in seen:
                    seen[key] = tag

    return sorted(seen.values(), key=lambda t: t.lower())

def _iter_device_backup_paths():
    """
    Итератор по всем устройствам.

    yield (device_id, device_type, index, router, backup_path)

    Пропускает устройства без backup_path. backup_path —
    абсолютный Path. Гарантированно не падает, если
    plugin_manager=None.
    """
    if not plugin_manager:
        return

    for device_type, plugin_data in plugin_manager._plugins.items():
        routers = plugin_data.get("routers", [])
        for idx, router in enumerate(routers):
            raw = router.get("backup_path", "")
            if not raw:
                continue

            yield (
                f"{device_type}/{idx}",
                device_type,
                idx,
                router,
                _resolve_backup_path(raw),
            )
            
            
def classify_device(age_days, last_attempt_ok) -> str:
    """
    ok / warn / fail / none.

    age_days — давность последней УСПЕШНОЙ СИНХРОНИЗАЦИИ
    (state.json.last_run), а НЕ давность последнего
    git-коммита.

    Если с устройством успешно связались недавно, но
    изменений не нашли — коммита нет, однако устройство
    «Актуально»: оно исправно опрашивается.

    Порог ok — 14 дней (включительно).
    """
    if last_attempt_ok is False:
        return "fail"
    if age_days is None:
        return "none"
    if age_days <= 14:
        return "ok"
    if age_days <= 30:
        return "warn"
    return "fail"


# =========================================================
# DEVICE PAYLOAD
# =========================================================

def _build_device_payload(
    device_type: str,
    index: int,
    router: dict,
) -> dict:
    """Собирает всё, что нужно для карточки устройства."""

    b_path = router.get("backup_path", "backups/")
    b_path_abs = _resolve_backup_path(b_path)

    # ---- История из git ----
    git_commits = []
    if git_available():
        try:
            gs = GitStorage(b_path_abs)
            if gs.is_initialized():
                git_commits = gs.get_history(limit=200)

                # Заметки хранятся отдельно от коммитов
                # (в .git/nbm_notes.json). Подмешиваем их
                # в те же словари, что уже пойдут в шаблон,
                # чтобы не тянуть notes в контекст отдельно.
                notes = gs.get_notes()
                for c in git_commits:
                    c["note"] = notes.get(c["sha"], "")
        except Exception as exc:
            logging.warning(
                "git: не удалось прочитать историю %s/%s: %s",
                device_type, index, exc,
            )

    # ---- Метрики из git (последнее изменение конфига) ----
    #
    # git хранит только успешные версии — коммит создаётся
    # лишь когда конфиг реально изменился. Это делает
    # git-историю источником для last_success и backup_count,
    # но НЕ для статуса «свежести» устройства: если конфиг
    # не менялся, коммитов не будет, хотя устройство
    # исправно опрашивается.
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
    else:
        last_date = None
        age_days = None
        backup_count = 0

    # ---- Последняя попытка (state.json) ----
    #
    # state.json пишется plugin_manager._save_backend_state()
    # при каждом запуске бэкапа — вне зависимости от того,
    # создан коммит или нет. Это позволяет видеть результат
    # как удачных попыток, так и неудачных, которых в
    # git-истории не будет.
    #
    # Именно этот timestamp является правильным индикатором
    # «свежести» устройства: git-коммит не создаётся, если
    # изменений нет, но контакт с роутером был и завершился
    # успешно.
    #
    # last_attempt_ok:
    #   None  — попыток ещё не было
    #   True  — все включённые backend'ы вернули ok=True
    #           (skipped тоже ok=True: «нет изменений» = успех)
    #   False — хотя бы один backend вернул ok=False
    device_state = _load_device_state(f"{device_type}/{index}")

    last_run_str = device_state.get("last_run") or ""
    backends_state = device_state.get("backends") or {}

    if last_run_str:
        last_attempt_date = last_run_str

        try:
            last_attempt_dt = datetime.strptime(
                last_run_str, "%Y-%m-%d %H:%M:%S"
            )
            last_attempt_age_days = (
                datetime.now() - last_attempt_dt
            ).days
        except ValueError:
            last_attempt_age_days = None

        if backends_state:
            last_attempt_ok = all(
                bool(b.get("ok"))
                for b in backends_state.values()
            )
        else:
            # Бэкенды не задействованы, но попытка была —
            # считаем её успешной.
            last_attempt_ok = True
    else:
        last_attempt_date = None
        last_attempt_age_days = None
        last_attempt_ok = None

    total_bytes = _get_dir_size_excluding_git(b_path_abs)
    total_size = format_size(total_bytes)

    if backup_count > 0 and total_bytes > 0:
        avg_size = format_size(total_bytes // backup_count)
    else:
        avg_size = "—"

    # ---- Статус ----
    #
    # Приоритет — давность последней попытки: именно она
    # отражает, опрашивается ли устройство прямо сейчас.
    # Если state.json пуст (legacy-установки до появления
    # этого поля) — откатываемся на возраст git-коммита.
    if last_attempt_age_days is not None:
        status = classify_device(
            last_attempt_age_days, last_attempt_ok
        )
    else:
        status = classify_device(age_days, last_attempt_ok)

    ip_addr = (
        router.get("host")
        or router.get("ip")
        or router.get("address", "Не указан")
    )


    git_recent = []
    for c in git_commits[:3]:
        # .get() вместо [] — на случай старой/повреждённой
        # репы, где очередной коммит может не содержать
        # какого-то поля. Иначе получаем KeyError → 500,
        # и пользователь видит только «Не удалось загрузить».
        sha = c.get("sha", "")
        git_recent.append({
            "sha":     sha,
            "short":   c.get("short") or sha[:7],
            "message": c.get("message", ""),
            "time":    c.get("time", ""),
            "files":   c.get("files", []) or [],
            "note":    c.get("note", ""),
        })

    # ---- Версия прошивки ----
    # Плагины не заполняют это поле автоматически —
    # ожидаем, что пользователь вводит его вручную
    # (см. add.html / edit.html) или что оно появится
    # в routers.json из внешнего источника.
    version = (
        router.get("version")
        or router.get("os_version")
        or router.get("firmware")
        or "—"
    )

    # ---- Включён ли git-бэкенд для этого устройства ----
    # Логика зеркалит StorageManager.enabled_for():
    #   • git помечен always_enabled → включён всегда;
    #   • но если бэкенд не загрузился (например, dulwich
    #     не установлен и manager.errors() его не собрал),
    #     честнее показать «выключено», чем рисовать пустую
    #     историю как будто всё в порядке.
    git_backend = (
        storage_manager.get("git")
        if storage_manager is not None
        else None
    )
    if git_backend is None:
        git_enabled = False
    elif git_backend.always_enabled:
        git_enabled = True
    else:
        git_cfg = (router.get("storage") or {}).get("git") or {}
        git_enabled = bool(git_cfg.get("enabled"))

    # ---- Данные для диалога «Удалить устройство» ----

    # Можем ли автоматически удалить SSH-ключ?
    # Только если он задан относительным путём (лежит
    # внутри plugins/<type>/) И не используется другими
    # устройствами того же типа.
    _key_name = (
        router.get("ssh_key")
        or router.get("private_key")
        or ""
    )

    _key_path = _local_key_path(device_type, _key_name)
    ssh_key_local = _key_path is not None
    ssh_key_path = ""
    ssh_key_shared = False
    ssh_key_shared_with: list[str] = []

    if ssh_key_local:
        ssh_key_path = str(_key_path)

        # Проверяем, используют ли другие устройства
        # этого типа тот же ключ.
        if plugin_manager and device_type in plugin_manager._plugins:
            all_routers = (
                plugin_manager._plugins[device_type]
                .get("routers", [])
            )
            for i, other in enumerate(all_routers):
                if i == index:
                    continue
                other_key = (
                    other.get("ssh_key")
                    or other.get("private_key")
                    or ""
                )
                if other_key == _key_name:
                    ssh_key_shared = True
                    shared_name = (
                        other.get("name")
                        or other.get("host")
                        or f"#{i}"
                    )
                    ssh_key_shared_with.append(shared_name)

    # ---- Строки хранилищ ----
    # Вызывается дважды (в payload и для вычисления
    # has_syncable_storages), поэтому кэшируем в переменную.
    storage_rows = _build_storage_rows(
        router,
        device_state,
    )

    device_id = f"{device_type}/{index}"

    return {
        "id": device_id,
        "type": device_type,
        "index": index,
        "name": router.get("name") or "",
        "tags": _safe_router_tags(router),
        "pinned": bool(router.get("pinned")),
        "host": ip_addr,
        "ssh_key_local": ssh_key_local,
        "ssh_key_path": ssh_key_path,
        "ssh_key_shared": ssh_key_shared,
        "ssh_key_shared_with": ssh_key_shared_with,
        "port": router.get("port", 22),
        "status": status,
        "last_success": last_date,
        "last_success_age_days": age_days,
        "last_attempt": last_attempt_date,
        "last_attempt_ok": last_attempt_ok,
        "last_attempt_age_days": last_attempt_age_days,
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
        "git_enabled": git_enabled,
        "git_total": len(git_commits),
        "git_recent": git_recent,
        "storage_rows": storage_rows,

        # ---- Синхронизация хранилищ ----
        # True, если включён хотя бы один non-git backend.
        # Только git — синхронизировать некуда, кнопка
        # в UI не показывается.
        "has_syncable_storages": any(
            r["enabled"] and r["id"] != "git"
            for r in storage_rows
        ),
        "sync_in_progress": is_sync_running_for(device_id),
        "backup_in_progress": is_backup_running_for(device_id),
    }

# =========================================================
# DEVICE SUMMARY (для списка)
# =========================================================
#
# Лёгкая версия payload для левой колонки главной страницы.
# В отличие от _build_device_payload НЕ читает .git, НЕ делает
# rglob по каталогу бэкапов и НЕ загружает notes. Работает
# только с уже загруженным state.json (state_all).
#
# Используется только в read_root(). api_device_detail
# по-прежнему вызывает _build_device_payload — карточка
# устройства получает полный набор данных.

def _get_git_commit_count(backup_path: Path) -> int:
    """
    Быстрый счётчик коммитов (O(1), читает HEAD, не листает
    всю историю). Используется в _build_device_summary как
    fallback, если в state.json ещё нет total_commits.
    """
    if not git_available():
        return 0

    try:
        gs = GitStorage(backup_path)
        if not gs.is_initialized():
            return 0
        return gs.get_commit_count(include_marked=False)
    except Exception:
        return 0

def _count_backend_errors(router: dict, device_state: dict) -> int:
    """
    Количество включённых backend'ов у устройства,
    которые в последний прогон вернули ok=False.

    Используется для индикатора «⚠N сломанных хранилищ»
    в списке устройств.
    """
    if storage_manager is None:
        return 0

    storage_cfg = router.get("storage") or {}
    backends_state = device_state.get("backends") or {}

    count = 0
    for backend in storage_manager.all():
        cfg = storage_cfg.get(backend.id) or {}
        enabled = (
            backend.always_enabled
            or backend.is_authority
            or bool(cfg.get("enabled"))
        )
        if not enabled:
            continue
        st = backends_state.get(backend.id) or {}
        if st.get("ok") is False and not st.get("skipped"):
            count += 1
    return count


def _is_overdue(age_days) -> bool:
    """
    True, если устройство давно не опрашивалось.
    Порог — 2× интервал планировщика (если он включён)
    либо 7 дней (если планировщик выключен).
    """
    if age_days is None:
        return False
    try:
        cfg = scheduler.config
        if not cfg.get("enabled"):
            return age_days > 7
        if cfg.get("mode") == "interval":
            interval_days = max(1, int(cfg.get("interval_hours", 24)) // 24)
            return age_days > 2 * interval_days
        # daily
        return age_days > 2
    except Exception:
        return False

def _build_device_summary(
    device_type: str,
    index: int,
    router: dict,
    state_all: dict,
) -> dict:
    """
    Минимум для строки в списке устройств.

    Все данные — из router и уже загруженного state_all.
    Только fallback для backup_count читает .git (быстро).
    """
    device_id = f"{device_type}/{index}"
    st = state_all.get(device_id) or {}
    if not isinstance(st, dict):
        st = {}

    # ---- Последняя попытка (state.json) ----
    last_run_str = st.get("last_run") or ""
    backends_state = st.get("backends") or {}

    if last_run_str:
        try:
            last_attempt_dt = datetime.strptime(
                last_run_str, "%Y-%m-%d %H:%M:%S"
            )
            last_attempt_age_days = (
                datetime.now() - last_attempt_dt
            ).days
        except ValueError:
            last_attempt_age_days = None

        if backends_state:
            last_attempt_ok = all(
                bool(b.get("ok"))
                for b in backends_state.values()
            )
        else:
            last_attempt_ok = True
    else:
        last_attempt_age_days = None
        last_attempt_ok = None

    status = classify_device(
        last_attempt_age_days,
        last_attempt_ok,
    )

    # ---- backup_count ----
    # Источник 1: total_commits из state.json (появится после
    #             правки _save_backend_state — см. Вариант A).
    # Источник 2 (fallback): быстрый get_commit_count() по git.
    # Источник 3 (если git недоступен): 0.
    total_commits = st.get("total_commits")
    if isinstance(total_commits, int):
        backup_count = total_commits
    else:
        raw_path = router.get("backup_path", "")
        if raw_path:
            backup_path = _resolve_backup_path(raw_path)
            backup_count = _get_git_commit_count(backup_path)
        else:
            backup_count = 0

    ip_addr = (
        router.get("host")
        or router.get("ip")
        or router.get("address", "Не указан")
    )

    return {
        "id": device_id,
        "type": device_type,
        "index": index,
        "name": router.get("name") or "",
        "host": ip_addr,
        "tags": _safe_router_tags(router),
        "pinned": bool(router.get("pinned")),       # пригодится в этапе 4
        "status": status,
        "last_attempt": last_run_str or None,
        "last_attempt_ok": last_attempt_ok,
        "last_attempt_age_days": last_attempt_age_days,
        "backup_count": backup_count,
        "max_backups": router.get("max_backups", 7),
        "backup_in_progress": is_backup_running_for(device_id),
        "sync_in_progress": is_sync_running_for(device_id),

        # ---- Доменные индикаторы ----
        # 1. Никогда не было ни одной попытки бэкапа.
        "never_backed_up": (
            not last_run_str
            and backup_count == 0
        ),
        # 2. Сколько включённых backend'ов в ошибке (для ⚠N).
        "backends_error_count": _count_backend_errors(
            router, st,
        ),
        # 3. Флаг «давно не опрашивалось» — жёлтый фон.
        #    Считаем «давно» = 2× интервал планировщика.
        #    Если планировщик выключен — не подсвечиваем.
        "overdue": _is_overdue(
            last_attempt_age_days,
        ),
    }

# =========================================================
# STORAGE SYNC TASK
# =========================================================

def execute_storage_sync_task(
    device_type: str,
    index: int,
    user: str,
) -> None:
    device_id = f"{device_type}/{index}"

    # Тег как у бэкапа — иначе строки синхронизации не видны
    # в логе устройства (/logs/device/...).
    routers = (
        plugin_manager._plugins.get(device_type, {}).get("routers", [])
        if plugin_manager else []
    )
    set_device_context(
        device_log_tag(device_type, routers[index])
        if 0 <= index < len(routers)
        else device_id
    )
    sync_started(device_id)

    try:
        logging.info(
            "☁ Синхронизация хранилищ запущена: %s (от %s)",
            device_id, user,
        )

        if plugin_manager is None:
            logging.error(
                "☁ sync: plugin_manager не загружен"
            )
            return

        result = plugin_manager.sync_git_to_storages(
            device_type, index,
        )

        if result["ok"]:
            logging.info(
                "☁ Синхронизация завершена: %s",
                result["message"],
            )
        else:
            logging.error(
                "☁ Синхронизация прервана: %s",
                result["message"],
            )

        for err in result.get("errors", [])[:10]:
            logging.error("☁ sync: %s", err)

    except Exception as exc:
        logging.error(
            "☁ sync: исключение: %s", exc, exc_info=True,
        )
    finally:
        sync_finished(device_id)
        clear_device_context()

# =========================================================
# BACKUP TASKS
# =========================================================

def execute_device_backup_task(
    device_type: str,
    index: int,
    secure_router: dict,
    original: dict | None = None,
):
    # original — объект роутера из списка в памяти. Нужен,
    # чтобы служебные поля (ros_version) записать именно
    # этому устройству, даже если индекс сдвинулся.
    host = (
        secure_router.get("host")
        or secure_router.get("ip")
        or f"#{index}"
    )

    # Слот устройства уже занят в run_backup() через
    # try_start_device(); здесь только освобождаем его
    # в finally.
    device_id = f"{device_type}/{index}"

    debug_connection = bool(secure_router.get("debug_connection"))

    device_tag = device_log_tag(device_type, secure_router)
    set_device_context(device_tag)

    if debug_connection:
        set_connection_debug(device_tag)
        logging.info(
            "🐛 Debug подключения включён: %s/%s", device_type, host
        )

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
        
        # Если плагин определил служебные поля (ros_version),
        # переносим их в in-memory конфиг и routers.json.
        if plugin_manager is not None:
            try:
                plugin_manager.persist_router_service_fields(
                    device_type, index, secure_router,
                    original=original,
                )
            except Exception as exc:
                logging.warning(
                    "Не удалось сохранить служебные поля "
                    "роутера: %s", exc,
                )

        # created_files может быть [] — «нет изменений», успех.
        # Плагин, у которого что-то сломалось, обязан кинуть исключение.
        if created_files is None:
            raise RuntimeError(
                f"Плагин {device_type} вернул None вместо списка"
            )

        # Push в storage backends (git всегда, остальные — по конфигу).
        # Пустой список _push_to_storages обработает сам.
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
        if debug_connection:
            logging.info(
                "🐛 Debug подключения выключен: %s/%s",
                device_type, host,
            )
            clear_connection_debug()
        finish_device(device_id)
        clear_device_context()

def execute_all_backups_task():
    # Слот массового прогона уже занят в run_now_endpoint()
    # через try_start_mass(); здесь только освобождаем.
    try:

        if plugin_manager:

            plugin_manager.run_all_backups()

            # Автоочистка истории (пометка + опционально
            # sweep). Все ошибки логируются внутри.
            try:
                from core.maintenance import post_backup_cleanup
                post_backup_cleanup(plugin_manager)
            except Exception as cleanup_exc:
                logging.error(
                    "❌ [Фон] Ошибка автоочистки: %s",
                    cleanup_exc,
                    exc_info=True,
                )

    except Exception as exc:

        logging.error(
            "❌ [Фон] Ошибка массового "
            "бэкапа: %s",
            exc,
        )

    finally:

        finish_mass()


# =========================================================
# MAIN PAGE
# =========================================================

@app.get("/", response_class=HTMLResponse)
def read_root(
    request: Request,
    user: str = Depends(require_auth),
):
    # ---- Лёгкий список ----
    #
    # state.json читаем ОДИН раз и передаём во все summary.
    # Никаких _build_device_payload в цикле — только summary.
    state_all = load_json(STATE_FILE, {})
    if not isinstance(state_all, dict):
        state_all = {}

    devices = []

    if plugin_manager and hasattr(plugin_manager, "_plugins"):
        for device_type, plugin_data in plugin_manager._plugins.items():
            routers = plugin_data.get("routers", [])
            for index, router in enumerate(routers):
                devices.append(
                    _build_device_summary(
                        device_type, index, router, state_all,
                    )
                )

    selected_id = request.query_params.get("selected", "")

    if not selected_id:
        selected_id = request.cookies.get("nbm-selected-device", "")
        if selected_id:
            selected_id = urllib.parse.unquote(selected_id)

    # ---- Найти выбранный summary ----
    selected_summary = None
    if devices:
        if selected_id:
            for d in devices:
                if d["id"] == selected_id:
                    selected_summary = d
                    break
        if selected_summary is None:
            selected_summary = devices[0]

    # ---- Полный payload ТОЛЬКО для выбранного ----
    #
    # Один _build_device_payload за запрос вместо N.
    # Шаблоны (index.html → _device_detail.html) ожидают
    # именно полный payload в переменной `selected` — поэтому
    # ниже он и подставляется.
    selected = None
    if selected_summary is not None and plugin_manager:
        try:
            routers = plugin_manager._plugins[
                selected_summary["type"]
            ].get("routers", [])
            idx = selected_summary["index"]
            if 0 <= idx < len(routers):
                selected = _build_device_payload(
                    selected_summary["type"], idx, routers[idx],
                )
        except Exception as exc:
            logging.error(
                "read_root: не удалось построить payload %s: %s",
                selected_summary["id"], exc, exc_info=True,
            )

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
# API: DEVICE DETAIL
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

    try:
        payload = _build_device_payload(device_type, index, routers[index])
    except Exception as exc:
        logging.error(
            "api_device_detail(%s/%s): %s",
            device_type, index, exc, exc_info=True,
        )
        raise HTTPException(
            status_code=500,
            detail=f"Ошибка построения карточки устройства: {exc}",
        )

    return templates.TemplateResponse(
        request,
        "_device_detail.html",
        {"d": payload},
    )


# =========================================================
# API: SYSTEM INFO
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

@app.get("/api/backup-status")
def api_backup_status(user: str = Depends(require_auth)):
    """
    Текущее состояние бэкапов и синхронизаций.

    Ответ:
        {
          "running": true,
          "mass": false,
          "devices": ["mikrotik/0", "mikrotik/2"],
          "syncing": ["mikrotik/1"]
        }

    mass=true  — массовый прогон, все устройства заняты.
    devices    — device_id одиночных бэкапов.
    syncing    — device_id идущих синхронизаций хранилищ.

    Фронтенд использует оба списка для дизейбла кнопок
    «Запустить бэкап» и «Синхронизировать хранилища».
    """
    return {
        **get_backup_status(),
        "syncing": get_sync_status()["devices"],
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

    old_cfg: dict = {}
    if device_type and index >= 0:
        try:
            routers = plugin_manager._plugins[device_type].get("routers", [])
            if 0 <= index < len(routers):
                old_cfg = (routers[index].get("storage") or {}).get(backend_id) or {}
        except Exception:
            old_cfg = {}

    cfg: dict = {}
    for field in backend.fields:
        value = data.get(field.name, "")

        if field.type == "password":
            if value:
                cfg[field.name] = value
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

    if is_service_registered():
        return _render_service_action(
            request,
            "Регистрация службы",
            "Служба уже зарегистрирована.",
            ok=True,
        )

    if is_service_process():
        return _render_service_action(
            request,
            "Регистрация службы",
            "Процесс уже запущен в режиме службы. "
            "Регистрация не требуется.",
            ok=False,
        )

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

    ok = relaunch_elevated(["--install-service"])

    if not ok:
        return _render_service_action(
            request,
            "Регистрация службы",
            "Не удалось запросить права администратора.",
            ok=False,
        )

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

    if not is_service_registered():
        return _render_service_action(
            request,
            "Удаление службы",
            "Служба не зарегистрирована.",
            ok=True,
        )

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

            "all_existing_tags":
                _all_existing_tags(),
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
    device_name = (form.get("name") or "").strip()[:100]
    ip = (form.get("ip") or "").strip()
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

        # Сразу абсолютный — как после загрузки routers.json.
        # Иначе до перезапуска плагин, git и UI разрешали бы
        # относительный путь от разных каталогов.
        "backup_path":
            str(resolve_backup_path(backup_path)),

        "max_backups":
            int(max_backups),
    }

    # Название — опционально. Не пишем пустую строку
    # в routers.json, чтобы файл оставался компактным.
    if device_name:
        new_router["name"] = device_name

    if (
        device_type.lower()
        == "mikrotik"
    ):

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
    
    # Теги — опционально. Не пишем пустой массив в routers.json,
    # чтобы файл оставался компактным.
    tags = _parse_tags(form.get("tags") or "")
    if tags:
        new_router["tags"] = tags
    
    if form.get("debug_connection") == "true":
        new_router["debug_connection"] = True

    routers = plugin_manager._plugins[device_type]["routers"]

    with plugin_manager.routers_lock:
        _check_storage_conflicts(device_type, new_router)
        routers.append(new_router)
        try:
            plugin_manager.save_routers(device_type)
        except Exception as exc:
            # Откат: в памяти не должно остаться устройства,
            # которого нет в файле.
            routers.remove(new_router)
            _raise_routers_save_error("save-router", exc)

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
# SYNC STORAGES (git → все non-authority backend'ы)
# =========================================================

@app.post("/sync-storages/{device_type}/{index}")
def sync_storages_endpoint(
    request: Request,           # <--- ДОБАВЛЕНО
    device_type: str,
    index: int,
    background_tasks: BackgroundTasks,
    user: str = Depends(require_auth),
):
    device_id = f"{device_type}/{index}"

    # Не запускаем параллельно с бэкапом или другим sync
    if (
        is_sync_running_for(device_id)
        or is_backup_running_for(device_id)
    ):
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return Response(status_code=204)
        return RedirectResponse(
            url=f"/?selected={device_id}",
            status_code=303,
        )

    if not plugin_manager or device_type not in plugin_manager._plugins:
        raise HTTPException(404, "Плагин не найден")

    routers = plugin_manager._plugins[device_type].get(
        "routers", []
    )
    if not (0 <= index < len(routers)):
        raise HTTPException(404, "Устройство не найдено")

    background_tasks.add_task(
        execute_storage_sync_task,
        device_type, index, user,
    )

    logging.info(
        "☁ Запрошена синхронизация хранилищ для %s (от %s)",
        device_id, user,
    )

    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return Response(status_code=204)

    return RedirectResponse(
        url=f"/?selected={device_id}",
        status_code=303,
    )
    
# =========================================================
# SINGLE BACKUP
# =========================================================

@app.post(
    "/run-progress/{device_type}/{index}"
)
def run_backup(
    device_type: str,
    request: Request,
    index: int,
    background_tasks:
        BackgroundTasks,
    user: str = Depends(
        require_auth
    ),
):
    device_id = f"{device_type}/{index}"
    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"

    # Блокируем запуск, если:
    #   • занято именно это устройство;
    #   • идёт массовый прогон;
    #   • идёт синхронизация хранилищ этого устройства
    if (
        is_backup_running_for(device_id)
        or is_sync_running_for(device_id)
    ):
        if is_ajax:
            return Response(status_code=204)
        return RedirectResponse(
            url=f"/logs/device/{device_type}/{index}",
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
        if is_ajax:
            return Response(status_code=404)
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

    # Плагин и git получают абсолютный путь по общему
    # правилу (относительный — от BASE_DIR), а не разрешают
    # его каждый по-своему.
    if secure_router.get("backup_path"):
        secure_router["backup_path"] = str(
            resolve_backup_path(secure_router["backup_path"])
        )

    # Атомарно проверяем и занимаем устройство. Проверка
    # выше (is_backup_running_for) — только быстрый ответ
    # UI; без этого захвата два запроса подряд могли оба
    # пройти её до старта фоновой задачи.
    if not try_start_device(device_id):
        if is_ajax:
            return Response(status_code=204)
        return RedirectResponse(
            url=f"/logs/device/{device_type}/{index}",
            status_code=303,
        )

    background_tasks.add_task(
        execute_device_backup_task,
        device_type,
        index,
        secure_router,
        router,
    )

    if is_ajax:
        return Response(status_code=204)
    return RedirectResponse(
        url=f"/logs/device/{device_type}/{index}",
        status_code=303,
    )

# =========================================================
# ALL BACKUPS
# =========================================================

@app.post(
    "/run-now"
)
def run_now_endpoint(
    request: Request,
    background_tasks:
        BackgroundTasks,
    user: str = Depends(
        require_auth
    ),
):

    # Атомарно: если уже идёт любой бэкап (в т.ч. плановый
    # из планировщика) — новый массовый прогон не стартует.
    if try_start_mass():

        background_tasks.add_task(
            execute_all_backups_task
        )

    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return Response(status_code=204)
    return RedirectResponse(
        url="/logs/main",
        status_code=303,
    )

# =========================================================
# TOGGLE PIN
# =========================================================

@app.post("/toggle-pin/{device_type}/{index}")
def toggle_pin(
    request: Request,
    device_type: str,
    index: int,
    user: str = Depends(require_auth),
):
    """
    Переключает флаг pinned у устройства и сохраняет routers.json.

    Возвращает редирект на Referer (чтобы пользователь остался
    на той же странице с теми же фильтрами).
    """
    device_type = device_type.lower()

    if not plugin_manager or device_type not in plugin_manager._plugins:
        raise HTTPException(404, "Плагин не найден")

    routers = plugin_manager._plugins[device_type].get("routers", [])
    if not (0 <= index < len(routers)):
        raise HTTPException(404, "Устройство не найдено")

    with plugin_manager.routers_lock:
        router = routers[index]
        was_pinned = bool(router.get("pinned"))

        # 1. Вычисляем новое состояние
        is_pinned = not was_pinned

        # 2. Обновляем словарь
        if is_pinned:
            router["pinned"] = True
        else:
            # Не мусорим в routers.json, если сняли закрепление
            router.pop("pinned", None)

        # 3. Сохраняем в routers.json (атомарно)
        try:
            plugin_manager.save_routers(device_type)
        except Exception as exc:
            if was_pinned:
                router["pinned"] = True
            else:
                router.pop("pinned", None)
            _raise_routers_save_error("toggle-pin", exc)

    # 4. Логируем, используя безопасную переменную is_pinned
    logging.info(
        "📌 Устройство %s/%s %s (запрос от %s)",
        device_type, index,
        "закреплено" if is_pinned else "откреплено",
        user,
    )

    # 5. Редирект обратно на страницу, с которой пришёл запрос
    referer = request.headers.get("referer")
    if referer and referer.startswith(str(request.base_url)):
        return RedirectResponse(url=referer, status_code=303)

    return RedirectResponse(
        url=f"/?selected={device_type}/{index}",
        status_code=303,
    )
# =========================================================
# BACKUPS (legacy redirect)
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
    # Версионирование полностью в git.
    # Страница «Архивы» оставлена как редирект для совместимости
    # с закладками пользователей.
    return RedirectResponse(
        url=f"/git/{device_type}/{index}",
        status_code=307,
    )


# =========================================================
# SCHEDULER
# =========================================================

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
        url="/settings?tab=scheduler&message=saved",
        status_code=303,
    )


# =========================================================
# DOWNLOAD BACKUP (flat layout only)
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

    backup_path = resolve_backup_path(
        router.get("backup_path", "backups")
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
# GIT: HELPERS
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


# =========================================================
# GIT: HISTORY
# =========================================================

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
    total_marked = 0

    if initialized:
        try:
            # include_marked=True — показываем помеченные,
            # чтобы пользователь видел, что попадёт под sweep.
            commits = storage.get_history(
                limit=limit,
                include_marked=True,
            )
            notes = storage.get_notes()
            total_marked = len(storage.get_deleted_shas())
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
    name = router.get("name") or ""

    return templates.TemplateResponse(
        request,
        "git_history.html",
        {
            "device_type": device_type,
            "index": index,
            "host": host,
            "name": name,
            "backup_path": str(backup_path),
            "commits": commits,
            "total": len(commits),
            "total_marked": total_marked,
            "limit": limit,
            "git_available": available,
            "git_initialized": initialized,
            "sweep_error": request.query_params.get(
                "sweep_error", ""
            ),
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
# GIT: MARK DELETE
# =========================================================

@app.post(
    "/git/{device_type}/{index}/mark-delete",
)
async def git_mark_delete(
    device_type: str,
    index: int,
    request: Request,
    user: str = Depends(require_auth),
):
    """
    Помечает выбранные версии к удалению.

    Физического удаления не происходит — только запись
    sha в .git/nbm_deleted.json. Пользователь увидит
    изменения сразу (строки станут приглушёнными).
    Реальное удаление — через /sweep.
    """
    router, backup_path, storage = _get_git_storage_for(
        device_type, index
    )
    if router is None:
        raise HTTPException(404, "Устройство не найдено")

    if not git_available() or not storage.is_initialized():
        raise HTTPException(404, "Репозиторий git недоступен")

    form = await request.form()
    shas_raw = form.get("shas") or ""
    shas = {
        s.strip()
        for s in shas_raw.split(",")
        if s.strip()
    }

    if shas:
        added = maint_mark_deleted(backup_path, shas)
        logging.info(
            "git[%s/%s]: помечено к удалению %d версий "
            "(запрос от %s)",
            device_type, index, added, user,
        )

    return RedirectResponse(
        url=f"/git/{device_type}/{index}",
        status_code=303,
    )


@app.post(
    "/git/{device_type}/{index}/unmark-delete",
)
async def git_unmark_delete(
    device_type: str,
    index: int,
    request: Request,
    user: str = Depends(require_auth),
):
    """
    Снимает пометки. scope=all — очищает всё.
    Иначе — по списку shas в поле.
    """
    router, backup_path, storage = _get_git_storage_for(
        device_type, index
    )
    if router is None:
        raise HTTPException(404, "Устройство не найдено")

    if not git_available() or not storage.is_initialized():
        raise HTTPException(404, "Репозиторий git недоступен")

    form = await request.form()
    scope = form.get("scope") or "selected"

    if scope == "all":
        removed = maint_unmark_deleted(backup_path, None)
    else:
        shas_raw = form.get("shas") or ""
        shas = {
            s.strip()
            for s in shas_raw.split(",")
            if s.strip()
        }
        removed = maint_unmark_deleted(backup_path, shas) if shas else 0

    if removed:
        logging.info(
            "git[%s/%s]: снято %d пометок (запрос от %s)",
            device_type, index, removed, user,
        )

    return RedirectResponse(
        url=f"/git/{device_type}/{index}",
        status_code=303,
    )


@app.post(
    "/git/{device_type}/{index}/sweep",
)
async def git_sweep(
    device_type: str,
    index: int,
    user: str = Depends(require_auth),
):
    """
    Физически пересобирает историю: удаляет помеченные
    коммиты, перепривязывает parent-указатели, освобождает
    объекты через git gc (если git есть в PATH).
    """
    from urllib.parse import quote

    router, backup_path, storage = _get_git_storage_for(
        device_type, index
    )
    if router is None:
        raise HTTPException(404, "Устройство не найдено")

    if not git_available() or not storage.is_initialized():
        raise HTTPException(404, "Репозиторий git недоступен")

    ok, msg = sweep_repo(backup_path)

    if ok:
        logging.info(
            "git[%s/%s]: sweep — %s (запрос от %s)",
            device_type, index, msg, user,
        )
        return RedirectResponse(
            url=f"/git/{device_type}/{index}",
            status_code=303,
        )

    logging.error(
        "git[%s/%s]: sweep не удался — %s",
        device_type, index, msg,
    )

    return RedirectResponse(
        url=(
            f"/git/{device_type}/{index}"
            f"?sweep_error={quote(msg)}"
        ),
        status_code=303,
    )


@app.post(
    "/git/{device_type}/{index}/mark-older-than",
)
async def git_mark_older_than(
    device_type: str,
    index: int,
    request: Request,
    user: str = Depends(require_auth),
):
    """
    Помечает версии старше указанной даты.

    Параметры формы:
        older_than       — YYYY-MM-DD
        keep_last        — int, минимум сохраняемых
        keep_with_notes  — 'true' | отсутствует
    """
    router, backup_path, storage = _get_git_storage_for(
        device_type, index
    )
    if router is None:
        raise HTTPException(404, "Устройство не найдено")

    if not git_available() or not storage.is_initialized():
        raise HTTPException(404, "Репозиторий git недоступен")

    form = await request.form()

    older_than_str = (form.get("older_than") or "").strip()
    keep_last_str = (form.get("keep_last") or "20").strip()
    keep_with_notes = form.get("keep_with_notes") == "true"

    # Парсинг даты
    older_than_date = None
    if older_than_str:
        try:
            older_than_date = datetime.strptime(
                older_than_str, "%Y-%m-%d"
            )
        except ValueError:
            logging.warning(
                "git[%s/%s]: некорректная дата %r",
                device_type, index, older_than_str,
            )
            return RedirectResponse(
                url=f"/git/{device_type}/{index}",
                status_code=303,
            )

    if older_than_date is None:
        return RedirectResponse(
            url=f"/git/{device_type}/{index}",
            status_code=303,
        )

    try:
        keep_last = max(0, int(keep_last_str))
    except (TypeError, ValueError):
        keep_last = 20

    # Переводим дату в «дней назад» для apply_retention
    age_days = (datetime.now() - older_than_date).days
    if age_days < 1:
        age_days = 1

    marked = apply_retention(
        backup_path,
        {
            "keep_last": keep_last,
            "max_age_days": age_days,
            "keep_with_notes": keep_with_notes,
        },
    )

    logging.info(
        "git[%s/%s]: помечено %d версий старше %s "
        "(keep_last=%d, keep_with_notes=%s, запрос от %s)",
        device_type, index,
        len(marked), older_than_str,
        keep_last, keep_with_notes, user,
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

    # include_marked=True — пользователь может сравнить
    # версию, уже помеченную к удалению: например, чтобы
    # убедиться, что удаляет именно то, что хотел.
    history = storage.get_history(
        limit=500,
        include_marked=True,
    )
    
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

    filenames = sorted(set(commit_a["files"]) | set(commit_b["files"]))

    try:
        diffs = storage.get_diff(full_a, full_b, filenames)
    except Exception as exc:
        logging.error(
            "git: ошибка построения diff %s..%s: %s",
            sha_a[:7], sha_b[:7], exc, exc_info=True,
        )
        diffs = {"(ошибка)": f"Не удалось построить diff: {exc}"}

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
    name = router.get("name") or ""

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
            "name": name,
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

    # Нормализуем регистр: ключи _plugins — lower-case.
    # URL может прийти как /logs/device/Mikrotik/0.
    device_type = device_type.lower()

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
                
            "back_url":
                f"/?selected={device_type}/{index}",
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

            "all_existing_tags":
                _all_existing_tags(),
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

    device_name = (form.get("name") or "").strip()[:100]
    ip = (form.get("ip") or "").strip()
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

        # Сразу абсолютный — как после загрузки routers.json.
        # Иначе до перезапуска плагин, git и UI разрешали бы
        # относительный путь от разных каталогов.
        "backup_path":
            str(resolve_backup_path(backup_path)),

        "max_backups":
            int(max_backups),
    }
    
    # Обновление name: словарь собирается заново, поэтому
    # пустая строка означает «удалить имя» — ключ просто
    # не попадёт в новый dict, что и требуется.
    if device_name:
        updated_router["name"] = device_name

    if (
        device_type.lower()
        == "mikrotik"
    ):

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

    old_storage = old_router.get("storage") or {}
    updated_router["storage"] = _parse_storage_form(form, old_storage)

    # Теги: словарь пересобирается заново — если поле пустое,
    # ключ не попадёт, и старые теги удалятся (это ожидаемое
    # поведение «стёр в форме — стёр в файле»).
    tags = _parse_tags(form.get("tags") or "")
    if tags:
        updated_router["tags"] = tags

    if form.get("debug_connection") == "true":
        updated_router["debug_connection"] = True

    with plugin_manager.routers_lock:
        # Пока разбиралась форма, список мог измениться
        # (удалили соседнее устройство) — не перезаписываем
        # чужую запись.
        if not (
            0 <= index < len(routers_list)
            and routers_list[index] is old_router
        ):
            raise HTTPException(
                status_code=409,
                detail=(
                    "Список устройств изменился. "
                    "Откройте редактирование заново."
                ),
            )

        # Служебные поля (ros_version) берём на момент
        # записи — бэкап мог обновить их, пока шло
        # редактирование.
        for field in plugin_manager._SERVICE_FIELDS:
            if field in old_router:
                updated_router[field] = old_router[field]

        _check_storage_conflicts(
            device_type, updated_router, exclude=old_router,
        )

        routers_list[index] = updated_router
        try:
            plugin_manager.save_routers(device_type)
        except Exception as exc:
            routers_list[index] = old_router
            _raise_routers_save_error("update-router", exc)

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
# RETENTION
# =========================================================

def _collect_maintenance_stats() -> list:
    """
    Данные для таблицы на /retention.

    По каждому устройству: количество версий, сколько
    помечено, размер репы.
    """
    rows = []

    for device_id, device_type, idx, router, backup_path in (
        _iter_device_backup_paths()
    ):
        row = {
            "device_id": device_id,
            "device_type": device_type,
            "index": idx,
            "name": router.get("name") or "",
            "host": router.get("host") or router.get("ip") or "",
            "total": 0,
            "marked": 0,
            "size": "—",
            "repo_exists": backup_path.exists(),
            "has_git": False,
        }

        if not row["repo_exists"] or not git_available():
            rows.append(row)
            continue

        try:
            gs = GitStorage(backup_path)
            if gs.is_initialized():
                row["has_git"] = True
                # include_marked=True — «Версий: N» на
                # /retention совпадает с total на /git/...,
                # где помеченные тоже показываются.
                row["total"] = gs.get_commit_count(
                    include_marked=True
                )
                row["marked"] = len(gs.get_deleted_shas())
                row["size"] = format_size(
                    get_dir_size(backup_path)
                )
        except Exception as exc:
            logging.warning(
                "retention: не удалось прочитать %s: %s",
                device_id, exc,
            )

        rows.append(row)

    return rows

@app.post("/retention/save")
async def retention_save(
    user: str = Depends(require_auth),
    enabled: bool = Form(False),
    max_age_days: int = Form(365),
    keep_last: int = Form(20),
    keep_with_notes: bool = Form(False),
    auto_sweep: bool = Form(False),
):
    config = load_json(CONFIG_FILE, {})
    config["retention"] = {
        "enabled": bool(enabled),
        "max_age_days": max(1, int(max_age_days)),
        "keep_last": max(0, int(keep_last)),
        "keep_with_notes": bool(keep_with_notes),
        "auto_sweep": bool(auto_sweep),
    }

    try:
        save_json(CONFIG_FILE, config)
    except Exception as exc:
        logging.error(
            "retention: не удалось сохранить config.json: %s",
            exc, exc_info=True,
        )
        raise HTTPException(500, "Не удалось сохранить настройки")

    logging.info(
        "⚙ retention: настройки сохранены "
        "(enabled=%s, max_age=%d, keep_last=%d, "
        "keep_notes=%s, auto_sweep=%s, запрос от %s)",
        enabled, max_age_days, keep_last,
        keep_with_notes, auto_sweep, user,
    )

    return RedirectResponse(
        "/settings?tab=retention&message=saved",
        status_code=303,
    )


@app.post("/maintenance/prune-all")
def maintenance_prune_all(
    user: str = Depends(require_auth),
):
    """
    Помечает старые версии на всех устройствах по правилам
    из config.json. Физического удаления не делает.
    """
    config = load_json(CONFIG_FILE, {})
    retention = config.get("retention") or {}

    if not retention:
        return RedirectResponse(
            "/settings?tab=retention&message=no_config",
            status_code=303,
        )

    if not git_available():
        return RedirectResponse(
            "/settings?tab=retention&message=no_git",
            status_code=303,
        )

    total_marked = 0
    devices_processed = 0
    errors = 0

    for device_id, _dt, _idx, _router, backup_path in (
        _iter_device_backup_paths()
    ):
        if not backup_path.exists():
            continue

        try:
            marked = apply_retention(backup_path, retention)
            total_marked += len(marked)
            devices_processed += 1
        except Exception as exc:
            errors += 1
            logging.error(
                "retention: %s — %s",
                device_id, exc, exc_info=True,
            )

    logging.info(
        "retention: помечено %d версий на %d устройствах "
        "(ошибок: %d, запрос от %s)",
        total_marked, devices_processed, errors, user,
    )

    return RedirectResponse(
        f"/settings?tab=retention&message=pruned"
        f"&count={total_marked}"
        f"&devices={devices_processed}",
        status_code=303,
    )


@app.post("/maintenance/sweep-all")
def maintenance_sweep_all(
    user: str = Depends(require_auth),
):
    """
    Физически удаляет помеченные версии на всех устройствах.
    """
    if not git_available():
        return RedirectResponse(
            "/settings?tab=retention&message=no_git",
            status_code=303,
        )

    try:
        results = sweep_all_devices(plugin_manager)
    except Exception as exc:
        logging.error(
            "sweep-all: %s", exc, exc_info=True,
        )
        raise HTTPException(500, "Ошибка очистки")

    total_removed = 0
    devices_processed = 0
    errors = 0

    for device_id, (ok, msg) in results.items():
        if ok:
            devices_processed += 1
            if "Удалено коммитов:" in msg:
                try:
                    total_removed += int(
                        msg.split("Удалено коммитов:")[1]
                        .split(";")[0]
                        .strip()
                    )
                except (IndexError, ValueError):
                    pass
        else:
            errors += 1
            logging.warning(
                "sweep-all: %s — %s", device_id, msg,
            )

    logging.info(
        "sweep-all: удалено %d версий на %d устройствах "
        "(ошибок: %d, запрос от %s)",
        total_removed, devices_processed, errors, user,
    )

    return RedirectResponse(
        f"/settings?tab=retention&message=swept"
        f"&count={total_removed}"
        f"&devices={devices_processed}",
        status_code=303,
    )


@app.post("/maintenance/unmark-all")
def maintenance_unmark_all(
    user: str = Depends(require_auth),
):
    """Снимает все пометки удаления на всех устройствах."""
    if not git_available():
        return RedirectResponse(
            "/settings?tab=retention&message=no_git",
            status_code=303,
        )

    total_removed = 0

    for device_id, _dt, _idx, _router, backup_path in (
        _iter_device_backup_paths()
    ):
        if not backup_path.exists():
            continue

        try:
            removed = maint_unmark_deleted(backup_path, None)
            total_removed += removed
        except Exception as exc:
            logging.error(
                "unmark-all: %s — %s",
                device_id, exc, exc_info=True,
            )

    logging.info(
        "unmark-all: снято %d пометок (запрос от %s)",
        total_removed, user,
    )

    return RedirectResponse(
        f"/settings?tab=retention&message=unmarked&count={total_removed}",
        status_code=303,
    )


# =========================================================
# STORAGE SETTINGS PAGE
# =========================================================

@app.post("/storage-settings/save")
async def storage_settings_save(
    user: str = Depends(require_auth),
    rclone_path: str = Form(""),
):
    try:
        rclone_mod.set_rclone_path(rclone_path)
    except Exception as exc:
        logging.error(
            "storage-settings: не удалось сохранить: %s",
            exc, exc_info=True,
        )
        raise HTTPException(500, "Не удалось сохранить")

    logging.info(
        "storage-settings: путь к rclone сохранён "
        "(path=%r, запрос от %s)",
        rclone_path, user,
    )

    return RedirectResponse(
        "/settings?tab=storage&message=saved",
        status_code=303,
    )

# =========================================================
# DELETE DEVICE
# =========================================================

def _protected_dirs() -> list[Path]:
    """
    Каталоги, которые нельзя удалить ни сами, ни вместе
    с родителем: само приложение, его данные и общая папка
    бэкапов, домашний каталог, системные каталоги.
    """
    dirs = [
        BASE_DIR,
        DATA_DIR,
        LOGS_DIR,
        PLUGINS_DIR,
        WEB_DIR,
        BACKUPS_DIR,
        BASE_DIR / "storages",
        BASE_DIR / "core",
        BASE_DIR / "bin",
        BASE_DIR / "venv",
        Path("C:/Windows"),
        Path("C:/Program Files"),
        Path("C:/Program Files (x86)"),
        Path("/etc"),
        Path("/usr"),
        Path("/var"),
        Path("/opt"),
        Path("/home"),
    ]
    try:
        dirs.append(Path.home())
    except Exception:
        pass

    result = []
    for d in dirs:
        try:
            result.append(d.resolve())
        except OSError:
            pass
    return result


def _is_safe_to_delete(
    path: Path,
    exclude_router: dict | None = None,
) -> tuple[bool, str]:
    """
    Страховка перед rmtree каталога бэкапов устройства.

    Нельзя удалить:
      • корень файловой системы;
      • защищённый каталог (_protected_dirs) или любой его
        РОДИТЕЛЬ — иначе удалились бы и они;
      • каталог, который использует другое устройство, его
        родитель или подкаталог — удалились бы чужие бэкапы.

    exclude_router — удаляемое устройство (с собой не
    сравниваем; на момент вызова оно уже может быть убрано
    из списка).

    Возвращает (safe, reason).
    """
    if not path.exists():
        return True, "путь не существует"

    if not path.is_dir():
        return False, "путь не является директорией"

    try:
        resolved = path.resolve()
    except OSError as exc:
        return False, f"не удалось разрешить путь: {exc}"

    if resolved.parent == resolved:
        return False, f"корень файловой системы: {resolved}"

    for protected in _protected_dirs():
        if resolved == protected:
            return False, f"защищённый каталог: {resolved}"
        if protected.is_relative_to(resolved):
            return False, (
                f"каталог содержит защищённый каталог "
                f"{protected}"
            )

    if plugin_manager:
        for other_type, plugin_data in plugin_manager._plugins.items():
            for other in plugin_data.get("routers", []):
                if other is exclude_router:
                    continue
                raw = other.get("backup_path")
                if not raw:
                    continue
                try:
                    other_path = resolve_backup_path(raw)
                except OSError:
                    continue
                if (
                    other_path == resolved
                    or other_path.is_relative_to(resolved)
                    or resolved.is_relative_to(other_path)
                ):
                    label = (
                        other.get("name")
                        or other.get("host")
                        or other_type
                    )
                    return False, (
                        f"каталог пересекается с бэкапами "
                        f"устройства «{label}» ({other_path})"
                    )

    return True, ""


def _local_key_path(device_type: str, key_name: str) -> Path | None:
    """
    Абсолютный путь к локальному SSH-ключу устройства или
    None, если удалять его нельзя.

    Ключ «локальный» — относительное имя внутри каталога
    плагина. Путь с '../' или указывающий на код/конфиг
    плагина (*.py, *.json) — не ключ: None.
    """
    if not key_name or Path(key_name).is_absolute():
        return None

    plugin = (
        plugin_manager._plugins.get(device_type)
        if plugin_manager else None
    )
    plugin_dir = Path(
        (plugin or {}).get("dir") or (PLUGINS_DIR / device_type)
    ).resolve()

    try:
        key_path = (plugin_dir / key_name).resolve()
    except OSError:
        return None

    if not key_path.is_relative_to(plugin_dir) or key_path == plugin_dir:
        return None
    if key_path.suffix.lower() in (".py", ".json", ".pyc"):
        return None

    return key_path


@app.post("/delete-device/{device_type}/{index}")
async def delete_device(
    device_type: str,
    index: int,
    request: Request,
    user: str = Depends(require_auth),
):
    """
    Удаляет устройство целиком:
        • запись из routers.json;
        • запись и переиндексацию в state.json;
        • директорию с бэкапами (recurse, включая .git);
        • опционально — SSH-ключ, но только если он
          локальный и не используется другими устройствами.

    Порядок:
        1. Собрать статистику.
        2. Удалить из routers.json.
        3. Пересобрать state.json.
        4. Удалить директорию бэкапов.
        5. Удалить SSH-ключ (если разрешено).
    """
    import shutil

    if not plugin_manager or device_type not in plugin_manager._plugins:
        raise HTTPException(404, "Плагин не найден")

    plugin_data = plugin_manager._plugins[device_type]
    routers = plugin_data.get("routers", [])

    if not (0 <= index < len(routers)):
        raise HTTPException(404, "Устройство не найдено")

    form = await request.form()
    remove_key = form.get("remove_key") == "true"

    if not (0 <= index < len(routers)):
        raise HTTPException(404, "Устройство не найдено")

    # Нельзя удалять устройство, по которому идёт бэкап или
    # синхронизация: задача допишет файлы в удалённый каталог
    # и запишет результат по сдвинувшемуся индексу.
    if (
        is_backup_running_for(f"{device_type}/{index}")
        or is_sync_running_for(f"{device_type}/{index}")
    ):
        raise HTTPException(
            409,
            "По устройству идёт бэкап или синхронизация. "
            "Дождитесь окончания и повторите удаление.",
        )

    router = routers[index]
    host = (
        router.get("host")
        or router.get("ip")
        or f"#{index}"
    )
    device_name = router.get("name") or host
    device_id = f"{device_type}/{index}"

    # ---- 1. Статистика (для лога) ----

    backup_path = None
    backup_path_raw = router.get("backup_path", "")
    if backup_path_raw:
        backup_path = _resolve_backup_path(backup_path_raw)

    removed_size = 0
    removed_bytes = 0
    if backup_path and backup_path.exists():
        try:
            removed_size = get_dir_size(backup_path)
            removed_bytes = sum(
                1 for f in backup_path.rglob("*") if f.is_file()
            )
        except Exception:
            pass

    # ---- 1.5. Проверка SSH-ключа до изменений ----

    # Ключ можно удалить только если:
    #   • запрошено пользователем (remove_key);
    #   • путь относительный (внутри plugins/<type>/);
    #   • он не используется другими устройствами.
    key_name = (
        router.get("ssh_key")
        or router.get("private_key")
        or ""
    )
    key_to_delete: Path | None = None
    key_delete_blocked_reason = ""

    if remove_key and key_name:
        if _local_key_path(device_type, key_name) is None:
            key_delete_blocked_reason = (
                "ключ вне каталога плагина"
            )

        else:
            # Проверяем использование другими устройствами
            # ДО удаления записи из routers.json.
            shared_with = []
            for i, other in enumerate(routers):
                if i == index:
                    continue
                other_key = (
                    other.get("ssh_key")
                    or other.get("private_key")
                    or ""
                )
                if other_key == key_name:
                    shared_with.append(
                        other.get("name")
                        or other.get("host")
                        or f"#{i}"
                    )

            if shared_with:
                key_delete_blocked_reason = (
                    "используется другими устройствами: "
                    + ", ".join(shared_with)
                )
            else:
                key_to_delete = _local_key_path(
                    device_type, key_name,
                )

    # ---- 2. Удалить запись из routers.json ----

    with plugin_manager.routers_lock:
        # Пока разбиралась форма, список мог измениться —
        # не удаляем чужую запись.
        if not (0 <= index < len(routers) and routers[index] is router):
            raise HTTPException(
                409,
                "Список устройств изменился. Обновите страницу.",
            )

        routers.pop(index)
        try:
            plugin_manager.save_routers(device_type)
        except Exception as exc:
            # Откат: устройство остаётся и в памяти, и в файле.
            routers.insert(index, router)
            _raise_routers_save_error("delete-device", exc)

    # ---- 3. Обновить state.json ----

    try:
        state = load_json(STATE_FILE, {})
        if not isinstance(state, dict):
            state = {}

        other_devices = {
            k: v for k, v in state.items()
            if not k.startswith(f"{device_type}/")
        }

        # Было: A(0), B(1), C(2). Удалили B(1).
        # Стало: A(0), C(1). Для A old_idx=0, для C old_idx=2.
        # Правило: new_i < deleted_index → old_i = new_i;
        #         new_i >= deleted_index → old_i = new_i + 1
        new_devices = {}
        for new_i in range(len(routers)):
            old_i = new_i if new_i < index else new_i + 1
            old_key = f"{device_type}/{old_i}"
            if old_key in state:
                new_devices[f"{device_type}/{new_i}"] = state[old_key]

        final_state = {**other_devices, **new_devices}
        save_json(STATE_FILE, final_state)

    except Exception as exc:
        logging.error(
            "delete-device: ошибка state.json: %s",
            exc, exc_info=True,
        )

    # ---- 4. Удалить директорию бэкапов ----

    backup_removed = False
    backup_error = ""
    if backup_path:
        safe, reason = _is_safe_to_delete(
            backup_path, exclude_router=router,
        )
        if not safe:
            backup_error = reason
            logging.error(
                "delete-device: %s — отказ удаления бэкапов: %s",
                device_id, reason,
            )
        elif not backup_path.exists():
            backup_removed = True
        else:
            try:
                shutil.rmtree(backup_path)
                backup_removed = True
            except Exception as exc:
                backup_error = str(exc)
                logging.error(
                    "delete-device: не удалось удалить %s: %s",
                    backup_path, exc, exc_info=True,
                )

    # ---- 5. Удалить SSH-ключ (если разрешено) ----

    key_removed = False
    if key_to_delete and key_to_delete.exists():
        try:
            key_to_delete.unlink()
            key_removed = True
        except OSError as exc:
            logging.warning(
                "delete-device: не удалось удалить ключ %s: %s",
                key_to_delete, exc,
            )

    # ---- Логирование ----

    size_str = format_size(removed_size)

    if key_delete_blocked_reason:
        key_status = f"сохранён ({key_delete_blocked_reason})"
    elif key_removed:
        key_status = "удалён"
    else:
        key_status = "сохранён"

    logging.info(
        "🗑 Удалено устройство %s (%s): "
        "файлов %d (%s), SSH-ключ %s, запрос от %s",
        device_name,
        device_id,
        removed_bytes,
        size_str,
        key_status,
        user,
    )
    if backup_error:
        logging.error(
            "🗑 Каталог бэкапов не удалён: %s (%s)",
            backup_path, backup_error,
        )

    return RedirectResponse(url="/", status_code=303)

# =========================================================
# SETTINGS
# =========================================================

@app.get("/settings", response_class=HTMLResponse)
def settings_page(
    request: Request,
    tab: str = "general",
    message: str = "",
    user: str = Depends(require_auth),
):
    """
    Единая страница настроек с табами:
        general   — общие параметры (config.json)
        scheduler — планировщик
        retention — очистка истории
        storage   — хранилища (rclone)
        service   — управление службой
    """
    if tab not in ("general", "scheduler", "retention", "storage", "service"):
        tab = "general"

    # Шапка (_header.html с header_show_status=True) требует
    # service, scheduler_config, logs_size, is_backup_running —
    # без них Jinja падает с UndefinedError → 500 на всех вкладках.
    context: dict = {
        "active_tab": tab,
        "message": message,
        "service": get_status(),
        "scheduler_config": scheduler.config,
        "logs_size": format_size(get_dir_size(LOGS_DIR)),
        "is_backup_running": is_backup_running(),
    }

    if tab == "general":
        context["config"] = load_json(CONFIG_FILE, {})

    elif tab == "scheduler":
        context["config"] = scheduler.config

    elif tab == "retention":
        app_config = load_json(CONFIG_FILE, {})
        retention = app_config.get("retention") or {}
        stats = _collect_maintenance_stats()
        context["retention"] = retention
        context["stats"] = stats
        context["total_versions"] = sum(r["total"] for r in stats)
        context["total_marked"] = sum(r["marked"] for r in stats)
        context["msg_count"] = request.query_params.get("count", "0")
        context["msg_devices"] = request.query_params.get("devices", "0")

    elif tab == "storage":
        context["rclone_path"] = rclone_mod.get_rclone_path()
        context["rclone_available"] = rclone_mod.is_available()
        context["rclone_version"] = rclone_mod.get_version()

    return templates.TemplateResponse(
        request,
        "settings.html",
        context,
    )


@app.post("/settings/general/save")
async def settings_general_save(
    request: Request,
    user: str = Depends(require_auth),
):
    form = await request.form()

    host = (form.get("host") or "").strip() or "127.0.0.1"
    try:
        port = int(form.get("port") or "8080")
        if not (1 <= port <= 65535):
            raise ValueError()
    except (TypeError, ValueError):
        port = 8080

    open_browser = form.get("open_browser") == "true"

    try:
        max_parallel = int(form.get("max_parallel_backups") or "5")
        max_parallel = max(1, min(20, max_parallel))
    except (TypeError, ValueError):
        max_parallel = 5

    try:
        timeout = int(form.get("backup_timeout_seconds") or "300")
        timeout = max(30, timeout)
    except (TypeError, ValueError):
        timeout = 300

    # Загружаем существующий config, чтобы не потерять retention и пр.
    config = load_json(CONFIG_FILE, {})
    config["host"] = host
    config["port"] = port
    config["open_browser"] = open_browser
    config["max_parallel_backups"] = max_parallel
    config["backup_timeout_seconds"] = timeout

    try:
        save_json(CONFIG_FILE, config)
    except Exception as exc:
        logging.error("settings: не удалось сохранить config.json: %s", exc, exc_info=True)
        raise HTTPException(500, "Не удалось сохранить настройки")

    logging.info(
        "⚙ Общие настройки обновлены: host=%s, port=%d, "
        "parallel=%d, timeout=%d (от %s)",
        host, port, max_parallel, timeout, user,
    )

    return RedirectResponse(
        url="/settings?tab=general&message=saved",
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