# =========================================================
# core/storage/encryption.py — шифрование для внешних хранилищ
# =========================================================
#
# На хранилища, где включено шифрование, каждый файл уходит
# архивом 7z (AES-256, зашифрованы и данные, и имена внутри):
#
#     config.rsc  →  config.rsc.7z
#
# Открывается без этой программы — 7-Zip, PeaZip, WinRAR,
# `7z x файл.7z` в Linux/macOS — по паролю.
#
# Пароль один на все хранилища; хранится в config.json
# зашифрованным ключом программы (secret.key) — без него
# программа не смогла бы шифровать автоматически.
#
# =========================================================

from __future__ import annotations

from pathlib import Path

ARCHIVE_SUFFIX = ".7z"
PASSWORD_KEY = "storage_encryption_password"
MIN_PASSWORD_LENGTH = 8

README_NAME = "README_DECRYPT.txt"
README_TEXT = """\
Файлы в этой папке зашифрованы программой NetworkBackupManager.

Каждый файл — архив 7z (AES-256) с паролем, заданным в
программе: Настройки → Хранилища → Шифрование.

Как открыть без программы:
  • Windows: 7-Zip (https://www.7-zip.org) — двойной щелчок
    по файлу .7z, ввести пароль.
  • Linux / macOS: 7z x <файл>.7z   (пакет p7zip / 7zip)

Имя файла: <дата>_<время>_<версия>_<имя файла>.7z

Files in this folder are encrypted 7z archives (AES-256).
Open them with 7-Zip using the password set in the program.
"""


def is_available() -> bool:
    try:
        import py7zr  # noqa: F401
    except ImportError:
        return False
    return True


def archive_name(name: str) -> str:
    return name + ARCHIVE_SUFFIX


def is_archive_name(name: str) -> bool:
    return name.lower().endswith(ARCHIVE_SUFFIX)


def _storage():
    from core.secure_storage import get_secure_storage
    return get_secure_storage()


def get_password() -> str:
    """Пароль шифрования (открытый) или '' — не задан."""
    from core.storage import global_config as gc

    enc = gc.load_json(gc.CONFIG_FILE, {}).get(PASSWORD_KEY) or ""
    if not enc:
        return ""
    try:
        return _storage().decrypt(enc) or ""
    except Exception:
        return ""


def is_password_set() -> bool:
    return bool(get_password())


def set_password(password: str) -> None:
    from core.storage import global_config as gc

    with gc.CONFIG_LOCK:
        config = gc.load_json(gc.CONFIG_FILE, {})
        config[PASSWORD_KEY] = _storage().encrypt(password)
        gc.save_json(gc.CONFIG_FILE, config)


def validate_password(password: str, confirm: str) -> str | None:
    """Текст ошибки или None."""
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"пароль короче {MIN_PASSWORD_LENGTH} символов"
    if password != confirm:
        return "пароли не совпадают"
    return None


def make_archive(src: Path, dst: Path, arcname: str, password: str) -> None:
    """src → dst (7z, AES-256, зашифрованы и заголовки)."""
    import py7zr

    if not password:
        raise ValueError("пароль шифрования не задан")
    with py7zr.SevenZipFile(dst, "w", password=password, header_encryption=True) as z:
        z.write(src, arcname)


def read_archive(path: Path, password: str) -> dict[str, bytes]:
    """Содержимое архива {имя: данные} — для проверки пароля и тестов."""
    import tempfile

    import py7zr

    with tempfile.TemporaryDirectory(prefix="nbm_7z_") as tmp:
        with py7zr.SevenZipFile(path, "r", password=password) as z:
            z.extractall(path=tmp)
        root = Path(tmp)
        return {
            p.relative_to(root).as_posix(): p.read_bytes()
            for p in root.rglob("*") if p.is_file()
        }
