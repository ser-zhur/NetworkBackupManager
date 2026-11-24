import os
import json
import copy
import logging
import threading
import secrets
import hashlib
import base64
import binascii
import time
import shutil
import tempfile
import urllib.parse

from pathlib import Path
from datetime import datetime
from contextlib import asynccontextmanager
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool
from core import rclone as rclone_mod
from core import rclone_oauth

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
    JSONResponse,
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


from core.secure_storage import get_secure_storage

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
    HOME_DIR,
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
from core.version import (
    APP_NAME, COPYRIGHT, LICENSE_ID, SOURCE_URL, __version__,
)
from core import devices as device_store
from core import device_status, events, monitoring, notifications, plugin_api
from core import backup_state, csv_import, settings_backup

from core.logger import (
    setup_logging,
    set_device_context,
    clear_device_context,
    device_log_tag,
)

from core.git_storage import (
    GitStorage,
    is_available as git_available,
)

from core.plugin_manager import is_device_disabled
from core.storage import global_config
from core.storage import encryption as storage_encryption
from core.storage.base import device_remote_dir

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
    try_start_sync,
    finish_sync,
    is_sync_running_for,
    get_sync_status,
)


# =========================================================
# CORE
# =========================================================

storage = get_secure_storage()

# WWW-Authenticate для всех 401: charset="UTF-8" (RFC 7617)
# подсказывает браузеру кодировать логин/пароль в UTF-8.
_WWW_AUTHENTICATE = 'Basic charset="UTF-8"'


class Utf8HTTPBasic(HTTPBasic):
    """
    HTTP Basic с поддержкой не-ASCII логина и пароля.

    Стандартный HTTPBasic декодирует учётные данные как
    ASCII: пользователь, созданный с кириллицей в логине
    или пароле (--create-user это разрешает), не мог войти
    вовсе. Здесь — UTF-8 (так шлют современные браузеры),
    при ошибке — Latin-1.
    """

    async def __call__(self, request: Request) -> HTTPBasicCredentials:
        def unauthorized(detail: str):
            return HTTPException(
                status_code=401,
                detail=detail,
                headers={"WWW-Authenticate": _WWW_AUTHENTICATE},
            )

        authorization = request.headers.get("Authorization") or ""
        scheme, _, param = authorization.partition(" ")
        if scheme.lower() != "basic" or not param:
            raise unauthorized("Not authenticated")

        try:
            raw = base64.b64decode(param.strip(), validate=True)
        except (ValueError, binascii.Error):
            raise unauthorized("Invalid authentication credentials")

        try:
            data = raw.decode("utf-8")
        except UnicodeDecodeError:
            data = raw.decode("latin-1")

        username, sep, password = data.partition(":")
        if not sep:
            raise unauthorized("Invalid authentication credentials")

        return HTTPBasicCredentials(username=username, password=password)


security = Utf8HTTPBasic()


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

    # Однократный перенос настроек хранилищ из устройств в
    # общие (config.json). Идемпотентен; копия devices.json —
    # devices.json.bak-<дата> рядом с оригиналом.
    #
    # Здесь, а не при импорте модуля: импорт (тесты, утилиты)
    # не должен менять рабочие данные.
    try:
        global_config.migrate(plugin_manager, storage_manager)
    except Exception as exc:
        logging.error(
            "storage: перенос настроек хранилищ не удался: %s",
            exc, exc_info=True,
        )

    scheduler.start()
    monitoring.mark_started()

    # Уведомления — до app.started, чтобы и оно могло уйти.
    notifications.dispatcher.start()

    events.emit(
        events.APP_STARTED,
        f"{APP_NAME} {__version__} запущен",
        version=__version__,
    )

    try:

        yield

    finally:

        logging.info(
            "🛑 Остановка планировщика..."
        )

        invalidate_status_cache()
        scheduler.stop()
        notifications.dispatcher.stop()


# =========================================================
# FASTAPI
# =========================================================

app = FastAPI(
    title=APP_NAME,
    version=__version__,
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


def _csrf_hosts(request: Request) -> set[str]:
    """
    Адреса, под которыми открыт сам сайт: Host и, за обратным
    прокси, X-Forwarded-Host (прокси может подменять Host на
    адрес upstream). Подделать X-Forwarded-Host чужая страница
    не может: браузер не даёт сайту добавить такой заголовок к
    межсайтовому запросу без CORS, а CORS мы не разрешаем.
    """
    hosts = {(request.headers.get("host") or "").lower()}
    forwarded = request.headers.get("x-forwarded-host") or ""
    hosts |= {h.strip().lower() for h in forwarded.split(",") if h.strip()}
    hosts.discard("")
    return hosts


def _csrf_source_ok(request: Request) -> bool:
    hosts = _csrf_hosts(request)

    origin = request.headers.get("origin")
    if origin is not None:
        # "null" — sandbox/file:// и т.п.: источник неизвестен
        if origin.strip().lower() == "null":
            return False
        return urllib.parse.urlsplit(origin).netloc.lower() in hosts

    referer = request.headers.get("referer")
    if referer:
        return urllib.parse.urlsplit(referer).netloc.lower() in hosts

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

if (WEB_DIR / "templates").exists():

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
        "disabled": {
            "dot": "bg-slate-400 dark:bg-slate-500",
            "ring": "ring-slate-400/15 dark:ring-slate-500/15",
            "badge": "bg-slate-200 dark:bg-slate-800 text-slate-600 dark:text-slate-400",
            "label": "Отключено",
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


def _inline_code(text: str):
    """`текст` → <code>; остальное экранируется (шаги PLUGIN["setup"])."""
    import re as _re
    from markupsafe import Markup, escape
    parts = _re.split(r"`([^`]+)`", str(text))
    out = []
    for i, part in enumerate(parts):
        if i % 2:
            out.append(Markup(
                '<code class="font-mono text-[12px] px-1 rounded bg-white/70 dark:bg-slate-800">{}</code>'
            ).format(part))
        else:
            out.append(escape(part))
    return Markup("").join(out)


def _static_version() -> str:
    """Версия CSS для ?v= в ссылке (время изменения файла):
    после пересборки браузер не возьмёт старый из кэша."""
    try:
        return str(int((WEB_DIR / "static" / "css" / "tailwind.css").stat().st_mtime))
    except OSError:
        return "0"


if templates is not None:
    templates.env.globals["static_version"] = _static_version()
    templates.env.globals["app_version"] = __version__
    templates.env.globals["app_copyright"] = COPYRIGHT
    templates.env.globals["license_id"] = LICENSE_ID
    templates.env.globals["source_url"] = SOURCE_URL
    templates.env.globals["status_meta"] = _status_meta
    templates.env.filters["inline_code"] = _inline_code
    templates.env.globals["age_label"] = _age_label
    templates.env.globals["age_class"] = _age_class


# =========================================================
# PASSWORD (verify only — hash делается в main.py)
# =========================================================

from core.auth import verify_password  # noqa: E402


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
                _WWW_AUTHENTICATE
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
                _WWW_AUTHENTICATE
            },
        )

    valid = (
        # Байты, а не str: compare_digest не принимает строки
        # с не-ASCII символами (TypeError → 500 вместо 401).
        secrets.compare_digest(
            credentials.username.encode("utf-8"),
            str(username).encode("utf-8"),
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
                _WWW_AUTHENTICATE
            },
        )

    return credentials.username


