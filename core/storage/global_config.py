# =========================================================
# core/storage/global_config.py
# =========================================================
#
# Внешние хранилища — общие для всех устройств.
#
#   config.json → "remotes": список хранилищ rclone
#   (см. core.storage.rclone_remote), их может быть
#   несколько одного типа:
#
#       [{"id": "webdav", "name": "Nextcloud офис",
#         "type": "webdav", "options": {...},
#         "base_dir": "/", "mode": "changes_only"}, ...]
#
#   routers.json — у устройства только ползунки:
#       "storage": {"webdav": {"enabled": true}, ...}
#
# Папка устройства на хранилище = <base_dir>/<имя локальной
# папки бэкапов> (core.storage.base.device_remote_dir).
#
# Перенос старых схем (идемпотентно, при старте сервера):
#   1) настройки в каждом устройстве (routers.json)
#   2) общие настройки по типам  (config.json → "storages")
#   → список хранилищ rclone     (config.json → "remotes")
#
# =========================================================

from __future__ import annotations

import collections
import json
import logging
import posixpath
import re
import shutil
import uuid
from datetime import datetime
from pathlib import Path

from core.context import CONFIG_FILE, load_json, save_json

logger = logging.getLogger(__name__)

REMOTES_KEY = "remotes"
LEGACY_STORAGES_KEY = "storages"


# =========================================================
# ЧТЕНИЕ / ЗАПИСЬ
# =========================================================

def load_remotes() -> list[dict]:
    data = load_json(CONFIG_FILE, {}).get(REMOTES_KEY)
    if not isinstance(data, list):
        return []
    return [r for r in data if isinstance(r, dict) and r.get("id") and r.get("type")]


def save_remotes(remotes: list[dict]) -> None:
    config = load_json(CONFIG_FILE, {})
    config[REMOTES_KEY] = remotes
    save_json(CONFIG_FILE, config)


def load_global() -> dict:
    """{id хранилища: описание} — для сопоставления с backend.id."""
    return {r["id"]: r for r in load_remotes()}


def new_remote_id(name: str, existing: set[str]) -> str:
    """Короткий читаемый id: латиница из имени + суффикс."""
    base = re.sub(r"[^a-z0-9]+", "-", str(name).lower()).strip("-")[:20] or "remote"
    while True:
        rid = f"{base}-{uuid.uuid4().hex[:6]}"
        if rid not in existing and rid != "git":
            return rid


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
    """Заполнены ли обязательные параметры хранилища."""
    if backend.is_authority or backend.always_enabled:
        return True
    if storages is None:
        storages = load_global()
    cfg = storages.get(backend.id)
    if not cfg:
        return False
    check = getattr(backend, "is_configured_cfg", None)
    return bool(check(cfg)) if check else True


def effective_config(
    router: dict,
    backend,
    storages: dict | None = None,
) -> dict:
    """Описание хранилища + enabled устройства."""
    if storages is None:
        storages = load_global()
    cfg = dict(storages.get(backend.id) or {})
    cfg["enabled"] = is_enabled_for(router, backend)
    return cfg


# =========================================================
# ПЕРЕНОС СТАРЫХ СХЕМ
# =========================================================

# Поля старых бэкендов (до перехода на rclone для всех)
_LEGACY_FIELDS = {
    "webdav": ["url", "vendor", "user", "pass", "remote_dir", "mode"],
    "ftp": ["host", "port", "remote_dir", "user", "pass", "use_tls", "mode"],
    "smb": ["host", "port", "user", "pass", "domain", "use_kerberos", "remote_dir", "mode"],
}
_LEGACY_REQUIRED = {
    "webdav": ["url", "user"],
    "ftp": ["host", "user"],
    "smb": ["host", "user", "remote_dir"],
}


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
    return posixpath.commonpath(norm) or "/"


