import os
import json
import copy
import traceback
import logging
import re
import threading
import secrets
import hashlib
import base64
from pathlib import Path
from datetime import datetime
from collections import defaultdict
from logging.handlers import RotatingFileHandler
from fastapi import FastAPI, HTTPException, Request, Form, BackgroundTasks, Depends, status
from contextlib import asynccontextmanager
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from core.secure_storage import SecureStorage
from core.scheduler import BackupScheduler

# --- 1. Базовые пути и логирование ---
BASE_DIR = Path(__file__).resolve().parent.parent
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

file_handler = RotatingFileHandler(
    LOG_DIR / "app.log",
    maxBytes=2 * 1024 * 1024,
    backupCount=3,
    encoding="utf-8"
)
file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))

logging.basicConfig(
    level=logging.INFO,
    handlers=[file_handler, logging.StreamHandler()]
)

# --- 2. Учет состояния выполняющихся бэкапов ---
backup_lock = threading.Lock()
active_backups_count = 0

def is_backup_running() -> bool:
    """Проверка: выполняется ли сейчас хотя бы один бэкап"""
    with backup_lock:
        return active_backups_count > 0

def backup_started():
    global active_backups_count
    with backup_lock:
        active_backups_count += 1

def backup_finished():
    global active_backups_count
    with backup_lock:
        if active_backups_count > 0:
            active_backups_count -= 1


# --- 3. Инициализация компонентов ядра ---
DATE_DIR_REGEX = re.compile(r"^\d{4}-\d{2}-\d{2}$")
storage = SecureStorage(str(BASE_DIR))
security = HTTPBasic()

try:
    from core.plugin_manager import PluginManager
    plugin_manager = PluginManager()
    plugin_manager.discover_and_load_plugins()
except Exception as e:
    logging.error(f"Ошибка загрузки PluginManager: {e}")
    plugin_manager = None

scheduler = BackupScheduler(plugin_manager)

@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.info("🚀 Запуск планировщика...")
    scheduler.start()

    try:
        yield
    finally:
        logging.info("🛑 Остановка планировщика...")
        scheduler.stop()

app = FastAPI(
    title="Network Backup Manager",
    lifespan=lifespan
)

app.mount("/static", StaticFiles(directory=str(BASE_DIR / "web" / "static")), name="static")

templates = None
if (BASE_DIR / "web" / "templates").exists():
    templates = Jinja2Templates(directory=str(BASE_DIR / "web" / "templates"))


# --- 4. Вспомогательные функции ---

def hash_password(password: str, salt: bytes | None = None) -> str:
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
        + base64.b64encode(salt).decode()
        + "$"
        + base64.b64encode(key).decode()
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        algorithm, iterations, salt_b64, key_b64 = stored.split("$")

        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(key_b64)

        test = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt,
            int(iterations),
        )

        return secrets.compare_digest(test, expected)

    except Exception:
        return False


def require_auth(credentials: HTTPBasicCredentials = Depends(security)):
    auth_file = BASE_DIR / "data" / "auth.json"

    if not auth_file.exists():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Файл auth.json не найден",
            headers={"WWW-Authenticate": "Basic"},
        )

    try:
        data = json.loads(auth_file.read_text(encoding="utf-8"))

        username = data["username"]
        password_hash = data["password_hash"]

    except Exception as e:
        logging.error(f"Ошибка чтения auth.json: {e}")

        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Некорректный файл auth.json",
            headers={"WWW-Authenticate": "Basic"},
        )

    valid = (
        secrets.compare_digest(credentials.username, username)
        and verify_password(credentials.password, password_hash)
    )

    if not valid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Неверный логин или пароль",
            headers={"WWW-Authenticate": "Basic"},
        )

    return credentials.username



def read_log_tail(file_path: Path, limit: str = "100"):
    if not file_path.exists():
        return "Лог-файл пока отсутствует...", 0, False

    try:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()

        total_lines = len(lines)

        if limit == "all":
            return "".join(lines), total_lines, False

        max_l = int(limit) if limit.isdigit() else 100
        if total_lines > max_l:
            tail = lines[-max_l:]
            return "".join(tail), len(tail), True

        return "".join(lines), total_lines, False

    except Exception as e:
        return f"Ошибка чтения файла логов: {e}", 0, False