async def require_monitoring(request: Request) -> str:
    """
    Чтение состояния (/api/status, /api/events): токен
    мониторинга (Authorization: Bearer <токен>) или обычный
    логин/пароль. Токен даёт только чтение — см. core/monitoring.py.
    """
    scheme, _, value = request.headers.get("Authorization", "").partition(" ")
    if scheme.lower() == "bearer":
        if monitoring.check_token(value.strip()):
            return "monitoring"
        raise HTTPException(
            status_code=401,
            detail="Неверный токен мониторинга.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return require_auth(await security(request))


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


def _device_ref(device_id: str) -> tuple[str, int]:
    """
    Постоянный id устройства → (тип, текущий номер в списке
    плагина). 404, если такого устройства нет.
    """
    if plugin_manager:
        with plugin_manager.routers_lock:
            for device_type, pd in plugin_manager._plugins.items():
                for index, router in enumerate(pd.get("routers", [])):
                    if router.get("id") == device_id:
                        return device_type, index
    raise HTTPException(status_code=404, detail="Устройство не найдено")


def _device_id(device_type: str, index: int) -> str:
    """
    (тип, номер) → постоянный id устройства
    (см. PluginManager.router_id).
    """
    return plugin_manager.device_id(device_type, index)


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

def _external_backends() -> list:
    """Внешние хранилища (всё, кроме git)."""
    if storage_manager is None:
        return []
    return [
        b for b in storage_manager.all()
        if not b.is_authority and not b.always_enabled
    ]


def _parse_storage_form(form, old_storage: dict) -> dict:
    """
    Хранилища устройства (add/edit): только ползунки.

    storage_<backend>_enabled → {backend_id: {"enabled": bool}}.
    git включён всегда. Ненастроенное хранилище включить
    нельзя — ползунок в форме заблокирован, а здесь защита
    от прямого запроса.
    """
    if storage_manager is None:
        return {}

    storages = global_config.load_global()
    result: dict = {}

    for backend in storage_manager.all():
        if backend.always_enabled or backend.is_authority:
            enabled = True
        else:
            enabled = (
                form.get(f"storage_{backend.id}_enabled") == "true"
                and global_config.is_configured(backend, storages)
            )
        result[backend.id] = {"enabled": enabled}

    return result


def _device_host(r: dict) -> str:
    return r.get("host") or r.get("ip") or r.get("address") or ""


def _device_folder(r: dict) -> str:
    """Имя локальной папки бэкапов устройства — так же
    называется его папка на внешних хранилищах."""
    raw = r.get("backup_path")
    return resolve_backup_path(raw).name if raw else ""


def _find_storage_conflict(
    storages: dict,
    device_type: str,
    router: dict,
    exclude: dict | None = None,
) -> str | None:
    """
    Текст ошибки, если включённое внешнее хранилище
    устройства пишет в ту же папку того же сервера, что и
    другое устройство (имена файлов фиксированные —
    config.rsc и т.п. — устройства перезаписывали бы друг
    друга). None — пересечений нет.

    exclude — старая запись редактируемого устройства.
    """
    if storage_manager is None or not plugin_manager:
        return None

    for backend in _external_backends():
        if not global_config.is_enabled_for(router, backend):
            continue
        cfg = global_config.effective_config(router, backend, storages)
        key = backend.target_key(
            cfg,
            host=_device_host(router),
            name=router.get("name") or "",
            device_type=device_type,
            folder=_device_folder(router),
        )
        if key is None:
            continue

        for other_type, plugin_data in plugin_manager._plugins.items():
            for other in plugin_data.get("routers", []):
                if other is exclude or other is router:
                    continue
                if not global_config.is_enabled_for(other, backend):
                    continue
                other_key = backend.target_key(
                    global_config.effective_config(other, backend, storages),
                    host=_device_host(other),
                    name=other.get("name") or "",
                    device_type=other_type,
                    folder=_device_folder(other),
                )
                if other_key == key:
                    me = router.get("name") or _device_host(router)
                    them = other.get("name") or _device_host(other)
                    return (
                        f"{backend.name}: устройства «{me}» и «{them}» "
                        f"попадают в одну папку «{key[2]}» — у них "
                        f"одинаковое имя локальной папки бэкапов, и "
                        f"файлы перезаписывали бы друг друга. Задайте "
                        f"устройствам разные локальные папки."
                    )
    return None


def _check_storage_conflicts(
    device_type: str,
    router: dict,
    exclude: dict | None = None,
) -> None:
    """HTTP 400 при пересечении папок (см. _find_storage_conflict)."""
    message = _find_storage_conflict(
        global_config.load_global(), device_type, router, exclude,
    )
    if message:
        raise HTTPException(status_code=400, detail=message)


def _find_any_storage_conflict(storages: dict) -> str | None:
    """Пересечения папок между ЛЮБЫМИ устройствами при данных
    общих настройках — проверка перед их сохранением."""
    if not plugin_manager:
        return None
    for device_type, plugin_data in plugin_manager._plugins.items():
        for router in plugin_data.get("routers", []):
            message = _find_storage_conflict(storages, device_type, router)
            if message:
                return message
    return None


def _device_storage_view(
    device_type: str,
    router: dict | None,
) -> list[dict]:
    """
    Хранилища для формы устройства (add/edit): ползунок,
    настроено ли, итоговая папка этого устройства.

    router=None — форма добавления: включены по умолчанию
    все настроенные хранилища, папка — шаблон.
    """
    if storage_manager is None:
        return []

    storages = global_config.load_global()
    defaults = storage_manager.defaults_for_new_router(storages)
    rows = []

    for backend in storage_manager.all():
        configured = global_config.is_configured(backend, storages)
        if router is None:
            enabled = bool(defaults.get(backend.id, {}).get("enabled"))
        else:
            enabled = global_config.is_enabled_for(router, backend)

        dir_field = getattr(backend, "remote_dir_field", None)
        base = (
            (storages.get(backend.id) or {}).get(dir_field, "")
            if dir_field else ""
        )
        if not dir_field or not configured:
            folder = ""
        elif router is not None:
            folder = device_remote_dir(
                base,
                folder=_device_folder(router),
                host=_device_host(router),
                name=router.get("name") or "",
                device_type=device_type,
            )
        else:
            # Форма добавления: локальной папки ещё нет
            base_dir = device_remote_dir(base, folder="").rstrip("/")
            folder = f"{base_dir}/<имя локальной папки>"

        rows.append({
            "backend": backend,
            "configured": configured,
            "enabled": enabled and configured,
            "folder": folder,
        })

    return rows




# =========================================================
# PATH UTILS
# =========================================================

def _raise_routers_save_error(where: str, exc: Exception):
    """Лог + HTTP 500 при неудачной записи devices.json."""
    logging.error(
        "%s: не удалось сохранить devices.json: %s",
        where, exc, exc_info=True,
    )
    raise HTTPException(
        500, f"Не удалось сохранить devices.json: {exc}",
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
    пользователь вручную испортил devices.json (например,
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
                plugin_manager.router_id(router),
                device_type,
                idx,
                router,
                _resolve_backup_path(raw),
            )
            
            
classify_device = device_status.classify


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
    # Карточке нужны только последние 3 коммита (git_recent)
    # и точное общее число версий. Раньше читалось 200
    # коммитов и считалась длина списка — после 200 версий
    # счётчик «застревал», а каждый коммит ещё и разбирался
    # (список файлов). get_commit_count обходит историю без
    # разбора деревьев.
    git_commits = []
    git_total = 0
    if git_available():
        try:
            gs = GitStorage(b_path_abs)
            if gs.is_initialized():
                git_commits = gs.get_history(limit=3)
                git_total = gs.get_commit_count(include_marked=False)

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

        backup_count = git_total
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
    device_state = _load_device_state(_device_id(device_type, index))
    health = device_status.summarize(
        device_state, disabled=is_device_disabled(router),
    )

    # Общий размер — вся папка устройства вместе с .git:
    # столько бэкапы реально занимают на диске.
    # Последняя версия — текущие файлы конфигурации (без .git).
    # Раньше «общим» показывался размер последней версии,
    # а «средним» — он же, делённый на число версий.
    total_size = format_size(get_dir_size(b_path_abs))

    latest_bytes = _get_dir_size_excluding_git(b_path_abs)
    latest_size = format_size(latest_bytes) if latest_bytes > 0 else "—"

    # ---- Статус ----
    # Давность последнего успешного опроса и исход последней
    # попытки (core/device_status.py). Если опросов ещё не
    # было (state.json пуст) — по возрасту git-коммита.
    if health["status"] == "none" and age_days is not None:
        status = classify_device(age_days, health["last_attempt_ok"])
    else:
        status = health["status"]

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
    # внутри data/keys/<type>/) И не используется другими
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

    device_id = _device_id(device_type, index)

    return {
        "id": device_id,
        "type": device_type,
        "index": index,
        "disabled": is_device_disabled(router),
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
        # Последний успешный опрос (блок «Последняя успешная связь»)
        "last_contact": health["last_success"],
        "last_contact_age_days": health["success_age_days"],
        "last_contact_ok": health["backends_ok"] is not False,
        # Последняя попытка и её ошибка
        "last_attempt": health["last_attempt"],
        "last_attempt_ok": health["last_attempt_ok"],
        "last_error": health["last_error"],
        "failures": health["failures"],
        "backup_count": backup_count,
        "max_backups": router.get("max_backups", 7),
        "total_size": total_size,
        "latest_size": latest_size,
        "backup_path": b_path,
        "ssh_key": (
            router.get("ssh_key")
            or router.get("private_key")
            or "id_rsa.key"
        ),
        "git_enabled": git_enabled,
        "git_total": git_total,
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
    device_id = _device_id(device_type, index)
    st = state_all.get(device_id) or {}
    if not isinstance(st, dict):
        st = {}

    disabled = is_device_disabled(router)
    health = device_status.summarize(st, disabled=disabled)
    status = health["status"]

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
        "last_attempt": health["last_attempt"],
        "last_attempt_ok": health["last_attempt_ok"],
        # Для сортировки «по давности связи» — успешный опрос.
        "last_attempt_age_days": health["success_age_days"],
        "last_error": health["last_error"],
        "failures": health["failures"],
        "backup_count": backup_count,
        "max_backups": router.get("max_backups", 7),
        "backup_in_progress": is_backup_running_for(device_id),
        "sync_in_progress": is_sync_running_for(device_id),

        # ---- Доменные индикаторы ----
        # 1. Никогда не было ни одной попытки бэкапа.
        "disabled": disabled,
        # Для отключённого устройства предупреждения ниже не
        # показываем: бэкапы для него намеренно не идут.
        "never_backed_up": (
            not disabled
            and not health["last_attempt"]
            and backup_count == 0
        ),
        # 2. Сколько включённых backend'ов в ошибке (для ⚠N).
        "backends_error_count": (
            0 if disabled else _count_backend_errors(router, st)
        ),
        # 3. Флаг «давно не опрашивалось» — жёлтый фон.
        #    Считаем «давно» = 2× интервал планировщика.
        #    Если планировщик выключен — не подсвечиваем.
        "overdue": (
            not disabled and _is_overdue(health["success_age_days"])
        ),
        # Отпечаток записи state.json: страница перерисовывает
        # строку и карточку, только если он изменился.
        "sig": hashlib.sha1(
            json.dumps(st, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()[:12],
    }


def _all_device_summaries() -> list[dict]:
    """Строки списка устройств (state.json читается один раз)."""
    state_all = load_json(STATE_FILE, {})
    if not isinstance(state_all, dict):
        state_all = {}

    devices = []
    if plugin_manager and hasattr(plugin_manager, "_plugins"):
        for device_type, plugin_data in plugin_manager._plugins.items():
            routers = plugin_data.get("routers", [])
            for index, router in enumerate(routers):
                devices.append(
                    _build_device_summary(device_type, index, router, state_all)
                )
    return devices


def _state_version() -> str:
    """Меняется при каждой записи state.json (каждая попытка бэкапа)."""
    try:
        st = STATE_FILE.stat()
    except OSError:
        return "0"
    return f"{st.st_mtime_ns}-{st.st_size}"


# =========================================================
# ШАПКА: режим запуска, планировщик, сводка устройств
# =========================================================

DEVICE_STATUSES = ("ok", "warn", "fail", "none", "disabled")


def _device_counts(statuses=None) -> dict:
    """Сколько устройств в каждом статусе (и всего)."""
    if statuses is None:
        state_all = load_json(STATE_FILE, {})
        if not isinstance(state_all, dict):
            state_all = {}
        statuses = []
        if plugin_manager and hasattr(plugin_manager, "_plugins"):
            for device_type, plugin_data in plugin_manager._plugins.items():
                for index, router in enumerate(plugin_data.get("routers", [])):
                    st = state_all.get(_device_id(device_type, index))
                    statuses.append(device_status.summarize(
                        st if isinstance(st, dict) else {},
                        disabled=is_device_disabled(router),
                    )["status"])
    counts = {k: 0 for k in DEVICE_STATUSES}
    for status in statuses:
        counts[status] = counts.get(status, 0) + 1
    counts["total"] = len(statuses)
    return counts


def _run_mode() -> dict:
    """Как запущена программа: значок и подсказка для шапки."""
    from core import settings as nbm_settings
    manager = nbm_settings.service_manager()
    if manager == "docker":
        return {"kind": "docker", "icon": "🐳", "label": "Docker",
                "hint": "Работает в контейнере Docker. Перезапуск и "
                        "обновление — через docker compose."}
    if manager == "systemd":
        return {"kind": "systemd", "icon": "🐧", "label": "Служба Linux",
                "hint": "Работает как служба systemd "
                        "(networkbackupmanager.service)."}
    if is_service_process():
        return {"kind": "service", "icon": "🪟", "label": "Служба Windows",
                "hint": "Работает как служба Windows: запускается вместе "
                        "с компьютером, без входа пользователя."}

    import sys
    frozen = getattr(sys, "frozen", False)
    hint = ("Запущено вручную (EXE), не как служба. " if frozen
            else "Запущено как скрипт Python (main.py), не как служба. ")
    try:
        registered = get_status().get("registered")
    except Exception:
        registered = False
    hint += ("Служба установлена, но эта копия работает отдельно от неё."
             if registered else
             "Программа остановится при закрытии окна или выходе из системы.")
    return {"kind": "exe" if frozen else "script",
            "icon": "🖥️" if frozen else "🐍",
            "label": "Приложение" if frozen else "Скрипт Python",
            "hint": hint}


def _scheduler_info() -> dict:
    """Планировщик для шапки: включён ли, когда следующий запуск."""
    cfg = scheduler.config or {}
    if not cfg.get("enabled"):
        return {"state": "off", "short": "выкл",
                "hint": "Автобэкап выключен"}
    if cfg.get("mode", "daily") == "interval":
        short = f"{cfg.get('interval_hours', 24)} ч"
        hint = f"Автобэкап: каждые {cfg.get('interval_hours', 24)} ч."
    else:
        short = cfg.get("daily_time", "03:00")
        hint = f"Автобэкап: ежедневно в {short}"
    if not scheduler.is_alive():
        return {"state": "stopped", "short": short,
                "hint": hint + ". Поток планировщика не работает — "
                               "перезапустите программу."}
    if scheduler.is_job_running():
        hint += ". Сейчас идёт плановый бэкап"
    else:
        nxt = scheduler.next_run()
        if nxt:
            hint += ". Следующий запуск: " + nxt.strftime("%d.%m %H:%M")
    return {"state": "on", "short": short, "hint": hint}


def _header_info() -> dict:
    return {
        "run": _run_mode(),
        "scheduler": _scheduler_info(),
        "devices": _device_counts(),
    }


if templates is not None:
    templates.env.globals["header_info"] = _header_info

# =========================================================
# STORAGE SYNC TASK
# =========================================================

def execute_storage_sync_task(
    device_type: str,
    index: int,
    user: str,
) -> dict | None:
    """
    Синхронизация хранилищ одного устройства. Слот уже занят
    вызывающим (try_start_sync); здесь только освобождается.

    Возвращает результат sync_git_to_storages или None при
    исключении.
    """
    device_id = _device_id(device_type, index)

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
    # Слот синхронизации уже занят в sync_storages_endpoint()
    # через try_start_sync(); здесь только освобождаем.

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
        returned = result

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

        return returned

    except Exception as exc:
        logging.error(
            "☁ sync: исключение: %s", exc, exc_info=True,
        )
        return None
    finally:
        finish_sync(device_id)
        clear_device_context()


# =========================================================
# SYNC ALL (все устройства)
# =========================================================
#
# Синхронизация хранилищ на всех устройствах: сначала файлы
# всех устройств (вся git-история) готовятся во временной
# папке, затем каждое хранилище получает их за одно
# подключение (rclone copy) — а не по подключению на
# устройство.
#
# Занятые устройства (идёт бэкап или их синхронизация)
# пропускаются — это видно в итоговой строке лога.

_sync_all_lock = threading.Lock()
_sync_all_running = False


def is_sync_all_running() -> bool:
    with _sync_all_lock:
        return _sync_all_running


def _sync_all_targets() -> list[tuple[str, int, dict]]:
    """Устройства, у которых включено хоть одно настроенное
    внешнее хранилище — только им есть что синхронизировать."""
    if not plugin_manager or storage_manager is None:
        return []
    storages = global_config.load_global()
    externals = [
        b for b in _external_backends()
        if global_config.is_configured(b, storages)
    ]
    targets = []
    for device_type, plugin_data in plugin_manager._plugins.items():
        for idx, router in enumerate(plugin_data.get("routers", [])):
            if any(global_config.is_enabled_for(router, b) for b in externals):
                targets.append((device_type, idx, router))
    return targets


def execute_sync_all_task(user: str) -> dict:
    """
    Фоновая задача «Синхронизировать хранилища» для всех
    устройств. Флаг _sync_all_running уже выставлен
    обработчиком; здесь только сбрасывается.
    """
    global _sync_all_running

    stats = {"devices": 0, "ok": 0, "errors": 0, "skipped": 0}
    try:
        targets = _sync_all_targets()
        logging.info(
            "☁ Синхронизация хранилищ всех устройств: %d устройств "
            "(от %s)", len(targets), user,
        )

        stats["devices"] = len(targets)
        routers_by_id = {plugin_manager.router_id(r): (t, r) for t, i, r in targets}

        # Слот устройства занят только на время подготовки его
        # файлов (чтение git); выгрузка идёт уже без него, и
        # бэкапы в это время не блокируются.
        held: set[str] = set()

        def can_start(device_type: str, idx: int) -> bool:
            device_id = plugin_manager.device_id(device_type, idx)
            if not try_start_sync(device_id):
                stats["skipped"] += 1
                logging.info(
                    "☁ sync-all: %s пропущено — идёт бэкап "
                    "или синхронизация", device_id,
                )
                return False
            held.add(device_id)
            return True

        def on_prepared(device_type: str, idx: int) -> None:
            device_id = plugin_manager.device_id(device_type, idx)
            if device_id in held:
                held.discard(device_id)
                finish_sync(device_id)

        try:
            results = plugin_manager.sync_many_to_storages(
                [(t, i) for t, i, _ in targets],
                can_start=can_start,
                on_prepared=on_prepared,
            )
        finally:
            for device_id in list(held):
                finish_sync(device_id)

        # Итог — в лог каждого устройства, как при одиночной
        for device_id, result in results.items():
            device_type, router = routers_by_id.get(device_id) or ("", {})
            set_device_context(device_log_tag(device_type, router) if router else device_id)
            try:
                if result.get("ok"):
                    logging.info(
                        "☁ Синхронизация завершена (все устройства, от %s): %s",
                        user, result["message"],
                    )
                else:
                    logging.error(
                        "☁ Синхронизация прервана: %s", result["message"],
                    )
                for err in result.get("errors", [])[:10]:
                    logging.error("☁ sync: %s", err)
            finally:
                clear_device_context()

            failed = (
                not result.get("ok")
                or any(
                    b.get("err") for b in result.get("backends", {}).values()
                )
            )
            stats["errors" if failed else "ok"] += 1

        logging.info(
            "☁ Синхронизация хранилищ всех устройств завершена: "
            "устройств %d, успешно %d, с ошибками %d, пропущено %d",
            stats["devices"], stats["ok"], stats["errors"],
            stats["skipped"],
        )
        return stats

    except Exception as exc:
        logging.error(
            "☁ sync-all: исключение: %s", exc, exc_info=True,
        )
        return stats

    finally:
        with _sync_all_lock:
            _sync_all_running = False

# =========================================================
# BACKUP TASKS
# =========================================================

def execute_device_backup_task(
    device_type: str,
    index: int,
    router: dict,
    original: dict | None = None,
):
    """
    Фоновый одиночный бэкап. Слот устройства уже занят в
    run_backup() (try_start_device); здесь только освобождаем.

    router   — конфиг устройства (пароль зашифрован);
    original — объект из списка в памяти, если router — копия.
    Сам бэкап и запись исхода — plugin_manager.backup_device().
    """
    original = original if original is not None else router
    device_id = (
        plugin_manager.router_id(original) if plugin_manager
        else original.get("id", "")
    )
    try:
        if plugin_manager is None:
            return
        plugin_manager.backup_device(
            device_type, router, index, original=original,
        )
    except Exception as exc:
        logging.error("❌ [Фон] Ошибка при бэкапе: %s", exc)
    finally:
        finish_device(device_id)

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

def _clamp_tag_rows(raw, default: int = 2) -> int:
    """
    Число строк тегов в фильтре главной страницы:
    0 — показывать все, иначе 1..20. Мусор — default.
    """
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(0, min(20, value))


@app.get("/", response_class=HTMLResponse)
def read_root(
    request: Request,
    user: str = Depends(require_auth),
):
    # ---- Лёгкий список ----
    #
    # state.json читаем ОДИН раз и передаём во все summary.
    # Никаких _build_device_payload в цикле — только summary.
    devices = _all_device_summaries()

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
            "tag_rows": _clamp_tag_rows(
                load_json(CONFIG_FILE, {}).get("tag_rows", 2)
            ),
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
    "/api/device/{device_id}",
    response_class=HTMLResponse,
)
def api_device_detail(
    request: Request,
    device_id: str,
    user: str = Depends(require_auth),
):
    device_type, index = _device_ref(device_id)
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

@app.get("/api/health")
def api_health():
    """
    Жива ли программа — без пароля (Docker healthcheck,
    проверка доступности). 200 — да, 503 — планировщик упал
    или папка данных недоступна для записи.
    """
    alive, body = monitoring.health(scheduler)
    return JSONResponse(body, status_code=200 if alive else 503)


@app.get("/api/status")
def api_status(user: str = Depends(require_monitoring)):
    """Сводка для мониторинга (Zabbix и т.п.) — см. core/monitoring.py."""
    return monitoring.status(plugin_manager, scheduler)


@app.get("/api/events")
def api_events(
    limit: int = 50,
    after_id: int | None = None,
    device_id: str | None = None,
    kind: str | None = None,
    user: str = Depends(require_monitoring),
):
    """Последние события, новые первыми (core/events.py)."""
    return {
        "events": events.recent(
            limit=max(1, min(500, limit)),
            kinds=(kind,) if kind else None,
            device_id=device_id,
            after_id=after_id,
        ),
    }


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
        # Изменилась — главная запрашивает свежие строки
        # (/api/device-rows): статус после бэкапа без перезагрузки.
        "state_version": _state_version(),
    }


@app.get("/api/device-rows")
def api_device_rows(user: str = Depends(require_auth)):
    """
    Строки списка устройств главной страницы: HTML изменяемой
    части (web/templates/_device_row_body.html) и поля для
    фильтров и сортировки.
    """
    row_template = templates.env.get_template("_device_row_body.html")
    summaries = _all_device_summaries()
    return {
        "state_version": _state_version(),
        # Счётчики в шапке
        "counts": _device_counts([d["status"] for d in summaries]),
        "rows": [
            {
                "id": d["id"],
                "sig": d["sig"],
                "status": d["status"],
                "overdue": bool(d["overdue"]),
                "last_attempt_age": (
                    d["last_attempt_age_days"]
                    if d["last_attempt_age_days"] is not None else 999999
                ),
                "html": row_template.render(d=d),
            }
            for d in summaries
        ],
    }
# =========================================================
# ХРАНИЛИЩА RCLONE (Настройки → Хранилища)
# =========================================================
#
# Хранилище — запись в config.json → remotes. Форма строится
# из описания rclone (rclone config providers): параметры,
# подсказки, обязательность, пароли, варианты.

_POPULAR_REMOTE_TYPES = [
    ("webdav", "WebDAV", "Nextcloud, ownCloud, SharePoint, Яндекс.Диск по WebDAV"),
    ("ftp", "FTP / FTPS", "FTP-сервер, в том числе с TLS"),
    ("sftp", "SFTP", "Сервер по SSH"),
    ("s3", "S3 / MinIO", "Amazon S3 и совместимые: MinIO, Ceph, Cloudflare R2, Wasabi…"),
    ("onedrive", "OneDrive", "Microsoft OneDrive / SharePoint"),
    ("drive", "Google Drive", "Google Диск"),
    ("smb", "SMB", "Сетевая папка Windows / Samba / NAS"),
    ("local", "Локальная папка", "Папка на этом компьютере или подключённый диск"),
]


# «Обёртки» над другими хранилищами и хранилище в памяти —
# для бэкапов бессмысленны, в выборе типа не показываем.
_META_REMOTE_TYPES = frozenset({
    "alias", "archive", "cache", "chunker", "combine", "compress",
    "crypt", "hasher", "memory", "union",
})


def _is_oauth_type(info: dict) -> bool:
    """Тип требует входа через браузер (OAuth) — следующий этап."""
    return any(o.get("Name") == "token" for o in info.get("Options", []))


def _remote_type_card(info: dict, title: str = "", description: str = "") -> dict:
    from core.storage.rclone_remote import _ICONS
    name = info.get("Name", "")
    return {
        "name": name,
        "title": title or name,
        "description": description or info.get("Description", ""),
        "icon": _ICONS.get(name, "🗄"),
        "oauth": _is_oauth_type(info),
    }


def _remote_form_options(info: dict, remote: dict | None, submitted=None) -> list[dict]:
    """
    Параметры типа для формы. submitted — значения из
    отправленной формы (при ошибке форма перерисовывается
    с ними, а не с сохранёнными).
    """
    from core.storage.rclone_remote import secret_option_names

    saved = (remote or {}).get("options") or {}
    secrets_ = secret_option_names(info.get("Name", ""))
    result = []
    for o in info.get("Options", []):
        if o.get("Hide"):
            continue
        name = o.get("Name")
        if not name or name == "token":   # OAuth — следующий этап
            continue

        help_full = (o.get("Help") or "").strip()
        help_first = help_full.splitlines()[0] if help_full else ""
        examples = [
            {
                "value": str(ex.get("Value", "")),
                "help": (ex.get("Help") or "").splitlines()[0] if ex.get("Help") else "",
                "providers": ex.get("Provider") or "",
            }
            for ex in (o.get("Examples") or [])
        ]
        if o.get("Type") == "bool":
            kind = "bool"
        elif o.get("IsPassword") or name in secrets_:
            kind = "password"
        elif examples and (o.get("Exclusive") or name == "provider"):
            # provider (S3 и др.) — всегда список: от него
            # зависит, какие параметры показывать.
            kind = "select"
        else:
            kind = "text"

        if submitted is not None:
            value = submitted.get(f"opt_{name}")
            checked = submitted.get(f"opt_{name}") == "true"
        else:
            value = saved.get(name)
            if kind == "bool":
                checked = (
                    value if isinstance(value, bool)
                    else str(value).lower() == "true" if value is not None
                    else bool(o.get("Default"))
                )
            else:
                checked = False

        result.append({
            "name": name,
            "kind": kind,
            "required": bool(o.get("Required")),
            "advanced": bool(o.get("Advanced")),
            "help_first": help_first,
            "help_full": help_full,
            "default": o.get("DefaultStr") or "",
            "examples": examples,
            "providers": o.get("Provider") or "",
            "value": "" if value is None or kind == "bool" else str(value),
            "checked": checked,
            "saved": bool(saved.get(name)) if kind == "password" else False,
            "wide": name in ("url", "endpoint", "host"),
        })
    return result


def _remote_edit_context(info, remote, *, type_name, error="", submitted=None) -> dict:
    from core.storage.rclone_remote import _ICONS
    options = _remote_form_options(info, remote, submitted)
    src = submitted if submitted is not None else (remote or {})
    return {
        "remote_id": (remote or {}).get("id", ""),
        "type_name": type_name,
        "type_description": info.get("Description", ""),
        "icon": _ICONS.get(type_name, "🗄"),
        "name": src.get("name") or (remote or {}).get("name") or "",
        "base_dir": src.get("base_dir") if src.get("base_dir") is not None else "/",
        "mode": src.get("mode") or "changes_only",
        "encrypt": (
            src.get("encrypt") in (True, "true", "on")
            if submitted is not None else bool((remote or {}).get("encrypt"))
        ),
        "encryption_password_set": storage_encryption.is_password_set(),
        "encryption_available": storage_encryption.is_available(),
        "basic_options": [o for o in options if not o["advanced"]],
        "advanced_options": [o for o in options if o["advanced"]],
        "advanced_open": bool(error) and any(
            o["advanced"] and o["required"] for o in options
        ),
        "error": error,
        "is_oauth": rclone_mod.is_oauth_type(type_name),
        "has_token": bool(((remote or {}).get("options") or {}).get("token")),
    }


@app.get("/settings/storage/new", response_class=HTMLResponse)
def remote_new_page(request: Request, user: str = Depends(require_auth)):
    infos = {p.get("Name"): p for p in rclone_mod.providers()}
    popular = [
        _remote_type_card(infos[n], title, desc)
        for n, title, desc in _POPULAR_REMOTE_TYPES if n in infos
    ]
    all_types = sorted(
        (
            _remote_type_card(p) for p in infos.values()
            if not p.get("Hide") and p.get("Name") not in _META_REMOTE_TYPES
        ),
        key=lambda t: t["name"],
    )
    return templates.TemplateResponse(request, "storage_new.html", {
        "popular": popular,
        "all_types": all_types,
        "rclone_version": rclone_mod.get_version() if infos else "",
    })


@app.get("/settings/storage/edit", response_class=HTMLResponse)
def remote_edit_page(
    request: Request,
    id: str = "",
    type: str = "",
    user: str = Depends(require_auth),
):
    remote = global_config.load_global().get(id) if id else None
    if id and remote is None:
        raise HTTPException(404, "Хранилище не найдено")
    type_name = remote["type"] if remote else type
    info = rclone_mod.provider(type_name)
    if info is None:
        raise HTTPException(404, f"Тип rclone «{type_name}» не найден (rclone недоступен?)")
    return templates.TemplateResponse(
        request, "storage_edit.html",
        _remote_edit_context(info, remote, type_name=type_name),
    )


def _options_from_form(info: dict, form, old_options: dict) -> tuple[dict, list[str]]:
    """
    Параметры хранилища из формы.

    Пароли шифруются; пустое поле-пароль = прежний. Флажок
    сохраняется, только если отличается от умолчания rclone.
    Возвращает (options, незаполненные обязательные).
    """
    from core.storage.rclone_remote import secret_option_names

    secrets_ = secret_option_names(info.get("Name", ""))
    options: dict = {}
    missing: list[str] = []
    for o in info.get("Options", []):
        name = o.get("Name")
        if not name:
            continue
        # Параметры, которых нет в форме (скрытые, токен OAuth) —
        # сохраняем прежние значения: их выставил мастер входа.
        if o.get("Hide") or name == "token":
            if name in old_options:
                options[name] = old_options[name]
            continue

        key = f"opt_{name}"
        if o.get("Type") == "bool":
            if f"bool_{name}" not in form:
                if name in old_options:
                    options[name] = old_options[name]
                continue
            value = form.get(key) == "true"
            if value != bool(o.get("Default")):
                options[name] = value
        elif o.get("IsPassword") or name in secrets_:
            raw = form.get(key) or ""
            if raw:
                options[name] = storage.encrypt(raw)
            elif old_options.get(name):
                options[name] = old_options[name]
        else:
            raw = (form.get(key) or "").strip()
            if raw:
                options[name] = raw

        if o.get("Required") and str(options.get(name, "")).strip() == "":
            missing.append(name)
    return options, missing


def _merge_oauth_result(type_name: str, options: dict, result: dict) -> None:
    """
    Параметры от мастера входа rclone (token, drive_id, ...)
    → в параметры хранилища. Секреты шифруются. Пароли
    rclone (obscured в его файле) не берём — они из формы.
    """
    from core.storage.rclone_remote import password_option_names, secret_option_names

    passwords = password_option_names(type_name)
    secrets_ = secret_option_names(type_name)
    for k, v in (result or {}).items():
        if k == "type" or k in passwords or v in ("", None):
            continue
        options[k] = storage.encrypt(v) if k in secrets_ else v


@app.post("/settings/storage/save", response_class=HTMLResponse)
async def remote_save(request: Request, user: str = Depends(require_auth)):
    form = await request.form()
    rid = (form.get("id") or "").strip()
    type_name = (form.get("type") or "").strip()

    info = rclone_mod.provider(type_name)
    if info is None:
        raise HTTPException(400, f"Тип rclone «{type_name}» не найден")

    remotes = global_config.load_remotes()
    by_id = {r["id"]: r for r in remotes}
    old = by_id.get(rid) if rid else None
    if rid and old is None:
        raise HTTPException(404, "Хранилище не найдено")

    old_options = (old or {}).get("options") or {} if (old or {}).get("type") == type_name else {}
    options, missing = _options_from_form(info, form, old_options)

    name = (form.get("name") or "").strip()[:80]
    base_dir = (form.get("base_dir") or "").strip() or "/"
    mode = form.get("mode") if form.get("mode") in ("changes_only", "all") else "changes_only"
    encrypt = form.get("encrypt") == "true"

    def fail(message: str):
        submitted = {k: form.get(k) for k in form.keys()}
        submitted.update({"name": name, "base_dir": base_dir, "mode": mode,
                          "encrypt": encrypt})
        return templates.TemplateResponse(
            request, "storage_edit.html",
            _remote_edit_context(info, old, type_name=type_name,
                                 error=message, submitted=submitted),
            status_code=400,
        )

    if not name:
        return fail("укажите название")
    if missing:
        return fail("не заполнены обязательные параметры: " + ", ".join(missing))
    if encrypt and not storage_encryption.is_password_set():
        return fail(
            "для шифрования сначала задайте пароль: "
            "Настройки → Хранилища → Шифрование"
        )
    if encrypt and not storage_encryption.is_available():
        return fail("для шифрования нужна библиотека py7zr (pip install py7zr)")

    # Результат входа через браузер / токена (OneDrive, Drive, ...)
    flow = rclone_oauth.get(form.get("oauth_flow") or "")
    if flow is not None and flow.type_name == type_name and flow.state == "done":
        _merge_oauth_result(type_name, options, flow.result)

    if rclone_mod.is_oauth_type(type_name) and not options.get("token"):
        return fail(
            "нужен вход в учётную запись — нажмите «Войти через браузер» "
            "или вставьте токен"
        )

    remote = {
        "id": rid or global_config.new_remote_id(name, set(by_id) | {"git"}),
        "name": name,
        "type": type_name,
        "options": options,
        "base_dir": base_dir,
        "mode": mode,
        "encrypt": encrypt,
    }
    new_list = (
        [remote if r["id"] == rid else r for r in remotes] if old
        else remotes + [remote]
    )

    # Сохраняем и проверяем пересечения папок; при конфликте —
    # возвращаем прежний список.
    global_config.save_remotes(new_list)
    if storage_manager is not None:
        storage_manager.reload_remotes()
    conflict = _find_any_storage_conflict(global_config.load_global())
    if conflict:
        global_config.save_remotes(remotes)
        if storage_manager is not None:
            storage_manager.reload_remotes()
        return fail(conflict)

    logging.info(
        "⚙ Хранилище %s «%s» (%s, базовая папка %s) %s (запрос от %s)",
        remote["id"], name, type_name, base_dir,
        "изменено" if old else "добавлено", user,
    )
    return RedirectResponse("/settings?tab=storage&message=remote_saved", status_code=303)


@app.post("/settings/storage/{rid}/delete")
def remote_delete(rid: str, user: str = Depends(require_auth)):
    """
    Удаляет хранилище из настроек и отключает его у всех
    устройств. Файлы на самом хранилище не трогаются.
    """
    remotes = global_config.load_remotes()
    remote = next((r for r in remotes if r["id"] == rid), None)
    if remote is None:
        raise HTTPException(404, "Хранилище не найдено")

    global_config.save_remotes([r for r in remotes if r["id"] != rid])

    devices = 0
    if plugin_manager:
        with plugin_manager.routers_lock:
            for device_type, pd in plugin_manager._plugins.items():
                changed = False
                for router in pd.get("routers", []):
                    st = router.get("storage")
                    if isinstance(st, dict) and rid in st:
                        del st[rid]
                        changed = True
                        devices += 1
                if changed:
                    try:
                        plugin_manager.save_routers(device_type)
                    except Exception as exc:
                        logging.error(
                            "хранилище %s: не удалось обновить devices.json (%s): %s",
                            rid, device_type, exc,
                        )

    if storage_manager is not None:
        storage_manager.reload_remotes()

    logging.info(
        "🗑 Хранилище %s «%s» удалено, отключено у %d устройств (запрос от %s)",
        rid, remote.get("name"), devices, user,
    )
    return RedirectResponse("/settings?tab=storage&message=remote_deleted", status_code=303)


@app.post("/settings/storage/encryption")
async def storage_encryption_save(request: Request, user: str = Depends(require_auth)):
    """
    Пароль шифрования внешних хранилищ (один на все). При
    смене уже выгруженные архивы остаются со старым паролем.
    """
    form = await request.form()
    password = form.get("password") or ""
    error = storage_encryption.validate_password(password, form.get("confirm") or "")
    if error:
        return RedirectResponse(
            "/settings?tab=storage&error=" + urllib.parse.quote("пароль шифрования: " + error),
            status_code=303,
        )
    changed = storage_encryption.is_password_set()
    if changed and password == storage_encryption.get_password():
        return RedirectResponse("/settings?tab=storage&message=encryption_same", status_code=303)

    storage_encryption.set_password(password)
    logging.info(
        "🔒 Пароль шифрования хранилищ %s (запрос от %s)",
        "изменён" if changed else "задан", user,
    )
    return RedirectResponse(
        "/settings?tab=storage&message="
        + ("encryption_changed" if changed else "encryption_set"),
        status_code=303,
    )


@app.post("/settings/storage/{rid}/enable-all")
def remote_enable_all(rid: str, user: str = Depends(require_auth)):
    """
    Включает хранилище у всех устройств (тот же ползунок, что
    на странице устройства). Уже включённые не трогаются.
    """
    backend = next((b for b in _external_backends() if b.id == rid), None)
    if backend is None:
        raise HTTPException(404, "Хранилище не найдено")
    if not global_config.is_configured(backend):
        return RedirectResponse(
            "/settings?tab=storage&message=remote_not_configured", status_code=303,
        )

    def revert(items):
        for st, prev in items:
            if prev is None:
                st.pop(rid, None)
            else:
                st[rid] = prev

    devices = 0
    errors = 0
    if plugin_manager:
        with plugin_manager.routers_lock:
            # 1) Включаем в памяти
            changed: dict[str, list] = {}
            for device_type, pd in plugin_manager._plugins.items():
                for router in pd.get("routers", []):
                    if global_config.is_enabled_for(router, backend):
                        continue
                    st = router.get("storage")
                    if not isinstance(st, dict):
                        st = router["storage"] = {}
                    changed.setdefault(device_type, []).append((st, st.get(rid)))
                    st[rid] = {"enabled": True}

            # 2) Две папки устройств не должны совпасть на хранилище
            conflict = _find_any_storage_conflict(global_config.load_global())
            if conflict:
                for items in changed.values():
                    revert(items)
                return RedirectResponse(
                    "/settings?tab=storage&error=" + urllib.parse.quote(conflict),
                    status_code=303,
                )

            # 3) Сохраняем
            for device_type, items in changed.items():
                try:
                    plugin_manager.save_routers(device_type)
                    devices += len(items)
                except Exception as exc:
                    errors += 1
                    revert(items)           # в памяти — как на диске
                    logging.error(
                        "хранилище %s: не удалось обновить devices.json (%s): %s",
                        rid, device_type, exc,
                    )

    logging.info(
        "☁ Хранилище %s «%s» включено у всех устройств: добавлено %d (запрос от %s)",
        rid, backend.name, devices, user,
    )
    message = "remote_enable_failed" if errors else "remote_enabled_all"
    return RedirectResponse(
        f"/settings?tab=storage&message={message}&count={devices}", status_code=303,
    )


@app.post("/api/oauth/start")
async def api_oauth_start(request: Request, user: str = Depends(require_auth)):
    """
    Начать вход (OneDrive, Google Drive, ...).

    {"type", "options": {client_id, ...}, "token": ""} — без
    token: вход через браузер (ссылка в ответе); с token
    (вставлен вручную) — сразу мастер rclone.
    """
    try:
        data = await request.json()
    except Exception:
        data = {}
    type_name = data.get("type") or ""
    if not rclone_mod.is_oauth_type(type_name):
        return {"state": "error", "message": f"Тип «{type_name}» не использует вход через браузер"}

    from core.storage.rclone_remote import password_option_names

    # Пароли rclone в мастер не передаём (в аргументах процесса)
    passwords = password_option_names(type_name)
    options = {
        k: v for k, v in (data.get("options") or {}).items()
        if k not in passwords and v not in ("", None, False)
    }
    flow = rclone_oauth.start(type_name, options, token=(data.get("token") or "").strip())
    logging.info("oauth[%s]: вход начат (запрос от %s)", type_name, user)
    return flow.view()


@app.get("/api/oauth/{flow_id}")
def api_oauth_status(flow_id: str, user: str = Depends(require_auth)):
    flow = rclone_oauth.get(flow_id)
    if flow is None:
        return {"state": "error", "message": "Сеанс входа не найден или истёк — начните заново"}
    return flow.view()


@app.post("/api/oauth/{flow_id}/answer")
async def api_oauth_answer(flow_id: str, request: Request, user: str = Depends(require_auth)):
    flow = rclone_oauth.get(flow_id)
    if flow is None:
        return {"state": "error", "message": "Сеанс входа не найден или истёк — начните заново"}
    try:
        data = await request.json()
    except Exception:
        data = {}
    flow.answer(data.get("result", ""))
    return flow.view()


@app.post("/api/oauth/{flow_id}/cancel")
def api_oauth_cancel(flow_id: str, user: str = Depends(require_auth)):
    flow = rclone_oauth.get(flow_id)
    if flow is not None:
        flow.cancel()
    return {"state": "error", "message": "Вход отменён"}


@app.post("/api/remotes/test")
async def api_remote_test(request: Request, user: str = Depends(require_auth)):
    """
    Проверка соединения.

    {"id": ..., "saved": true} — сохранённое хранилище как есть;
    иначе — параметры из формы ({"type", "options", "base_dir"}),
    пустой пароль = сохранённый.
    """
    from core.storage.rclone_remote import RcloneRemote

    try:
        data = await request.json()
    except Exception:
        data = {}

    saved = global_config.load_global().get(data.get("id") or "")

    if data.get("saved"):
        if saved is None:
            return {"ok": False, "message": "Хранилище не найдено"}
        cfg = dict(saved)
    else:
        type_name = data.get("type") or ""
        info = rclone_mod.provider(type_name)
        if info is None:
            return {"ok": False, "message": f"Тип rclone «{type_name}» не найден"}
        from core.storage.rclone_remote import secret_option_names

        old_options = (saved or {}).get("options") or {} if (saved or {}).get("type") == type_name else {}
        passwords = secret_option_names(type_name)
        options = {}
        for name, value in (data.get("options") or {}).items():
            if name in passwords:
                if value:
                    options[name] = storage.encrypt(str(value))
                elif old_options.get(name):
                    options[name] = old_options[name]
            elif value not in ("", None):
                options[name] = value
        for k, v in old_options.items():          # скрытые / от мастера
            options.setdefault(k, v)
        flow = rclone_oauth.get(data.get("oauth_flow") or "")
        if flow is not None and flow.type_name == type_name and flow.state == "done":
            _merge_oauth_result(type_name, options, flow.result)
        cfg = {"type": type_name, "options": options, "base_dir": data.get("base_dir") or "/"}

    try:
        result = RcloneRemote({"id": cfg.get("id") or "test", "type": cfg["type"],
                               "name": cfg.get("name") or "test"}).test(cfg)
    except Exception as exc:
        logging.error("remote test: %s", exc, exc_info=True)
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

    infos = (
        [pd["info"] for pd in plugin_manager._plugins.values()]
        if plugin_manager
        else []
    )
    infos.sort(key=lambda i: i.title.lower())

    return templates.TemplateResponse(
        request,
        "add.html",
        {
            "plugin_infos":
                infos,

            "plugin_errors":
                plugin_manager.plugin_errors if plugin_manager else {},

            "selected_type":
                request.query_params.get("type") or "",

            "device_storages":
                _device_storage_view("", None),

            "all_existing_tags":
                _all_existing_tags(),
        },
    )


def _parse_port(form) -> int:
    try:
        port = int(form.get("port") or 22)
    except (TypeError, ValueError):
        port = 0
    if not 1 <= port <= 65535:
        raise HTTPException(status_code=400, detail="Порт SSH: число от 1 до 65535")
    return port


def _plugin_options(device_type: str, form, previous: dict) -> dict:
    """Поля, объявленные плагином (opt_<поле>), → значения устройства."""
    info = plugin_manager.plugin_info(device_type)
    if info is None:
        return {}
    try:
        return plugin_api.parse_field_values(info, form, previous, storage.encrypt)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


# =========================================================
# SAVE ROUTER
# =========================================================

def _build_new_router(form) -> tuple[str, dict]:
    """
    Новое устройство из полей формы «Добавить устройство»
    (или строки импорта CSV в том же виде). Ничего не
    сохраняет. HTTPException(400) — значение не подходит.
    """
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

    port = _parse_port(form)
    options = _plugin_options(device_type, form, {})

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

        # Сразу абсолютный — как после загрузки devices.json.
        # Иначе до перезапуска плагин, git и UI разрешали бы
        # относительный путь от разных каталогов.
        "backup_path":
            str(resolve_backup_path(backup_path)),

        "max_backups":
            int(max_backups),
    }

    if port != 22:
        new_router["port"] = port
    new_router.update(options)

    # Название — опционально. Не пишем пустую строку
    # в devices.json, чтобы файл оставался компактным.
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
    
    # Теги — опционально. Не пишем пустой массив в devices.json,
    # чтобы файл оставался компактным.
    tags = _parse_tags(form.get("tags") or "")
    if tags:
        new_router["tags"] = tags
    
    if form.get("debug_connection") == "true":
        new_router["debug_connection"] = True


    return device_type, new_router


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
    device_type, new_router = _build_new_router(form)

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
        new_router["host"],
    )

    invalidate_status_cache()

    return RedirectResponse(
        url="/",
        status_code=303,
    )


