import os
import sys
import logging
import argparse
import uvicorn

# --- 1. Базовые пути ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from core.service_installer import install_service, uninstall_service

# --- 2. Настройка логирования ---
log_file = os.path.join(BASE_DIR, "network_backup.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(log_file, encoding="utf-8"),
        logging.StreamHandler(sys.stdout)
    ]
)


def run_app(host: str = "0.0.0.0", port: int = 8000, reload: bool = False):
    """Запуск приложения (Веб-панель + Фоновый планировщик) через Uvicorn"""
    uvicorn.run("web.server:app", host=host, port=port, reload=reload)


def run_cli_backup():
    """Сохранение старой логики: Однократный консольный запуск всех бэкапов без веб-сервера"""
    from core.plugin_manager import PluginManager

    logging.info("🚀 Запуск менеджера резервного копирования (консольный режим)...")

    # Инициализация без передачи объекта storage
    manager = PluginManager(plugins_dir=os.path.join(BASE_DIR, "plugins"))
    manager.discover_and_load_plugins()
    manager.run_all_backups()

    logging.info("----------------------------------------")
    logging.info("🎉 Общий цикл резервного копирования завершен.\n")


def main():
    parser = argparse.ArgumentParser(description="Network Backup Manager")
    parser.add_argument("--install", action="store_true", help="Установить службу в систему")
    parser.add_argument("--uninstall", action="store_true", help="Удалить службу из системы")
    parser.add_argument("--service", action="store_true", help="Запуск в режиме системной службы ОС")
    parser.add_argument("--cli", action="store_true", help="Разовый запуск бэкапов без веб-сервера")
    parser.add_argument("--host", type=str, default="0.0.0.0", help="IP-адрес веб-сервера (по умолчанию 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8000, help="Порт веб-сервера (по умолчанию 8000)")

    args = parser.parse_args()

    if args.install:
        install_service()
    elif args.uninstall:
        uninstall_service()
    elif args.cli:
        run_cli_backup()
    elif args.service:
        # Режим службы ОС (без автоперезагрузки кода)
        logging.info("🚀 Запуск службы Network Backup Manager...")
        run_app(host=args.host, port=args.port, reload=False)
    else:
        # Обычный запуск из консоли
        logging.info(f"🚀 Запуск веб-панели и планировщика на http://{args.host}:{args.port}")
        run_app(host=args.host, port=args.port, reload=True)


if __name__ == "__main__":
    main()