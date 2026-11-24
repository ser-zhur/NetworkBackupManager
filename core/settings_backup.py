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
# =========================================================

from __future__ import annotations

import fnmatch
import json
import shutil
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

from core import context
from core.version import __version__

MANIFEST_NAME = "NBM_SETTINGS.json"
RESTORE_NAME = "RESTORE.txt"
REMOTE_FOLDER = "_nbm_settings"
MIN_PASSWORD_LENGTH = 8

# Не настройки: журнал событий, копии перед миграциями,
# временные файлы. Сравнение — по имени файла или папки.
_EXCLUDE = (
    "events.jsonl*",
    "migration-backup-*",
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

Восстановление:
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
        "program": "Network Backup Manager",
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