# =========================================================
# ЛИЦЕНЗИЯ И СТОРОННИЕ КОМПОНЕНТЫ
# =========================================================
#
# AGPL-3.0: пользователям веб-интерфейса — ссылка на исходный
# код и текст лицензии (подвал страниц, _footer.html).
# Файлы лежат рядом с программой: LICENSE в исходниках и в
# Docker-образе, LICENSE.txt — в установке Windows.
# THIRD_PARTY_LICENSES.txt создаёт при сборке
# tools/third_party_licenses.py.

def _app_text_file(*names: str) -> str | None:
    for name in names:
        try:
            return (BASE_DIR / name).read_text(encoding="utf-8")
        except OSError:
            continue
    return None


def _plain_text(text: str) -> Response:
    return Response(text, media_type="text/plain; charset=utf-8")


@app.get("/license")
def license_text(user: str = Depends(require_auth)):
    text = _app_text_file("LICENSE", "LICENSE.txt")
    if text is None:
        text = f"GNU Affero General Public License v3.0: {SOURCE_URL}/blob/main/LICENSE\n"
    return _plain_text(
        f"{APP_NAME} {__version__}\n{COPYRIGHT}\n"
        f"Исходный код: {SOURCE_URL}\n\n" + text
    )


@app.get("/third-party-licenses")
def third_party_licenses(user: str = Depends(require_auth)):
    text = _app_text_file("THIRD_PARTY_LICENSES.txt")
    if text is None:
        # Запуск из исходников: собираем список на лету
        try:
            import sys as _sys
            tools_dir = str(BASE_DIR / "tools")
            if tools_dir not in _sys.path:
                _sys.path.insert(0, tools_dir)
            import third_party_licenses as tpl
            text = tpl.build()
        except Exception as exc:
            logging.warning("Список сторонних компонентов: %s", exc)
            text = (
                "Файл THIRD_PARTY_LICENSES.txt не найден.\n"
                f"Список компонентов: {SOURCE_URL}\n"
            )
    return _plain_text(text)


