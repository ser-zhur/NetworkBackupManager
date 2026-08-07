import os
from datetime import datetime

LOGS_DIR = "logs"
os.makedirs(LOGS_DIR, exist_ok=True)

def write_log(message: str, device_key: str = None):
    """Запись сообщения в общий лог панели и опционально в лог конкретного устройства"""
    timestamp = datetime.now().strftime("[%Y-%m-%d %H:%M:%S]")
    log_line = f"{timestamp} {message}\n"
    
    # Запись в новый общий лог панели
    global_path = os.path.join(LOGS_DIR, "global.log")
    with open(global_path, "a", encoding="utf-8") as f:
        f.write(log_line)
        
    # Запись в лог устройства (если указан ключ, например "mikrotik_0")
    if device_key:
        safe_key = device_key.replace("/", "_")
        device_path = os.path.join(LOGS_DIR, f"device_{safe_key}.log")
        with open(device_path, "a", encoding="utf-8") as f:
            f.write(log_line)

def get_log_content(filename: str, lines_count: int = 200) -> str:
    """Чтение последних строк файла лога (поддерживает корневой файл и папку logs/)"""
    if filename == "network_backup.log":
        path = "network_backup.log"
    else:
        path = os.path.join(LOGS_DIR, filename)
        
    if not os.path.exists(path):
        return f"Лог-файл ({filename}) пока отсутствует..."
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
            return "".join(lines[-lines_count:])
    except Exception as e:
        return f"Ошибка чтения лога: {e}"