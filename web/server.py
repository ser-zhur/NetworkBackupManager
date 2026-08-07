import os
import json
import copy
import traceback
import logging
import re
from datetime import datetime
from collections import defaultdict

from logging.handlers import RotatingFileHandler
from pathlib import Path
from datetime import datetime

from fastapi import FastAPI, HTTPException, Request, Form, BackgroundTasks
from fastapi import FastAPI, HTTPException, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
from fastapi.templating import Jinja2Templates

from core.secure_storage import SecureStorage

# Корень проекта
BASE_DIR = Path(__file__).resolve().parent.parent

# Регулярное выражение для поиска папок формата ГГГГ-ММ-ДД (например, 2026-08-07)
DATE_DIR_REGEX = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Инициализация шифрования
storage = SecureStorage()

# --- Настройка логирования с авто-ротацией (максимум 2 МБ на файл) ---
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

file_handler = RotatingFileHandler(
    LOG_DIR / "app.log",
    maxBytes=2 * 1024 * 1024,  # 2 МБ
    backupCount=3,
    encoding="utf-8"
)
file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))

logging.basicConfig(
    level=logging.INFO,
    handlers=[file_handler, logging.StreamHandler()]
)

# Инициализация FastAPI и шаблонов
app = FastAPI(title="Network Backup Manager")

templates = None
if (BASE_DIR / "web" / "templates").exists():
    templates = Jinja2Templates(directory=str(BASE_DIR / "web" / "templates"))

# Папка логов и логирование
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "app.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)

# Инициализация менеджера плагинов
try:
    from core.plugin_manager import PluginManager
    plugin_manager = PluginManager(plugins_dir=str(BASE_DIR / "plugins"))
    plugin_manager.discover_and_load_plugins()
except Exception as e:
    logging.error(f"Ошибка загрузки PluginManager: {e}")
    plugin_manager = None


# --- Вспомогательные функции ---

# --- Функция чтения хвоста лог-файла ---
def read_log_tail(file_path: Path, limit: str = "100"):
    """Читает с диска только указанное количество последних строк файла"""
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

DATE_DIR_REGEX = re.compile(r"^\d{4}-\d{2}-\d{2}$")

def get_device_backup_info(backup_path_str: str):
    """
    Считает количество не пустых папок YYYY-MM-DD,
    дату последнего бэкапа и возраст бэкапа в днях.
    """
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


@app.get("/", response_class=HTMLResponse)
def read_root(request: Request):
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

                # --- Определение цвета строки ---
                if count == 0 or age_days is None:
                    # Нет бэкапа — белый фон
                    row_class = "bg-white hover:bg-gray-50"
                elif age_days < 15:
                    # Менее 15 дней — бледно-зеленый
                    row_class = "bg-emerald-50/80 hover:bg-emerald-100/80"
                elif 15 <= age_days <= 30:
                    # От 15 до 30 дней — желтый
                    row_class = "bg-amber-100/70 hover:bg-amber-200/70"
                else:
                    # Более 30 дней — красный
                    row_class = "bg-rose-100/80 hover:bg-rose-200/80"

                devices.append({
                    "type": device_type,
                    "ip": ip_addr,
                    "backup_path": b_path,
                    "backup_count": count,
                    "last_backup_date": last_date,
                    "row_class": row_class,
                    "index": index
                })

    return templates.TemplateResponse(request, "index.html", {
        "devices": devices
    })


# --- Маршруты страниц (Routes) ---

@app.get("/", response_class=HTMLResponse)
def read_root(request: Request):
    if not templates:
        return HTMLResponse("<h2>⚠️ Папка web/templates не найдена</h2>", status_code=500)

    devices = []
    if plugin_manager and hasattr(plugin_manager, "_plugins"):
        for device_type, plugin_data in plugin_manager._plugins.items():
            routers = plugin_data.get("routers", [])
            for index, router in enumerate(routers):
                ip_addr = router.get("host") or router.get("ip") or router.get("address", "Не указан")
                b_path = router.get("backup_path", "backups/")
                
                count, last_date = get_device_backup_info(b_path)

                devices.append({
                    "type": device_type,
                    "ip": ip_addr,
                    "backup_path": b_path,
                    "backup_count": count,
                    "last_backup_date": last_date,
                    "index": index
                })

    return templates.TemplateResponse(request, "index.html", {
        "devices": devices
    })


@app.get("/add", response_class=HTMLResponse)
def add_router_page(request: Request):
    plugins_list = list(plugin_manager._plugins.keys()) if plugin_manager else []
    if templates and (BASE_DIR / "web" / "templates" / "add.html").exists():
        return templates.TemplateResponse(request, "add.html", {
            "plugins": plugins_list
        })
    return HTMLResponse("<h3>Форма добавления недоступна</h3><a href='/'>Назад</a>")


