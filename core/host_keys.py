# =========================================================
# core/host_keys.py
# =========================================================
#
# Проверка ключей хоста SSH по принципу TOFU
# (trust on first use):
#
#   • первое подключение к устройству — ключ запоминается;
#   • ключ совпал — подключаемся;
#   • ключ ИЗМЕНИЛСЯ — отказ (BadHostKeyException).
#
# Раньше плагины принимали любой ключ (AutoAddPolicy,
# StrictHostKeyChecking=no): подменённый сервер получил бы
# бэкап и подключение молча.
#
# Если ключ сменился законно (переустановка роутера) —
# удалите строку устройства из data/known_hosts
# (paramiko) и/или data/known_hosts_ssh (ssh.exe, ROS6).
#
# Два файла, а не один: ssh.exe и paramiko пишут в них
# независимо и параллельно — общий файл они бы портили.
#
# =========================================================

from __future__ import annotations

import logging
import threading
from pathlib import Path

import paramiko

from core.context import DATA_DIR

logger = logging.getLogger(__name__)

# paramiko (SSHClient / Transport)
KNOWN_HOSTS_FILE: Path = DATA_DIR / "known_hosts"

# системный ssh.exe / scp.exe (MikroTik ROS6)
SSH_EXE_KNOWN_HOSTS_FILE: Path = DATA_DIR / "known_hosts_ssh"

_lock = threading.Lock()


def _host_entry(hostname: str, port: int | str = 22) -> str:
    """Имя записи как в OpenSSH: 'host' или '[host]:port'."""
    try:
        port = int(port)
    except (TypeError, ValueError):
        port = 22
    if port == 22:
        return hostname
    return f"[{hostname}]:{port}"


def _load(path: Path) -> paramiko.HostKeys:
    keys = paramiko.HostKeys()
    if path.exists():
        keys.load(str(path))
    return keys


def check_and_remember(
    hostname: str,
    port: int | str,
    key: paramiko.PKey,
) -> None:
    """
    Сверяет ключ хоста с запомненным.

    Хост ещё не известен — запоминает.
    Запись есть и не совпадает — BadHostKeyException. Ключ
    другого типа, чем запомненный, — тоже отказ (как в
    paramiko.SSHClient): иначе подменённый сервер выдал бы
    себя за устройство, предъявив ключ ECDSA вместо RSA.
    """
    entry = _host_entry(hostname, port)
    path = KNOWN_HOSTS_FILE

    with _lock:
        keys = _load(path)
        known = keys.lookup(entry)
        expected = known.get(key.get_name()) if known else None
        if expected is None and known:
            expected = next(iter(known.values()))

        if expected is not None:
            if expected == key:
                return
            logger.error(
                "🔐 Ключ хоста %s ИЗМЕНИЛСЯ (%s). Подключение "
                "отклонено. Если ключ сменился законно — "
                "удалите строку %s из %s.",
                entry, key.get_name(), entry, path,
            )
            raise paramiko.BadHostKeyException(entry, key, expected)

        keys.add(entry, key.get_name(), key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        keys.save(str(tmp))
        tmp.replace(path)

        logger.info(
            "🔐 Запомнен ключ хоста %s (%s, %s)",
            entry, key.get_name(), key.get_fingerprint().hex(),
        )


class TofuPolicy(paramiko.MissingHostKeyPolicy):
    """
    Политика для SSHClient: незнакомый ключ — запомнить.

    Изменённый ключ ловит сам paramiko (он сверяет с ключами,
    загруженными через prepare_client) и бросает
    BadHostKeyException ещё до вызова политики. На случай
    гонки (другой поток запомнил ключ после загрузки)
    check_and_remember сверяет повторно.
    """

    def __init__(self, port: int | str = 22):
        self.port = port

    def missing_host_key(self, client, hostname, key):
        # hostname уже может быть в виде '[host]:port'
        if hostname.startswith("["):
            host, _, port = hostname[1:].partition("]:")
            check_and_remember(host, port or 22, key)
        else:
            check_and_remember(hostname, self.port, key)


def prepare_client(client: paramiko.SSHClient, port: int | str = 22) -> None:
    """
    Настраивает SSHClient: запомненные ключи + TOFU.

    Вызывать вместо set_missing_host_key_policy(AutoAddPolicy()).
    """
    with _lock:
        if KNOWN_HOSTS_FILE.exists():
            # load() у HostKeys клиента не назначает файл для
            # автосохранения — пишет только check_and_remember.
            client.get_host_keys().load(str(KNOWN_HOSTS_FILE))
    client.set_missing_host_key_policy(TofuPolicy(port))


def prefer_known_key_types(
    transport: paramiko.Transport,
    hostname: str,
    port: int | str = 22,
) -> None:
    """
    До start_client: просить у сервера ключ хоста того типа,
    что уже запомнен (как делает SSHClient). Иначе после
    смены порядка алгоритмов в paramiko исправное устройство
    предъявило бы ключ другого типа и было бы отклонено.
    """
    with _lock:
        known = _load(KNOWN_HOSTS_FILE).lookup(_host_entry(hostname, port))
    if not known:
        return
    options = transport.get_security_options()
    available = list(options.key_types)
    # rsa-sha2-* — тот же ключ ssh-rsa, другая подпись
    first = [
        t for t in available
        if t in known or (t.startswith("rsa-sha2-") and "ssh-rsa" in known)
    ]
    if first:
        options.key_types = first + [t for t in available if t not in first]


def verify_transport(
    transport: paramiko.Transport,
    hostname: str,
    port: int | str = 22,
) -> None:
    """Проверка ключа для «голого» Transport (после start_client)."""
    check_and_remember(hostname, port, transport.get_remote_server_key())


def ssh_exe_options() -> list[str]:
    """
    Опции для системного ssh.exe / scp.exe: запомнить новый
    ключ, отказать при изменённом.
    """
    SSH_EXE_KNOWN_HOSTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    path = SSH_EXE_KNOWN_HOSTS_FILE.as_posix()
    return [
        "-oStrictHostKeyChecking=accept-new",
        # Кавычки: значение UserKnownHostsFile разбивается
        # по пробелам на несколько файлов.
        f'-oUserKnownHostsFile="{path}"',
    ]


# Подстроки в выводе ssh.exe при изменённом/непринятом ключе
SSH_EXE_HOST_KEY_MARKERS = (
    "remote host identification has changed",
    "host key verification failed",
    "host key for",            # "Host key for X has changed ..."
)


def is_host_key_error(exc: BaseException) -> bool:
    if isinstance(exc, paramiko.BadHostKeyException):
        return True
    text = str(exc).lower()
    return any(m in text for m in SSH_EXE_HOST_KEY_MARKERS)
