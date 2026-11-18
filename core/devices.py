# =========================================================
# core/devices.py — хранение устройств (data/devices.json)
# =========================================================
#
#     {"devices": [
#         {"id": "7f3a9c2e41b0", "type": "mikrotik", "name": "R1", ...},
#         ...
#     ]}
#
# • id — постоянный идентификатор устройства (12 hex), выдаётся
#   один раз и не меняется при удалении соседей и редактировании;
# • type — тип устройства = имя папки плагина в нижнем регистре;
# • порядок в списке — порядок на экране.
#
# SSH-ключи устройств — data/keys/<type>/<имя файла>.
# Плагин получает уже абсолютный путь (prepare_key_path).
#
# Прежние версии хранили устройства в plugins/<Тип>/routers.json,
# ключи — там же; migrate_from_plugins() переносит их один раз.
#
# =========================================================

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import uuid
from datetime import datetime
from pathlib import Path

from core import context
from core.context import load_json, save_json

logger = logging.getLogger(__name__)

DEFAULT_KEY_NAME = "id_rsa.key"

_ID_RE = re.compile(r"^[0-9a-f]{12}$")


def new_id(existing=()) -> str:
    taken = set(existing)
    while True:
        value = uuid.uuid4().hex[:12]
        if value not in taken:
            return value


def is_valid_id(value) -> bool:
    return isinstance(value, str) and bool(_ID_RE.match(value))


def ensure_ids(devices: list[dict]) -> bool:
    """Выдать id устройствам без id (и с повтором). True — что-то выдано."""
    seen: set[str] = set()
    changed = False
    for dev in devices:
        dev_id = dev.get("id")
        if not is_valid_id(dev_id) or dev_id in seen:
            dev["id"] = new_id(seen | {d.get("id") for d in devices})
            changed = True
        seen.add(dev["id"])
    return changed


# =========================================================
# Файл
# =========================================================

def load_devices(path: Path | None = None) -> list[dict]:
    path = Path(path or context.DEVICES_FILE)
    data = load_json(path, {})
    devices = data.get("devices")
    if not isinstance(devices, list):
        return []
    return [d for d in devices if isinstance(d, dict) and d.get("type")]


def save_devices(devices: list[dict], path: Path | None = None) -> None:
    save_json(Path(path or context.DEVICES_FILE), {"devices": devices})


# =========================================================
# SSH-ключи
# =========================================================

def key_name(router: dict) -> str:
    return (router.get("ssh_key") or router.get("private_key") or "").strip()


def local_key_path(device_type: str, name: str) -> Path | None:
    """
    data/keys/<type>/<name> для относительного имени ключа или
    None (имя пустое, абсолютное или выходит за папку ключей).
    """
    if not name or Path(name).is_absolute():
        return None
    base = (context.KEYS_DIR / device_type.lower()).resolve()
    try:
        path = (base / name).resolve()
    except OSError:
        return None
    if path == base or not path.is_relative_to(base):
        return None
    return path


def prepare_key_path(device_type: str, router: dict) -> None:
    """
    Рабочая копия устройства для плагина: относительное имя
    ключа → абсолютный путь в data/keys/<type>/ (плагины
    принимают абсолютный ssh_key как есть). Если файла там
    нет — копию не трогаем: плагин ищет по-старому.
    """
    path = local_key_path(device_type, key_name(router) or DEFAULT_KEY_NAME)
    if path is not None and path.is_file():
        _restrict_key_permissions(path)
        router["ssh_key"] = str(path)


def _restrict_key_permissions(path: Path) -> None:
    """
    Linux/macOS: системный ssh (MikroTik ROS6) отказывается
    работать с ключом, доступным группе или остальным
    («UNPROTECTED PRIVATE KEY FILE») — приводим к 600.
    На Windows права задаются ACL, здесь не трогаем.
    """
    if os.name == "nt":
        return
    try:
        mode = path.stat().st_mode & 0o777
        if mode & 0o077:
            path.chmod(0o600)
            logger.info("🔑 Права ключа %s: %o → 600", path, mode)
    except OSError as exc:
        logger.warning("Не удалось ограничить права ключа %s: %s", path, exc)


# =========================================================
# Миграция из plugins/<Тип>/routers.json
# =========================================================