@app.post("/save-router")
async def save_router_endpoint(
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
    """Обработка добавления нового роутера"""
    if not plugin_manager or device_type not in plugin_manager._plugins:
        raise HTTPException(status_code=400, detail="Указанный плагин не найден")

    # 1. Шифруем пароли
    encrypted_ssh_pass = storage.encrypt(ssh_pass) if ssh_pass else ""
    
    # 2. Формируем структуру роутера
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

    # 3. Обновляем память и сохраняем в routers.json
    plugin_data = plugin_manager._plugins[device_type]
    plugin_data["routers"].append(new_router)

    # Ищем файл routers.json в папке плагина
    target_dir = BASE_DIR / "plugins"
    for p_folder in target_dir.iterdir():
        if p_folder.is_dir() and p_folder.name.lower() == device_type.lower():
            json_file = p_folder / "routers.json"
            with open(json_file, "w", encoding="utf-8") as f:
                json.dump(plugin_data["routers"], f, ensure_ascii=False, indent=4)
            break

    logging.info(f"➕ Добавлено новое устройство ({device_type}): {ip}")
    return RedirectResponse(url="/", status_code=303)


def execute_device_backup_task(device_type: str, index: int, secure_router: dict):
    """Выполнение бэкапа в фоновом потоке с записью в лог"""
    plugin = plugin_manager._plugins.get(device_type)
    if not plugin:
        return

    backup_func = plugin.get("func")
    if not backup_func:
        return

    ip_disp = secure_router.get("host") or secure_router.get("ip")
    logging.info(f"🚀 [Фон] Запуск бэкапа: {device_type} / {ip_disp}")

    try:
        created_files = backup_func(secure_router)
        if created_files and hasattr(plugin_manager, "_rotate_local"):
            plugin_manager._rotate_local(secure_router.get("backup_path"), secure_router.get("max_backups", 7))
        logging.info(f"✅ [Фон] Бэкап успешно завершен для {ip_disp}")
    except Exception as e:
        logging.error(f"❌ [Фон] Ошибка при бэкапе {ip_disp}: {e}")


@app.get("/run-progress/{device_type}/{index}")
def run_backup(device_type: str, index: int, background_tasks: BackgroundTasks):
    """Запуск бэкапа в фоне и мгновенное перенаправление на страницу лога"""
    plugin = plugin_manager._plugins.get(device_type)
    if not plugin or index < 0 or index >= len(plugin.get("routers", [])):
        return HTMLResponse("<h3>Устройство не найдено</h3><a href='/'>Назад</a>", status_code=404)

    router = plugin["routers"][index]

    # Подготовка изолированной конфигурации с расшифрованными паролями
    secure_router = copy.deepcopy(router)
    if "ssh_pass" in secure_router and secure_router["ssh_pass"]:
        secure_router["ssh_pass"] = storage.decrypt(secure_router["ssh_pass"])

    # Добавляем задачу бэкапа в фоновый поток FastAPI
    background_tasks.add_task(execute_device_backup_task, device_type, index, secure_router)

    # Мгновенный редирект на лог устройства
    return RedirectResponse(url=f"/logs/device/{device_type}/{index}", status_code=303)



@app.get("/backups/{device_type}/{index}", response_class=HTMLResponse)
def backups_page(request: Request, device_type: str, index: int):
    """Просмотр списка архивов с группировкой по датам"""
    plugin = plugin_manager._plugins.get(device_type)
    if not plugin or index >= len(plugin.get("routers", [])):
        return HTMLResponse("<h3>Устройство не найдено</h3>", status_code=404)

    router = plugin["routers"][index]
    backup_path = Path(router.get("backup_path", "backups"))
    if not backup_path.is_absolute():
        backup_path = BASE_DIR / backup_path

    grouped_backups = defaultdict(list)

    if backup_path.exists() and backup_path.is_dir():
        # Сканируем элементы и сортируем каталоги по убыванию (свежие даты вверху)
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


@app.get("/download-backup/{device_type}/{index}/{filepath:path}")
def download_backup(device_type: str, index: int, filepath: str):
    """Эндпоинт прямого скачивания файла бэкапа"""
    plugin = plugin_manager._plugins.get(device_type)
    if not plugin or index >= len(plugin.get("routers", [])):
        raise HTTPException(status_code=404, detail="Устройство не найдено")

    router = plugin["routers"][index]
    backup_path = Path(router.get("backup_path", "backups"))
    if not backup_path.is_absolute():
        backup_path = BASE_DIR / backup_path

    file_full_path = (backup_path / filepath).resolve()

    # Защита от Path Traversal (выход за пределы папки бэкапов)
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
def logs_main_endpoint(request: Request, limit: str = "100"):
    log_file = BASE_DIR / "network_backup.log"
    if not log_file.exists():
        log_file = LOG_DIR / "app.log"

    content, lines_shown, has_more = read_log_tail(log_file, limit)
    return templates.TemplateResponse(request, "logs.html", {
        "log_type": "Main Service Log",
        "content": content,
        "lines_shown": lines_shown,
        "has_more": has_more,
        "limit": limit
    })

@app.get("/logs/global", response_class=HTMLResponse)
def logs_global_endpoint(request: Request, limit: str = "100"):
    log_file = LOG_DIR / "app.log"
    content, lines_shown, has_more = read_log_tail(log_file, limit)
    return templates.TemplateResponse(request, "logs.html", {
        "log_type": "Web Server Log",
        "content": content,
        "lines_shown": lines_shown,
        "has_more": has_more,
        "limit": limit
    })


@app.get("/logs/device/{device_type}/{index}", response_class=HTMLResponse)
def device_logs(request: Request, device_type: str, index: int, limit: str = "100", raw: bool = False):
    """Лог конкретного устройства с поддержкой выдачи чистого текста для автообновления"""
    device_name = f"{device_type} #{index}"
    
    if plugin_manager and device_type in plugin_manager._plugins:
        routers = plugin_manager._plugins[device_type].get("routers", [])
        if 0 <= index < len(routers):
            router = routers[index]
            device_name = router.get("host") or router.get("ip") or router.get("address") or device_name

    log_file = LOG_DIR / "app.log"
    content, lines_shown, has_more = read_log_tail(log_file, limit)

    # Если клиент запрашивает сырой текст (для живого автообновления JS)
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
def edit_router(request: Request, device_type: str, index: int):
    """Страница редактирования параметров устройства"""
    plugin = plugin_manager._plugins.get(device_type)
    if not plugin or index >= len(plugin.get("routers", [])):
        return HTMLResponse("<h3>Устройство не найдено</h3>", status_code=404)

    router = copy.deepcopy(plugin["routers"][index])

    # Расшифровываем пароли для отображения в форме
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
    ip: str = Form(...),
    version: str = Form("7"),
    port: int = Form(22),
    username: str = Form("admin"),
    ssh_pass: str = Form(""),
    private_key: str = Form(""),
    backup_path: str = Form(...),
    max_backups: int = Form(5),
    ftp_host: str = Form(""),
    ftp_port: int = Form(21),
    ftp_dir: str = Form(""),
    ftp_user: str = Form(""),
    ftp_pass: str = Form("")
):
    """Сохранение отредактированных параметров устройства"""
    if not plugin_manager or device_type not in plugin_manager._plugins:
        raise HTTPException(status_code=400, detail="Плагин не найден")

    routers_list = plugin_manager._plugins[device_type].get("routers", [])
    if index < 0 or index >= len(routers_list):
        raise HTTPException(status_code=404, detail="Устройство не найдено")

    # Формируем обновленный объект устройства
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

    # Настройки FTP
    if ftp_host:
        updated_router["ftp"] = {
            "host": ftp_host,
            "port": int(ftp_port),
            "remote_dir": ftp_dir,
            "user": ftp_user,
            "pass": storage.encrypt(ftp_pass) if ftp_pass else ""
        }

    # Обновляем в памяти
    routers_list[index] = updated_router

    # Перезаписываем в json-файл плагина
    target_dir = BASE_DIR / "plugins"
    for p_folder in target_dir.iterdir():
        if p_folder.is_dir() and p_folder.name.lower() == device_type.lower():
            json_file = p_folder / "routers.json"
            with open(json_file, "w", encoding="utf-8") as f:
                json.dump(routers_list, f, ensure_ascii=False, indent=4)
            break

    logging.info(f"⚙ Обновлены настройки устройства [{device_type}] #{index}: {ip}")
    return RedirectResponse(url="/", status_code=303)


@app.post("/run-now")
def run_now_endpoint():
    """Запуск бэкапов всех устройств"""
    if not plugin_manager:
        raise HTTPException(status_code=500, detail="Plugin manager не инициализирован")
    try:
        plugin_manager.run_all_backups()
        return RedirectResponse(url="/", status_code=303)
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("web.server:app", host="127.0.0.1", port=8000, reload=True)