# =========================================================
# ПАРА SSH-КЛЮЧЕЙ ДЛЯ УСТРОЙСТВА
# =========================================================

@app.post("/api/ssh-keys")
async def api_ssh_keys(request: Request, user: str = Depends(require_auth)):
    """
    {"type": "pfsense", "name": "id_rsa.key"} → создать пару
    ключей в data/keys/<тип>/ (существующий файл не меняется)
    и вернуть открытый ключ для вставки на устройство.
    """
    from core import ssh_keys

    try:
        data = await request.json()
    except Exception:
        data = {}
    device_type = str(data.get("type") or "").strip().lower()
    info = plugin_manager.plugin_info(device_type) if plugin_manager else None
    if info is None:
        return {"ok": False, "message": "Неизвестный тип устройства"}
    if not info.uses_key:
        return {"ok": False, "message": f"{info.title}: вход по ключу не поддерживается"}
    try:
        created, path, public = ssh_keys.ensure_key_pair(device_type, str(data.get("name") or ""))
    except ssh_keys.SshKeyError as exc:
        return {"ok": False, "message": str(exc)}
    except OSError as exc:
        logging.error("ssh-keys: %s", exc, exc_info=True)
        return {"ok": False, "message": "Не удалось записать файл ключа (подробности в журнале)"}

    shown = f"data/keys/{device_type}/{path.name}"
    if created:
        logging.info("🔑 Создана пара ключей %s (запрос от %s)", shown, user)
        message = (f"✓ Создан ключ {shown}. Вставьте открытый ключ на устройство "
                   f"и сохраните эту форму.")
    else:
        message = (f"Ключ {shown} уже есть — новый не создавался. "
                   f"Ниже его открытый ключ.")
    return {"ok": True, "created": created, "name": path.name,
            "public_key": public, "message": message}