def _legacy_device_lists(plugins_dir: Path) -> list[tuple[str, Path, list]]:
    found = []
    if not plugins_dir.is_dir():
        return found
    for item in sorted(plugins_dir.iterdir(), key=lambda p: p.name.lower()):
        routers_file = item / "routers.json"
        if not item.is_dir() or not routers_file.is_file():
            continue
        try:
            data = json.loads(routers_file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.error("Миграция: не прочитан %s: %s", routers_file, exc)
            continue
        if isinstance(data, list):
            found.append((item.name.lower(), item, data))
    return found


def _copy_keys(
    device_type: str, plugin_dir: Path, routers: list, failed: list[str],
) -> list[str]:
    """
    Ключи устройств → data/keys/<type>/. Ошибка копирования
    одного ключа (например, нет прав на чтение) не мешает
    перенести устройства: ключ попадает в failed, плагин
    по-прежнему найдёт его в своей папке.
    """
    names = {key_name(r) or DEFAULT_KEY_NAME for r in routers if isinstance(r, dict)}
    copied = []
    for name in sorted(names):
        dst = local_key_path(device_type, name)
        if dst is None or dst.exists():
            continue
        src = (plugin_dir / name).resolve()
        if not src.is_file() or not src.is_relative_to(plugin_dir.resolve()):
            continue
        if src.suffix.lower() in (".py", ".json", ".pyc"):
            continue
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        except OSError as exc:
            logger.warning(
                "Миграция: ключ %s не скопирован в %s: %s — скопируйте "
                "его вручную", src, dst, exc,
            )
            failed.append(f"{device_type}/{name}")
            continue
        copied.append(f"{device_type}/{name}")
    return copied


def migrate_from_plugins(plugins_dir: Path | None = None) -> dict | None:
    """
    Один раз: plugins/<Тип>/routers.json → data/devices.json,
    ключи → data/keys/<type>/, ключи state.json «тип/номер» → id.

    Ничего не делает, если devices.json уже есть или старых
    файлов нет. Старые файлы не трогает (копия в
    data/migration-backup-<дата>/). Возвращает сводку или None.
    """
    plugins_dir = Path(plugins_dir or context.PLUGINS_DIR)
    devices_file = context.DEVICES_FILE
    if devices_file.exists():
        return None

    legacy = _legacy_device_lists(plugins_dir)
    if not legacy:
        return None

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_dir = context.DATA_DIR / f"migration-backup-{stamp}"
    backup_dir.mkdir(parents=True, exist_ok=True)
    for _, plugin_dir, _ in legacy:
        shutil.copy2(plugin_dir / "routers.json", backup_dir / f"{plugin_dir.name}.routers.json")
    if context.STATE_FILE.exists():
        shutil.copy2(context.STATE_FILE, backup_dir / "state.json")

    devices: list[dict] = []
    id_by_old_key: dict[str, str] = {}
    keys: list[str] = []
    keys_failed: list[str] = []
    for device_type, plugin_dir, routers in legacy:
        for index, router in enumerate(routers):
            if not isinstance(router, dict):
                continue
            dev = {k: v for k, v in router.items() if k not in ("id", "type")}
            dev = {"id": new_id(id_by_old_key.values()), "type": device_type, **dev}
            devices.append(dev)
            id_by_old_key[f"{device_type}/{index}"] = dev["id"]
        keys += _copy_keys(device_type, plugin_dir, routers, keys_failed)

    state = load_json(context.STATE_FILE, {})
    new_state = {id_by_old_key.get(k, k): v for k, v in state.items()}

    save_devices(devices, devices_file)
    if state:
        save_json(context.STATE_FILE, new_state)

    summary = {
        "devices": len(devices),
        "keys": keys,
        "keys_failed": keys_failed,
        "state_entries": sum(1 for k in state if k in id_by_old_key),
        "backup_dir": str(backup_dir),
    }
    logger.info(
        "📦 Миграция устройств: %d → %s; ключей: %d (не скопировано: %s); "
        "записей состояния: %d; "
        "копия старых файлов: %s. Файлы plugins/*/routers.json и ключи в "
        "папках плагинов больше не используются — их можно удалить.",
        summary["devices"], devices_file, len(keys), keys_failed or "—",
        summary["state_entries"], backup_dir,
    )
    return summary
