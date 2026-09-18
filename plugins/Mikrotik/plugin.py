import os
import re
import sys
import time
import subprocess
import logging
from datetime import datetime

import paramiko
from scp import SCPClient


# =========================================================
# WINDOWS
# =========================================================

CREATE_NO_WINDOW = (
    subprocess.CREATE_NO_WINDOW
    if sys.platform == "win32"
    else 0
)


# =========================================================
# ОБЩИЕ ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# =========================================================

# =========================================================
# NORMALIZE FOR COMPARE
# =========================================================
#
# MikroTik в первую строку .rsc пишет дату экспорта.
# Форматы ROS 6 и 7 различаются:
#
#   ROS 6:  # sep/17/2026 12:45:17 by RouterOS 6.49.21
#   ROS 7:  # 2026-09-17 14:41:00 by RouterOS 7.24.2
#
# Эта строка меняется при каждом бэкапе, даже если конфиг
# не менялся. Нормализатор удаляет её — тогда сравнение
# в git-хранилище корректно определяет, есть ли реальные
# изменения. Сам файл в git сохраняется целиком, как
# скачан с роутера.
#
# =========================================================

_RSC_TIMESTAMP_RE = re.compile(
    rb"^#\s+"
    rb"\S+"                    # дата: "sep/17/2026" или "2026-09-17"
    rb"\s+"
    rb"\d{1,2}:\d{2}:\d{2}"    # время: "12:45:17" или "9:12:34"
    rb"\s+by\s+RouterOS\b"
)


def normalize_for_compare(filename: str, data: bytes) -> bytes:
    """
    Возвращает содержимое файла, приведённое к виду,
    пригодному для сравнения «изменилось или нет».

    Для .rsc удаляет первую строку, если она содержит
    timestamp экспорта RouterOS.

    Для остальных файлов возвращает данные без изменений.
    """
    if not filename.endswith(".rsc"):
        return data

    if not data:
        return data

    lines = data.splitlines(keepends=True)

    if lines and _RSC_TIMESTAMP_RE.match(lines[0]):
        return b"".join(lines[1:])

    return data

def _run_command(cmd, timeout=60):
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        stdin=subprocess.DEVNULL,
        creationflags=CREATE_NO_WINDOW,
    )

    if result.returncode != 0:
        error = (
            result.stderr.strip()
            or result.stdout.strip()
            or "Команда завершилась с ошибкой"
        )
        raise RuntimeError(error)

    return result.stdout


def _get_key_path(router):
    key_filename = router.get(
        "ssh_key",
        router.get("private_key", "id_rsa.key"),
    )

    current_plugin_dir = os.path.dirname(
        os.path.abspath(__file__)
    )

    if os.path.isabs(key_filename):
        key_path = key_filename
    else:
        key_path = os.path.join(
            current_plugin_dir, key_filename,
        )

    key_path = os.path.abspath(key_path)

    if not os.path.exists(key_path):
        raise FileNotFoundError(
            f"SSH-ключ не найден: {key_path}. "
            f"Проверьте параметр 'ssh_key' или "
            f"'private_key' в конфиге роутера "
            f"(host={router.get('host')})."
        )

    if not os.path.isfile(key_path):
        raise IsADirectoryError(
            f"Путь к SSH-ключу указывает на каталог, "
            f"а не на файл: {key_path}"
        )

    if not os.access(key_path, os.R_OK):
        raise PermissionError(
            f"Нет прав на чтение SSH-ключа: {key_path}. "
            f"Проверьте права файла (например, chmod 600)."
        )

    return key_path


# =========================================================
# ROS6
# =========================================================

def wait_for_files_ros6(
    host,
    user,
    port,
    key_path,
    filenames,
    timeout=30,
):
    start = time.time()

    ssh_cmd = [
        "ssh",
        "-p", str(port),
        "-i", key_path,
        "-T", "-n",
        "-oBatchMode=yes",
        "-oConnectTimeout=10",
        "-oStrictHostKeyChecking=no",
        "-oUserKnownHostsFile=/dev/null",
        "-oMACs=hmac-sha1,hmac-md5",
        "-oKexAlgorithms=+diffie-hellman-group1-sha1",
        "-oPubkeyAcceptedAlgorithms=+ssh-rsa",
        f"{user}@{host}",
        "/file print without-paging",
    ]

    while time.time() - start < timeout:
        try:
            result = subprocess.run(
                ssh_cmd,
                capture_output=True,
                text=True,
                timeout=10,
                stdin=subprocess.DEVNULL,
                creationflags=CREATE_NO_WINDOW,
            )

            output = result.stdout or ""
            existing_files = set()

            for line in output.splitlines():
                line = line.strip()
                if not line:
                    continue
                parts = line.split()
                if len(parts) >= 2:
                    existing_files.add(parts[1].strip())

            if all(fn in existing_files for fn in filenames):
                return True

        except Exception:
            pass

        time.sleep(1)

    return False