def build_global_from_routers(storage_manager, all_routers: list[dict]) -> dict:
    """
    Схема 1 → 2: общие настройки по типам из настроек
    устройств. Самый частый набор полей среди устройств, где
    хранилище включено; базовая папка — общая родительская.

    storage_manager не используется (оставлен для
    совместимости): поля старых типов известны заранее.
    """
    result: dict = {}

    for bid, field_names in _LEGACY_FIELDS.items():
        def cfgs(only_enabled: bool):
            out = []
            for r in all_routers:
                c = (r.get("storage") or {}).get(bid)
                if not isinstance(c, dict):
                    continue
                if only_enabled and not c.get("enabled"):
                    continue
                if not only_enabled and not any(
                    str(c.get(n) or "").strip()
                    for n in field_names if n not in ("remote_dir", "mode", "port")
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
        result[bid] = chosen

    return result


def legacy_storage_to_remote(bid: str, cfg: dict) -> dict | None:
    """
    Схема 2 → 3: старые общие настройки типа → хранилище
    rclone с тем же id (ползунки устройств и история
    статусов сохраняются). None — тип не был настроен.
    """
    cfg = cfg or {}
    if bid not in _LEGACY_FIELDS:
        return None
    if any(not str(cfg.get(f) or "").strip() for f in _LEGACY_REQUIRED[bid]):
        return None

    def put(opts, key, value):
        if value not in (None, ""):
            opts[key] = value

    options: dict = {}
    if bid == "webdav":
        put(options, "url", cfg.get("url"))
        put(options, "vendor", cfg.get("vendor"))
        put(options, "user", cfg.get("user"))
        put(options, "pass", cfg.get("pass"))
        name = "WebDAV"
        host = re.sub(r"^https?://", "", str(cfg.get("url"))).split("/")[0]
    elif bid == "ftp":
        put(options, "host", cfg.get("host"))
        put(options, "port", cfg.get("port"))
        put(options, "user", cfg.get("user"))
        put(options, "pass", cfg.get("pass"))
        if cfg.get("use_tls"):
            # Был явный FTPS (FTP_TLS + PROT P) без проверки
            # сертификата — то же в терминах rclone.
            options["explicit_tls"] = True
            options["no_check_certificate"] = True
        name = "FTP"
        host = str(cfg.get("host"))
    else:  # smb
        put(options, "host", cfg.get("host"))
        put(options, "port", cfg.get("port"))
        put(options, "user", cfg.get("user"))
        put(options, "pass", cfg.get("pass"))
        put(options, "domain", cfg.get("domain"))
        if cfg.get("use_kerberos"):
            options["use_kerberos"] = True
        name = "SMB"
        host = str(cfg.get("host"))

    return {
        "id": bid,
        "name": f"{name} · {host}" if host else name,
        "type": bid,
        "options": options,
        "base_dir": cfg.get("remote_dir") or "/",
        "mode": cfg.get("mode") or "changes_only",
    }


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


def _backup_config(stamp: str) -> str | None:
    src = Path(CONFIG_FILE)
    if not src.exists():
        return None
    dst = src.with_name(f"{src.name}.bak-{stamp}")
    if not dst.exists():
        shutil.copy2(src, dst)
    return str(dst)


def migrate(plugin_manager, storage_manager) -> dict | None:
    """
    Перенос старых схем хранения настроек хранилищ.
    Идемпотентно. Копии: devices.json.bak-<дата>,
    config.json.bak-<дата>.

    Возвращает отчёт или None, если делать было нечего.
    """
    if plugin_manager is None:
        return None

    report = {"created_remotes": [], "devices_stripped": 0, "backups": []}
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")

    all_routers = [
        r
        for pd in plugin_manager._plugins.values()
        for r in pd.get("routers", [])
    ]

    config = load_json(CONFIG_FILE, {})
    if REMOTES_KEY not in config:
        legacy = config.get(LEGACY_STORAGES_KEY)
        if not isinstance(legacy, dict):
            legacy = build_global_from_routers(storage_manager, all_routers)

        remotes = [
            r for r in (
                legacy_storage_to_remote(bid, legacy.get(bid) or {})
                for bid in ("webdav", "ftp", "smb")
            ) if r
        ]

        backup = _backup_config(stamp)
        if backup:
            report["backups"].append(backup)

        config = load_json(CONFIG_FILE, {})
        config[REMOTES_KEY] = remotes
        config.pop(LEGACY_STORAGES_KEY, None)
        save_json(CONFIG_FILE, config)

        report["created_remotes"] = [r["id"] for r in remotes]
        logger.info(
            "storage: хранилища переведены на rclone: %s",
            {r["id"]: (r["type"], r["base_dir"]) for r in remotes},
        )

        if storage_manager is not None:
            storage_manager.reload_remotes()

    with plugin_manager.routers_lock:
        stripped = sum(
            1
            for pd in plugin_manager._plugins.values()
            for r in pd.get("routers", [])
            if strip_device_storage(r)
        )
        if stripped:
            from core.context import DEVICES_FILE

            if DEVICES_FILE.exists():
                dst = DEVICES_FILE.with_name(f"devices.json.bak-{stamp}")
                if not dst.exists():
                    shutil.copy2(DEVICES_FILE, dst)
                    report["backups"].append(str(dst))

            plugin_manager.save_routers()
            report["devices_stripped"] += stripped

    if report["created_remotes"] or report["devices_stripped"] or report["backups"]:
        logger.info("storage: перенос настроек хранилищ: %s", report)
        return report
    return None