# =========================================================
# ИМПОРТ УСТРОЙСТВ ИЗ CSV
# =========================================================
#
# Только из файла: сеть не сканируется, к устройствам при
# импорте никто не подключается. Порядок: загрузить файл →
# посмотреть, что будет добавлено → подтвердить.

def _plugin_infos() -> list:
    infos = (
        [pd["info"] for pd in plugin_manager._plugins.values()]
        if plugin_manager else []
    )
    return sorted(infos, key=lambda i: i.title.lower())


def _host_key(device_type: str, router: dict) -> tuple:
    try:
        port = int(router.get("port") or 22)
    except (TypeError, ValueError):
        port = 22
    return (device_type, _device_host(router).strip().lower(), port)


def _path_key(router: dict) -> str:
    return str(_resolve_backup_path(router.get("backup_path") or "")).lower()


def _import_plan(text: str) -> list[dict]:
    """
    Что будет с каждой строкой файла: status ok (можно
    добавить), dup (такое устройство уже есть) или error.
    csv_import.CsvImportError — файл не читается целиком.
    """
    rows = csv_import.read_rows(text)
    infos = _plugin_infos()
    defaults = (
        storage_manager.defaults_for_new_router() if storage_manager else {}
    )

    hosts: dict = {}
    paths: dict = {}
    if plugin_manager:
        for device_type, plugin_data in plugin_manager._plugins.items():
            for router in plugin_data.get("routers", []):
                label = router.get("name") or _device_host(router)
                hosts[_host_key(device_type, router)] = f"уже есть: {label}"
                paths[_path_key(router)] = label
    storages = global_config.load_global()

    plan = []
    for line, row in rows:
        item = {
            "line": line, "name": row.get("name", ""), "ip": row.get("ip", ""),
            "type": "", "title": row.get("type", ""),
            "status": "error", "message": "", "router": None,
        }
        plan.append(item)

        device_type = csv_import.find_type(row.get("type", ""), infos)
        if device_type is None:
            item["message"] = (
                (f"Неизвестный тип «{row.get('type', '')}»" if row.get("type")
                 else "Не указан тип")
                + ". Допустимо: " + ", ".join(i.type for i in infos)
            )
            continue
        info = plugin_manager.plugin_info(device_type)
        item["type"], item["title"] = device_type, info.title

        form = csv_import.to_form(row, info, defaults)
        item["folder"] = form["backup_path"]      # как в файле — короче
        try:
            _, router = _build_new_router(form)
        except HTTPException as exc:
            item["message"] = str(exc.detail)
            continue
        if csv_import.is_yes(row.get("disabled")):
            router["disabled"] = True
        item["router"] = router

        host_key = _host_key(device_type, router)
        path_key = _path_key(router)
        if host_key in hosts:
            item["status"] = "dup"
            message = hosts[host_key]
            item["message"] = message[:1].upper() + message[1:]
            continue
        if path_key in paths:
            item["message"] = (
                f"Папка бэкапов {router['backup_path']} занята "
                f"устройством {paths[path_key]} — укажите backup_path"
            )
            continue
        conflict = _find_storage_conflict(storages, device_type, router)
        if conflict:
            item["message"] = conflict
            continue

        item["status"] = "ok"
        hosts[host_key] = f"повторяется в файле (строка {line})"
        paths[path_key] = item["name"] or item["ip"]
    return plan


def _import_page(request: Request, **context):
    infos = _plugin_infos()
    return templates.TemplateResponse(request, "import.html", {
        "plugin_infos": infos,
        "columns": csv_import.COLUMNS,
        "max_rows": csv_import.MAX_ROWS,
        **context,
    })


@app.get("/import", response_class=HTMLResponse)
def import_page(request: Request, user: str = Depends(require_auth)):
    return _import_page(request)


