from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pathlib import Path
import traceback
import os
import logging



# Импорт вашего менеджера плагинов 
try:
    from core.plugin_manager import PluginManager
except ImportError:
    plugin_manager = None

# Инициализация шаблонизатора Jinja2
templates = None
try:
    if os.path.exists("web/templates"):
        templates = Jinja2Templates(directory="web/templates")
except ImportError:
    pass

# Корень проекта
BASE_DIR = Path(__file__).resolve().parent.parent

# ==========================================
# ИНИЦИАЛИЗАЦИЯ ПРИЛОЖЕНИЯ
# ==========================================
app = FastAPI()

# Папка логов
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "app.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)

# Инициализация PluginManager
plugin_manager = PluginManager(
    plugins_dir=str(BASE_DIR / "plugins")
)

plugin_manager.discover_and_load_plugins()
LOG_DIR.mkdir(exist_ok=True)

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "app.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)


BASE_DIR = Path(__file__).resolve().parent.parent 

plugin_manager = PluginManager( plugins_dir=str(BASE_DIR / "plugins") ) 
plugin_manager.discover_and_load_plugins()

# --- Главная страница: отображение веб-панели со списком устройств ---
@app.get("/", response_class=HTMLResponse)
def read_root(request: Request):
    if not templates:
        return """
        <div style="font-family: Arial; padding: 40px; text-align: center; background: #0f172a; color: #fff; height: 100vh;">
            <h2>⚠️ Ошибка шаблонизатора</h2>
            <p>Папка <b>web/templates</b> не найдена или не установлен Jinja2.</p>
        </div>
        """, 500

    devices = []
    
    if plugin_manager and hasattr(plugin_manager, "_plugins"):
        for device_type, plugin_data in plugin_manager._plugins.items():
            routers = plugin_data.get("routers", [])
            for index, router in enumerate(routers):
                devices.append({
                    "type": device_type,
                    "ip": router.get("host") or router.get("ip") or router.get("address", "Не указан"),
                    "backup_path": router.get("backup_path", "backups/"),
                    "backup_count": router.get("backup_count", 0),
                    "last_backup_date": router.get("last_backup_date", "Нет данных"),
                    "index": index
                })
                
    return templates.TemplateResponse(request, "index.html", {
        "devices": devices
    })

# --- Эндпоинты интерфейса для логов и добавления (чтобы не было 404) ---

@app.get("/logs/global", response_class=HTMLResponse)
def logs_global_endpoint(request: Request):
    """Выдача глобальных логов системы"""
    if templates and os.path.exists("web/templates/logs.html"):
        return templates.TemplateResponse(request, "logs.html", {"log_type": "global"})
    return HTMLResponse("<h3>Глобальные логи</h3><pre>Логи системы пока пусти или файл шаблона logs.html отсутствует.</pre><a href='/'>Назад</a>")

@app.get("/logs/main", response_class=HTMLResponse)
def logs_main_endpoint(request: Request):
    """Выдача основных логов"""
    if templates and os.path.exists("web/templates/logs.html"):
        return templates.TemplateResponse(request, "logs.html", {"log_type": "main"})
    return HTMLResponse("<h3>Основные логи</h3><pre>Логи работы плагинов.</pre><a href='/'>Назад</a>")

@app.get("/add", response_class=HTMLResponse)
def add_router_page(request: Request):
    """Страница или форма добавления роутера"""
    if templates and os.path.exists("web/templates/add.html"):
        return templates.TemplateResponse(request, "add.html", {})
    return HTMLResponse("<h3>Добавление устройства</h3><p>Функция добавления через веб-интерфейс в разработке.</p><a href='/'>Назад</a>")

@app.post("/run-now")
def run_now_endpoint():
    """Кнопка 'Запустить сейчас' для всех бэкапов сразу"""
    if not plugin_manager:
        raise HTTPException(status_code=500, detail="Plugin manager не инициализирован")
    
    try:
        if hasattr(plugin_manager, "run_all_backups"):
            plugin_manager.run_all_backups()
            return {"status": "success", "message": "Резервное копирование всех устройств запущено!"}
        else:
            raise HTTPException(status_code=500, detail="Метод run_all_backups не найден в plugin_manager")
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))

