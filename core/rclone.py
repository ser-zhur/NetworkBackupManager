# =========================================================
# core/rclone.py
# =========================================================
#
# Обёртка над бинарником rclone для storage-backend'ов.
#
# Единая точка настройки пути к бинарнику и запуска
# rclone-команд. Backend'ы (WebDAV, в будущем S3, SMB,
# Dropbox, Google Drive, OneDrive, FTPS) не работают
# с subprocess напрямую — только через этот модуль.
#
# Настройки хранятся в config.json:
#     {
#         "rclone": {
#             "binary_path": "C:\\Tools\\rclone.exe"
#         }
#     }
#
# =========================================================

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from core.context import (
    CONFIG_FILE,
    load_json,
    save_json,
)


logger = logging.getLogger(__name__)


DEFAULT_BINARY = "rclone"


# =========================================================
# НАСТРОЙКИ
# =========================================================

def get_rclone_path() -> str:
    """
    Настроенный путь к бинарнику rclone.

    Пустая строка = "rclone" (поиск в PATH).
    """
    try:
        config = load_json(CONFIG_FILE, {})
        path = (config.get("rclone") or {}).get("binary_path", "")
        return (path or "").strip() or DEFAULT_BINARY
    except Exception as exc:
        logger.warning(
            "rclone: не удалось прочитать конфиг: %s", exc
        )
        return DEFAULT_BINARY


def set_rclone_path(path: str) -> None:
    config = load_json(CONFIG_FILE, {})
    if not isinstance(config, dict):
        config = {}
    if "rclone" not in config or not isinstance(config["rclone"], dict):
        config["rclone"] = {}
    config["rclone"]["binary_path"] = (path or "").strip()
    save_json(CONFIG_FILE, config)


# =========================================================
# ПОИСК БИНАРНИКА
# =========================================================

def resolve_binary() -> str | None:
    """Абсолютный путь к rclone или None, если не найден."""
    path = get_rclone_path()
    if not path:
        return None

    # Содержит разделитель — трактуем как файл
    if "/" in path or "\\" in path or Path(path).is_absolute():
        p = Path(path)
        return str(p) if p.is_file() else None

    return shutil.which(path)


def is_available() -> bool:
    return resolve_binary() is not None


# =========================================================
# ВЕРСИЯ
# =========================================================

def get_version() -> str:
    """Первая строка `rclone version` или ''."""
    binary = resolve_binary()
    if binary is None:
        return ""

    try:
        result = subprocess.run(
            [binary, "version"],
            capture_output=True,
            text=True,
            timeout=10,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode != 0:
            return ""
        lines = (result.stdout or "").splitlines()
        return lines[0].strip() if lines else ""
    except Exception as exc:
        logger.error("rclone version: %s", exc)
        return ""


# =========================================================
# OBSCURE
# =========================================================

def obscure(password: str) -> str | None:
    """
    Шифрует пароль для rclone-конфига.

    rclone не принимает плейн-пароли в конфиге —
    только obscured-строки.
    """
    if not password:
        return ""

    binary = resolve_binary()
    if binary is None:
        logger.error(
            "rclone: бинарник не найден (%s)",
            get_rclone_path(),
        )
        return None

    try:
        result = subprocess.run(
            [binary, "obscure", password],
            capture_output=True,
            text=True,
            timeout=10,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode != 0:
            logger.error(
                "rclone obscure: %s",
                (result.stderr or "").strip(),
            )
            return None
        return result.stdout.strip()
    except Exception as exc:
        logger.error("rclone obscure: %s", exc)
        return None


# =========================================================
# ВРЕМЕННЫЙ КОНФИГ
# =========================================================

def build_temp_config(
    remote_name: str,
    remote_config: dict,
) -> tuple[dict, Path]:
    """
    Создаёт временный rclone.conf с единственным remote.

    Возвращает (env, config_path). env — копия os.environ
    с RCLONE_CONFIG, указывающим на файл.

    Вызывающий обязан удалить config_path после работы.
    Пользовательский rclone.conf не затрагивается.
    """
    lines = [f"[{remote_name}]"]
    for key, value in remote_config.items():
        # rclone config: "key = value". \n в значениях
        # не встречается (url/user/obscured-pass безопасны).
        lines.append(f"{key} = {value}")

    fd, path = tempfile.mkstemp(
        suffix=".conf",
        prefix="nbm_rclone_",
        text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except Exception:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise

    env = os.environ.copy()
    env["RCLONE_CONFIG"] = path

    return env, Path(path)


# =========================================================
# RUN
# =========================================================

def run(
    args: list[str],
    *,
    env: dict | None = None,
    timeout: int = 120,
) -> subprocess.CompletedProcess:
    """
    Запускает rclone с аргументами.

    RuntimeError — если бинарник не найден.
    Возвращает CompletedProcess без интерпретации rc.
    """
    binary = resolve_binary()
    if binary is None:
        raise RuntimeError(
            "rclone не найден. Укажите путь к бинарнику "
            "в настройках хранилища (/storage-settings)."
        )

    return subprocess.run(
        [binary, *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        encoding="utf-8",
        errors="replace",
        env=env,
    )