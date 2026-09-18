# =========================================================
# core/plugin_manager.py
# =========================================================

import os
import copy
import importlib
import importlib.util
import json
import logging
import threading

from datetime import datetime
from pathlib import Path

from core.secure_storage import SecureStorage
from core.context import (
    PLUGINS_DIR,
    BASE_DIR,
    STATE_FILE,
    load_json,
    save_json,
)
from core.storage import PushContext
from core.storage.base import identity_normalize
from concurrent.futures import (
    ThreadPoolExecutor,
    as_completed,
    TimeoutError,
)


# Lock для атомарного read-modify-write data/state.json.
# Параллельные бэкапы пишут в один файл — без блокировки
# возможны потери записей.
_STATE_LOCK = threading.Lock()


class PluginManager:
    def __init__(self, storage_manager=None):
        self.plugins_dir = PLUGINS_DIR
        self._plugins = {}
        self.storage = SecureStorage()
        self.storage_manager = storage_manager

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

                # Ищем нормализатор, объявленный плагином.
                # Если его нет — используем identity.
                normalize_fn = getattr(
                    module, "normalize_for_compare", None,
                )

                if normalize_fn is None:
                    normalize_fn = identity_normalize

                elif not callable(normalize_fn):
                    logging.warning(
                        "🔌 %s: normalize_for_compare "
                        "не callable, использую identity",
                        device_type,
                    )
                    normalize_fn = identity_normalize

                self._plugins[device_type] = {
                    "func": module.run_backup,
                    "routers": routers_data,
                    "normalize": normalize_fn,
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
    # STORAGE HELPERS
    # =====================================================

    @staticmethod
    def _normalize_created_files(created_files) -> dict:
        """
        Приводит список путей к словарю {имя_в_репо: Path}.

        Плагины возвращают разные имена, а в git-репо мы хотим
        видеть фиксированные config.rsc / config.backup / config.xml.
        """
        result = {}
        for p in created_files or []:
            path = Path(p)
            if not path.is_file():
                continue
            ext = path.suffix.lower()
            if ext == ".rsc":
                result["config.rsc"] = path
            elif ext == ".backup":
                result["config.backup"] = path
            elif ext == ".xml":
                result["config.xml"] = path
            else:
                result[path.name] = path
        return result

    def _decrypt_backend_config(self, backend, cfg: dict) -> dict:
        """
        Копия конфига backend'а с расшифрованными значениями
        полей-паролей. Backend получает плейн-значения.
        """
        result = dict(cfg or {})
        for field in backend.fields:
            if field.type != "password":
                continue
            value = result.get(field.name, "")
            if value:
                result[field.name] = self.storage.decrypt(value)
        return result

    def _push_to_storages(
        self,
        device_type: str,
        index: int,
        device_host: str,
        router: dict,
        backup_path: Path,
        created_files: list,
    ) -> None:
        """
        Пробегает по включённым backend'ам и пушит файлы.

        Authority (git) идёт первым; его вердикт changed
        попадает в ctx и используется остальными при
        mode == "changes_only".
        """
        if self.storage_manager is None:
            return

        backends = self.storage_manager.enabled_for(router)
        if not backends:
            return

        files = self._normalize_created_files(created_files)
        if not files:
            logging.warning(
                "storage: нет файлов для отправки (%s)",
                device_host,
            )
            return

        storage_cfg = router.get("storage") or {}

        # Достаём нормализатор, объявленный плагином
        # (для MikroTik — удаляет первую строку с timestamp
        # из .rsc при сравнении).
        plugin = self._plugins.get(device_type)
        normalize_fn = (
            plugin.get("normalize", identity_normalize)
            if plugin
            else identity_normalize
        )

        ctx = PushContext(
            device_id=f"{device_type}/{index}",
            device_host=device_host,
            device_type=device_type,
            backup_path=Path(backup_path),
            files=files,
            changed=None,
            run_time=datetime.now(),
            normalize=normalize_fn,
        )
        results = {}

        # ---- authority первым ----
        for backend in backends:
            if not backend.is_authority:
                continue
            cfg = self._decrypt_backend_config(
                backend,
                storage_cfg.get(backend.id) or {},
            )
            try:
                result = backend.push(cfg, ctx)
            except Exception as exc:
                logging.error(
                    "storage[%s]: исключение в push: %s",
                    backend.id,
                    exc,
                    exc_info=True,
                )
                from core.storage.base import PushResult
                result = PushResult.failure(f"Исключение: {exc}")

            results[backend.id] = result

            if result.details and "changed" in result.details:
                ctx.changed = result.details["changed"]

            self._log_storage_result(backend.id, result)

        # ---- остальные ----
        for backend in backends:
            if backend.is_authority:
                continue
            cfg = self._decrypt_backend_config(
                backend,
                storage_cfg.get(backend.id) or {},
            )

            # mode=changes_only и git сказал «не изменилось» — skip
            if backend._should_skip_for_changes(cfg, ctx):
                from core.storage.base import PushResult
                result = PushResult.skipped(
                    "Изменений нет — выгрузка пропущена"
                )
                results[backend.id] = result
                self._log_storage_result(backend.id, result)
                continue

            try:
                result = backend.push(cfg, ctx)
            except Exception as exc:
                logging.error(
                    "storage[%s]: исключение в push: %s",
                    backend.id,
                    exc,
                    exc_info=True,
                )
                from core.storage.base import PushResult
                result = PushResult.failure(f"Исключение: {exc}")

            results[backend.id] = result
            self._log_storage_result(backend.id, result)

        # ---- сохраняем в state.json ----
        self._save_backend_state(ctx.device_id, results)

    @staticmethod
    def _log_storage_result(backend_id: str, result) -> None:
        if result.details and result.details.get("skipped"):
            logging.info(
                "storage[%s]: %s",
                backend_id,
                result.message,
            )
        elif result.ok:
            logging.info(
                "storage[%s]: ✓ %s",
                backend_id,
                result.message,
            )
        else:
            logging.error(
                "storage[%s]: ✗ %s",
                backend_id,
                result.message,
            )

    @staticmethod
    def _save_backend_state(device_id: str, results: dict) -> None:
        """
        Обновляет data/state.json по одному устройству.
        """
        with _STATE_LOCK:
            state = load_json(STATE_FILE, {})
            if not isinstance(state, dict):
                state = {}

            state[device_id] = {
                "last_run": datetime.now().strftime(
                    "%Y-%m-%d %H:%M:%S"
                ),
                "backends": {
                    bid: {
                        "ok": bool(r.ok),
                        "message": r.message or "",
                        "skipped": bool(
                            r.details
                            and r.details.get("skipped")
                        ),
                    }
                    for bid, r in results.items()
                },
            }

            try:
                save_json(STATE_FILE, state)
            except Exception as exc:
                logging.error(
                    "storage: не удалось сохранить state.json: %s",
                    exc,
                )

    # =====================================================
    # RUN ALL BACKUPS
    # =====================================================

    def run_all_backups(self):
        """Параллельный запуск бэкапов для всех устройств"""

        MAX_WORKERS = 5
        BACKUP_TIMEOUT = 300  # секунд

        tasks = []

        for device_type, data in self._plugins.items():

            plugin_func = data["func"]
            routers = data["routers"]

            for index, router in enumerate(routers):
                tasks.append({
                    "device_type": device_type,
                    "index": index,
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
            # Импорт локальный — чтобы не создавать цикл
            # на этапе импорта модуля.
            from core.logger import (
                set_device_context,
                clear_device_context,
            )

            device_type = task["device_type"]
            plugin_func = task["plugin_func"]
            router = task["router"]

            ip = router.get("ip", router.get("host", "Unknown"))

            set_device_context(f"{device_type}/{ip}")

            try:
                backup_path = router["backup_path"]

                secure_router = copy.deepcopy(router)

                if "ssh_pass" in secure_router:
                    secure_router["ssh_pass"] = self.storage.decrypt(
                        secure_router["ssh_pass"]
                    )

                logging.info("🔄 Старт бэкапа: %s", ip)

                created_files = plugin_func(secure_router)

                if not created_files:
                    logging.warning(
                        "⚠ Файлы не созданы: %s", ip
                    )
                    return ip

                # ---- push в storage backends (git всегда,
                #      остальные — по конфигу) ----
                try:
                    self._push_to_storages(
                        device_type=device_type,
                        index=task["index"],
                        device_host=ip,
                        router=router,
                        backup_path=Path(backup_path),
                        created_files=created_files,
                    )
                except Exception as exc:
                    logging.error(
                        "storage: ошибка диспетчеризации "
                        "backend'ов: %s",
                        exc,
                        exc_info=True,
                    )

                logging.info("✅ Успешно: %s", ip)
                return ip

            finally:
                clear_device_context()

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