@app.get("/import/template.csv")
def import_template(user: str = Depends(require_auth)):
    # BOM — чтобы Excel открыл кириллицу правильно
    data = "\ufeff" + csv_import.template_csv(_plugin_infos())
    return Response(
        content=data.encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="devices.csv"'},
    )


@app.post("/import/preview", response_class=HTMLResponse)
async def import_preview(request: Request, user: str = Depends(require_auth)):
    form = await request.form()
    upload = form.get("file")
    data = b""
    if upload is not None and hasattr(upload, "read"):
        data = await upload.read(csv_import.MAX_BYTES + 1)
    if not data.strip():
        data = (form.get("text") or "").encode("utf-8")
    try:
        text = csv_import.decode(data)
        plan = _import_plan(text)
    except csv_import.CsvImportError as exc:
        return _import_page(request, error=str(exc))
    if not plan:
        return _import_page(request, error="В файле нет ни одного устройства")
    return _import_page(
        request,
        plan=plan,
        csv_b64=base64.b64encode(text.encode("utf-8")).decode("ascii"),
        counts={k: sum(1 for p in plan if p["status"] == k)
                for k in ("ok", "dup", "error")},
    )


@app.post("/import/apply", response_class=HTMLResponse)
async def import_apply(request: Request, user: str = Depends(require_auth)):
    form = await request.form()
    try:
        text = base64.b64decode(form.get("csv_b64") or "").decode("utf-8")
        plan = _import_plan(text)
    except (csv_import.CsvImportError, ValueError, binascii.Error) as exc:
        return _import_page(request, error=f"Не удалось прочитать список: {exc}")

    wanted = {int(x) for x in form.getlist("line") if str(x).isdigit()}
    added, failed = [], []
    with plugin_manager.routers_lock:
        storages = global_config.load_global()
        new_by_type: dict = {}
        for item in plan:
            if item["line"] not in wanted or item["status"] != "ok":
                continue
            router = item["router"]
            # Пересечение папок хранилищ с устройствами,
            # добавленными выше из этого же файла
            conflict = _find_storage_conflict(storages, item["type"], router)
            if conflict:
                item["message"] = conflict
                failed.append(item)
                continue
            plugin_manager._plugins[item["type"]]["routers"].append(router)
            new_by_type.setdefault(item["type"], []).append(item)

        for device_type, items in new_by_type.items():
            routers = plugin_manager._plugins[device_type]["routers"]
            try:
                plugin_manager.save_routers(device_type)
                added.extend(items)
            except Exception as exc:
                logging.error("import: не удалось сохранить devices.json: %s",
                              exc, exc_info=True)
                for item in items:
                    routers.remove(item["router"])
                    item["message"] = f"Не удалось сохранить devices.json: {exc}"
                    failed.append(item)

    if added:
        logging.info("📥 Импорт из CSV (%s): добавлено устройств: %d",
                     user, len(added))
        invalidate_status_cache()
    by_line = lambda it: it["line"]   # noqa: E731 — порядок как в файле
    return _import_page(request, result={
        "added": sorted(added, key=by_line),
        "failed": sorted(failed, key=by_line),
    })


# =========================================================
# SYNC STORAGES (git → все non-authority backend'ы)
# =========================================================

@app.post("/device/{device_id}/sync-storages")
def sync_storages_endpoint(
    request: Request,           # <--- ДОБАВЛЕНО
    device_id: str,
    background_tasks: BackgroundTasks,
    user: str = Depends(require_auth),
):
    device_type, index = _device_ref(device_id)

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

    # Атомарно: не стартуем во время массового прогона,
    # бэкапа или другой синхронизации этого устройства.
    if not try_start_sync(device_id):
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return Response(status_code=204)
        return RedirectResponse(
            url=f"/?selected={device_id}",
            status_code=303,
        )

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
    "/device/{device_id}/run"
)
def run_backup(
    device_id: str,
    request: Request,
    background_tasks:
        BackgroundTasks,
    user: str = Depends(
        require_auth
    ),
):
    device_type, index = _device_ref(device_id)
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
            url=f"/device/{device_id}/logs",
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

    # Выведено из эксплуатации — бэкап не запускаем.
    # Кнопок в интерфейсе нет; это защита от прямого запроса.
    if is_device_disabled(router):
        logging.info(
            "⏸ Бэкап %s не запущен: устройство отключено", device_id,
        )
        if is_ajax:
            return Response(status_code=409)
        return HTMLResponse(
            "<h3>Устройство отключено — бэкап не запускается.</h3>"
            "<p>Снимите отметку «Отключено» на странице "
            "редактирования.</p><a href='/'>Назад</a>",
            status_code=409,
        )

    # Атомарно проверяем и занимаем устройство. Проверка
    # выше (is_backup_running_for) — только быстрый ответ
    # UI; без этого захвата два запроса подряд могли оба
    # пройти её до старта фоновой задачи.
    if not try_start_device(device_id):
        if is_ajax:
            return Response(status_code=204)
        return RedirectResponse(
            url=f"/device/{device_id}/logs",
            status_code=303,
        )

    background_tasks.add_task(
        execute_device_backup_task,
        device_type,
        index,
        router,
    )

    if is_ajax:
        return Response(status_code=204)
    return RedirectResponse(
        url=f"/device/{device_id}/logs",
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

@app.post("/device/{device_id}/toggle-pin")
def toggle_pin(
    request: Request,
    device_id: str,
    user: str = Depends(require_auth),
):
    """
    Переключает флаг pinned у устройства и сохраняет devices.json.

    Возвращает редирект на Referer (чтобы пользователь остался
    на той же странице с теми же фильтрами).
    """
    device_type, index = _device_ref(device_id)
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
            # Не мусорим в devices.json, если сняли закрепление
            router.pop("pinned", None)

        # 3. Сохраняем в devices.json (атомарно)
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
        url=f"/?selected={device_id}",
        status_code=303,
    )
# =========================================================
# BACKUPS (legacy redirect)
# =========================================================

@app.get(
    "/device/{device_id}/backups",
    response_class=HTMLResponse,
)
def backups_page(
    request: Request,
    device_id: str,
    user: str = Depends(
        require_auth
    ),
):
    device_type, index = _device_ref(device_id)
    # Версионирование полностью в git.
    # Страница «Архивы» оставлена как редирект для совместимости
    # с закладками пользователей.
    return RedirectResponse(
        url=f"/device/{device_id}/git",
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
    "/device/{device_id}/download-backup/{filepath:path}"
)
def download_backup(
    device_id: str,
    filepath: str,
    user: str = Depends(
        require_auth
    ),
):
    device_type, index = _device_ref(device_id)

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
    "/device/{device_id}/git",
    response_class=HTMLResponse,
)
def git_history_page(
    request: Request,
    device_id: str,
    limit: int = 100,
    user: str = Depends(require_auth),
):
    device_type, index = _device_ref(device_id)
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
            "device_id": device_id,
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
    "/device/{device_id}/git/download/{sha}/{filename}",
)
def git_download_file(
    device_id: str,
    sha: str,
    filename: str,
    user: str = Depends(require_auth),
):
    device_type, index = _device_ref(device_id)
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
            "Content-Disposition": _attachment_header(filename),
        },
    )


def _attachment_header(filename: str) -> str:
    """
    Content-Disposition для скачивания (RFC 6266 / 5987).

    Заголовки HTTP — только latin-1: не-ASCII имя вида
    «конфиг.rsc» в filename="..." роняло ответ (500).
    Кавычка в имени ломала заголовок. Даём ASCII-имя для
    старых клиентов и точное имя в filename*=UTF-8''...
    """
    fallback = "".join(
        ch if 32 <= ord(ch) < 127 and ch not in ('"', chr(92)) else "_"
        for ch in filename
    ) or "download"
    quoted = urllib.parse.quote(filename, safe="")
    return (
        f'attachment; filename="{fallback}"; '
        f"filename*=UTF-8''{quoted}"
    )


# =========================================================
# GIT: SET NOTE
# =========================================================

@app.post(
    "/device/{device_id}/git/note/{sha}",
)
async def git_set_note(
    device_id: str,
    sha: str,
    request: Request,
    user: str = Depends(require_auth),
):
    device_type, index = _device_ref(device_id)
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
        url=f"/device/{device_id}/git",
        status_code=303,
    )

# =========================================================
# GIT: MARK DELETE
# =========================================================

@app.post(
    "/device/{device_id}/git/mark-delete",
)
async def git_mark_delete(
    device_id: str,
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
    device_type, index = _device_ref(device_id)
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
        url=f"/device/{device_id}/git",
        status_code=303,
    )


@app.post(
    "/device/{device_id}/git/unmark-delete",
)
async def git_unmark_delete(
    device_id: str,
    request: Request,
    user: str = Depends(require_auth),
):
    """
    Снимает пометки. scope=all — очищает всё.
    Иначе — по списку shas в поле.
    """
    device_type, index = _device_ref(device_id)
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
        url=f"/device/{device_id}/git",
        status_code=303,
    )


@app.post(
    "/device/{device_id}/git/sweep",
)
async def git_sweep(
    device_id: str,
    user: str = Depends(require_auth),
):
    """
    Физически пересобирает историю: удаляет помеченные
    коммиты, перепривязывает parent-указатели, освобождает
    объекты через git gc (если git есть в PATH).
    """
    device_type, index = _device_ref(device_id)
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
            url=f"/device/{device_id}/git",
            status_code=303,
        )

    logging.error(
        "git[%s/%s]: sweep не удался — %s",
        device_type, index, msg,
    )

    return RedirectResponse(
        url=(
            f"/device/{device_id}/git"
            f"?sweep_error={quote(msg)}"
        ),
        status_code=303,
    )


@app.post(
    "/device/{device_id}/git/mark-older-than",
)
async def git_mark_older_than(
    device_id: str,
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
    device_type, index = _device_ref(device_id)
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
                url=f"/device/{device_id}/git",
                status_code=303,
            )

    if older_than_date is None:
        return RedirectResponse(
            url=f"/device/{device_id}/git",
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
        url=f"/device/{device_id}/git",
        status_code=303,
    )

# =========================================================
# GIT: DIFF
# =========================================================

@app.get(
    "/device/{device_id}/git/diff/{sha_a}/{sha_b}",
    response_class=HTMLResponse,
)
def git_diff_page(
    request: Request,
    device_id: str,
    sha_a: str,
    sha_b: str,
    user: str = Depends(require_auth),
):
    device_type, index = _device_ref(device_id)
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
            "device_id": device_id,
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
    "/device/{device_id}/logs",
    response_class=HTMLResponse,
)
def device_logs(
    request: Request,
    device_id: str,
    limit: str = "100",
    raw: bool = False,
    focus_date: str = "",
    user: str = Depends(
        require_auth
    ),
):
    device_type, index = _device_ref(device_id)

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
                f"/?selected={device_id}",
        },
    )


# =========================================================
# EDIT ROUTER
# =========================================================

@app.get(
    "/device/{device_id}/edit",
    response_class=HTMLResponse,
)
def edit_router(
    request: Request,
    device_id: str,
    user: str = Depends(
        require_auth
    ),
):
    device_type, index = _device_ref(device_id)

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

            "device_id":
                device_id,

            "router":
                router,

            "info":
                plugin_manager.plugin_info(device_type),

            "device_storages":
                _device_storage_view(device_type, router),

            "all_existing_tags":
                _all_existing_tags(),
        },
    )


# =========================================================
# UPDATE ROUTER
# =========================================================

@app.post(
    "/device/{device_id}/update"
)
async def update_router(
    device_id: str,
    request: Request,
    user: str = Depends(
        require_auth
    ),
):
    device_type, index = _device_ref(device_id)
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
    options = _plugin_options(device_type, form, old_router)

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

        # Сразу абсолютный — как после загрузки devices.json.
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

    updated_router.update(options)

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

    # Статус. Форма редактирования шлёт ползунок «active»
    # (включён = устройство активно) и маркер active_field:
    # снятый чекбокс браузер не отправляет, и без маркера
    # запрос без поля молча отключил бы устройство.
    # Без маркера — прежнее поле "disabled" (или ничего).
    if form.get("active_field"):
        disabled = form.get("active") != "true"
    else:
        disabled = form.get("disabled") == "true"

    # Не пишем "disabled": false — файл остаётся компактным.
    if disabled:
        updated_router["disabled"] = True

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
        if "id" in old_router:
            updated_router["id"] = old_router["id"]

        _check_storage_conflicts(
            device_type, updated_router, exclude=old_router,
        )

        routers_list[index] = updated_router
        try:
            plugin_manager.save_routers(device_type)
        except Exception as exc:
            routers_list[index] = old_router
            _raise_routers_save_error("update-router", exc)

    if is_device_disabled(old_router) != is_device_disabled(updated_router):
        logging.info(
            "%s Устройство [%s] #%s %s (запрос от %s)",
            "⏸" if is_device_disabled(updated_router) else "▶",
            device_type, index,
            "отключено" if is_device_disabled(updated_router)
            else "снова включено",
            user,
        )

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
            "disabled": is_device_disabled(router),
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

    for device_id, _dt, _idx, router, backup_path in (
        _iter_device_backup_paths()
    ):
        if not backup_path.exists():
            continue
        # Отключённое устройство: история хранится как есть
        if is_device_disabled(router):
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

