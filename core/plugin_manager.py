import os
import copy
import importlib
import json
import logging
from datetime import datetime
from ftplib import FTP, error_perm
from core.secure_storage import SecureStorage

class PluginManager:
    def __init__(self, plugins_dir="plugins"):
        self.plugins_dir = plugins_dir
        self._plugins = {}
        self.storage = SecureStorage()

    def discover_and_load_plugins(self):
        """Автоматический поиск и загрузка плагинов из папки plugins"""
        if not os.path.exists(self.plugins_dir):
            return

        for item in os.listdir(self.plugins_dir):
            item_path = os.path.join(self.plugins_dir, item)
            if os.path.isdir(item_path):
                plugin_file = os.path.join(item_path, "plugin.py")
                if os.path.exists(plugin_file):
                    device_type = item.lower()
                    try:
                        module = importlib.import_module(f"plugins.{item}.plugin")
                        if hasattr(module, "run_backup"):
                            routers_path = os.path.join(item_path, "routers.json")
                            routers_data = []
                            if os.path.exists(routers_path):
                                with open(routers_path, "r", encoding="utf-8") as f:
                                    routers_data = json.load(f)
                            
                            self._plugins[device_type] = {
                                "func": module.run_backup,
                                "routers": routers_data
                            }
                            logging.info(f"🔌 Загружен плагин: {device_type} (устройств: {len(routers_data)})")
                    except Exception as e:
                        logging.error(f"❌ Ошибка загрузки плагина {item}: {e}")

    def _rotate_local(self, backup_path, max_backups):
        """Локальная ротация бэкапов (удаление старых папок)"""
        if not os.path.exists(backup_path):
            return
        
        try:
            subdirs = [os.path.join(backup_path, d) for d in os.listdir(backup_path) if os.path.isdir(os.path.join(backup_path, d))]
            subdirs.sort(key=os.path.getmtime)

            while len(subdirs) > max_backups:
                oldest_dir = subdirs.pop(0)
                for root, dirs, files in os.walk(oldest_dir, topdown=False):
                    for file in files:
                        os.remove(os.path.join(root, file))
                    for dir in dirs:
                        os.rmdir(os.path.join(root, dir))
                os.rmdir(oldest_dir)
                logging.info(f"🗑 Удалена старая локальная копия: {oldest_dir}")
        except Exception as e:
            logging.error(f"❌ Ошибка локальной ротации: {e}")

    def _upload_to_ftp(self, ftp_config, created_files):
        """Выгрузка бэкапов на FTP с расшифровкой пароля"""
        host = ftp_config.get("host")
        port = ftp_config.get("port", 21)
        user = ftp_config.get("user")
        
        encrypted_pass = ftp_config.get("pass", "")
        password = self.storage.decrypt(encrypted_pass)

        today = datetime.now().strftime("%Y-%m-%d")

        try:
            with FTP() as ftp:
                ftp.connect(host, port)
                ftp.login(user=user, passwd=password)
                
                parts = (ftp_config["remote_dir"] + "/" + today).strip("/").split("/")
                for part in parts:
                    try:
                        ftp.cwd(part)
                    except error_perm:
                        ftp.mkd(part)
                        ftp.cwd(part)

                for file_path in created_files:
                    file_name = os.path.basename(file_path)
                    with open(file_path, "rb") as f:
                        ftp.storbinary(f"STOR {file_name}", f)
                        logging.info(f"📤 Загружен на FTP: {file_name}")

        except Exception as e:
            logging.error(f"❌ Ошибка FTP-загрузки в ядре: {e}")

    def run_all_backups(self):
        """Запуск бэкапов для всех устройств всех подключенных плагинов"""
        for device_type, data in self._plugins.items():
            plugin_func = data["func"]
            routers = data["routers"]

            for router in routers:
                ip = router.get("ip", router.get("host", "Unknown"))
                max_backups = router.get("max_backups", 7)
                backup_path = router["backup_path"]
                ftp_config = router.get("ftp")

                # Создаем изолированную копию конфигурации роутера для расшифровки секретов в памяти
                secure_router = copy.deepcopy(router)
                
                # Если в конфиге есть зашифрованный ssh_pass, расшифровываем его для плагина
                if "ssh_pass" in secure_router:
                    secure_router["ssh_pass"] = self.storage.decrypt(secure_router["ssh_pass"])

                logging.info("-" * 40)
                logging.info(f"🔄 [Плагин: {device_type}] Старт бэкапа для: {ip}")

                try:
                    # Передаем плагину конфигурацию с уже расшифрованным SSH-паролем
                    created_files = plugin_func(secure_router)
                    
                    if created_files:
                        self._rotate_local(backup_path, max_backups)

                        if ftp_config:
                            self._upload_to_ftp(ftp_config, created_files)
                            
                        logging.info(f"✅ Успешно завершено для: {ip}")
                except Exception as e:
                    logging.error(f"❌ Ошибка при бэкапе {ip}: {e}")