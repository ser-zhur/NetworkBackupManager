# =========================================================
# core/storage/global_config.py
# =========================================================
#
# Общие настройки хранилищ — одни на все устройства.
#
#   config.json:
#       "storages": {
#           "webdav": {"url": ..., "user": ..., "pass": <enc>,
#                      "remote_dir": "/", "mode": ...},
#           "ftp":    {...},
#       }
#
#   routers.json (у устройства — только включено или нет):
#       "storage": {"webdav": {"enabled": true}, ...}
#
# Папка устройства на сервере = <базовая папка>/<имя его
# локальной папки бэкапов> (core.storage.base.device_remote_dir).
#
# Пароли хранятся зашифрованными (SecureStorage) — как и
# раньше в routers.json. Расшифровка — в PluginManager.
#
# =========================================================

from __future__ import annotations

import collections
import json
import logging
import posixpath
import shutil
from datetime import datetime
from pathlib import Path

from core.context import CONFIG_FILE, load_json, save_json

logger = logging.getLogger(__name__)

STORAGES_KEY = "storages"


# =========================================================
# ЧТЕНИЕ / ЗАПИСЬ
# =========================================================

def load_global() -> dict:
    """{backend_id: {поле: значение}} из config.json."""
    data = load_json(CONFIG_FILE, {}).get(STORAGES_KEY)
    return data if isinstance(data, dict) else {}


def save_global(storages: dict) -> None:
    config = load_json(CONFIG_FILE, {})
    config[STORAGES_KEY] = storages
    save_json(CONFIG_FILE, config)


# =========================================================
# УСТРОЙСТВО × ХРАНИЛИЩЕ
# =========================================================

def is_enabled_for(router: dict, backend) -> bool:
    """Включено ли хранилище у устройства (ползунок)."""
    if backend.always_enabled:
        return True
    cfg = (router.get("storage") or {}).get(backend.id)
    return isinstance(cfg, dict) and bool(cfg.get("enabled"))


def is_configured(backend, storages: dict | None = None) -> bool:
    """Заполнены ли обязательные поля общих настроек."""
    if backend.is_authority or backend.always_enabled:
        return True
    if storages is None:
        storages = load_global()
    cfg = storages.get(backend.id) or {}
    if not cfg:
        return False
    return all(
        str(cfg.get(f.name) or "").strip()
        for f in backend.fields
        if f.required and f.type != "password"
    )


def effective_config(
    router: dict,
    backend,
    storages: dict | None = None,
) -> dict:
    """
    Конфиг backend'а для устройства: общие настройки +
    enabled устройства. Пароли — зашифрованные.
    """
    if storages is None:
        storages = load_global()
    cfg = dict(storages.get(backend.id) or {})
    cfg["enabled"] = is_enabled_for(router, backend)
    return cfg


# =========================================================
# ПЕРЕНОС СО СТАРОЙ СХЕМЫ (настройки в каждом устройстве)
# =========================================================

def _common_dir(dirs: list[str]) -> str:
    """Общая родительская папка: ['/a/x', '/a/y/z'] → '/a'."""
    norm = [
        "/" + "/".join(p for p in d.replace("\\", "/").split("/") if p)
        for d in dirs if d
    ]
    if not norm:
        return "/"
    if len(norm) == 1:
        return posixpath.dirname(norm[0]) or "/"
    common = posixpath.commonpath(norm)
    # Если общая часть совпала с одной из папок целиком
    # (/home/ftptest и /home/ftptest/x) — это и есть база.
    return common or "/"


def build_global_from_routers(storage_manager, all_routers: list[dict]) -> dict:
    """
    Общие настройки из старых настроек устройств.

    Для каждого внешнего хранилища берётся самый частый
    набор полей среди устройств, где оно включено (а если
    нигде не включено — среди всех с заполненными полями).
    Базовая папка — общая родительская папка нынешних;
    папка устройства внутри неё = имя его локальной папки.
    """
    result: dict = {}

    for backend in storage_manager.all():
        if backend.is_authority or backend.always_enabled:
            continue

        field_names = [f.name for f in backend.fields]

        def cfgs(only_enabled: bool):
            out = []
            for r in all_routers:
                c = (r.get("storage") or {}).get(backend.id)
                if not isinstance(c, dict):
                    continue
                if only_enabled and not c.get("enabled"):
                    continue
                if not only_enabled and not any(
                    str(c.get(n) or "").strip()
                    for n in field_names if n not in ("remote_dir", "mode")
                ):
                    continue
                out.append(c)
            return out

        candidates = cfgs(True) or cfgs(False)
        if not candidates:
            continue

        counter = collections.Counter(
            json.dumps(
                {n: c.get(n) for n in field_names if n != "remote_dir"},
                sort_keys=True, ensure_ascii=False,
            )
            for c in candidates
        )
        chosen = json.loads(counter.most_common(1)[0][0])
        chosen = {k: v for k, v in chosen.items() if v is not None}

        chosen["remote_dir"] = _common_dir(
            [str(c.get("remote_dir") or "") for c in candidates]
        )

        result[backend.id] = chosen

    return result


def strip_device_storage(router: dict) -> bool:
    """
    Оставляет в router["storage"] только {"enabled": bool}.
    True — если что-то изменилось.
    """
    storage = router.get("storage")
    if not isinstance(storage, dict):
        return False
    new = {
        bid: {"enabled": bool(cfg.get("enabled"))}
        for bid, cfg in storage.items()
        if isinstance(cfg, dict)
    }
    if new == storage:
        return False
    router["storage"] = new
    return True


def migrate(plugin_manager, storage_manager) -> dict | None:
    """
    Однократный перенос: настройки хранилищ из устройств →
    config.json; у устройств остаётся только enabled.

    Идемпотентно: общий раздел создаётся, только если его
    ещё нет; лишние поля у устройств вычищаются всегда
    (например, после ручной правки routers.json).

    Перед первой правкой routers.json рядом кладётся копия
    routers.json.bak-<дата>.

    Возвращает отчёт или None, если делать было нечего.
    """
    if plugin_manager is None or storage_manager is None:
        return None

    report = {"created_global": False, "devices_stripped": 0, "backups": []}

    all_routers = [
        r
        for pd in plugin_manager._plugins.values()
        for r in pd.get("routers", [])
    ]

    config = load_json(CONFIG_FILE, {})
    if STORAGES_KEY not in config:
        storages = build_global_from_routers(storage_manager, all_routers)
        save_global(storages)
        report["created_global"] = True
        logger.info(
            "storage: общие настройки хранилищ созданы из настроек "
            "устройств: %s",
            {k: v.get("remote_dir") for k, v in storages.items()},
        )

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    with plugin_manager.routers_lock:
        for device_type, pd in plugin_manager._plugins.items():
            changed = [r for r in pd.get("routers", []) if strip_device_storage(r)]
            if not changed:
                continue

            plugin_dir = pd.get("dir")
            if plugin_dir:
                src = Path(plugin_dir) / "routers.json"
                if src.exists():
                    dst = src.with_name(f"routers.json.bak-{stamp}")
                    if not dst.exists():
                        shutil.copy2(src, dst)
                        report["backups"].append(str(dst))

            plugin_manager.save_routers(device_type)
            report["devices_stripped"] += len(changed)

    if report["created_global"] or report["devices_stripped"]:
        logger.info("storage: перенос настроек хранилищ: %s", report)
        return report
    return None
