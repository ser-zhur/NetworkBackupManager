# =========================================================
# core/settings_backup.py — резервная копия настроек программы
# =========================================================
#
# Архив папки data/: устройства, пароли (зашифрованные) и
# ключ шифрования secret.key, SSH-ключи, ключи хостов,
# хранилища, расписание, уведомления, пользователь входа.
# Бэкапы устройств (backups/) и журналы сюда не входят.
#
#     data/...                  как в папке данных
#     NBM_SETTINGS.json         версия программы, время, список файлов
#     RESTORE.txt               как восстановить
#
# С паролем — 7z (AES-256, зашифрованы и имена файлов),
# без пароля — zip. Внутри secret.key и закрытые SSH-ключи,
# поэтому на хранилища копия уходит только зашифрованной.
#
# Здесь же:
#   • автоматическая копия на хранилища по расписанию
#     (data/settings_backup.json) с удалением старых копий;
#   • восстановление из архива: проверка и распаковка во
#     временную папку, затем замена файлов data/ (прежние
#     сохраняются в data/restore-backup-<дата>/).
#
# =========================================================

from __future__ import annotations

import fnmatch
import json
import logging
import os
import re
import shutil
import tempfile
import threading
import zipfile
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath

from core import context, events
from core.version import __version__

logger = logging.getLogger(__name__)

PROGRAM = "Network Backup Manager"
MANIFEST_NAME = "NBM_SETTINGS.json"
RESTORE_NAME = "RESTORE.txt"
REMOTE_FOLDER = "_nbm_settings"
MIN_PASSWORD_LENGTH = 8
MAX_RESTORE_BYTES = 50 * 1024 * 1024        # архив и распакованное

# Имя копии: nbm-settings-20261124-110849.7z — по нему же
# находятся старые копии на хранилище (чужие файлы не трогаем).
ARCHIVE_RE = re.compile(r"^nbm-settings-\d{8}-\d{6}\.(7z|zip)$")

# Не настройки: журнал событий, копии перед миграциями и
# восстановлением, временные файлы. Сравнение — по имени
# файла или папки.
_EXCLUDE = (
    "events.jsonl*",
    "migration-backup-*",
    "restore-backup-*",
    "*.bak-*",
    "*.tmp",
    ".health",
    "__pycache__",
)

RESTORE_TEXT = """\
Резервная копия настроек Network Backup Manager.

Внутри — папка data/ программы: устройства, их пароли
(зашифрованы ключом data/secret.key, он тоже здесь),
SSH-ключи, хранилища, расписание, уведомления, пользователь
входа. Бэкапов устройств (папка backups/) здесь нет.

Восстановление в веб-интерфейсе:
  Настройки → Копия настроек → Восстановить из архива.

Восстановление вручную:
  1. Остановите программу (службу, контейнер).
  2. Сохраните в стороне текущую папку data/, если она есть.
  3. Распакуйте архив в папку программы (Docker — в nbm/),
     чтобы data/ из архива заменила прежнюю.
  4. Запустите программу.

Храните копию как пароль: secret.key из неё расшифровывает
все пароли устройств и хранилищ.

Settings backup of Network Backup Manager: extract it into
the program folder so that data/ is replaced, then start it.
"""


class SettingsBackupError(Exception):
    """Понятная пользователю ошибка."""


# =========================================================
# Создание архива
# =========================================================

def _excluded(name: str) -> bool:
    return any(fnmatch.fnmatch(name, p) for p in _EXCLUDE)


def collect_files(data_dir: Path | None = None) -> list[Path]:
    """Файлы настроек — пути относительно data/, по порядку."""
    root = Path(data_dir or context.DATA_DIR)
    if not root.is_dir():
        return []
    found: list[Path] = []
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if any(_excluded(part) for part in rel.parts):
            continue
        if path.is_file() and not path.is_symlink():
            found.append(rel)
    return found


def archive_name(encrypted: bool, now: datetime | None = None) -> str:
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    return f"nbm-settings-{stamp}.{'7z' if encrypted else 'zip'}"


