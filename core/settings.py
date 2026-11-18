# =========================================================
# core/settings.py — настройки запуска (окружение + config.json)
# =========================================================
#
# Переменные окружения важнее config.json — так программу
# настраивают в Docker и systemd, не правя файлы:
#
#   NBM_HOME             папка данных (core/context.py)
#   NBM_HOST             адрес веб-интерфейса (config.json: host)
#   NBM_PORT             порт (config.json: port)
#   NBM_OPEN_BROWSER     0/1 — открывать браузер при запуске
#   NBM_TRUSTED_PROXIES  адреса обратных прокси через запятую
#                        или * — им доверяются X-Forwarded-*
#   NBM_ADMIN_USER       логин первого пользователя (если
#   NBM_ADMIN_PASSWORD   data/auth.json ещё нет)
#   NBM_RCLONE           путь к rclone (config.json: rclone.binary_path)
#   NBM_CONTAINER        1 — запуск в контейнере: службой ОС
#                        управляет не программа (Docker и т.п.)
#
# =========================================================

from __future__ import annotations

import logging
import os
from pathlib import Path

from core import context

logger = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8080

_TRUE = {"1", "true", "yes", "on", "да"}
_FALSE = {"0", "false", "no", "off", "нет"}


def env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def env_bool(name: str, default: bool | None = None) -> bool | None:
    value = env(name).lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    return default


# =========================================================
# Веб-сервер
# =========================================================

def _valid_port(raw) -> int | None:
    try:
        port = int(raw)
    except (TypeError, ValueError):
        return None
    return port if 1 <= port <= 65535 else None


def web_config() -> tuple[str, int]:
    """(host, port): NBM_HOST/NBM_PORT, затем config.json, затем умолчания."""
    config = context.load_json(context.CONFIG_FILE, {})

    host = env("NBM_HOST") or config.get("host") or DEFAULT_HOST
    if not isinstance(host, str) or not host.strip():
        logger.warning("Некорректный host (%r), использую %s", host, DEFAULT_HOST)
        host = DEFAULT_HOST

    port = None
    for source, raw in (("NBM_PORT", env("NBM_PORT")), ("config.json", config.get("port"))):
        if raw in ("", None):
            continue
        port = _valid_port(raw)
        if port is None:
            logger.warning("Некорректный порт в %s: %r", source, raw)
            continue
        break

    return host.strip(), port or DEFAULT_PORT


def open_browser() -> bool:
    """Открывать ли браузер при запуске (не в контейнере и не если выключено)."""
    if in_container():
        return False
    flag = env_bool("NBM_OPEN_BROWSER")
    if flag is not None:
        return flag
    return bool(context.load_json(context.CONFIG_FILE, {}).get("open_browser", True))


# =========================================================
# Обратный прокси
# =========================================================

def trusted_proxies() -> str:
    """
    Значение для uvicorn forwarded_allow_ips и проверки CSRF:
    '' — никому не доверять, '*' — всем, иначе адреса через запятую.
    """
    return ",".join(p.strip() for p in env("NBM_TRUSTED_PROXIES").split(",") if p.strip())


def is_trusted_proxy(client_host: str | None) -> bool:
    proxies = trusted_proxies()
    if not proxies:
        return False
    if proxies == "*":
        return True
    return bool(client_host) and client_host in proxies.split(",")


# =========================================================
# Контейнер
# =========================================================

def in_container() -> bool:
    """
    Запуск в контейнере: службу ОС не устанавливаем и не
    останавливаем — жизненным циклом управляет Docker.
    """
    flag = env_bool("NBM_CONTAINER")
    if flag is not None:
        return flag
    return Path("/.dockerenv").exists()


# =========================================================
# Первый пользователь из окружения
# =========================================================

def admin_from_env() -> tuple[str, str] | None:
    user, password = env("NBM_ADMIN_USER") or "admin", os.environ.get("NBM_ADMIN_PASSWORD") or ""
    if not password:
        return None
    return user, password
