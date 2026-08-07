import os
import sys
import logging
from core.plugin_manager import PluginManager

# Настройка логирования
script_dir = os.path.dirname(os.path.abspath(__file__))
log_file = os.path.join(script_dir, "network_backup.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(log_file, encoding="utf-8"),
        logging.StreamHandler(sys.stdout)
    ]
)

def main():
    logging.info("🚀 Запуск менеджера резервного копирования (децентрализованный режим)...")

    # Инициализируем и запускаем менеджер
    manager = PluginManager(plugins_dir="plugins")
    manager.discover_and_load_plugins()
    
    # Запускаем обход всех плагинов и их устройств
    manager.run_all_backups()

    logging.info(f"----------------------------------------")
    logging.info("🎉 Общий цикл резервного копирования завершен.\n")

if __name__ == "__main__":
    main()