def get_device_backup_info(backup_path_str: str):
    if not backup_path_str:
        return 0, "Нет данных", None

    path = Path(backup_path_str)
    if not path.is_absolute():
        path = BASE_DIR / path

    if not path.exists() or not path.is_dir():
        return 0, "Нет данных", None

    valid_backup_dirs = []
    for item in path.rglob("*"):
        if item.is_dir() and DATE_DIR_REGEX.match(item.name):
            has_files = any(f.is_file() for f in item.rglob("*"))
            if has_files:
                valid_backup_dirs.append(item)

    if not valid_backup_dirs:
        return 0, "Нет данных", None

    count = len(valid_backup_dirs)

    newest_mtime = 0
    for d in valid_backup_dirs:
        for f in d.rglob("*"):
            if f.is_file():
                mtime = f.stat().st_mtime
                if mtime > newest_mtime:
                    newest_mtime = mtime

    if newest_mtime > 0:
        dt_last = datetime.fromtimestamp(newest_mtime)
        last_date = dt_last.strftime("%Y-%m-%d %H:%M")
        age_days = (datetime.now() - dt_last).days
    else:
        latest_dir = max(valid_backup_dirs, key=lambda d: d.name)
        last_date = latest_dir.name
        try:
            dt_last = datetime.strptime(latest_dir.name, "%Y-%m-%d")
            age_days = (datetime.now() - dt_last).days
        except Exception:
            age_days = 0

    return count, last_date, age_days


def execute_device_backup_task(device_type: str, index: int, secure_router: dict):
    """Выполнение одиночного бэкапа с учетом состояния активности"""
    backup_started()
    try:
        plugin = plugin_manager._plugins.get(device_type) if plugin_manager else None
        if not plugin:
            return

        backup_func = plugin.get("func")
        if not backup_func:
            return

        ip_disp = secure_router.get("host") or secure_router.get("ip")
        logging.info(f"🚀 [Фон] Запуск бэкапа: {device_type} / {ip_disp}")

        created_files = backup_func(secure_router)
        if created_files and hasattr(plugin_manager, "_rotate_local"):
            plugin_manager._rotate_local(secure_router.get("backup_path"), secure_router.get("max_backups", 7))
        logging.info(f"✅ [Фон] Бэкап успешно завершен для {ip_disp}")
    except Exception as e:
        logging.error(f"❌ [Фон] Ошибка при бэкапе: {e}")
    finally:
        backup_finished()


def execute_all_backups_task():
    """Выполнение массового бэкапа всех устройств с учетом состояния активности"""
    backup_started()
    try:
        if plugin_manager:
            plugin_manager.run_all_backups()
    except Exception as e:
        logging.error(f"❌ [Фон] Ошибка при массовом бэкапе: {e}")
    finally:
        backup_finished()


# --- 5. Маршруты страниц (Routes) ---

@app.get("/", response_class=HTMLResponse)
def read_root(
    request: Request,
    user: str = Depends(require_auth)
):
    """Главная страница"""
    if not templates:
        return HTMLResponse("<h2>⚠️ Папка web/templates не найдена</h2>", status_code=500)

    devices = []
    if plugin_manager and hasattr(plugin_manager, "_plugins"):
        for device_type, plugin_data in plugin_manager._plugins.items():
            routers = plugin_data.get("routers", [])
            for index, router in enumerate(routers):
                ip_addr = router.get("host") or router.get("ip") or router.get("address", "Не указан")
                b_path = router.get("backup_path", "backups/")
                
                count, last_date, age_days = get_device_backup_info(b_path)

                if count == 0 or age_days is None:
                    row_class = "bg-white hover:bg-gray-50"
                elif age_days < 15:
                    row_class = "bg-emerald-50/80 hover:bg-emerald-100/80"
                elif 15 <= age_days <= 30:
                    row_class = "bg-amber-100/70 hover:bg-amber-200/70"
                else:
                    row_class = "bg-rose-100/80 hover:bg-rose-200/80"

                devices.append({
                    "type": device_type,
                    "ip": ip_addr,
                    "backup_path": b_path,
                    "backup_dir_name": Path(b_path).name,
                    "backup_count": count,
                    "last_backup_date": last_date,
                    "row_class": row_class,
                    "index": index
                })

    return templates.TemplateResponse(request, "index.html", {
        "devices": devices,
        "scheduler_status": scheduler.get_status_text(),
        "is_backup_running": is_backup_running()
    })


