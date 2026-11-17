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

from datetime import datetime
from pathlib import Path

from core import backup_state
from core.secure_storage import SecureStorage
from core.context import (
    PLUGINS_DIR,
    BASE_DIR,
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
        # Явно тот же ключ, что и у web/server.py.
        self.storage = SecureStorage(BASE_DIR)
        self.storage_manager = storage_manager

        # Сериализует изменения списков routers в памяти и
        # запись routers.json. RLock — чтобы код, уже
        # держащий лок (веб-обработчик), мог вызвать
        # save_routers() без взаимоблокировки.
        self.routers_lock = threading.RLock()

    # =====================================================
    # ROUTERS.JSON
    # =====================================================

    def save_routers(self, device_type: str) -> None:
        """
        Атомарно сохраняет routers.json плагина device_type
        из текущего списка в памяти.

        • под routers_lock — параллельные записи (веб +
          потоки бэкапа) не перемешиваются;
        • через save_json: tmp + fsync + os.replace —
          сбой посреди записи не оставит обрезанный файл.

        Бросает исключение при ошибке: вызывающий решает,
        как сообщить пользователю.
        """
        with self.routers_lock:
            plugin = self._plugins.get(device_type)
            if plugin is None:
                raise KeyError(f"Плагин не найден: {device_type}")

            plugin_dir = plugin.get("dir")
            if not plugin_dir:
                raise FileNotFoundError(
                    f"Каталог плагина {device_type} не известен"
                )

            save_json(
                Path(plugin_dir) / "routers.json",
                copy.deepcopy(plugin["routers"]),
            )

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
                            router["backup_path"] = str(
                                resolve_backup_path(backup_path)
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
                    # Реальный каталог (с исходным регистром
                    # имени) — для save_routers().
                    "dir": item_path,
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
                f"{device_type}/{index}",
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
            device_id=f"{device_type}/{index}",
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
        )[f"{device_type}/{index}"]

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
                device_id = f"{device_type}/{index}"
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
            device_id=f"{device_type}/{index}",
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

            entry = {
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
        конфиг и в routers.json.

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
                        "routers.json: устройство %s/%s изменено "
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
                    "routers.json: обновлены поля %s "
                    "для %s/%s",
                    changed_fields,
                    device_type,
                    index,
                )
            except Exception as exc:
                logging.warning(
                    "Не удалось сохранить routers.json "
                    "(%s): %s",
                    device_type, exc,
                )


    # =====================================================
    # RUN ALL BACKUPS
    # =====================================================

    def run_all_backups(self):
        """Параллельный запуск бэкапов для всех устройств"""

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

        if total == 0:
            logging.warning("⚠ Нет устройств для бэкапа")
            return


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
            # Импорт локальный — чтобы не создавать цикл
            # на этапе импорта модуля.
            from core.logger import (
                set_device_context,
                clear_device_context,
                set_connection_debug,
                clear_connection_debug,
                device_log_tag,
            )

            device_type = task["device_type"]
            plugin_func = task["plugin_func"]
            router = task["router"]

            ip = router.get("ip", router.get("host", "Unknown"))
            # Тег — тот же, по которому фильтрует /logs/device
            device_tag = device_log_tag(device_type, router)
            debug_connection = bool(router.get("debug_connection"))

            # Устройство занято, пока ЭТОТ поток реально
            # работает — даже если прогон уже завершился
            # по общему таймауту (finish_device в finally).
            device_id = f"{device_type}/{task['index']}"
            backup_state.mark_device_running(device_id)

            set_device_context(device_tag)
            if debug_connection:
                set_connection_debug(device_tag)
                logging.info(
                    "🐛 Debug подключения включён: %s", device_tag
                )

            try:
                backup_path = resolve_backup_path(router["backup_path"])

                secure_router = copy.deepcopy(router)
                # Плагин получает уже абсолютный путь — сам
                # он относительные пути не разрешает.
                secure_router["backup_path"] = str(backup_path)

                if "ssh_pass" in secure_router:
                    secure_router["ssh_pass"] = self.storage.decrypt(
                        secure_router["ssh_pass"]
                    )

                logging.info("🔄 Старт бэкапа: %s", ip)

                created_files = plugin_func(secure_router)

                try:
                    self.persist_router_service_fields(
                        device_type,
                        task["index"],
                        secure_router,
                        original=router,
                    )
                except Exception as exc:
                    logging.warning(
                        "Не удалось сохранить служебные "
                        "поля роутера: %s", exc,
                    )

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
                if debug_connection:
                    logging.info(
                        "🐛 Debug подключения выключен: %s", device_tag
                    )
                    clear_connection_debug()
                clear_device_context()
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
                        future.result()
                        success += 1
                    except Exception as e:
                        failed += 1
                        logging.error(
                            "❌ Ошибка бэкапа %s: %s",
                            ip,
                            e,
                       )

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