def validate_password(password: str, confirm: str) -> str | None:
    """Текст ошибки или None. Пустой пароль — без шифрования."""
    if not password and not confirm:
        return None
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"пароль короче {MIN_PASSWORD_LENGTH} символов"
    if password != confirm:
        return "пароли не совпадают"
    return None


def _stage(tmp: Path, data_dir: Path) -> list[str]:
    """Копия data/ и служебные файлы во временной папке → имена файлов."""
    files = collect_files(data_dir)
    if not files:
        raise SettingsBackupError("папка данных пуста — нечего сохранять")
    for rel in files:
        dst = tmp / "data" / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(data_dir / rel, dst)
        except OSError as exc:
            raise SettingsBackupError(f"не удалось прочитать {rel.as_posix()}: {exc}") from exc

    names = [f"data/{rel.as_posix()}" for rel in files]
    manifest = {
        "program": PROGRAM,
        "version": __version__,
        "created": datetime.now().astimezone().isoformat(timespec="seconds"),
        "files": names,
    }
    (tmp / MANIFEST_NAME).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    (tmp / RESTORE_NAME).write_text(RESTORE_TEXT, encoding="utf-8", newline="\r\n")
    return [MANIFEST_NAME, RESTORE_NAME, *names]


def build(dst_dir: Path, password: str = "", data_dir: Path | None = None) -> Path:
    """
    Архив настроек в dst_dir. С паролем — 7z AES-256,
    без пароля — zip. → путь к архиву.
    """
    data_dir = Path(data_dir or context.DATA_DIR)
    target = Path(dst_dir) / archive_name(bool(password))

    with tempfile.TemporaryDirectory(prefix="nbm_settings_") as tmp_name:
        tmp = Path(tmp_name)
        names = _stage(tmp, data_dir)
        if password:
            try:
                import py7zr
            except ImportError as exc:
                raise SettingsBackupError(
                    "не установлена библиотека py7zr — архив с паролем недоступен"
                ) from exc
            with py7zr.SevenZipFile(target, "w", password=password,
                                    header_encryption=True) as z:
                for name in names:
                    z.write(tmp / name, name)
        else:
            with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
                for name in names:
                    z.write(tmp / name, name)
    return target


# =========================================================
# Выгрузка на хранилища
# =========================================================

def remote_dir(base_dir: str) -> str:
    """
    Папка копий на хранилище: <базовая папка>/_nbm_settings.
    Подстановки устройства ({host}, {name}, {type}) к копии
    настроек не относятся — базовая папка берётся до них.
    """
    parts = []
    for part in str(base_dir or "").replace("\\", "/").split("/"):
        if "{" in part:
            break
        if part:
            parts.append(part)
    return "/" + "/".join([*parts, REMOTE_FOLDER])


def resolve_targets(ids, backends: list, storages: dict) -> tuple[list, str]:
    """
    Хранилища по id → ([(backend, cfg), ...], текст ошибки).
    Годятся только настроенные rclone-хранилища.
    """
    from core.storage import global_config

    by_id = {b.id: b for b in backends if hasattr(b, "put_file")}
    targets = []
    for rid in dict.fromkeys(str(i) for i in ids or []):
        backend = by_id.get(rid)
        if backend is None:
            return [], f"хранилище «{rid}» не найдено"
        if not global_config.is_configured(backend, storages):
            return [], f"хранилище «{backend.name}» не настроено"
        targets.append((backend, storages.get(rid) or {}))
    if not targets:
        return [], "не выбрано ни одного хранилища"
    return targets, ""