def backup_router_ros6(router, local_dir):
    host = router["host"]
    user = router.get("username", "admin")
    port = router.get("port", 22)
    key = _get_key_path(router)

    logging.info(
        "ROS6 backup via system ssh/scp: %s:%s",
        host, port,
    )

    ssh_base = [
        "ssh",
        "-p", str(port),
        "-i", key,
        "-T", "-n",
        "-oBatchMode=yes",
        "-oConnectTimeout=10",
        "-oServerAliveInterval=5",
        "-oStrictHostKeyChecking=no",
        "-oUserKnownHostsFile=/dev/null",
        "-oMACs=hmac-sha1,hmac-md5",
        "-oKexAlgorithms=+diffie-hellman-group1-sha1",
        "-oPubkeyAcceptedAlgorithms=+ssh-rsa",
        f"{user}@{host}",
    ]

    scp_base = [
        "scp",
        "-P", str(port),
        "-i", key,
        "-oBatchMode=yes",
        "-oConnectTimeout=10",
        "-oStrictHostKeyChecking=no",
        "-oUserKnownHostsFile=/dev/null",
        "-oMACs=hmac-sha1,hmac-md5",
        "-oKexAlgorithms=+diffie-hellman-group1-sha1",
        "-oPubkeyAcceptedAlgorithms=+ssh-rsa",
    ]

    combined_command = (
        "/system backup save "
        "name=auto-backup "
        "dont-encrypt=yes; "
        "/export file=auto-export"
    )

    _run_command(
        ssh_base + [combined_command],
        timeout=90,
    )

    os.makedirs(local_dir, exist_ok=True)
    time.sleep(2)

    if not wait_for_files_ros6(
        host, user, port, key,
        ["auto-backup.backup", "auto-export.rsc"],
        timeout=30,
    ):
        raise RuntimeError(
            "Файлы backup/export не появились на роутере"
        )

    created_files = []

    for source, suffix in [
        ("auto-backup.backup", ".backup"),
        ("auto-export.rsc", ".rsc"),
    ]:
        destination = os.path.join(
            local_dir,
            f"config{suffix}",
        )

        _run_command(
            scp_base + [
                f"{user}@{host}:{source}",
                destination,
            ],
            timeout=60,
        )

        if not os.path.exists(destination):
            raise RuntimeError(f"Файл не скачан: {destination}")

        if (
            suffix == ".backup"
            and os.path.getsize(destination) == 0
        ):
            raise RuntimeError(
                f"Пустой backup-файл: {destination}"
            )

        created_files.append(destination)

    try:
        _run_command(
            ssh_base + [
                "/file remove auto-backup.backup; "
                "/file remove auto-export.rsc"
            ],
            timeout=20,
        )
    except Exception as exc:
        logging.warning(
            "Не удалось удалить временные файлы на %s: %s",
            host, exc,
        )

    logging.info("ROS6 backup saved: %s", host)
    return created_files


# =========================================================
# ROS7
# =========================================================

def wait_for_files_ros7(
    ssh_client,
    filenames,
    timeout=30,
):
    start = time.time()

    while time.time() - start < timeout:
        try:
            stdin, stdout, stderr = ssh_client.exec_command(
                "/file print without-paging"
            )
            output = stdout.read().decode(errors="ignore")

            existing_files = set()
            for line in output.splitlines():
                line = line.strip()
                if not line:
                    continue
                parts = line.split()
                if len(parts) >= 2:
                    existing_files.add(parts[1].strip())

            if all(fn in existing_files for fn in filenames):
                return True

        except Exception:
            pass

        time.sleep(1)

    return False


def _connect_ros7(
    host,
    port,
    user,
    key_path,
    ssh_password="",
):
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(
        paramiko.AutoAddPolicy()
    )

    pkey = paramiko.RSAKey.from_private_key_file(key_path)

    transport = paramiko.Transport((host, port))
    transport.start_client(timeout=10)

    transport.auth_publickey(
        username=user,
        key=pkey,
    )

    if not transport.is_authenticated():
        transport.close()
        raise paramiko.AuthenticationException(
            "RSA public-key authentication не выполнена"
        )

    client._transport = transport
    return client


