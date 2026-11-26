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
#   NBM_TLS_CERT         сертификат PEM — включает HTTPS (config.json:
#   NBM_TLS_KEY          tls_cert/tls_key; путь от папки data/ или
#                        абсолютный; ключ — если он в отдельном файле)
#   NBM_ADMIN_USER       логин первого пользователя (если
#   NBM_ADMIN_PASSWORD   data/auth.json ещё нет)
#   NBM_RCLONE           путь к rclone (config.json: rclone.binary_path)
#   NBM_API_TOKEN        токен мониторинга для /api/status (наравне
#                        с созданным в Настройки → Мониторинг)
#   NBM_CONTAINER        1 — запуск в контейнере: службой ОС
#                        управляет не программа (Docker и т.п.)
#   NBM_UPDATE_CHECK     0/1 — проверять новые версии (важнее
#                        настройки; core/update_check.py)
#   NBM_MANAGED_BY       systemd — служба установлена install.sh:
#                        остановка и удаление — через systemctl
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
# HTTPS
# =========================================================

class TlsConfigError(Exception):
    """Сертификат задан, но использовать его нельзя."""


def _data_path(raw: str) -> Path:
    """Относительный путь — от папки data/."""
    path = Path(raw).expanduser()
    return path if path.is_absolute() else context.DATA_DIR / path


def tls_files() -> tuple[Path, Path | None] | None:
    """
    (сертификат, ключ) для HTTPS или None — работать по HTTP.

    NBM_TLS_CERT/NBM_TLS_KEY, затем config.json tls_cert/tls_key.
    Ключ можно не указывать, если он в одном файле с
    сертификатом. Файлы проверяются: при ошибке TlsConfigError —
    молча перейти на HTTP нельзя, пароль ушёл бы открытым.
    """
    import ssl

    config = context.load_json(context.CONFIG_FILE, {})
    cert_raw = env("NBM_TLS_CERT") or str(config.get("tls_cert") or "").strip()
    key_raw = env("NBM_TLS_KEY") or str(config.get("tls_key") or "").strip()
    if not cert_raw:
        if key_raw:
            raise TlsConfigError("Задан ключ (NBM_TLS_KEY / tls_key), но не задан сертификат (NBM_TLS_CERT / tls_cert)")
        return None

    cert = _data_path(cert_raw)
    key = _data_path(key_raw) if key_raw else None
    for path in (cert, key):
        if path is not None and not path.is_file():
            raise TlsConfigError(f"Файл не найден: {path}")

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    try:
        # Пароль-заглушка: ключ с паролем не поддерживается, а
        # без неё OpenSSL спросил бы пароль в консоли
        ctx.load_cert_chain(cert, key, password=lambda: b"")
    except (ssl.SSLError, OSError, ValueError) as exc:
        hint = "" if key else " Если ключ в отдельном файле, укажите его в NBM_TLS_KEY / tls_key."
        raise TlsConfigError(
            f"Не удалось загрузить сертификат {cert}"
            + (f" и ключ {key}" if key else "")
            + f": {exc}. Нужны PEM-файлы, ключ без пароля.{hint}"
        ) from exc
    return cert, key


def cert_expires(cert: Path):
    """Дата окончания сертификата (datetime, UTC) или None."""
    import ssl
    from datetime import datetime, timezone

    try:
        info = ssl._ssl._test_decode_cert(str(cert))      # без сторонних библиотек
        return datetime.fromtimestamp(ssl.cert_time_to_seconds(info["notAfter"]), timezone.utc)
    except Exception:
        return None


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


def service_manager() -> str:
    """
    Кто управляет запуском программы:
      "docker"  — контейнер;
      "systemd" — служба, установленная install.sh (программа
                  работает не от root и не может остановить
                  или удалить свою службу);
      ""        — сама программа (служба Windows, --install-service).
    """
    if in_container():
        return "docker"
    if env("NBM_MANAGED_BY").lower() == "systemd":
        return "systemd"
    return ""


# =========================================================
# Первый пользователь из окружения
# =========================================================

def admin_from_env() -> tuple[str, str] | None:
    user, password = env("NBM_ADMIN_USER") or "admin", os.environ.get("NBM_ADMIN_PASSWORD") or ""
    if not password:
        return None
    return user, password