@app.get("/add", response_class=HTMLResponse)
def add_router_page(
    request: Request,
    user: str = Depends(require_auth)
):
    plugins_list = list(plugin_manager._plugins.keys()) if plugin_manager else []
    if templates and (BASE_DIR / "web" / "templates" / "add.html").exists():
        return templates.TemplateResponse(request, "add.html", {
            "plugins": plugins_list
        })
    return HTMLResponse("<h3>Форма добавления недоступна</h3><a href='/'>Назад</a>")


@app.post("/save-router")
async def save_router_endpoint(
    user: str = Depends(require_auth),
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
    ftp_pass: str = Form("")
):
    if not plugin_manager or device_type not in plugin_manager._plugins:
        raise HTTPException(status_code=400, detail="Указанный плагин не найден")

    encrypted_ssh_pass = storage.encrypt(ssh_pass) if ssh_pass else ""
    
    new_router = {
        ("ip" if device_type.lower() == "mikrotik" else "host"): ip,
        "username": username,
        "ssh_pass": encrypted_ssh_pass,
        "backup_path": backup_path,
        "max_backups": int(max_backups)
    }

    if device_type.lower() == "mikrotik":
        new_router["version"] = version
        new_router["private_key"] = private_key if private_key else "id_rsa.key"
    else:
        new_router["ssh_key"] = private_key if private_key else "id_rsa.key"
        new_router["port"] = 6132

    if ftp_host:
        new_router["ftp"] = {
            "host": ftp_host,
            "port": 21,
            "remote_dir": ftp_dir,
            "user": ftp_user,
            "pass": storage.encrypt(ftp_pass) if ftp_pass else ""
        }

    plugin_data = plugin_manager._plugins[device_type]
    plugin_data["routers"].append(new_router)

    target_dir = BASE_DIR / "plugins"
    for p_folder in target_dir.iterdir():
        if p_folder.is_dir() and p_folder.name.lower() == device_type.lower():
            json_file = p_folder / "routers.json"
            with open(json_file, "w", encoding="utf-8") as f:
                json.dump(plugin_data["routers"], f, ensure_ascii=False, indent=4)
            break

    logging.info(f"➕ Добавлено новое устройство ({device_type}): {ip}")
    return RedirectResponse(url="/", status_code=303)


@app.get("/run-progress/{device_type}/{index}")
def run_backup(
    device_type: str,
    index: int,
    background_tasks: BackgroundTasks,
    user: str = Depends(require_auth)
):
    if is_backup_running():
        return RedirectResponse(url=f"/logs/device/{device_type}/{index}", status_code=303)

    plugin = plugin_manager._plugins.get(device_type) if plugin_manager else None
    if not plugin or index < 0 or index >= len(plugin.get("routers", [])):
        return HTMLResponse("<h3>Устройство не найдено</h3><a href='/'>Назад</a>", status_code=404)

    router = plugin["routers"][index]

    secure_router = copy.deepcopy(router)
    if "ssh_pass" in secure_router and secure_router["ssh_pass"]:
        secure_router["ssh_pass"] = storage.decrypt(secure_router["ssh_pass"])

    background_tasks.add_task(execute_device_backup_task, device_type, index, secure_router)
    return RedirectResponse(url=f"/logs/device/{device_type}/{index}", status_code=303)


@app.post("/run-now")
def run_now_endpoint(
    background_tasks: BackgroundTasks,
    user: str = Depends(require_auth)
):
    if not is_backup_running():
        background_tasks.add_task(execute_all_backups_task)
    return RedirectResponse(url="/logs/main", status_code=303)