def backup_router_ros7(router, local_dir, client=None):
    host = router["host"]
    user = router.get("username", "admin")
    port = router.get("port", 22)
    key = _get_key_path(router)
    ssh_password = router.get("ssh_pass", "")

    logging.info(
        "ROS7 backup via Paramiko/SCP: %s:%s",
        host, port,
    )

    own_client = client is None

    try:
        if own_client:
            client = _connect_ros7(
                host=host,
                port=port,
                user=user,
                key_path=key,
                ssh_password=ssh_password,
            )
            logging.info(
                "ROS7 SSH authentication successful: %s",
                host,
            )

        os.makedirs(local_dir, exist_ok=True)

        stdin, stdout, stderr = client.exec_command(
            "/system backup save name=auto-backup"
        )
        exit_status = stdout.channel.recv_exit_status()

        if exit_status != 0:
            error = stderr.read().decode(errors="ignore").strip()
            raise RuntimeError(
                "Ошибка создания backup: "
                + (error or f"exit code {exit_status}")
            )

        stdin, stdout, stderr = client.exec_command(
            "/export file=auto-export"
        )
        exit_status = stdout.channel.recv_exit_status()

        if exit_status != 0:
            error = stderr.read().decode(errors="ignore").strip()
            raise RuntimeError(
                "Ошибка создания export: "
                + (error or f"exit code {exit_status}")
            )

        if not wait_for_files_ros7(
            client,
            ["auto-backup.backup", "auto-export.rsc"],
            timeout=30,
        ):
            raise RuntimeError(
                "Файлы backup/export не появились на роутере"
            )

        created_files = []

        with SCPClient(client.get_transport()) as scp:
            for source, suffix in [
                ("auto-backup.backup", ".backup"),
                ("auto-export.rsc", ".rsc"),
            ]:
                destination = os.path.join(
                    local_dir,
                    f"config{suffix}",
                )

                scp.get(source, destination)

                if not os.path.exists(destination):
                    raise RuntimeError(
                        f"Файл не скачан: {destination}"
                    )

                if (
                    suffix == ".backup"
                    and os.path.getsize(destination) == 0
                ):
                    raise RuntimeError(
                        f"Пустой backup-файл: {destination}"
                    )

                created_files.append(destination)

        try:
            client.exec_command(
                "/file remove auto-backup.backup; "
                "/file remove auto-export.rsc"
            )
        except Exception as exc:
            logging.warning(
                "Не удалось удалить временные файлы на %s: %s",
                host, exc,
            )

        logging.info("ROS7 backup saved: %s", host)
        return created_files

    finally:
        if own_client and client is not None:
            try:
                client.close()
            except Exception:
                pass


# =========================================================
# ОБЩАЯ ФУНКЦИЯ BACKUP
# =========================================================

def backup_router(router):
    """
    Резервное копирование MikroTik.

    Версия RouterOS определяется автоматически.
    Файлы пишутся плоско в backup_path:
    config.rsc, config.backup — ровно то, что скачано.
    """

    backup_path = router["backup_path"]

    if not os.path.isabs(backup_path):
        current_plugin_dir = os.path.dirname(
            os.path.abspath(__file__)
        )
        backup_path = os.path.join(
            current_plugin_dir, backup_path,
        )

    backup_path = os.path.abspath(backup_path)
    os.makedirs(backup_path, exist_ok=True)

    host = router.get("host", "unknown")

    # ---- Попытка ROS7 ----
    client = None
    try:
        client = _connect_ros7(
            host=host,
            port=router.get("port", 22),
            user=router.get("username", "admin"),
            key_path=_get_key_path(router),
            ssh_password=router.get("ssh_pass", ""),
        )
    except (paramiko.SSHException, OSError) as exc:
        logging.info(
            "ROS7 connect failed (%s: %s) — "
            "fallback to ROS6: %s",
            type(exc).__name__, exc, host,
        )

    if client is None:
        return backup_router_ros6(router, backup_path)

    logging.info("RouterOS 7 detected: %s", host)

    try:
        return backup_router_ros7(
            router, backup_path, client=client,
        )
    finally:
        try:
            client.close()
        except Exception:
            pass


def run_backup(router):
    """
    Точка входа PluginManager.

    Контракт с core.plugin_manager:
        • Возвращает list[str | Path] — пути к созданным файлам.
        • Пустой список [] — «нет изменений» (успешный бэкап,
          файлов для выгрузки нет).
        • Любая ошибка — исключение. Глотать ошибки и
          возвращать [] запрещено: это приведёт к тихо-зелёному
          статусу на сломанном устройстве.

    Текущая реализация скачивает файлы и возвращает их пути.
    Если в будущем появится логика «сравни с прошлым бэкапом
    и удали при совпадении» — она должна возвращать [].
    """
    return backup_router(router)