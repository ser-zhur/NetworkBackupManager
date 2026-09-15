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
from datetime import datetime
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
    DATA_DIR,
    LOGS_DIR,
    BACKUPS_DIR,
    PLUGINS_DIR,
    WEB_DIR,
    AUTH_FILE,
    load_json,
)

from core.logger import (
    setup_logging,
    set_device_context,
    clear_device_context,
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
# PLUGIN MANAGER
# =========================================================

try:

    from core.plugin_manager import (
        PluginManager,
    )

    plugin_manager = PluginManager()

    plugin_manager.discover_and_load_plugins()

except Exception as exc:

    logging.error(
        "Ошибка загрузки PluginManager: %s",
        exc,
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
# PASSWORD
# =========================================================

def hash_password(
    password: str,
    salt: bytes | None = None,
) -> str:

    if salt is None:

        salt = os.urandom(16)

    key = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        260000,
    )

    return (
        "pbkdf2_sha256$260000$"
        + base64.b64encode(
            salt
        ).decode()
        + "$"
        + base64.b64encode(
            key
        ).decode()
    )


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
):
    """
    Читает хвост файла логов.

    Если filter_tag задан — оставляет только строки,
    содержащие подстроку '[filter_tag]' (маркер устройства).
    """

    if not file_path.exists():
        return (
            "Лог-файл пока отсутствует...",
            0,
            False,
        )

    try:
        with open(
            file_path,
            "r",
            encoding="utf-8",
            errors="ignore",
        ) as f:
            lines = f.readlines()
    except Exception as exc:
        return (
            f"Ошибка чтения файла логов: {exc}",
            0,
            False,
        )

    if filter_tag:
        needle = f"[{filter_tag}]"
        lines = [ln for ln in lines if needle in ln]

    total_lines = len(lines)

    if limit == "all":
        return ("".join(lines), total_lines, False)

    max_l = int(limit) if limit.isdigit() else 100

    if total_lines > max_l:
        tail = lines[-max_l:]
        return ("".join(tail), len(tail), True)

    return ("".join(lines), total_lines, False)

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
        
        invalidate_status_cache()

        created_files = backup_func(secure_router)

        if created_files and hasattr(plugin_manager, "_rotate_local"):
            plugin_manager._rotate_local(
                secure_router.get("backup_path"),
                secure_router.get("max_backups", 7),
            )

        logging.info("✅ [Фон] Бэкап успешно завершен для %s", ip_disp)
        
        invalidate_status_cache()

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

@app.get(
    "/",
    response_class=HTMLResponse,
)
def read_root(
    request: Request,
    user: str = Depends(
        require_auth
    ),
):

    devices = []

    if (
        plugin_manager
        and hasattr(
            plugin_manager,
            "_plugins",
        )
    ):

        for (
            device_type,
            plugin_data,
        ) in (
            plugin_manager
            ._plugins
            .items()
        ):

            routers = plugin_data.get(
                "routers",
                [],
            )

            for index, router in enumerate(
                routers
            ):

                ip_addr = (
                    router.get("host")
                    or router.get("ip")
                    or router.get(
                        "address",
                        "Не указан",
                    )
                )

                b_path = router.get(
                    "backup_path",
                    "backups/",
                )

                (
                    count,
                    last_date,
                    age_days,
                ) = get_device_backup_info(
                    b_path
                )

                if (
                    count == 0
                    or age_days is None
                ):

                    row_class = (
                        "bg-white "
                        "hover:bg-gray-50"
                    )

                elif age_days < 15:

                    row_class = (
                        "bg-emerald-50/80 "
                        "hover:bg-emerald-100/80"
                    )

                elif age_days <= 30:

                    row_class = (
                        "bg-amber-100/70 "
                        "hover:bg-amber-200/70"
                    )

                else:

                    row_class = (
                        "bg-rose-100/80 "
                        "hover:bg-rose-200/80"
                    )

                devices.append({
                    "type":
                        device_type,

                    "ip":
                        ip_addr,

                    "backup_path":
                        b_path,

                    "backup_dir_name":
                        Path(
                            b_path
                        ).name,

                    "backup_count":
                        count,

                    "last_backup_date":
                        last_date,

                    "row_class":
                        row_class,

                    "index":
                        index,
                })

    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "devices":
                devices,

            "scheduler_status":
                scheduler.get_status_text(),

            "is_backup_running":
                is_backup_running(),

            "service":
                get_status(),
        },
    )


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
                plugins_list
        },
    )