@app.get("/backups/{device_type}/{index}", response_class=HTMLResponse)
def backups_page(
    request: Request,
    device_type: str,
    index: int,
    user: str = Depends(require_auth)
):
    plugin = plugin_manager._plugins.get(device_type) if plugin_manager else None
    if not plugin or index >= len(plugin.get("routers", [])):
        return HTMLResponse("<h3>Устройство не найдено</h3>", status_code=404)

    router = plugin["routers"][index]
    backup_path = Path(router.get("backup_path", "backups"))
    if not backup_path.is_absolute():
        backup_path = BASE_DIR / backup_path

    grouped_backups = defaultdict(list)

    if backup_path.exists() and backup_path.is_dir():
        for item in sorted(backup_path.iterdir(), reverse=True):
            if item.is_dir():
                folder_name = item.name
                files_in_dir = sorted(
                    [f for f in item.iterdir() if f.is_file()],
                    key=lambda x: x.name
                )
                for f in files_in_dir:
                    size_bytes = f.stat().st_size
                    size_str = f"{size_bytes / 1024:.1f} KB" if size_bytes >= 1024 else f"{size_bytes} B"
                    rel_path = f"{folder_name}/{f.name}"
                    
                    grouped_backups[folder_name].append({
                        "name": f.name,
                        "rel_path": rel_path,
                        "size": size_str
                    })
            elif item.is_file():
                size_bytes = item.stat().st_size
                size_str = f"{size_bytes / 1024:.1f} KB" if size_bytes >= 1024 else f"{size_bytes} B"
                grouped_backups["Корневой каталог"].append({
                    "name": item.name,
                    "rel_path": item.name,
                    "size": size_str
                })

    return templates.TemplateResponse(request, "backups.html", {
        "device_type": device_type,
        "index": index,
        "router": router,
        "grouped_backups": dict(grouped_backups),
        "backup_path": str(backup_path)
    })


@app.get("/scheduler", response_class=HTMLResponse)
def scheduler_page(
    request: Request,
    user: str = Depends(require_auth)
):
    return templates.TemplateResponse(request, "scheduler.html", {
        "config": scheduler.config
    })


@app.post("/scheduler/save")
async def save_scheduler_config(
    user: str = Depends(require_auth),
    enabled: bool = Form(False),
    mode: str = Form("daily"),
    daily_time: str = Form("03:00"),
    interval_hours: int = Form(24)
):
    scheduler.save_config({
        "enabled": enabled,
        "mode": mode,
        "daily_time": daily_time,
        "interval_hours": interval_hours
    })
    logging.info(f"⏰ Изменены настройки планировщика: {scheduler.get_status_text()}")
    return RedirectResponse(url="/", status_code=303)


@app.get("/download-backup/{device_type}/{index}/{filepath:path}")
def download_backup(
    device_type: str,
    index: int,
    filepath: str,
    user: str = Depends(require_auth)
):
    plugin = plugin_manager._plugins.get(device_type) if plugin_manager else None
    if not plugin or index >= len(plugin.get("routers", [])):
        raise HTTPException(status_code=404, detail="Устройство не найдено")

    router = plugin["routers"][index]
    backup_path = Path(router.get("backup_path", "backups"))
    if not backup_path.is_absolute():
        backup_path = BASE_DIR / backup_path

    file_full_path = (backup_path / filepath).resolve()

    if not str(file_full_path).startswith(str(backup_path.resolve())):
        raise HTTPException(status_code=403, detail="Отказано в доступе")

    if not file_full_path.exists() or not file_full_path.is_file():
        raise HTTPException(status_code=404, detail="Файл бэкапа не найден")

    return FileResponse(
        path=file_full_path,
        filename=file_full_path.name,
        media_type="application/octet-stream"
    )


@app.get("/logs/main", response_class=HTMLResponse)
def logs_main_endpoint(
    request: Request,
    limit: str = "100",
    raw: bool = False,
    user: str = Depends(require_auth)
):
    log_file = BASE_DIR / "network_backup.log"
    if not log_file.exists():
        log_file = LOG_DIR / "app.log"

    content, lines_shown, has_more = read_log_tail(log_file, limit)
    if raw:
        return HTMLResponse(content, media_type="text/plain")

    return templates.TemplateResponse(request, "logs.html", {
        "log_type": "Main Service Log",
        "content": content,
        "lines_shown": int(lines_shown),
        "has_more": has_more,
        "limit": limit
    })


@app.get("/logs/global", response_class=HTMLResponse)
def logs_global_endpoint(
    request: Request,
    limit: str = "100",
    raw: bool = False,
    user: str = Depends(require_auth)
):
    log_file = LOG_DIR / "app.log"
    content, lines_shown, has_more = read_log_tail(log_file, limit)
    if raw:
        return HTMLResponse(content, media_type="text/plain")

    return templates.TemplateResponse(request, "logs.html", {
        "log_type": "Web Server Log",
        "content": content,
        "lines_shown": int(lines_shown),
        "has_more": has_more,
        "limit": limit
    })