# --- Основной эндпоинт точечного запуска бэкапа конкретного роутера ---
@app.post("/execute-backup/{device_type}/{index}")
def execute_backup_endpoint(device_type: str, index: int):
    if not plugin_manager:
        raise HTTPException(status_code=500, detail="Plugin manager не инициализирован")
        
    try:
        plugin_data = plugin_manager._plugins.get(device_type)
        if not plugin_data:
            raise HTTPException(status_code=404, detail="Плагин не найден в системе.")
        
        routers = plugin_data.get("routers", [])
        if index < 0 or index >= len(routers):
            raise HTTPException(status_code=404, detail="Устройство с таким индексом не найдено в конфигурации.")
            
        router_config = routers[index]
        plugin_func = plugin_data.get("func")
        
        if callable(plugin_func):
            plugin_func(router_config)
            return {"status": "success", "message": "Процесс резервного копирования успешно завершен!"}
        else:
            plugin = plugin_data.get("plugin")
            executed = False
            if plugin:
                for method_name in ["backup_router", "backup", "run", "execute"]:
                    if hasattr(plugin, method_name):
                        getattr(plugin, method_name)(router_config)
                        executed = True
                        break
            if not executed:
                raise HTTPException(status_code=500, detail=f"Не найдена исполняемая функция у плагина '{device_type}'.")
            return {"status": "success", "message": "Резервное копирование выполнено."}
            
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Ошибка выполнения бэкапа: {str(e)}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("web.server:app", host="127.0.0.1", port=8000, reload=True)
    
@app.get("/run-progress/{device_type}/{index}", response_class=HTMLResponse)
def run_backup(request: Request, device_type: str, index: int):
    try:
        plugin = plugin_manager._plugins.get(device_type)

        if not plugin:
            return HTMLResponse(
                "<h3>Плагин не найден</h3><a href='/'>Назад</a>",
                status_code=404
            )

        routers = plugin.get("routers", [])

        if index < 0 or index >= len(routers):
            return HTMLResponse(
                "<h3>Устройство не найдено</h3><a href='/'>Назад</a>",
                status_code=404
            )

        router = routers[index]
        
        logging.info(
            f"Запуск бэкапа: {device_type} / {router.get('name')} / {router.get('host')}"
        )

        backup_func = plugin.get("func")

        if backup_func is None:
            return HTMLResponse(
                "<h3>Функция backup не найдена</h3><a href='/'>Назад</a>",
                status_code=500
            )

        try:
            result = backup_func(router)
        except Exception as e:
            logging.exception(
                f"Ошибка бэкапа: {device_type} / {router.get('name')} / {router.get('host')}"
            )

            result = {
                "status": "error",
                "message": str(e)
            }

        return templates.TemplateResponse(
            request,
            "progress.html",
            {
                "device_type": device_type,
                "router": router,
                "result": result
            }
        )

    except Exception as e:
        traceback.print_exc()

        return HTMLResponse(
            f"<h3>Ошибка запуска бэкапа</h3><pre>{e}</pre><a href='/'>Назад</a>",
            status_code=500
        )

@app.get("/logs/device/{device_type}/{index}", response_class=HTMLResponse)
def device_logs(request: Request, device_type: str, index: int):
    log_file = BASE_DIR / "logs" / "app.log"

    content = "Лог не найден"

    if log_file.exists():
        content = log_file.read_text(
            encoding="utf-8",
            errors="ignore"
        )

    return templates.TemplateResponse(
        request,
        "logs.html",
        {
            "log_type": f"{device_type}_{index}",
            "content": content
        }
    )

@app.get("/backups/{device_type}/{index}", response_class=HTMLResponse)
def backups_page(request: Request, device_type: str, index: int):
    plugin = plugin_manager._plugins.get(device_type)

    if not plugin:
        return HTMLResponse(
            "<h3>Плагин не найден</h3>",
            status_code=404
        )

    routers = plugin.get("routers", [])

    if index < 0 or index >= len(routers):
        return HTMLResponse(
            "<h3>Устройство не найдено</h3>",
            status_code=404
        )

    router = routers[index]

    backup_path = Path(router.get("backup_path", "backups"))

    if not backup_path.is_absolute():
        backup_path = BASE_DIR / backup_path

    files = []

    if backup_path.exists():
        files = sorted(
            [f.name for f in backup_path.iterdir() if f.is_file()],
            reverse=True
        )

    return templates.TemplateResponse(
        request,
        "backups.html",
        {
            "device_type": device_type,
            "router": router,
            "files": files,
            "backup_path": str(backup_path)
        }
    )
    
@app.get("/edit/{device_type}/{index}", response_class=HTMLResponse)
def edit_router(request: Request, device_type: str, index: int):
    plugin = plugin_manager._plugins.get(device_type)

    if not plugin:
        return HTMLResponse(
            "<h3>Плагин не найден</h3>",
            status_code=404
        )

    routers = plugin.get("routers", [])

    if index < 0 or index >= len(routers):
        return HTMLResponse(
            "<h3>Устройство не найдено</h3>",
            status_code=404
        )

    router = routers[index]

    return templates.TemplateResponse(
        request,
        "edit.html",
        {
            "device_type": device_type,
            "index": index,
            "router": router
        }
    )
    
    