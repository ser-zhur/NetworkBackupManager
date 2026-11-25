# =========================================================
# plugins/Fortigate/plugin.py — Fortinet FortiGate (FortiOS)
# =========================================================
#
# Скачивает файл конфигурации sys_config по SCP — тот же, что
# даёт «Backup» в веб-интерфейсе FortiGate и что принимает
# «Restore». Аналог команды:
#
#   scp -O admin@fortigate:sys_config fortigate.conf
#
# На FortiGate должно быть включено admin-scp (шаги setup).
#
# =========================================================

from __future__ import annotations

import logging
import os
import re
import socket
from pathlib import Path

import paramiko
from scp import SCPClient, SCPException

from core import ssh
from core.plugin_api import PluginError

REMOTE_FILE = "sys_config"
LOCAL_FILE = "fortigate.conf"
HEADER = b"#config-version="
DEFAULT_READ_TIMEOUT = 60.0

# Для сравнения «изменилось или нет»:
#  • #conf_file_ver= — счётчик сохранений конфигурации;
#  • ENC <шифротекст> — FortiOS шифрует пароли со случайной
#    солью, и шифротекст может меняться без изменения пароля.
_CONF_VER_RE = re.compile(rb"^#conf_file_ver=.*$", re.M)
_ENC_RE = re.compile(rb"\bENC \S+")


def _read_timeout(device: dict) -> float:
    try:
        value = float(device.get("read_timeout") or DEFAULT_READ_TIMEOUT)
    except (TypeError, ValueError):
        raise PluginError(
            f"Ожидание файла — не число: {device.get('read_timeout')!r}"
        ) from None
    return value if value > 0 else DEFAULT_READ_TIMEOUT


def _explain_scp_error(exc: Exception) -> str:
    text = str(exc).strip() or exc.__class__.__name__
    low = text.lower()
    if "permission" in low or "denied" in low:
        return (
            f"FortiGate отказал в скачивании {REMOTE_FILE} ({text}). "
            "Проверьте, что включено admin-scp (config system global → "
            "set admin-scp enable) и у пользователя профиль super_admin."
        )
    if "no such file" in low:
        return f"На FortiGate нет файла {REMOTE_FILE} ({text})"
    return f"Не удалось скачать {REMOTE_FILE} по SCP: {text}"


def check_config(data: bytes) -> None:
    """Файл — конфигурация FortiOS, а не сообщение об ошибке."""
    if not data.strip():
        raise PluginError(f"FortiGate вернул пустой {REMOTE_FILE}")
    if not data.lstrip().startswith(HEADER):
        first = data.lstrip().split(b"\n", 1)[0][:120].decode("utf-8", "replace")
        raise PluginError(
            f"Скачанный {REMOTE_FILE} не похож на конфигурацию FortiOS "
            f"(нет строки {HEADER.decode()}…, начало: {first!r})"
        )


def backup_fortigate(device: dict) -> list[str]:
    backup_dir = Path(device["backup_path"])
    backup_dir.mkdir(parents=True, exist_ok=True)
    local_file = backup_dir / LOCAL_FILE
    # Скачиваем во временный файл: оборванная загрузка не
    # затирает прежнюю конфигурацию.
    part_file = local_file.with_name(local_file.name + ".part")
    read_timeout = _read_timeout(device)

    client = ssh.connect(device, default_username=PLUGIN["default_username"])
    try:
        try:
            with SCPClient(client.get_transport(), socket_timeout=read_timeout) as scp:
                scp.get(REMOTE_FILE, str(part_file))
        except SCPException as exc:
            raise PluginError(_explain_scp_error(exc)) from None
        except (socket.timeout, TimeoutError):
            raise PluginError(
                f"FortiGate не передал {REMOTE_FILE} за {read_timeout:g} с — "
                "увеличьте «Ожидание файла»"
            ) from None
        except (paramiko.SSHException, OSError) as exc:
            raise PluginError(f"Сбой SCP при скачивании {REMOTE_FILE}: {exc}") from None

        check_config(part_file.read_bytes())
        os.replace(part_file, local_file)
    finally:
        client.close()
        try:
            part_file.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            logging.warning("FortiGate: не удалось удалить %s: %s", part_file, exc)

    path = str(local_file.resolve())
    logging.info("✅ Бэкап FortiGate сохранён: %s", path)
    return [path]


def normalize_for_compare(filename: str, data: bytes) -> bytes:
    """Убрать счётчик сохранений и шифротексты паролей (только для сравнения)."""
    if filename != LOCAL_FILE:
        return data
    data = _CONF_VER_RE.sub(b"", data)
    return _ENC_RE.sub(b"ENC *", data)


# Описание для ядра (контракт v1, core/plugin_api.py)
PLUGIN = {
    "api": 1,
    "title": "Fortinet FortiGate",
    "description": (
        "Файл конфигурации sys_config по SCP — тот же, что «Backup» в "
        "веб-интерфейсе FortiGate. Под super_admin — полная копия для «Restore»."
    ),
    "auth": ["password", "key"],
    "default_username": "nbm-backup",
    "fields": [
        {
            "name": "read_timeout", "label": "Ожидание файла, с", "type": "number",
            "placeholder": "60",
            "help": "Сколько ждать данных при скачивании конфигурации.",
        },
    ],
    "setup": [
        "Ключ (необязательно, можно входить по паролю): нажмите «🔑 Создать "
        "пару ключей» под полем «Файл SSH-ключа» и скопируйте открытый ключ — "
        "он понадобится в шаге 3.",
        "CLI FortiGate (консоль в веб-интерфейсе или SSH): разрешите "
        "скачивание по SCP — `config system global`, `set admin-scp enable`, "
        "`end`.",
        "Создайте администратора для бэкапа: `config system admin`, "
        "`edit nbm-backup`, `set accprofile super_admin` (почему не "
        "ограниченный профиль — в шаге 4), `set password <пароль>`, для "
        "входа по ключу — `set ssh-public-key1 \"ssh-rsa AAAA…\"` (строка "
        "из шага 1; если FortiGate её не примет, уберите комментарий в конце), "
        "`next`, `end`.",
        "Профиль влияет на содержимое копии. С ограниченным профилем "
        "(не `super_admin`) FortiGate отдаёт только то, что этому профилю "
        "видно: в `config system admin` будет лишь сам пользователь бэкапа, "
        "в `config system accprofile` — лишь его профиль; учётной записи "
        "`admin` и профилей `super_admin`, `prof_admin` в файле не будет. "
        "Для истории изменений («было/стало») такие копии подходят, но "
        "восстановление из них удалит `admin` — войти можно будет только "
        "пользователем бэкапа. Если используете ограниченный профиль, "
        "периодически делайте полную копию под `super_admin` (например, "
        "«Backup» в веб-интерфейсе FortiGate).",
        "Рекомендуется ограничить вход адресом программы: в том же `edit "
        "nbm-backup` — `set trusthost1 <адрес программы> 255.255.255.255`. "
        "На интерфейсе, к которому подключается программа, должен быть "
        "разрешён SSH (`set allowaccess` … `ssh`).",
        "В этой форме: пользователь `nbm-backup`, пароль или имя файла ключа. "
        "После сохранения запустите бэкап устройства, чтобы проверить "
        "подключение. В файле — пароли FortiGate в виде ENC; храните бэкапы "
        "так же бережно, как само устройство.",
    ],
}


def run_backup(device):
    """Точка входа PluginManager: список путей к файлам; ошибка — исключение."""
    return backup_fortigate(device)