def upload(targets: list, password: str, keep: int = 0) -> tuple[str, list[dict]]:
    """
    Одна копия (7z с паролем) на все хранилища. keep > 0 —
    после загрузки оставить на хранилище столько последних
    копий. → (имя файла, [{id, name, ok, message}]).
    """
    if not password:
        raise SettingsBackupError(
            "не задан пароль шифрования хранилищ (Настройки → Хранилища → Шифрование)"
        )
    with tempfile.TemporaryDirectory(prefix="nbm_settings_up_") as tmp:
        path = build(Path(tmp), password)
        results = []
        for backend, cfg in targets:
            directory = remote_dir(cfg.get("base_dir") or "")
            r = backend.put_file(cfg, path, directory)
            message = r.message
            if r.ok and keep > 0:
                pruned = backend.prune_files(cfg, directory, ARCHIVE_RE, keep)
                if not pruned.ok:
                    message += f"; старые копии не удалены: {pruned.message}"
                elif (pruned.details or {}).get("deleted"):
                    message += f"; удалено старых копий: {pruned.details['deleted']}"
            results.append({"id": backend.id, "name": backend.name,
                            "ok": r.ok, "message": message})
            (logger.info if r.ok else logger.error)(
                "💾 Копия настроек → %s: %s", backend.name, message,
            )
        return path.name, results


# =========================================================
# Автоматическая копия по расписанию
# =========================================================

AUTO_DEFAULTS = {
    "enabled": False,
    "every_days": 1,
    "time": "04:00",
    "storages": [],
    "keep": 10,
}
_STATE_KEYS = ("last_run", "last_ok", "last_message")
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_auto_lock = threading.Lock()          # чтение-запись файла
_auto_running = threading.Lock()       # идёт копия


def _auto_file() -> Path:
    return context.DATA_DIR / "settings_backup.json"


def load_auto() -> dict:
    """Настройки и итог последнего запуска, с умолчаниями."""
    raw = context.load_json(_auto_file(), {})
    if not isinstance(raw, dict):
        raw = {}
    cfg = dict(AUTO_DEFAULTS)
    cfg["enabled"] = bool(raw.get("enabled"))
    try:
        cfg["every_days"] = min(max(int(raw.get("every_days", 1)), 1), 90)
    except (TypeError, ValueError):
        pass
    if isinstance(raw.get("time"), str) and _TIME_RE.match(raw["time"]):
        cfg["time"] = raw["time"]
    if isinstance(raw.get("storages"), list):
        cfg["storages"] = [str(s) for s in raw["storages"]]
    try:
        cfg["keep"] = min(max(int(raw.get("keep", 10)), 0), 1000)
    except (TypeError, ValueError):
        pass
    for key in _STATE_KEYS:
        cfg[key] = raw.get(key)
    return cfg


def validate_auto(form: dict) -> tuple[dict, str]:
    """Данные формы → (настройки, текст ошибки)."""
    cfg = {
        "enabled": bool(form.get("enabled")),
        "time": str(form.get("time") or "").strip(),
        "storages": [str(s) for s in form.get("storages") or []],
    }
    if not _TIME_RE.match(cfg["time"]):
        return cfg, "время — в виде ЧЧ:ММ, например 04:00"
    try:
        cfg["every_days"] = int(form.get("every_days"))
        cfg["keep"] = int(form.get("keep"))
    except (TypeError, ValueError):
        return cfg, "периодичность и число копий — целые числа"
    if not 1 <= cfg["every_days"] <= 90:
        return cfg, "периодичность — от 1 до 90 дней"
    if not 0 <= cfg["keep"] <= 1000:
        return cfg, "число хранимых копий — от 0 до 1000"
    if cfg["enabled"] and not cfg["storages"]:
        return cfg, "выберите хотя бы одно хранилище"
    return cfg, ""


def save_auto(cfg: dict) -> None:
    """Сохранить настройки, не трогая итог последнего запуска."""
    with _auto_lock:
        current = load_auto()
        current.update({k: cfg[k] for k in AUTO_DEFAULTS if k in cfg})
        context.save_json(_auto_file(), current)


def _save_result(ok: bool, message: str, at: datetime) -> None:
    with _auto_lock:
        current = load_auto()
        current.update(
            last_run=at.strftime("%Y-%m-%d %H:%M:%S"),
            last_ok=ok, last_message=message,
        )
        context.save_json(_auto_file(), current)