@app.get("/logs/device/{device_type}/{index}", response_class=HTMLResponse)
def device_logs(
    request: Request,
    device_type: str,
    index: int,
    limit: str = "100",
    raw: bool = False,
    user: str = Depends(require_auth)
):
    device_name = f"{device_type} #{index}"

    if plugin_manager and device_type in plugin_manager._plugins:
        routers = plugin_manager._plugins[device_type].get("routers", [])
        if 0 <= index < len(routers):
            router = routers[index]
            device_name = router.get("host") or router.get("ip") or router.get("address") or device_name

    log_file = LOG_DIR / "app.log"
    content, lines_shown, has_more = read_log_tail(log_file, limit)

    if raw:
        return HTMLResponse(content, media_type="text/plain")

    return templates.TemplateResponse(request, "logs.html", {
        "log_type": f"Устройство {device_name}",
        "content": content,
        "lines_shown": int(lines_shown),
        "has_more": has_more,
        "limit": limit
    })


@app.get("/edit/{device_type}/{index}", response_class=HTMLResponse)
def edit_router(
    request: Request,
    device_type: str,
    index: int,
    user: str = Depends(require_auth)
):
    plugin = plugin_manager._plugins.get(device_type) if plugin_manager else None
    if not plugin or index >= len(plugin.get("routers", [])):
        return HTMLResponse("<h3>Устройство не найдено</h3>", status_code=404)

    router = copy.deepcopy(plugin["routers"][index])

    if "ssh_pass" in router and router["ssh_pass"]:
        router["ssh_pass_decrypted"] = storage.decrypt(router["ssh_pass"])
    else:
        router["ssh_pass_decrypted"] = ""

    if "ftp" in router and isinstance(router["ftp"], dict):
        if "pass" in router["ftp"] and router["ftp"]["pass"]:
            router["ftp"]["pass_decrypted"] = storage.decrypt(router["ftp"]["pass"])
        else:
            router["ftp"]["pass_decrypted"] = ""

    return templates.TemplateResponse(request, "edit.html", {
        "device_type": device_type,
        "index": index,
        "router": router
    })


@app.post("/update-router/{device_type}/{index}")
    
async def update_router(
    device_type: str,
    index: int,
    user: str = Depends(require_auth),
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
    ftp_pass: str = Form("")
):
    if not plugin_manager or device_type not in plugin_manager._plugins:
        raise HTTPException(status_code=400, detail="Плагин не найден")

    routers_list = plugin_manager._plugins[device_type].get("routers", [])
    if index < 0 or index >= len(routers_list):
        raise HTTPException(status_code=404, detail="Устройство не найдено")

    updated_router = {
        ("ip" if device_type.lower() == "mikrotik" else "host"): ip,
        "username": username,
        "ssh_pass": storage.encrypt(ssh_pass) if ssh_pass else "",
        "backup_path": backup_path,
        "max_backups": int(max_backups)
    }

    if device_type.lower() == "mikrotik":
        updated_router["version"] = version
        updated_router["private_key"] = private_key if private_key else "id_rsa.key"
        if port != 22:
            updated_router["port"] = port
    else:
        updated_router["ssh_key"] = private_key if private_key else "id_rsa.key"
        updated_router["port"] = port

    if ftp_host:
        updated_router["ftp"] = {
            "host": ftp_host,
            "port": int(ftp_port),
            "remote_dir": ftp_dir,
            "user": ftp_user,
            "pass": storage.encrypt(ftp_pass) if ftp_pass else ""
        }

    routers_list[index] = updated_router

    target_dir = BASE_DIR / "plugins"
    for p_folder in target_dir.iterdir():
        if p_folder.is_dir() and p_folder.name.lower() == device_type.lower():
            json_file = p_folder / "routers.json"
            with open(json_file, "w", encoding="utf-8") as f:
                json.dump(routers_list, f, ensure_ascii=False, indent=4)
            break

    logging.info(f"⚙ Обновлены настройки устройства [{device_type}] #{index}: {ip}")
    return RedirectResponse(url="/", status_code=303)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("web.server:app", host="127.0.0.1", port=8000, reload=True)