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

import json
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
        # Пароль — через stdin ("-"), а не аргументом: аргументы
        # процесса видны другим пользователям (диспетчер задач).
        result = subprocess.run(
            [binary, "obscure", "-"],
            input=password,
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
# ОПИСАНИЕ ТИПОВ ХРАНИЛИЩ (rclone config providers)
# =========================================================
#
# rclone сам описывает все поддерживаемые хранилища:
# параметры, подсказки, обязательность, пароли, варианты.
# По этому описанию строится форма в веб-интерфейсе — без
# ручного кода под каждый тип. Кэш — по пути и времени
# изменения бинарника (обновили rclone → перечитаем).

_providers_cache: dict = {}


def providers() -> list[dict]:
    """
    Список типов хранилищ rclone ([] — rclone недоступен).

    Каждый: {"Name": "webdav", "Description": ...,
             "Options": [{"Name", "Help", "Required",
                          "IsPassword", "Advanced", ...}]}
    """
    binary = resolve_binary()
    if binary is None:
        return []

    try:
        key = (binary, os.path.getmtime(binary))
    except OSError:
        key = (binary, 0)
    if key in _providers_cache:
        return _providers_cache[key]

    try:
        result = subprocess.run(
            [binary, "config", "providers"],
            capture_output=True,
            text=True,
            timeout=30,
            encoding="utf-8",
            errors="replace",
            env=_isolated_env(),
        )
        data = json.loads(result.stdout or "[]")
    except Exception as exc:
        logger.error("rclone config providers: %s", exc)
        return []

    if not isinstance(data, list):
        return []

    _providers_cache.clear()
    _providers_cache[key] = data
    return data


def provider(type_name: str) -> dict | None:
    """Описание одного типа хранилища (или None)."""
    for p in providers():
        if p.get("Name") == type_name:
            return p
    return None


# =========================================================
# ХРАНИЛИЩЕ ИЗ ПЕРЕМЕННЫХ ОКРУЖЕНИЯ
# =========================================================
#
# rclone берёт удалённое хранилище из переменных:
#     RCLONE_CONFIG_NBM_TYPE=webdav
#     RCLONE_CONFIG_NBM_URL=https://...
#     RCLONE_CONFIG_NBM_PASS=<obscured>
# и адресуется как "NBM:путь". Файл конфига не нужен —
# пароль не попадает на диск, пользовательский rclone.conf
# не затрагивается (RCLONE_CONFIG → пустое устройство).

REMOTE_NAME = "NBM"


def _isolated_env() -> dict:
    env = os.environ.copy()
    # Убираем чужие RCLONE_CONFIG_* (например, из окружения
    # пользователя), чтобы они не подмешались к нашему.
    for k in list(env):
        if k.upper().startswith("RCLONE_CONFIG_"):
            del env[k]
    env["RCLONE_CONFIG"] = _empty_config_path()
    return env


def is_oauth_type(type_name: str) -> bool:
    """Тип с входом через браузер (OAuth): у него есть параметр token."""
    info = provider(type_name) or {}
    return any(o.get("Name") == "token" for o in info.get("Options", []))


def write_remote_config(
    path: Path,
    type_name: str,
    options: dict,
    password_options: set[str] | frozenset[str] = frozenset(),
) -> None:
    """
    Файл конфига rclone с одним хранилищем REMOTE_NAME.

    Нужен OAuth-хранилищам: rclone обновляет токен и
    записывает новый в конфиг — из переменных окружения его
    не сохранить (а у Microsoft старый токен со временем
    перестаёт действовать). Пароли — obscured, как ждёт rclone.
    """
    lines = [f"[{REMOTE_NAME}]", f"type = {type_name}"]
    for name, value in (options or {}).items():
        if value is None or value == "":
            continue
        if isinstance(value, bool):
            value = "true" if value else "false"
        value = str(value)
        if "\n" in value or "\r" in value:
            raise ValueError(f"параметр «{name}» содержит перевод строки")
        if name in password_options:
            obscured = obscure(value)
            if obscured is None:
                raise RuntimeError(f"rclone не смог зашифровать параметр «{name}»")
            value = obscured
        lines.append(f"{name} = {value}")

    path = Path(path)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def read_remote_section(path: Path, name: str = REMOTE_NAME) -> dict:
    """Параметры хранилища из файла конфига rclone ({} — нет)."""
    import configparser

    parser = configparser.RawConfigParser(strict=False)
    try:
        parser.read(str(path), encoding="utf-8")
    except configparser.Error as exc:
        logger.warning("rclone: не удалось прочитать %s: %s", path, exc)
        return {}
    if not parser.has_section(name):
        return {}
    return dict(parser.items(name))


def config_env(config_path: Path) -> dict:
    """Окружение с указанным файлом конфига (без RCLONE_CONFIG_*)."""
    env = _isolated_env()
    env["RCLONE_CONFIG"] = str(config_path)
    return env


def _empty_config_path() -> str:
    """
    Пустой файл конфига rclone: пользовательский rclone.conf
    не читается, предупреждения «config not found» нет.

    Не os.devnull: на Windows rclone отвергает «nul» как
    зарезервированное имя устройства.
    """
    path = Path(tempfile.gettempdir()) / "nbm_rclone_empty.conf"
    try:
        if not path.exists() or path.stat().st_size:
            path.write_text("", encoding="utf-8")
    except OSError as exc:
        logger.warning("rclone: пустой конфиг %s: %s", path, exc)
    return str(path)


def remote_env(
    type_name: str,
    options: dict,
    password_options: set[str] | frozenset[str] = frozenset(),
) -> dict:
    """
    Окружение для запуска rclone с хранилищем REMOTE_NAME.

    options — параметры в открытом виде (пароли уже
    расшифрованы нашим ключом). Параметры из
    password_options rclone требует «obscured» — делаем
    здесь. Пустые значения не передаются (берётся умолчание
    rclone).

    RuntimeError — если не удалось obscure пароль.
    """
    env = _isolated_env()
    prefix = f"RCLONE_CONFIG_{REMOTE_NAME}_"
    env[prefix + "TYPE"] = type_name

    for name, value in (options or {}).items():
        if value is None or value == "":
            continue
        if isinstance(value, bool):
            value = "true" if value else "false"
        value = str(value)
        if name in password_options:
            obscured = obscure(value)
            if obscured is None:
                raise RuntimeError(
                    f"rclone не смог зашифровать параметр «{name}»"
                )
            value = obscured
        env[prefix + name.upper()] = value

    return env


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
            "в настройках (/settings?tab=storage)."
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