def next_run(cfg: dict, now: datetime | None = None) -> datetime | None:
    """
    Когда сделать следующую копию; None — выключено.
    Пропущенная (программа была выключена) делается сразу.
    """
    if not cfg.get("enabled") or not cfg.get("storages"):
        return None
    now = now or datetime.now()
    at = datetime.strptime(cfg.get("time") or "04:00", "%H:%M").time()
    last = None
    try:
        if cfg.get("last_run"):
            last = datetime.strptime(cfg["last_run"], "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        last = None
    if last is None:
        today = datetime.combine(now.date(), at)
        return today if now < today else now
    return datetime.combine(last.date() + timedelta(days=cfg.get("every_days", 1)), at)


def is_running() -> bool:
    return _auto_running.locked()


def tick(backends_provider, now: datetime | None = None) -> bool:
    """
    Вызывается планировщиком каждые ~30 с. Пора — копия в
    отдельном потоке (выгрузка в облако может идти минутами).
    → True, если копия запущена.
    """
    cfg = load_auto()
    when = next_run(cfg, now)
    if when is None or (now or datetime.now()) < when:
        return False
    if not _auto_running.acquire(blocking=False):
        return False
    threading.Thread(
        target=_run_auto, args=(cfg, backends_provider),
        daemon=True, name="SettingsBackup",
    ).start()
    return True


def _run_auto(cfg: dict, backends_provider) -> None:
    from core.storage import encryption, global_config

    started = datetime.now()
    try:
        logger.info("💾 Плановая копия настроек")
        targets, error = resolve_targets(
            cfg["storages"], backends_provider(), global_config.load_global(),
        )
        if error:
            raise SettingsBackupError(error)
        name, results = upload(targets, encryption.get_password(), cfg.get("keep", 0))
        failed = [r for r in results if not r["ok"]]
        if failed:
            raise SettingsBackupError(
                "; ".join(f"{r['name']}: {r['message']}" for r in failed)
            )
        _save_result(True, f"{name}: выгружено ({len(results)})", started)
    except Exception as exc:
        # last_run записывается и при ошибке: повтор — в
        # следующий срок, а не каждые 30 секунд.
        logger.error("💾 Плановая копия настроек не удалась: %s", exc)
        _save_result(False, str(exc), started)
        events.emit(events.SCHEDULER_ERROR, f"Копия настроек не выгружена: {exc}",
                    level="error")
    finally:
        _auto_running.release()


# =========================================================
# Восстановление
# =========================================================

def _check_name(name: str) -> str:
    """Имя файла в архиве — безопасное и наше; иначе ошибка."""
    clean = name.rstrip("/")
    path = PurePosixPath(clean)
    if (
        not clean or "\\" in clean or ":" in clean or path.is_absolute()
        or any(part in ("", ".", "..") for part in path.parts)
        or not (clean in (MANIFEST_NAME, RESTORE_NAME) or path.parts[0] == "data")
    ):
        raise SettingsBackupError(f"посторонний файл в архиве: {name!r}")
    return clean


def _is_7z(path: Path) -> bool:
    with open(path, "rb") as fh:
        return fh.read(6) == b"7z\xbc\xaf\x27\x1c"


def _extract(archive: Path, password: str, dst: Path) -> None:
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as z:
            infos = z.infolist()
            if sum(i.file_size for i in infos) > MAX_RESTORE_BYTES:
                raise SettingsBackupError("архив слишком большой для копии настроек")
            for info in infos:
                name = _check_name(info.filename)
                if (info.external_attr >> 16) & 0o170000 == 0o120000:
                    raise SettingsBackupError(f"ссылка в архиве: {name}")
                if info.is_dir():
                    continue
                if info.flag_bits & 0x1:
                    raise SettingsBackupError("zip с паролем не поддерживается — используйте 7z")
                target = dst / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(z.read(info))
        return

    if not _is_7z(archive):
        raise SettingsBackupError("это не архив zip или 7z")
    try:
        import py7zr
    except ImportError as exc:
        raise SettingsBackupError("не установлена библиотека py7zr — 7z не открыть") from exc
    try:
        with py7zr.SevenZipFile(archive, "r", password=password or None) as z:
            entries = z.list()
            if sum(e.uncompressed or 0 for e in entries) > MAX_RESTORE_BYTES:
                raise SettingsBackupError("архив слишком большой для копии настроек")
            for e in entries:
                _check_name(e.filename)
                if getattr(e, "is_symlink", False):
                    raise SettingsBackupError(f"ссылка в архиве: {e.filename}")
            z.extractall(path=dst)
    except SettingsBackupError:
        raise
    except Exception as exc:
        raise SettingsBackupError(
            "архив не открылся: неверный пароль или файл повреждён"
            if password else
            "архив не открылся: нужен пароль или файл повреждён"
        ) from exc
    for p in dst.rglob("*"):
        if p.is_symlink():
            raise SettingsBackupError(f"ссылка в архиве: {p.relative_to(dst).as_posix()}")


def _count_devices(data: Path) -> int:
    try:
        raw = json.loads((data / "devices.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    if isinstance(raw, dict):
        raw = raw.get("devices", [])
    return len(raw) if isinstance(raw, list) else 0


def stage_restore(archive: Path, password: str, dst: Path) -> dict:
    """
    Проверить архив и распаковать в dst (пустая папка).
    → сведения для подтверждения: версия, дата, число файлов
    и устройств, пользователь входа.
    """
    _extract(Path(archive), password, Path(dst))

    try:
        manifest = json.loads((dst / MANIFEST_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise SettingsBackupError(
            "в архиве нет описания копии (NBM_SETTINGS.json) — это не копия настроек программы"
        ) from None
    if not isinstance(manifest, dict) or manifest.get("program") != PROGRAM:
        raise SettingsBackupError("это не копия настроек Network Backup Manager")

    data = dst / "data"
    files = collect_files(data)
    if not files:
        raise SettingsBackupError("в архиве нет файлов настроек")

    user = ""
    try:
        user = str(json.loads((data / "auth.json").read_text(encoding="utf-8")).get("username") or "")
    except (OSError, ValueError, AttributeError):
        pass

    version = str(manifest.get("version") or "")
    newer = False
    try:
        newer = int(version.split(".")[0]) > int(__version__.split(".")[0])
    except ValueError:
        pass

    return {
        "version": version or "?",
        "newer": newer,
        "created": str(manifest.get("created") or "?"),
        "files": len(files),
        "devices": _count_devices(data),
        "user": user,
        "has_secret": (data / "secret.key").is_file(),
    }


def apply_restore(staged: Path, data_dir: Path | None = None) -> Path:
    """
    Заменить настройки в data/ файлами из staged/data.
    Текущие настройки копируются в data/restore-backup-<дата>/;
    файлы настроек, которых нет в копии, удаляются. Журнал
    событий и прежние копии остаются. → папка с прежними.
    """
    data_dir = Path(data_dir or context.DATA_DIR)
    src = Path(staged) / "data"
    new_files = collect_files(src)
    if not new_files:
        raise SettingsBackupError("в архиве нет файлов настроек")

    old_files = collect_files(data_dir)
    saved = data_dir / f"restore-backup-{datetime.now():%Y%m%d-%H%M%S}"
    for rel in old_files:
        dst = saved / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(data_dir / rel, dst)

    keep = set(new_files)
    for rel in old_files:
        if rel not in keep:
            (data_dir / rel).unlink(missing_ok=True)

    for rel in new_files:
        dst = data_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(dst.name + ".restore.tmp")
        shutil.copyfile(src / rel, tmp)
        if os.name != "nt":
            os.chmod(tmp, 0o600)
        os.replace(tmp, dst)

    logger.warning(
        "💾 Настройки восстановлены из копии (%d файлов); прежние — в %s",
        len(new_files), saved,
    )
    return saved
