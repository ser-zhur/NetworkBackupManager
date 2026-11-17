# =========================================================
# core/storage/manager.py
# =========================================================
#
# Сканирует каталог storages/ и собирает все backend'ы.
#
# Каталог storages/ лежит рядом с EXE (или рядом с
# корнем проекта в режиме разработки) — по аналогии с
# plugins/ для роутеров.
#
# =========================================================

from __future__ import annotations

import importlib.util
import inspect
import logging
import sys
from pathlib import Path

from core.context import BASE_DIR
from core.storage.base import StorageBackend


logger = logging.getLogger(__name__)


# =========================================================
# STORAGE MANAGER
# =========================================================

class StorageManager:
    """
    Загружает backend'ы из каталога storages/.

    Использование:

        sm = StorageManager()
        sm.discover()
        sm.all()                    # [backend, ...]
        sm.get("git")               # backend или None
        sm.enabled_for(router)      # [backend, ...] только включённые
    """

    def __init__(self, directory: Path | None = None):
        if directory is None:
            directory = BASE_DIR / "storages"
        self.dir = Path(directory)
        self._backends: dict[str, StorageBackend] = {}
        self._errors: list[str] = []

    # -----------------------------------------------------
    # DISCOVER
    # -----------------------------------------------------

    def discover(self) -> None:
        """Найти и загрузить все *.py в storages/."""

        self._backends.clear()
        self._errors.clear()

        if not self.dir.exists():
            logger.warning(
                "storage: каталог storages/ не найден: %s",
                self.dir,
            )
            return

        # Сортируем по имени для предсказуемого порядка
        # (git должен попасться раньше остальных —
        # имя 'git.py' < 'smb.py' и т.п.)
        for path in sorted(self.dir.glob("*.py")):
            if path.name.startswith("_"):
                continue
            if path.name == "__init__.py":
                continue

            try:
                self._load_file(path)
            except Exception as exc:
                msg = f"{path.name}: {exc}"
                self._errors.append(msg)
                logger.error(
                    "storage: не удалось загрузить %s: %s",
                    path.name,
                    exc,
                    exc_info=True,
                )

        # Проверка: должен быть ровно один authority
        authorities = [
            b for b in self._backends.values() if b.is_authority
        ]
        if len(authorities) > 1:
            names = ", ".join(b.id for b in authorities)
            logger.warning(
                "storage: несколько authority backend'ов (%s); "
                "будет использован первый",
                names,
            )

        logger.info(
            "storage: загружено %d backend'ов: %s",
            len(self._backends),
            ", ".join(self._backends.keys()) or "—",
        )

    def _load_file(self, path: Path) -> None:
        """Загрузить один файл и вытащить все backend'ы."""

        module_name = f"nbm_storage_{path.stem}"

        spec = importlib.util.spec_from_file_location(
            module_name, str(path)
        )
        if spec is None or spec.loader is None:
            raise ImportError(f"не удалось создать spec для {path}")

        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)

        # Ищем все подклассы StorageBackend в модуле
        found_any = False
        for _, obj in inspect.getmembers(module, inspect.isclass):
            if obj is StorageBackend:
                continue
            if not issubclass(obj, StorageBackend):
                continue
            # Только классы, объявленные в этом файле
            if obj.__module__ != module_name:
                continue

            instance = obj()
            self._validate(instance, path)
            self._backends[instance.id] = instance
            found_any = True

        if not found_any:
            raise ValueError(
                "в файле нет ни одного класса-наследника "
                "StorageBackend"
            )

    @staticmethod
    def _validate(backend: StorageBackend, path: Path) -> None:
        """Базовая валидация backend'а."""
        if not backend.id or backend.id == "base":
            raise ValueError(
                f"{path.name}: не задан корректный id"
            )
        if not backend.name:
            raise ValueError(
                f"{backend.id}: не задан name"
            )
        if not isinstance(backend.fields, list):
            raise ValueError(
                f"{backend.id}: fields должен быть списком"
            )

    # -----------------------------------------------------
    # ДОСТУП
    # -----------------------------------------------------

    def all(self) -> list[StorageBackend]:
        return list(self._backends.values())

    def get(self, backend_id: str) -> StorageBackend | None:
        return self._backends.get(backend_id)

    def ids(self) -> list[str]:
        return list(self._backends.keys())

    def errors(self) -> list[str]:
        return list(self._errors)

    # -----------------------------------------------------
    # ПРИМЕНЕНИЕ К УСТРОЙСТВУ
    # -----------------------------------------------------
    def enabled_for(self, router: dict) -> list[StorageBackend]:
        """
        Возвращает backend'ы, включённые в конфиге устройства.

        Backend'ы с always_enabled (git) включаются всегда,
        вне зависимости от cfg.get("enabled").
        """
        storage_cfg = router.get("storage") or {}
        if not isinstance(storage_cfg, dict):
            storage_cfg = {}

        result: list[StorageBackend] = []
        for backend in self._backends.values():
            cfg = storage_cfg.get(backend.id) or {}

            if backend.always_enabled:
                result.append(backend)
                continue

            if not isinstance(cfg, dict):
                continue
            if not cfg.get("enabled"):
                continue
            result.append(backend)

        # Сортировка: authority первым
        result.sort(key=lambda b: (not b.is_authority, b.id))
        return result
    
    def defaults_for_new_router(self, storages: dict | None = None) -> dict:
        """
        Хранилища нового устройства: включены все, у которых
        заполнены общие настройки (Настройки → Хранилища).
        Сами настройки у устройства не хранятся.
        """
        from core.storage.global_config import is_configured, load_global

        if storages is None:
            storages = load_global()

        return {
            backend.id: {
                "enabled": (
                    backend.always_enabled
                    or backend.is_authority
                    or is_configured(backend, storages)
                ),
            }
            for backend in self._backends.values()
        }


# =========================================================
# ГЛОБАЛЬНЫЙ ЭКЗЕМПЛЯР (создаётся в web/server.py)
# =========================================================
#
# Явно не создаём здесь, чтобы избежать побочных эффектов
# при импорте. server.py вызовет StorageManager().discover()
# после ensure_web_files().
#
# =========================================================