@app.post("/maintenance/sync-all")
def sync_all_endpoint(
    background_tasks: BackgroundTasks,
    user: str = Depends(require_auth),
):
    """Синхронизировать хранилища на всех устройствах."""
    global _sync_all_running

    with _sync_all_lock:
        if _sync_all_running:
            return RedirectResponse(
                "/settings?tab=storage&message=sync_running",
                status_code=303,
            )
        _sync_all_running = True

    background_tasks.add_task(execute_sync_all_task, user)
    logging.info(
        "☁ Запрошена синхронизация хранилищ всех устройств (от %s)",
        user,
    )
    return RedirectResponse(
        "/settings?tab=storage&message=sync_started",
        status_code=303,
    )


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
        HOME_DIR,
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

    Ключ «локальный» — относительное имя внутри
    data/keys/<type>/. Путь с '../' — не ключ: None.
    """
    return device_store.local_key_path(device_type, key_name)


@app.post("/device/{device_id}/delete")
async def delete_device(
    device_id: str,
    request: Request,
    user: str = Depends(require_auth),
):
    """
    Удаляет устройство целиком:
        • запись из devices.json;
        • запись и переиндексацию в state.json;
        • директорию с бэкапами (recurse, включая .git);
        • опционально — SSH-ключ, но только если он
          локальный и не используется другими устройствами.

    Порядок:
        1. Собрать статистику.
        2. Удалить из devices.json.
        3. Пересобрать state.json.
        4. Удалить директорию бэкапов.
        5. Удалить SSH-ключ (если разрешено).
    """
    device_type, index = _device_ref(device_id)
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
        is_backup_running_for(device_id)
        or is_sync_running_for(device_id)
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
    #   • путь относительный (внутри data/keys/<type>/);
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
            # ДО удаления записи из devices.json.
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

    # ---- 2. Удалить запись из devices.json ----

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
        # Ключ — постоянный id: у остальных устройств ничего
        # не сдвигается, убираем только запись удалённого.
        state = load_json(STATE_FILE, {})
        if isinstance(state, dict) and device_id in state:
            del state[device_id]
            save_json(STATE_FILE, state)

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

    # Раньше лог всегда писал «файлов N», даже если каталог
    # бэкапов не удалялся (защита _is_safe_to_delete).
    if backup_removed:
        backups_status = f"удалено файлов {removed_bytes} ({size_str})"
    elif backup_path:
        backups_status = "каталог бэкапов сохранён"
    else:
        backups_status = "каталога бэкапов нет"

    logging.info(
        "🗑 Удалено устройство %s (%s): %s, SSH-ключ %s, "
        "запрос от %s",
        device_name,
        device_id,
        backups_status,
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
        monitoring — мониторинг: токен, адреса API, события
        notifications — уведомления: каналы и правила
        backup    — копия настроек программы
    """
    if tab not in ("general", "scheduler", "retention", "storage", "service",
                   "monitoring", "notifications", "backup"):
        tab = "general"

    # Вкладки используют service, scheduler_config, logs_size,
    # is_backup_running — без них Jinja падает с UndefinedError.
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

        storages = global_config.load_global()
        context["total_devices"] = sum(
            len(pd.get("routers", []))
            for pd in (plugin_manager._plugins.values() if plugin_manager else [])
        )
        context["msg_count"] = request.query_params.get("count", "0")
        context["encryption_password_set"] = storage_encryption.is_password_set()
        context["encryption_available"] = storage_encryption.is_available()
        context["encryption_min_length"] = storage_encryption.MIN_PASSWORD_LENGTH
        context["remotes"] = [
            {
                "backend": b,
                "remote": storages.get(b.id) or {},
                "configured": global_config.is_configured(b, storages),
                "devices": sum(
                    1
                    for pd in (plugin_manager._plugins.values() if plugin_manager else [])
                    for r in pd.get("routers", [])
                    if global_config.is_enabled_for(r, b)
                ),
            }
            for b in _external_backends()
        ]
        context["error"] = request.query_params.get("error", "")
        context["sync_all_running"] = is_sync_all_running()
        context["sync_all_devices"] = len(_sync_all_targets())

    elif tab == "monitoring":
        context.update(_monitoring_context(request))

    elif tab == "notifications":
        context.update(_notifications_context(request))

    elif tab == "backup":
        context.update(_settings_backup_context(request))

    return templates.TemplateResponse(
        request,
        "settings.html",
        context,
    )


def _monitoring_context(request: Request, new_token: str = "") -> dict:
    base = str(request.base_url).rstrip("/")
    return {
        "token": monitoring.token_info(),
        "new_token": new_token,
        "base_url": base,
        "health_url": f"{base}/api/health",
        "status_url": f"{base}/api/status",
        "events_url": f"{base}/api/events",
        "zabbix_template_url": "/static/monitoring/zbx_nbm_template.json",
        "recent_events": events.recent(limit=30),
        "status": monitoring.status(plugin_manager, scheduler),
    }


@app.post("/settings/monitoring/token", response_class=HTMLResponse)
def settings_monitoring_token(
    request: Request,
    user: str = Depends(require_auth),
):
    """
    Новый токен мониторинга. Показывается один раз — прямо в
    ответе (не редиректом: токен не должен попасть в адрес
    страницы и историю браузера).
    """
    token = monitoring.generate_token()
    logging.info("🔑 Создан новый токен мониторинга")
    context = {
        "active_tab": "monitoring",
        "message": "",
        "service": get_status(),
        "scheduler_config": scheduler.config,
        "logs_size": format_size(get_dir_size(LOGS_DIR)),
        "is_backup_running": is_backup_running(),
        **_monitoring_context(request, new_token=token),
    }
    response = templates.TemplateResponse(request, "settings.html", context)
    response.headers["Cache-Control"] = "no-store"
    return response


@app.post("/settings/monitoring/token/revoke")
def settings_monitoring_token_revoke(user: str = Depends(require_auth)):
    monitoring.revoke_token()
    logging.info("🔑 Токен мониторинга отозван")
    return RedirectResponse("/settings?tab=monitoring&message=revoked", status_code=303)


# =========================================================
# КОПИЯ НАСТРОЕК
# =========================================================

def _settings_backup_context(request: Request, **extra) -> dict:
    storages = global_config.load_global()
    auto = settings_backup.load_auto()
    when = settings_backup.next_run(auto)
    return {
        "backup_files": len(settings_backup.collect_files()),
        "backup_min_length": settings_backup.MIN_PASSWORD_LENGTH,
        "encryption_password_set": storage_encryption.is_password_set(),
        "encryption_available": storage_encryption.is_available(),
        "rclone_available": rclone_mod.is_available(),
        "backup_remotes": [
            {
                "id": b.id,
                "name": b.name,
                "icon": b.icon,
                "configured": global_config.is_configured(b, storages),
                "dir": settings_backup.remote_dir((storages.get(b.id) or {}).get("base_dir") or ""),
            }
            for b in _external_backends()
            if hasattr(b, "put_file")
        ],
        "auto": auto,
        "auto_next": when.strftime("%Y-%m-%d %H:%M") if when else "",
        "auto_running": settings_backup.is_running(),
        "restore_max_mb": settings_backup.MAX_RESTORE_BYTES // (1024 * 1024),
        "error": request.query_params.get("error", ""),
        "restore": None,
        "restore_token": "",
        **extra,
    }


def _settings_backup_page(request: Request, **extra):
    context = {
        "active_tab": "backup",
        "message": request.query_params.get("message", ""),
        "service": get_status(),
        "scheduler_config": scheduler.config,
        "logs_size": format_size(get_dir_size(LOGS_DIR)),
        "is_backup_running": is_backup_running(),
        **_settings_backup_context(request, **extra),
    }
    response = templates.TemplateResponse(request, "settings.html", context)
    response.headers["Cache-Control"] = "no-store"
    return response


def _backup_user_message(exc: Exception) -> str:
    """
    Текст ошибки для пользователя: свои понятные сообщения —
    как есть, неожиданные (пути, тексты ОС) — только в журнал.
    """
    if isinstance(exc, settings_backup.SettingsBackupError):
        return str(exc)
    return "ошибка на сервере — подробности в общем логе"


def _backup_error(text: str):
    return RedirectResponse(
        "/settings?tab=backup&error=" + urllib.parse.quote(text), status_code=303,
    )


# Плановая копия настроек — на круге планировщика
scheduler.extra_jobs.append(lambda: settings_backup.tick(_external_backends))


@app.post("/settings/backup/download")
async def settings_backup_download(request: Request, user: str = Depends(require_auth)):
    """
    Архив папки data/ в браузер: с паролем — 7z (AES-256),
    без пароля — zip.
    """
    form = await request.form()
    password = form.get("password") or ""
    error = settings_backup.validate_password(password, form.get("confirm") or "")
    if error:
        return _backup_error(error)

    tmpdir = Path(tempfile.mkdtemp(prefix="nbm_settings_dl_"))
    try:
        path = await run_in_threadpool(settings_backup.build, tmpdir, password)
    except Exception as exc:
        shutil.rmtree(tmpdir, ignore_errors=True)
        logging.error("💾 Копия настроек не создана: %s", exc, exc_info=True)
        return _backup_error(_backup_user_message(exc))
    logging.info(
        "💾 Копия настроек скачана: %s (%s, запрос от %s)",
        path.name, "с паролем" if password else "без пароля", user,
    )
    return FileResponse(
        path,
        filename=path.name,
        media_type="application/x-7z-compressed" if password else "application/zip",
        headers={"Cache-Control": "no-store"},
        background=BackgroundTask(shutil.rmtree, tmpdir, ignore_errors=True),
    )


@app.post("/api/settings-backup/upload")
async def api_settings_backup_upload(request: Request, user: str = Depends(require_auth)):
    """
    Копия настроек на хранилища: {"ids": [...]}. Всегда 7z
    с паролем шифрования хранилищ — в копии secret.key и
    закрытые SSH-ключи. Старые копии удаляются по настройке
    «Хранить копий». → {"ok", "message", "results": [...]}.
    """
    try:
        data = await request.json()
    except Exception:
        data = {}
    ids = data.get("ids") if isinstance(data, dict) else None
    if not isinstance(ids, list) or not ids:
        return {"ok": False, "message": "Не выбрано ни одного хранилища", "results": []}

    password = storage_encryption.get_password()
    if not password:
        return {
            "ok": False,
            "message": "Задайте пароль шифрования (Настройки → Хранилища → Шифрование): "
                       "без него копия на хранилище не выгружается",
            "results": [],
        }

    targets, error = settings_backup.resolve_targets(
        ids, _external_backends(), global_config.load_global(),
    )
    if error:
        return {"ok": False, "message": error[:1].upper() + error[1:], "results": []}

    keep = settings_backup.load_auto().get("keep", 0)
    try:
        name, results = await run_in_threadpool(settings_backup.upload, targets, password, keep)
    except Exception as exc:
        logging.error("💾 Копия настроек не создана: %s", exc, exc_info=True)
        return {"ok": False, "message": "Копия не создана: " + _backup_user_message(exc),
                "results": []}

    ok = all(r["ok"] for r in results)
    return {
        "ok": ok,
        "message": f"{name}: " + ("выгружено" if ok else "есть ошибки"),
        "results": results,
    }