# =========================================================
# SAVE ROUTER
# =========================================================

@app.post(
    "/save-router"
)
async def save_router_endpoint(
    user: str = Depends(
        require_auth
    ),
    device_type: str = Form(...),
    ip: str = Form(...),
    version: str = Form("7"),
    username: str = Form("admin"),
    ssh_pass: str = Form(""),
    private_key: str = Form(""),
    backup_path: str = Form(...),
    max_backups: int = Form(5),
    ftp_host: str = Form(""),
    ftp_dir: str = Form(""),
    ftp_user: str = Form(""),
    ftp_pass: str = Form(""),
):

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

        new_router[
            "port"
        ] = 6132

    if ftp_host:

        new_router["ftp"] = {
            "host":
                ftp_host,

            "port":
                21,

            "remote_dir":
                ftp_dir,

            "user":
                ftp_user,

            "pass":
                storage.encrypt(
                    ftp_pass
                )
                if ftp_pass
                else "",
        }

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

            if item.is_dir():

                folder_name = (
                    item.name
                )

                files_in_dir = sorted(
                    [
                        f
                        for f in item.iterdir()
                        if f.is_file()
                    ],
                    key=lambda x:
                        x.name,
                )

                for f in files_in_dir:

                    size_bytes = (
                        f.stat()
                        .st_size
                    )

                    if (
                        size_bytes
                        >= 1024
                    ):

                        size_str = (
                            f"{size_bytes / 1024:.1f} KB"
                        )

                    else:

                        size_str = (
                            f"{size_bytes} B"
                        )

                    rel_path = (
                        f"{folder_name}/"
                        f"{f.name}"
                    )

                    grouped_backups[
                        folder_name
                    ].append({
                        "name":
                            f.name,

                        "rel_path":
                            rel_path,

                        "size":
                            size_str,
                    })

            elif item.is_file():

                size_bytes = (
                    item.stat()
                    .st_size
                )

                if size_bytes >= 1024:

                    size_str = (
                        f"{size_bytes / 1024:.1f} KB"
                    )

                else:

                    size_str = (
                        f"{size_bytes} B"
                    )

                grouped_backups[
                    "Корневой каталог"
                ].append({
                    "name":
                        item.name,

                    "rel_path":
                        item.name,

                    "size":
                        size_str,
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

    content, lines_shown, has_more = (
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
# GLOBAL LOG
# =========================================================

@app.get(
    "/logs/global",
    response_class=HTMLResponse,
)
def logs_global_endpoint(
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

    content, lines_shown, has_more = (
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
                "Web Server Log",

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

    content, lines_shown, has_more = read_log_tail(log_file, limit)

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

    content, lines_shown, has_more = (
        read_log_tail(
            log_file,
            limit,
            filter_tag=filter_tag,
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
    user: str = Depends(
        require_auth
    ),
    ip: str = Form(...),
    version: str = Form("7"),
    username: str = Form("admin"),
    ssh_pass: str = Form(""),
    private_key: str = Form(""),
    backup_path: str = Form(...),
    max_backups: int = Form(5),
    ftp_host: str = Form(""),
    ftp_dir: str = Form(""),
    ftp_user: str = Form(""),
    ftp_pass: str = Form(""),
    port: int = Form(22),
    ftp_port: int = Form(21),
):

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

    if ftp_host:

        # Если новый FTP-пароль не введён — оставляем старый.
        if ftp_pass:
            ftp_pass_encrypted = storage.encrypt(ftp_pass)
        else:
            old_ftp = old_router.get("ftp") or {}
            ftp_pass_encrypted = old_ftp.get("pass", "")

        updated_router["ftp"] = {
            "host":
                ftp_host,

            "port":
                int(ftp_port),

            "remote_dir":
                ftp_dir,

            "user":
                ftp_user,

            "pass":
                ftp_pass_encrypted,
        }

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