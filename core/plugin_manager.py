# =========================================================
# core/plugin_manager.py
# =========================================================

import os
import re
import copy
import shutil
import importlib
import importlib.util
import json
import logging
from datetime import datetime
from ftplib import FTP, error_perm
from pathlib import Path

from core.secure_storage import SecureStorage
from core.context import PLUGINS_DIR, BASE_DIR
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError


# =========================================================
# КОНСТАНТЫ РОТАЦИИ
# =========================================================

# Формат каталогов бэкапов: YYYY-MM-DD
DATE_DIR_REGEX = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Сколько пустых каталогов разрешено хранить (сверх max_backups).
# Пустые каталоги - это следы неудачных попыток бэкапа.
# Их не удаляем сразу, чтобы было видно, что бэкап пытался запуститься,
# но и не даём им бесконечно накапливаться.
MAX_EMPTY_DIRS = 5


class PluginManager:
    def __init__(self):
        self.plugins_dir = PLUGINS_DIR
        self._plugins = {}
        self.storage = SecureStorage()

    # =====================================================
    # DISCOVER & LOAD
    # =====================================================

    def discover_and_load_plugins(self):
        """Автоматический поиск и загрузка плагинов из папки plugins"""

        if not os.path.exists(self.plugins_dir):
            return

        for item in os.listdir(self.plugins_dir):

            item_path = os.path.join(self.plugins_dir, item)

            if not os.path.isdir(item_path):
                continue

            plugin_file = os.path.join(item_path, "plugin.py")

            if not os.path.exists(plugin_file):
                continue

            device_type = item.lower()

            try:
                spec = importlib.util.spec_from_file_location(
                    f"plugin_{item}",
                    plugin_file,
                )

                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)

                if not hasattr(module, "run_backup"):
                    continue

                routers_path = os.path.join(item_path, "routers.json")
                routers_data = []

                if os.path.exists(routers_path):
                    with open(routers_path, "r", encoding="utf-8") as f:
                        routers_data = json.load(f)

                    # Приводим относительные пути бэкапов к BASE_DIR
                    for router in routers_data:
                        backup_path = router.get("backup_path")
                        if backup_path:
                            p = Path(backup_path)
                            if not p.is_absolute():
                                router["backup_path"] = str(
                                    (BASE_DIR / p).resolve()
                                )

                self._plugins[device_type] = {
                    "func": module.run_backup,
                    "routers": routers_data,
                }

                logging.info(
                    "🔌 Загружен плагин: %s (устройств: %s)",
                    device_type,
                    len(routers_data),
                )

            except Exception as e:
                logging.error(
                    "❌ Ошибка загрузки плагина %s: %s",
                    item,
                    e,
                )

    # =====================================================
    # ROTATION HELPERS
    # =====================================================

    @staticmethod
    def _resolve_backup_root(backup_path):
        """
        Приводит путь к абсолютному.

        Если путь относительный - считаем от BASE_DIR.
        """
        if not backup_path:
            return None

        root = Path(backup_path)

        if not root.is_absolute():
            root = BASE_DIR / root

        return root

    @staticmethod
    def _is_empty_dir(path):
        """
        True, если в каталоге нет ни одного файла (только подкаталоги
        тоже считаются "пустыми", если в них нет файлов).

        Ошибки доступа трактуем как "пустой" - безопаснее не удалять.
        """
        try:
            for entry in path.iterdir():
                if entry.is_file():
                    return False
                if entry.is_dir():
                    # Рекурсивная проверка вложенных каталогов
                    if not PluginManager._is_empty_dir(entry):
                        return False
            return True
        except (PermissionError, OSError):
            return True

    # =====================================================
    # ROTATION
    # =====================================================

    def _rotate_local(self, backup_path, max_backups):
        """
        Локальная ротация бэкапов.

        Удаляет самые старые подкаталоги формата YYYY-MM-DD,
        содержащие хотя бы один файл.

        Пустые каталоги НЕ учитываются в лимите max_backups,
        но при накоплении сверх MAX_EMPTY_DIRS - удаляются
        отдельным проходом.
        """

        root = self._resolve_backup_root(backup_path)

        if root is None or not root.exists() or not root.is_dir():
            return

        max_backups = int(max_backups)

        date_dirs = []   # непустые каталоги формата YYYY-MM-DD
        empty_dirs = []  # пустые каталоги формата YYYY-MM-DD

        try:
            for item in root.iterdir():

                if not item.is_dir():
                    continue

                if not DATE_DIR_REGEX.match(item.name):
                    continue

                if self._is_empty_dir(item):
                    empty_dirs.append(item)
                else:
                    date_dirs.append(item)

        except (PermissionError, OSError) as exc:
            logging.error(
                "Ошибка чтения каталога бэкапов %s: %s",
                root,
                exc,
            )
            return

        # -------------------------------------------------
        # Сортируем по имени каталога.
        # Лексикографическая сортировка YYYY-MM-DD = хронологическая.
        # Это надёжнее, чем mtime, который может меняться
        # от антивируса или индексатора.
        # -------------------------------------------------

        date_dirs.sort(key=lambda p: p.name)
        empty_dirs.sort(key=lambda p: p.name)

        # -------------------------------------------------
        # 1. Удаляем самые старые НЕПУСТЫЕ, пока их больше max_backups
        # -------------------------------------------------

        excess = len(date_dirs) - max_backups

        if excess > 0:

            for old_dir in date_dirs[:excess]:

                try:
                    shutil.rmtree(old_dir, ignore_errors=False)

                    logging.info(
                        "🗑 Удалена старая локальная копия: %s",
                        old_dir,
                    )

                except Exception as exc:
                    logging.error(
                        "Ошибка удаления %s: %s",
                        old_dir,
                        exc,
                    )

        # -------------------------------------------------
        # 2. Удаляем самые старые ПУСТЫЕ, пока их больше MAX_EMPTY_DIRS
        # -------------------------------------------------

        excess_empty = len(empty_dirs) - MAX_EMPTY_DIRS

        if excess_empty > 0:

            for empty_dir in empty_dirs[:excess_empty]:

                try:
                    # На всякий случай: удаляем только если всё ещё пуст
                    if self._is_empty_dir(empty_dir):
                        shutil.rmtree(empty_dir, ignore_errors=True)

                        logging.info(
                            "🗑 Удалён пустой каталог: %s",
                            empty_dir,
                        )

                except Exception as exc:
                    logging.error(
                        "Ошибка удаления пустого каталога %s: %s",
                        empty_dir,
                        exc,
                    )

    # =====================================================
    # FTP UPLOAD
    # =====================================================

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

                parts = (
                    ftp_config["remote_dir"] + "/" + today
                ).strip("/").split("/")

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

                        logging.info(
                            "📤 Загружен на FTP: %s",
                            file_name,
                        )

        except Exception as e:
            logging.error(
                "❌ Ошибка FTP-загрузки в ядре: %s",
                e,
            )

    # =====================================================
    # RUN ALL BACKUPS
    # =====================================================

    def run_all_backups(self):
        """Параллельный запуск бэкапов для всех устройств"""

        MAX_WORKERS = 5
        BACKUP_TIMEOUT = 300  # секунд

        tasks = []

        # Подготавливаем список заданий
        for device_type, data in self._plugins.items():

            plugin_func = data["func"]
            routers = data["routers"]

            for router in routers:
                tasks.append({
                    "device_type": device_type,
                    "plugin_func": plugin_func,
                    "router": router,
                })

        total = len(tasks)

        if total == 0:
            logging.warning("⚠ Нет устройств для бэкапа")
            return

        logging.info(
            "🚀 Запуск бэкапа %s устройств (параллельно: %s)",
            total,
            MAX_WORKERS,
        )

        success = 0
        failed = 0

        def worker(task):

            device_type = task["device_type"]
            plugin_func = task["plugin_func"]
            router = task["router"]

            ip = router.get("ip", router.get("host", "Unknown"))
            max_backups = router.get("max_backups", 7)
            backup_path = router["backup_path"]
            ftp_config = router.get("ftp")

            secure_router = copy.deepcopy(router)

            if "ssh_pass" in secure_router:
                secure_router["ssh_pass"] = self.storage.decrypt(
                    secure_router["ssh_pass"]
                )

            logging.info(
                "🔄 [%s] Старт бэкапа: %s",
                device_type,
                ip,
            )

            created_files = plugin_func(secure_router)

            if created_files:

                self._rotate_local(backup_path, max_backups)

                if ftp_config:
                    self._upload_to_ftp(ftp_config, created_files)

                logging.info(
                    "✅ [%s] Успешно: %s",
                    device_type,
                    ip,
                )

            else:
                logging.warning(
                    "⚠ [%s] Файлы не созданы: %s",
                    device_type,
                    ip,
                )

            return ip

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:

            future_map = {
                executor.submit(worker, task): task
                for task in tasks
            }

            for future in as_completed(future_map):

                task = future_map[future]

                ip = task["router"].get(
                    "ip",
                    task["router"].get("host", "Unknown"),
                )

                try:
                    future.result(timeout=BACKUP_TIMEOUT)
                    success += 1

                except TimeoutError:
                    failed += 1
                    logging.error(
                        "⏰ Таймаут бэкапа: %s",
                        ip,
                    )

                except Exception as e:
                    failed += 1
                    logging.error(
                        "❌ Ошибка бэкапа %s: %s",
                        ip,
                        e,
                    )

        logging.info(
            "🏁 Бэкап завершён. Успешно: %s, ошибок: %s, всего: %s",
            success,
            failed,
            total,
        )