@app.post("/settings/backup/auto")
async def settings_backup_auto(request: Request, user: str = Depends(require_auth)):
    """Расписание автоматической копии на хранилища."""
    form = await request.form()
    cfg, error = settings_backup.validate_auto({
        "enabled": form.get("enabled"),
        "every_days": form.get("every_days"),
        "time": form.get("time"),
        "keep": form.get("keep"),
        "storages": form.getlist("storages"),
    })
    if not error and cfg["enabled"]:
        if not storage_encryption.is_password_set():
            error = "сначала задайте пароль шифрования хранилищ"
        else:
            _, error = settings_backup.resolve_targets(
                cfg["storages"], _external_backends(), global_config.load_global(),
            )
    if error:
        return _backup_error("Копия по расписанию: " + error)
    settings_backup.save_auto(cfg)
    logging.info(
        "💾 Копия настроек по расписанию: %s (запрос от %s)",
        f"каждые {cfg['every_days']} дн. в {cfg['time']}, хранить {cfg['keep']}"
        if cfg["enabled"] else "выключена", user,
    )
    return RedirectResponse("/settings?tab=backup&message=auto_saved", status_code=303)


# ---------------------------------------------------------
# Восстановление: загрузка → проверка → подтверждение
# ---------------------------------------------------------

# Распакованный архив ждёт подтверждения: {token: (папка, время)}.
# Одновременно — один: новая загрузка убирает прежнюю.
_restore_staged: dict[str, tuple[Path, float]] = {}
_restore_lock = threading.Lock()
_RESTORE_TTL = 30 * 60


def _drop_staged(token: str | None = None) -> None:
    with _restore_lock:
        for t in list(_restore_staged):
            path, at = _restore_staged[t]
            if token is None or t == token or time.time() - at > _RESTORE_TTL:
                shutil.rmtree(path, ignore_errors=True)
                del _restore_staged[t]


@app.post("/settings/backup/restore/upload", response_class=HTMLResponse)
async def settings_backup_restore_upload(request: Request, user: str = Depends(require_auth)):
    form = await request.form()
    upload = form.get("file")
    if upload is None or not hasattr(upload, "read"):
        return _backup_error("Восстановление: выберите файл архива")
    raw = await upload.read(settings_backup.MAX_RESTORE_BYTES + 1)
    if not raw:
        return _backup_error("Восстановление: файл пустой")
    if len(raw) > settings_backup.MAX_RESTORE_BYTES:
        return _backup_error("Восстановление: файл слишком большой для копии настроек")

    _drop_staged()
    workdir = Path(tempfile.mkdtemp(prefix="nbm_restore_"))
    archive = workdir / "archive.bin"
    archive.write_bytes(raw)
    staged = workdir / "staged"
    staged.mkdir()
    try:
        info = await run_in_threadpool(
            settings_backup.stage_restore, archive, form.get("password") or "", staged,
        )
    except settings_backup.SettingsBackupError as exc:
        shutil.rmtree(workdir, ignore_errors=True)
        return _backup_error(f"Восстановление: {exc}")
    except Exception as exc:
        shutil.rmtree(workdir, ignore_errors=True)
        logging.error("💾 Архив для восстановления не прочитан: %s", exc, exc_info=True)
        return _backup_error("Восстановление: архив не прочитан: " + _backup_user_message(exc))
    finally:
        archive.unlink(missing_ok=True)

    token = secrets.token_urlsafe(24)
    with _restore_lock:
        _restore_staged[token] = (workdir, time.time())
    info["filename"] = getattr(upload, "filename", "") or ""
    return _settings_backup_page(request, restore=info, restore_token=token)


@app.post("/settings/backup/restore/cancel")
async def settings_backup_restore_cancel(request: Request, user: str = Depends(require_auth)):
    form = await request.form()
    _drop_staged(str(form.get("token") or "") or None)
    return RedirectResponse("/settings?tab=backup", status_code=303)


def _restart_after_restore() -> tuple[bool, str]:
    """
    Перезапуск, чтобы программа перечитала настройки.
    Docker и systemd поднимают процесс сами после выхода,
    служба Windows — через schedule_restart. Запущенную
    вручную программу нужно запустить снова.
    """
    from core import settings as nbm_settings

    manager = nbm_settings.service_manager()
    if manager in ("docker", "systemd") or not is_service_process():
        def _exit():
            time.sleep(2)
            logging.info("💾 Выход после восстановления настроек")
            logging.shutdown()
            os._exit(1 if manager else 0)

        threading.Thread(target=_exit, daemon=True).start()
        if manager:
            return True, ("Программа перезапускается. Через полминуты обновите "
                          "страницу и войдите с логином и паролем из копии.")
        return True, ("Программа завершает работу — запустите её снова. "
                      "Входите с логином и паролем из копии.")
    return schedule_restart()


@app.post("/settings/backup/restore/apply", response_class=HTMLResponse)
async def settings_backup_restore_apply(request: Request, user: str = Depends(require_auth)):
    form = await request.form()
    token = str(form.get("token") or "")
    with _restore_lock:
        entry = _restore_staged.get(token)
    if entry is None or time.time() - entry[1] > _RESTORE_TTL:
        _drop_staged(token)
        return _backup_error("Восстановление: архив устарел — загрузите его ещё раз")
    if not form.get("confirm"):
        return _backup_error("Восстановление: подтвердите замену настроек")

    if not backup_state.try_start_mass():
        return _backup_error("Восстановление: идёт бэкап — дождитесь окончания")

    workdir = entry[0]
    try:
        # Планировщик не должен записать старые настройки
        # поверх восстановленных до перезапуска.
        await run_in_threadpool(scheduler.stop)
        saved = await run_in_threadpool(settings_backup.apply_restore, workdir / "staged")
    except Exception as exc:
        backup_state.finish_mass()
        scheduler.start()
        logging.error("💾 Восстановление не удалось: %s", exc, exc_info=True)
        return _backup_error("Восстановление не удалось: " + _backup_user_message(exc))
    finally:
        _drop_staged(token)

    logging.warning("💾 Настройки восстановлены пользователем %s; прежние — %s", user, saved)
    ok, message = _restart_after_restore()
    return templates.TemplateResponse(
        request,
        "service_action.html",
        {
            "title": "Настройки восстановлены",
            "message": (
                f"Прежние настройки сохранены в data/{saved.name}. " + message
            ),
            "ok": ok,
        },
    )


# =========================================================
# УВЕДОМЛЕНИЯ
# =========================================================

def _notifications_context(
    request: Request,
    edit: dict | None = None,
    form_error: str = "",
) -> dict:
    """
    Вкладка «Уведомления». edit — канал в форме правки
    (новый или существующий); иначе берётся из ?edit=<id> /
    ?new=<тип>. Секретные значения в форму не попадают.
    """
    from core.notify_channels import CHANNELS

    q = request.query_params
    if edit is None:
        if q.get("edit"):
            edit = notifications.get_channel(q["edit"])
        elif q.get("new") in CHANNELS:
            edit = notifications.new_channel(q["new"])

    edit_view = None
    if edit is not None:
        impl = CHANNELS[edit["type"]]
        secrets_ = set(impl.secret_fields())
        stored = edit.get("settings") or {}
        # «Задано» — только про сохранённое: секрет из формы,
        # вернувшейся с ошибкой, нигде не записан.
        persisted = notifications.get_channel(edit["id"])
        saved = (persisted or {}).get("settings") or {}
        edit_view = {
            "channel": edit,
            "impl": impl,
            "is_new": persisted is None,
            "values": {
                f.name: ("" if f.name in secrets_ else stored.get(f.name, f.default))
                for f in impl.fields
            },
            "secret_set": {name: bool(saved.get(name)) for name in secrets_},
        }

    return {
        "channels": notifications.load_config()["channels"],
        "channel_types": CHANNELS,
        "channel_status": dict(notifications.dispatcher.status),
        "edit": edit_view,
        "form_error": form_error,
        "notice": q.get("notice", ""),
        "notice_ok": q.get("ok", "1") == "1",
    }


def _notifications_page(request: Request, **kw):
    context = {
        "active_tab": "notifications",
        "message": "",
        "service": get_status(),
        "scheduler_config": scheduler.config,
        "logs_size": format_size(get_dir_size(LOGS_DIR)),
        "is_backup_running": is_backup_running(),
        **_notifications_context(request, **kw),
    }
    return templates.TemplateResponse(request, "settings.html", context)


def _notifications_redirect(notice: str = "", ok: bool = True):
    from urllib.parse import urlencode
    query = {"tab": "notifications"}
    if notice:
        query.update(notice=notice, ok="1" if ok else "0")
    return RedirectResponse("/settings?" + urlencode(query), status_code=303)


@app.post("/settings/notifications/save", response_class=HTMLResponse)
async def settings_notifications_save(
    request: Request,
    user: str = Depends(require_auth),
):
    from core.notify_channels import CHANNELS

    form = await request.form()
    channel_type = str(form.get("type") or "")
    if channel_type not in CHANNELS:
        raise HTTPException(400, "Неизвестный тип канала")

    config = notifications.load_config()
    channel_id = str(form.get("id") or "")
    existing = next((c for c in config["channels"] if c["id"] == channel_id), None)
    if existing is not None and existing["type"] != channel_type:
        raise HTTPException(400, "Тип канала не меняется")

    channel = dict(existing) if existing else notifications.new_channel(channel_type)
    if not existing and channel_id:
        channel["id"] = channel_id
    impl = CHANNELS[channel_type]

    plain = {f.name: str(form.get(f"f_{f.name}") or "").strip() for f in impl.fields}
    channel["name"] = str(form.get("name") or "").strip() or impl.name
    channel["enabled"] = form.get("enabled") == "on"
    channel["rules"] = notifications.normalize_rules({
        key: form.get(key) == "on"
        for key in ("errors", "recovered", "config", "include_diff", "app")
    } | {"run": form.get("run")})
    channel["settings"] = notifications.encrypt_settings(
        channel_type, plain, (existing or {}).get("settings"),
    )

    error = impl.validate(notifications.decrypt_settings(channel))
    if error:
        # Показать форму снова — с введёнными значениями
        # (секретные поля — только признак «задано»).
        return _notifications_page(request, edit=channel, form_error=error)

    if existing:
        config["channels"] = [channel if c["id"] == channel["id"] else c
                              for c in config["channels"]]
    else:
        config["channels"].append(channel)
    notifications.save_config(config)
    logging.info("🔔 Канал уведомлений «%s» (%s) сохранён", channel["name"], channel_type)
    return _notifications_redirect(f"Канал «{channel['name']}» сохранён")


@app.post("/settings/notifications/{channel_id}/delete")
def settings_notifications_delete(channel_id: str, user: str = Depends(require_auth)):
    config = notifications.load_config()
    channel = next((c for c in config["channels"] if c["id"] == channel_id), None)
    if channel is None:
        raise HTTPException(404, "Канал не найден")
    config["channels"] = [c for c in config["channels"] if c["id"] != channel_id]
    notifications.save_config(config)
    logging.info("🔔 Канал уведомлений «%s» удалён", channel["name"])
    return _notifications_redirect(f"Канал «{channel['name']}» удалён")


@app.post("/settings/notifications/{channel_id}/toggle")
def settings_notifications_toggle(channel_id: str, user: str = Depends(require_auth)):
    config = notifications.load_config()
    for c in config["channels"]:
        if c["id"] == channel_id:
            c["enabled"] = not c.get("enabled")
            notifications.save_config(config)
            state = "включён" if c["enabled"] else "выключен"
            return _notifications_redirect(f"Канал «{c['name']}» {state}")
    raise HTTPException(404, "Канал не найден")


@app.post("/settings/notifications/{channel_id}/test")
def settings_notifications_test(channel_id: str, user: str = Depends(require_auth)):
    channel = notifications.get_channel(channel_id)
    if channel is None:
        raise HTTPException(404, "Канал не найден")
    ok, detail = notifications.dispatcher.send_test(channel)
    if ok:
        return _notifications_redirect(f"Проверочное сообщение в «{channel['name']}» отправлено")
    return _notifications_redirect(f"«{channel['name']}»: {detail}", ok=False)


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

    tag_rows = _clamp_tag_rows(form.get("tag_rows"))

    # Загружаем существующий config, чтобы не потерять retention и пр.
    config = load_json(CONFIG_FILE, {})
    config["host"] = host
    config["port"] = port
    config["open_browser"] = open_browser
    config["max_parallel_backups"] = max_parallel
    config["backup_timeout_seconds"] = timeout
    config["tag_rows"] = tag_rows

    try:
        save_json(CONFIG_FILE, config)
    except Exception as exc:
        logging.error("settings: не удалось сохранить config.json: %s", exc, exc_info=True)
        raise HTTPException(500, "Не удалось сохранить настройки")

    logging.info(
        "⚙ Общие настройки обновлены: host=%s, port=%d, "
        "parallel=%d, timeout=%d, tag_rows=%d (от %s)",
        host, port, max_parallel, timeout, tag_rows, user,
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