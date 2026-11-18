# =========================================================
# core/plugin_manager.py
# =========================================================

import os
import copy
import math
import importlib
import importlib.util
import json
import logging
import threading
import time

from datetime import datetime
from pathlib import Path

from core import backup_state
from core import devices as device_store
from core import device_status
from core.secure_storage import get_secure_storage
from core.context import (
    PLUGINS_DIR,
    STATE_FILE,
    load_json,
    save_json,
    resolve_backup_path,
)
from core.storage import PushContext
from core.storage import global_config
from core.storage.base import identity_normalize
from concurrent.futures import (
    ThreadPoolExecutor,
    as_completed,
    TimeoutError as FuturesTimeoutError,
)


def is_device_disabled(router: dict) -> bool:
    """
    Устройство выведено из эксплуатации (флаг «Отключено»).

    Бэкапы для него не запускаются, автоочистка истории
    не применяется — история хранится как есть.
    """
    return bool(router.get("disabled"))


# Lock для атомарного read-modify-write data/state.json.
# Параллельные бэкапы пишут в один файл — без блокировки
# возможны потери записей.
_STATE_LOCK = threading.Lock()


class PluginManager:
    def __init__(self, storage_manager=None):
        self.plugins_dir = PLUGINS_DIR
        self._plugins = {}
        # Устройства типов, чей плагин не загрузился: не теряем
        # их при сохранении devices.json.
        self._orphan_devices: list[dict] = []
        # Миграция не удалась — список в памяти неполный,
        # записывать devices.json нельзя (затёр бы устройства).
        self._devices_readonly = False
        # Явно тот же ключ, что и у web/server.py.
        self.storage = get_secure_storage()
        self.storage_manager = storage_manager

        # Сериализует изменения списков routers в памяти и
        # запись devices.json. RLock — чтобы код, уже
        # держащий лок (веб-обработчик), мог вызвать
        # save_routers() без взаимоблокировки.
        self.routers_lock = threading.RLock()

    # =====================================================
    # DEVICES.JSON
    # =====================================================

    def save_routers(self, device_type: str | None = None) -> None:
        """
        Атомарно сохраняет data/devices.json — все устройства
        из списков в памяти (device_type не важен: файл один;
        параметр оставлен для вызывающего кода).

        Устройствам без id выдаётся id (новое устройство).

        • под routers_lock — параллельные записи (веб +
          потоки бэкапа) не перемешиваются;
        • через save_json: tmp + fsync + os.replace —
          сбой посреди записи не оставит обрезанный файл.

        Бросает исключение при ошибке: вызывающий решает,
        как сообщить пользователю.
        """
        with self.routers_lock:
            if device_type is not None and device_type not in self._plugins:
                raise KeyError(f"Плагин не найден: {device_type}")
            if self._devices_readonly:
                raise RuntimeError(
                    "Устройства не перенесены из папок плагинов (см. лог) — "
                    "сохранение отключено, чтобы не потерять их"
                )

            all_routers = [
                r for pd in self._plugins.values() for r in pd["routers"]
            ]
            device_store.ensure_ids(all_routers + self._orphan_devices)

            devices = []
            for dtype, pd in self._plugins.items():
                for router in pd["routers"]:
                    dev = copy.deepcopy(router)
                    dev.pop("type", None)
                    devices.append({"id": dev.pop("id"), "type": dtype, **dev})
            devices += copy.deepcopy(self._orphan_devices)

            device_store.save_devices(devices)

    def load_devices(self) -> None:
        """
        data/devices.json → списки устройств плагинов в памяти
        (перед этим — разовая миграция из plugins/*/routers.json).
        """
        self._devices_readonly = False
        try:
            device_store.migrate_from_plugins(self.plugins_dir)
        except Exception:
            logging.exception("Миграция устройств из папок плагинов не удалась")
            from core import context
            if not context.DEVICES_FILE.exists():
                self._devices_readonly = True

        with self.routers_lock:
            for pd in self._plugins.values():
                pd["routers"] = []
            self._orphan_devices = []

            devices = device_store.load_devices()
            need_save = device_store.ensure_ids(devices)

            for dev in devices:
                dtype = str(dev.get("type", "")).lower()
                plugin = self._plugins.get(dtype)
                if plugin is None:
                    self._orphan_devices.append(dev)
                    continue
                router = {k: v for k, v in dev.items() if k != "type"}
                # Приводим относительные пути бэкапов к HOME_DIR
                if router.get("backup_path"):
                    router["backup_path"] = str(
                        resolve_backup_path(router["backup_path"])
                    )
                plugin["routers"].append(router)

            if self._orphan_devices:
                logging.warning(
                    "Устройства без загруженного плагина (сохраняются, "
                    "но не обслуживаются): %s",
                    ", ".join(sorted({d["type"] for d in self._orphan_devices})),
                )

            if need_save:
                self.save_routers()

        for dtype, pd in self._plugins.items():
            logging.info(
                "🔌 Плагин %s: устройств %s", dtype, len(pd["routers"]),
            )

    def router_id(self, router: dict) -> str:
        """
        Постоянный id устройства. Устройству без id (только что
        добавленному в памяти) выдаётся новый — в devices.json он
        попадёт при ближайшем save_routers().
        """
        with self.routers_lock:
            if not device_store.is_valid_id(router.get("id")):
                taken = (
                    r.get("id")
                    for pd in self._plugins.values()
                    for r in pd.get("routers", [])
                )
                router["id"] = device_store.new_id(taken)
            return router["id"]

    def device_id(self, device_type: str, index: int) -> str:
        """(тип, текущий номер в списке) → постоянный id."""
        with self.routers_lock:
            return self.router_id(self._plugins[device_type]["routers"][index])

    @staticmethod
    def find_router_index(routers: list, router: dict) -> int | None:
        """
        Индекс router в routers по идентичности объекта.

        Индекс устройства может сдвинуться, пока идёт бэкап
        (удалили соседнее устройство). Поиск по `is` не даёт
        записать результат не тому роутеру.
        """
        for i, r in enumerate(routers):
            if r is router:
                return i
        return None

    # =====================================================
    # DISCOVER & LOAD
    # =====================================================

    def discover_and_load_plugins(self):
        """Автоматический поиск и загрузка плагинов из папки plugins"""

        items = (
            os.listdir(self.plugins_dir)
            if os.path.exists(self.plugins_dir) else []
        )

        for item in items:

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
                    "routers": [],
                    "normalize": normalize_fn,
                    # Реальный каталог (с исходным регистром имени)
                    "dir": item_path,
                }

                logging.info("🔌 Загружен плагин: %s", device_type)

            except Exception as e:
                logging.error(
                    "❌ Ошибка загрузки плагина %s: %s",
                    item,
                    e,
                )

        self.load_devices()

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


        # ---- Контракт плагина ----
        # created_files — список путей.
        #   []  → «нет изменений» (успех, выгрузка не требуется).
        #   None → нарушение контракта, ошибка.
        #   [...paths...] → обычная выгрузка.
        #
        # Разбор состояния — до нормализации, чтобы отличить
        # «нет изменений» от «файлы битые/пустые».
        if created_files is None:
            logging.error(
                "storage: плагин вернул None вместо списка (%s)",
                device_host,
            )
            return

        if not created_files:
            from core.storage.base import PushResult

            results = {
                b.id: PushResult.skipped("Изменений не обнаружено")
                for b in backends
            }
            self._save_backend_state(
                self.router_id(router),
                results,
                backup_path=backup_path,
            )
            logging.info(
                "storage: изменений нет, выгрузка не требуется (%s)",
                device_host,
            )
            return

        files = self._normalize_created_files(created_files)
        if not files:
            # created_files непуст, но нормализация ничего не дала —
            # это уже внутренняя проблема (пути битые, файлы исчезли).
            logging.warning(
                "storage: нет валидных файлов для отправки (%s)",
                device_host,
            )
            return        

        # Общие настройки хранилищ (config.json) — одни на все
        # устройства; у устройства — только включено/выключено.
        storages = global_config.load_global()

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
            device_id=self.router_id(router),
            device_host=device_host,
            device_type=device_type,
            backup_path=Path(backup_path),
            files=files,
            changed=None,
            run_time=datetime.now(),
            normalize=normalize_fn,
            device_name=router.get("name") or "",
        )
        results = {}

        # ---- authority первым ----
        for backend in backends:
            if not backend.is_authority:
                continue
            cfg = self._decrypt_backend_config(
                backend,
                global_config.effective_config(router, backend, storages),
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

            if not global_config.is_configured(backend, storages):
                from core.storage.base import PushResult
                result = PushResult.failure(
                    "Хранилище не настроено "
                    "(Настройки → Хранилища)"
                )
                results[backend.id] = result
                self._log_storage_result(backend.id, result)
                continue

            cfg = self._decrypt_backend_config(
                backend,
                global_config.effective_config(router, backend, storages),
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
        self._save_backend_state(
            ctx.device_id,
            results,
            backup_path=backup_path,
        )

    # =====================================================
    # SYNC ALL GIT VERSIONS → STORAGES
    # =====================================================

    def sync_git_to_storages(self, device_type: str, index: int) -> dict:
        """
        Выгружает ВСЕ git-коммиты устройства во все включённые
        non-authority backend'ы (FTP, WebDAV, ...).

        Каждая версия получает уникальный префикс
        '<date>_<time>_<sha7>_<filename>', чтобы:
          • версии не затирали друг друга;
          • порядок сортировался по времени на сервере;
          • по имени файла можно было найти коммит в UI.

        Возвращает:
            {
                "ok": bool,
                "message": str,
                "commits": int,        # сколько версий прошло
                "files": int,          # сколько файлов загружено
                "backends": {
                    "<id>": {"ok": int, "err": int, "skip": int},
                },
                "errors": [str, ...],  # первые ошибки (для лога)
            }
        """
        return self.sync_many_to_storages(
            [(device_type, index)],
        )[self.device_id(device_type, index)]

    def sync_many_to_storages(
        self,
        targets: list[tuple[str, int]],
        can_start=None,
        on_prepared=None,
    ) -> dict[str, dict]:
        """
        Синхронизация нескольких устройств: сначала файлы ВСЕХ
        устройств готовятся во временной папке, затем каждое
        хранилище получает их за одно подключение
        (backend.push_many), а не по запуску на устройство.

        can_start(device_type, index) -> bool — перед подготовкой
        устройства (False — устройство занято, пропускаем; в
        результатах его нет).
        on_prepared(device_type, index) — файлы устройства
        подготовлены (дальше git устройства не нужен —
        вызывающий может освободить его слот).

        → {device_id: результат как у sync_git_to_storages}.
        """
        import tempfile as _tmp
        import time as _time

        from core.storage.base import PushResult

        results: dict[str, dict] = {}
        jobs: list[dict] = []
        storages = global_config.load_global()

        with _tmp.TemporaryDirectory(prefix="nbm_sync_") as tmpdir:
            # ---- 1) подготовка ----
            for k, (device_type, index) in enumerate(targets):
                device_id = self.device_id(device_type, index)
                if can_start is not None and not can_start(device_type, index):
                    continue
                try:
                    job = self._prepare_sync(
                        device_type, index,
                        Path(tmpdir) / str(k), storages,
                    )
                except Exception as exc:
                    logging.error(
                        "☁ sync %s: подготовка: %s", device_id, exc,
                        exc_info=True,
                    )
                    job = {"result": _sync_result(f"Ошибка подготовки: {exc}")}
                finally:
                    if on_prepared is not None:
                        on_prepared(device_type, index)
                results[device_id] = job["result"]
                if job.get("ctx") is not None:
                    jobs.append(job)

            # ---- 2) выгрузка: каждое хранилище — одним заходом ----
            by_backend: dict[str, tuple] = {}
            for job in jobs:
                for b in job["backends"]:
                    by_backend.setdefault(b.id, (b, []))[1].append(job)

            for backend_id, (backend, bjobs) in by_backend.items():
                files = sum(len(j["ctx"].files) for j in bjobs)
                if len(targets) > 1:
                    logging.info(
                        "☁ Выгрузка в «%s»: устройств %d, файлов %d",
                        getattr(backend, "name", backend_id), len(bjobs), files,
                    )
                started = _time.monotonic()
                try:
                    cfg = self._decrypt_backend_config(
                        backend,
                        global_config.effective_config(
                            bjobs[0]["router"], backend, storages,
                        ),
                    )
                    # Форсируем push — не даём _should_skip_for_changes
                    # отбросить выгрузку из-за mode=changes_only.
                    cfg["mode"] = "all"
                    pushed = backend.push_many(cfg, [j["ctx"] for j in bjobs])
                except Exception as exc:
                    logging.error(
                        "☁ Выгрузка в «%s»: %s",
                        getattr(backend, "name", backend_id), exc, exc_info=True,
                    )
                    pushed = [PushResult.failure(f"Ошибка: {exc}")] * len(bjobs)
                if len(targets) > 1:
                    logging.info(
                        "☁ Выгрузка в «%s» завершена за %.0f с",
                        getattr(backend, "name", backend_id),
                        _time.monotonic() - started,
                    )

                for job, r in zip(bjobs, pushed):
                    _account_push(job["result"], backend_id, r, len(job["ctx"].files))

        # ---- 3) итоги ----
        for job in jobs:
            result = job["result"]
            total_ok = sum(b["ok"] for b in result["backends"].values())
            total_err = sum(b["err"] for b in result["backends"].values())
            result["ok"] = True
            result["message"] = (
                f"Версий: {result['commits']}, "
                f"файлов: {len(job['ctx'].files)}, "
                f"хранилищ без ошибок: {total_ok} из {len(job['backends'])}"
                + (f", с ошибками: {total_err}" if total_err else "")
                + f", файлов на хранилищах: {result['files']}"
                + (f", открытых копий удалено: {result['plain_deleted']}"
                   if result["plain_deleted"] else "")
                + (f", открытых без зашифрованной копии оставлено: "
                   f"{result['plain_left']}" if result["plain_left"] else "")
            )

        return results

    def _prepare_sync(
        self,
        device_type: str,
        index: int,
        folder: Path,
        storages: dict,
    ) -> dict:
        """
        Файлы всех версий устройства — в folder.

        → {"result": ..., "ctx": PushContext | None,
           "backends": [...], "router": {...}}
        ctx=None — выгружать нечего (причина в result).
        """
        from datetime import datetime as _dt

        from core.git_storage import (
            GitStorage,
            is_available as git_available,
        )

        result = _sync_result()
        job = {"result": result, "ctx": None, "backends": [], "router": None}

        if self.storage_manager is None:
            result["message"] = "StorageManager не загружен"
            return job

        if not git_available():
            result["message"] = "dulwich не установлен"
            return job

        routers = self._plugins.get(device_type, {}).get("routers", [])
        if not (0 <= index < len(routers)):
            result["message"] = "Устройство не найдено"
            return job

        router = routers[index]
        job["router"] = router

        raw_path = router.get("backup_path", "")
        if not raw_path:
            result["message"] = "У устройства нет backup_path"
            return job

        backup_path = resolve_backup_path(raw_path)

        if not backup_path.exists():
            result["message"] = f"Каталог не найден: {backup_path}"
            return job

        gs = GitStorage(backup_path)
        if not gs.is_initialized():
            result["message"] = "git-репозиторий не инициализирован"
            return job

        commits = gs.get_history(limit=100000)
        if not commits:
            result["message"] = "История git пуста"
            return job

        backends = [
            b for b in self.storage_manager.enabled_for(router)
            if not b.is_authority
            and global_config.is_configured(b, storages)
        ]
        if not backends:
            result["message"] = (
                "Нет включённых и настроенных хранилищ (кроме git)"
            )
            return job

        for b in backends:
            result["backends"][b.id] = {"ok": 0, "err": 0, "skip": 0}

        folder.mkdir(parents=True, exist_ok=True)
        versioned: dict = {}

        for commit in commits:
            sha = commit["sha"]
            short = commit["short"]
            filenames = commit.get("files") or []
            if not filenames:
                continue

            try:
                ts = commit.get("ts") or 0
                time_part = _dt.fromtimestamp(ts).strftime(
                    "%Y-%m-%d_%H%M%S"
                )
            except Exception:
                time_part = short

            prefix = f"{time_part}_{short}"
            added = 0

            for fname in filenames:
                data = gs.get_file_at(sha, fname)
                if data is None:
                    continue

                safe_name = Path(fname).name
                if not safe_name or safe_name in (".", ".."):
                    continue

                new_name = f"{prefix}_{safe_name}"
                p = folder / new_name
                try:
                    p.write_bytes(data)
                except OSError as exc:
                    result["errors"].append(
                        f"{sha[:7]}/{safe_name}: запись: {exc}"
                    )
                    continue

                versioned[new_name] = p
                added += 1

            if added:
                result["commits"] += 1

        if not versioned:
            result["ok"] = True
            result["message"] = "Нет файлов для выгрузки"
            return job

        device_host = (
            router.get("host")
            or router.get("ip")
            or router.get("address")
            or f"#{index}"
        )
        job["ctx"] = PushContext(
            device_id=self.router_id(router),
            device_host=device_host,
            device_type=device_type,
            backup_path=backup_path,
            files=versioned,
            changed=True,
            run_time=_dt.now(),
            normalize=identity_normalize,
            device_name=router.get("name") or "",
            immutable=True,
        )
        job["backends"] = backends
        return job

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
    def _save_backend_state(
    device_id: str,
    results: dict,
    backup_path=None,
    ) -> None:
        """
        Обновляет data/state.json по одному устройству.

        backup_path (опционально) — путь к git-репе устройства.
        Если передан, в state.json кэшируется total_commits —
        это нужно, чтобы read_root() не открывал .git каждого
        устройства на каждый рендер страницы (см.
        _build_device_summary в web/server.py).

        total_commits считается ДО захвата _STATE_LOCK, чтобы
        не держать блокировку во время I/O git. Параллельные
        бэкапы разных устройств из-за этого не мешают друг
        другу.
        """

        # ---- total_commits (вне критической секции) ----
        total_commits = None
        if backup_path is not None:
            try:
                from core.git_storage import (
                    GitStorage,
                    is_available as git_available,
                )
                if git_available():
                    gs = GitStorage(Path(backup_path))
                    if gs.is_initialized():
                        # include_marked=False — помеченные к
                        # удалению не считаем: они не отображаются
                        # как «живые» версии в списке устройств.
                        total_commits = gs.get_commit_count(
                            include_marked=False,
                        )
            except Exception as exc:
                logging.warning(
                    "state: не удалось посчитать total_commits "
                    "для %s: %s", device_id, exc,
                )

        with _STATE_LOCK:
            state = load_json(STATE_FILE, {})
            if not isinstance(state, dict):
                state = {}

            # Дополняем запись, а не заменяем: в ней же
            # attempt (record_attempt) — исход последней попытки.
            prev = state.get(device_id)
            entry = dict(prev) if isinstance(prev, dict) else {}
            entry.update({
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
            })

            if total_commits is not None:
                entry["total_commits"] = total_commits

            state[device_id] = entry

            try:
                save_json(STATE_FILE, state)
            except Exception as exc:
                logging.error(
                    "storage: не удалось сохранить state.json: %s",
                    exc,
                )    

    @staticmethod
    def record_attempt(
        device_id: str,
        ok: bool,
        error: str = "",
        duration_s: float | None = None,
    ) -> None:
        """
        Исход попытки бэкапа → state.json (attempt): время,
        ошибка, неудач подряд. Пишется при КАЖДОМ запуске —
        и удачном, и упавшем (см. core/device_status.py).
        """
        with _STATE_LOCK:
            state = load_json(STATE_FILE, {})
            if not isinstance(state, dict):
                state = {}
            state[device_id] = device_status.record_attempt(
                state.get(device_id), ok, error, duration_s,
            )
            try:
                save_json(STATE_FILE, state)
            except Exception as exc:
                logging.error(
                    "state: не удалось сохранить исход бэкапа %s: %s",
                    device_id, exc,
                )

    # =====================================================
    # BACKUP ONE DEVICE
    # =====================================================

    def backup_device(
        self,
        device_type: str,
        router: dict,
        index: int = -1,
        original: dict | None = None,
    ) -> dict:
        """
        Бэкап одного устройства — общий для одиночного запуска
        и массового прогона.

        router   — конфиг устройства (пароль зашифрован; не
                   меняется: плагин получает копию);
        original — объект из списка в памяти, если router —
                   его копия: по нему пишутся служебные поля.

        Слот устройства (core/backup_state) занимает и
        освобождает вызывающий. Исключения не пробрасываются:
        исход — в ответе и в state.json.

        Возвращает {"ok", "error", "device_id", "host", "duration_s"}.
        """
        from core.logger import (
            set_device_context,
            clear_device_context,
            set_connection_debug,
            clear_connection_debug,
            device_log_tag,
        )

        original = original if original is not None else router
        device_id = self.router_id(original)
        host = router.get("host") or router.get("ip") or "Unknown"
        device_tag = device_log_tag(device_type, router)
        debug_connection = bool(router.get("debug_connection"))

        set_device_context(device_tag)
        if debug_connection:
            set_connection_debug(device_tag)
            logging.info("🐛 Debug подключения включён: %s", device_tag)

        started = time.monotonic()
        ok, error = False, ""
        try:
            plugin = self._plugins.get(device_type) or {}
            plugin_func = plugin.get("func")
            if not plugin_func:
                raise RuntimeError(f"Плагин {device_type} не загружен")

            secure_router = copy.deepcopy(router)
            # Плагин получает уже абсолютный путь — сам он
            # относительные пути не разрешает.
            secure_router["backup_path"] = str(
                resolve_backup_path(router["backup_path"])
            )
            if secure_router.get("ssh_pass"):
                secure_router["ssh_pass"] = self.storage.decrypt(
                    secure_router["ssh_pass"]
                )
            device_store.prepare_key_path(device_type, secure_router)

            logging.info("🔄 Старт бэкапа: %s", host)

            created_files = plugin_func(secure_router)

            # [] — «нет изменений», успех. Сломавшийся плагин
            # обязан бросить исключение; None — нарушение контракта.
            if created_files is None:
                raise RuntimeError(
                    f"Плагин {device_type} вернул None вместо списка"
                )

            try:
                self.persist_router_service_fields(
                    device_type, index, secure_router, original=original,
                )
            except Exception as exc:
                logging.warning(
                    "Не удалось сохранить служебные поля роутера: %s", exc,
                )

            try:
                self._push_to_storages(
                    device_type=device_type,
                    index=index,
                    device_host=host,
                    router=router,
                    backup_path=Path(secure_router["backup_path"]),
                    created_files=created_files,
                )
            except Exception as exc:
                logging.error(
                    "storage: ошибка диспетчеризации backend'ов: %s",
                    exc, exc_info=True,
                )

            ok = True
            logging.info("✅ Успешно: %s", host)

        except Exception as exc:
            error = str(exc) or exc.__class__.__name__
            logging.error("❌ Ошибка бэкапа %s: %s", host, error)

        finally:
            duration = time.monotonic() - started
            try:
                self.record_attempt(device_id, ok, error, duration)
            finally:
                if debug_connection:
                    logging.info(
                        "🐛 Debug подключения выключен: %s", device_tag,
                    )
                    clear_connection_debug()
                clear_device_context()

        return {
            "ok": ok,
            "error": error,
            "device_id": device_id,
            "host": host,
            "duration_s": round(duration, 1),
        }

    # =====================================================
    # SERVICE FIELDS (ros_version и т.п.)
    # =====================================================

    # Поля, которые заполняет НЕ пользователь, а плагин
    # в процессе работы. Синхронизируются из рабочей
    # копии роутера в in-memory конфиг и на диск.
    _SERVICE_FIELDS = ("ros_version",)

    def persist_router_service_fields(
        self,
        device_type: str,
        index: int,
        source: dict,
        original: dict | None = None,
    ) -> None:
        """
        Переносит служебные поля из рабочей копии роутера
        (source — тот же dict, что ушёл в плагин) в in-memory
        конфиг и в devices.json.

        original — объект роутера из списка в памяти, для
        которого запускался бэкап. Если передан, устройство
        ищется по нему, а не по index: пока шёл бэкап, индекс
        мог сдвинуться (удалили соседнее устройство) или
        запись могли пересоздать (отредактировали) — тогда
        молча пропускаем, чтобы не испортить чужую запись.

        Вызывается после успешного бэкапа. No-op, если
        ничего не поменялось.
        """
        with self.routers_lock:
            routers = (
                self._plugins.get(device_type, {}).get("routers", [])
            )

            if original is not None:
                found = self.find_router_index(routers, original)
                if found is None:
                    logging.info(
                        "devices.json: устройство %s/%s изменено "
                        "или удалено во время бэкапа — служебные "
                        "поля не сохраняем",
                        device_type, index,
                    )
                    return
                index = found

            if not (0 <= index < len(routers)):
                return

            target = routers[index]

            changed_fields = []
            for field in self._SERVICE_FIELDS:
                new_value = source.get(field)
                old_value = target.get(field)
                if new_value != old_value:
                    target[field] = new_value
                    changed_fields.append(field)

            if not changed_fields:
                return

            try:
                self.save_routers(device_type)
                logging.info(
                    "devices.json: обновлены поля %s "
                    "для %s/%s",
                    changed_fields,
                    device_type,
                    index,
                )
            except Exception as exc:
                logging.warning(
                    "Не удалось сохранить devices.json "
                    "(%s): %s",
                    device_type, exc,
                )


    # =====================================================
    # RUN ALL BACKUPS
    # =====================================================

    def run_all_backups(self) -> dict:
        """
        Параллельный запуск бэкапов для всех устройств.

        Возвращает сводку: started, finished, total, success,
        failed, timed_out, skipped_disabled.
        """

        # Читаем лимиты из config.json, чтобы настройки из UI работали.
        # Клампим значения, чтобы не сломать систему безумными цифрами
        # (например, 1000 потоков или 0-секундный таймаут).
        from core.context import CONFIG_FILE, load_json
        app_cfg = load_json(CONFIG_FILE, {})

        try:
            MAX_WORKERS = max(1, min(20, int(app_cfg.get("max_parallel_backups", 5))))
        except (TypeError, ValueError):
            MAX_WORKERS = 5

        try:
            BACKUP_TIMEOUT = max(30, int(app_cfg.get("backup_timeout_seconds", 300)))
        except (TypeError, ValueError):
            BACKUP_TIMEOUT = 300

        tasks = []
        skipped_disabled = 0

        for device_type, data in self._plugins.items():

            plugin_func = data["func"]
            routers = data["routers"]

            for index, router in enumerate(routers):
                if is_device_disabled(router):
                    skipped_disabled += 1
                    continue
                tasks.append({
                    "device_type": device_type,
                    "index": index,
                    "plugin_func": plugin_func,
                    "router": router,
                })

        total = len(tasks)

        if skipped_disabled:
            logging.info(
                "⏸ Пропущено отключённых устройств: %s",
                skipped_disabled,
            )

        summary = {
            "started": device_status.now_str(),
            "finished": None,
            "total": total,
            "success": 0,
            "failed": 0,
            "timed_out": 0,
            "skipped_disabled": skipped_disabled,
        }

        if total == 0:
            logging.warning("⚠ Нет устройств для бэкапа")
            summary["finished"] = device_status.now_str()
            return summary


        # Общий wall-clock таймаут на весь прогон.
        # Executor обрабатывает задачи батчами по MAX_WORKERS,
        # поэтому даём каждому батчу свой BACKUP_TIMEOUT.
        batches = max(1, math.ceil(total / MAX_WORKERS))
        overall_timeout = BACKUP_TIMEOUT * batches

        logging.info(
            "🚀 Запуск бэкапа %s устройств "
            "(параллельно: %s, общий таймаут: %s сек)",
            total,
            MAX_WORKERS,
            overall_timeout,
        )

        success = 0
        failed = 0        

        def worker(task):
            # Устройство занято, пока ЭТОТ поток реально
            # работает — даже если прогон уже завершился
            # по общему таймауту (finish_device в finally).
            device_id = self.router_id(task["router"])
            backup_state.mark_device_running(device_id)
            try:
                return self.backup_device(
                    task["device_type"], task["router"], task["index"],
                )
            finally:
                backup_state.finish_device(device_id)

        executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)
        try:
            future_map = {
                executor.submit(worker, task): task
                for task in tasks
            }

            processed: set = set()

            try:
                for future in as_completed(
                    future_map,
                    timeout=overall_timeout,
                ):
                    processed.add(future)
                    task = future_map[future]
                    ip = task["router"].get(
                        "ip",
                        task["router"].get("host", "Unknown"),
                    )

                    try:
                        outcome = future.result()
                    except Exception as e:
                        outcome = {"ok": False}
                        logging.error(
                            "❌ Ошибка бэкапа %s: %s",
                            ip,
                            e,
                        )
                    if outcome.get("ok"):
                        success += 1
                    else:
                        failed += 1

            except FuturesTimeoutError:
                logging.error(
                    "⏰ Общий таймаут бэкапа истёк (%s сек)",
                    overall_timeout,
                )

            # Всё, что не завершилось к моменту таймаута,
            # помечаем как провал. cancel() снимет с очереди
            # те задачи, что ещё не стартовали; уже запущенные
            # потоки отменить нельзя, но мы хотя бы не блокируемся.
            still_pending = set(future_map) - processed
            for future in still_pending:
                task = future_map[future]
                ip = task["router"].get(
                    "ip",
                    task["router"].get("host", "Unknown"),
                )
                if future.cancel():
                    logging.error(
                        "⏰ Таймаут бэкапа: %s — не запускался, снят",
                        ip,
                    )
                    self.record_attempt(
                        self.router_id(task["router"]), False,
                        "Не запускался: истёк общий таймаут прогона",
                    )
                else:
                    # Поток уже работает и остановить его нельзя.
                    # Устройство остаётся занятым (см.
                    # mark_device_running) — новые бэкапы его и
                    # массовые прогоны подождут, пока поток не
                    # завершится.
                    logging.error(
                        "⏰ Таймаут бэкапа: %s — поток ещё работает; "
                        "устройство занято до его завершения",
                        ip,
                    )

            failed += len(still_pending)
            summary["timed_out"] = len(still_pending)

        finally:
            # Не ждём зависшие потоки: лучше потерять слот
            # в пуле, чем заблокировать весь прогон.
            executor.shutdown(wait=False)

        logging.info(
            "🏁 Бэкап завершён. Успешно: %s, ошибок: %s, всего: %s",
            success,
            failed,
            total,
        )

        summary.update(
            finished=device_status.now_str(), success=success, failed=failed,
        )
        return summary


# =========================================================
# Синхронизация: служебное
# =========================================================

def _sync_result(message: str = "") -> dict:
    return {
        "ok": False,
        "message": message,
        "commits": 0,
        "files": 0,
        "backends": {},
        "errors": [],
        "plain_deleted": 0,     # открытые копии, удалённые (есть .7z)
        "plain_left": 0,        # открытые без зашифрованной пары
    }


def _account_push(result: dict, backend_id: str, r, total_files: int) -> None:
    """Учесть результат выгрузки одного хранилища в итоге устройства."""
    stats = result["backends"][backend_id]
    details = r.details or {}
    if details.get("skipped"):
        stats["skip"] += 1
        return
    result["plain_deleted"] += details.get("plain_deleted", 0)
    result["plain_left"] += details.get("plain_left", 0)
    result["files"] += details.get("uploaded", total_files if r.ok else 0)
    if r.ok and not details.get("failed"):
        stats["ok"] += 1
    else:
        stats["err"] += 1
        result["errors"].append(f"{backend_id}: {r.message}")
