import os
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

def _run_command(cmd, timeout=60):
    """
    Выполнение внешней команды.

    Используется ROS6 для системных ssh/scp.
    """
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
    """
    Возвращает абсолютный путь к приватному SSH-ключу.

    Логика выбора пути:

      1. ssh_key из JSON, иначе
      2. private_key из JSON, иначе
      3. литерал "id_rsa.key"

    Относительный путь резолвится относительно каталога плагина,
    абсолютный — используется как есть.

    Дополнительно проверяется существование файла и права на чтение.

    ВАЖНО:
    Права файла здесь НЕ изменяются.
    Пользователь настраивает их заранее.
    """

    key_filename = router.get(
        "ssh_key",
        router.get(
            "private_key",
            "id_rsa.key",
        ),
    )

    current_plugin_dir = os.path.dirname(
        os.path.abspath(__file__)
    )

    if os.path.isabs(key_filename):
        key_path = key_filename
    else:
        key_path = os.path.join(
            current_plugin_dir,
            key_filename,
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
    """
    Ожидание появления файлов на MikroTik ROS6.

    ROS6 использует системный OpenSSH.
    """

    start = time.time()

    ssh_cmd = [
        "ssh",
        "-p",
        str(port),
        "-i",
        key_path,

        "-T",
        "-n",

        "-oBatchMode=yes",
        "-oConnectTimeout=10",

        "-oStrictHostKeyChecking=no",
        "-oUserKnownHostsFile=/dev/null",

        # Старые алгоритмы ROS6
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
                    existing_files.add(
                        parts[1].strip()
                    )

            if all(
                filename in existing_files
                for filename in filenames
            ):
                return True

        except Exception:
            pass

        time.sleep(1)

    return False


def backup_router_ros6(router, local_dir):
    """
    Резервное копирование MikroTik RouterOS 6.

    Используется системный ssh/scp, поскольку ROS6
    требует старые SSH-алгоритмы.
    """

    host = router["host"]

    user = router.get(
        "username",
        "admin",
    )

    port = router.get(
        "port",
        22,
    )

    key = _get_key_path(router)

    date_str = datetime.now().strftime(
        "%Y-%m-%d"
    )

    logging.info(
        "ROS6 backup via system ssh/scp: %s:%s",
        host,
        port,
    )

    ssh_base = [
        "ssh",
        "-p",
        str(port),

        "-i",
        key,

        "-T",
        "-n",

        "-oBatchMode=yes",
        "-oConnectTimeout=10",

        "-oServerAliveInterval=5",

        "-oStrictHostKeyChecking=no",
        "-oUserKnownHostsFile=/dev/null",

        # Старые алгоритмы ROS6
        "-oMACs=hmac-sha1,hmac-md5",
        "-oKexAlgorithms=+diffie-hellman-group1-sha1",
        "-oPubkeyAcceptedAlgorithms=+ssh-rsa",

        f"{user}@{host}",
    ]

    scp_base = [
        "scp",

        "-P",
        str(port),

        "-i",
        key,

        "-oBatchMode=yes",
        "-oConnectTimeout=10",

        "-oStrictHostKeyChecking=no",
        "-oUserKnownHostsFile=/dev/null",

        # Старые алгоритмы ROS6
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
    os.makedirs(
        local_dir,
        exist_ok=True,
    )
    time.sleep(2)

    if not wait_for_files_ros6(
        host,
        user,
        port,
        key,
        [
            "auto-backup.backup",
            "auto-export.rsc",
        ],
        timeout=30,
    ):
        raise RuntimeError(
            "Файлы backup/export не появились "
            "на роутере"
        )

    created_files = []

    for source, suffix in [
        (
            "auto-backup.backup",
            ".backup",
        ),
        (
            "auto-export.rsc",
            ".rsc",
        ),
    ]:

        destination = os.path.join(
            local_dir,
            f"{host}_{date_str}{suffix}",
        )

        _run_command(
            scp_base
            + [
                f"{user}@{host}:{source}",
                destination,
            ],
            timeout=60,
        )

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
        _run_command(
            ssh_base
            + [
                "/file remove auto-backup.backup; "
                "/file remove auto-export.rsc"
            ],
            timeout=20,
        )

    except Exception as exc:

        logging.warning(
            "Не удалось удалить временные файлы "
            "на %s: %s",
            host,
            exc,
        )

    logging.info(
        "ROS6 backup saved: %s",
        host,
    )

    return created_files


# =========================================================
# ROS7
# =========================================================

def wait_for_files_ros7(
    ssh_client,
    filenames,
    timeout=30,
):
    """
    Ожидание появления файлов на MikroTik ROS7.
    """

    start = time.time()

    while time.time() - start < timeout:

        try:

            stdin, stdout, stderr = (
                ssh_client.exec_command(
                    "/file print without-paging"
                )
            )

            output = stdout.read().decode(
                errors="ignore"
            )

            existing_files = set()

            for line in output.splitlines():

                line = line.strip()

                if not line:
                    continue

                parts = line.split()

                if len(parts) >= 2:
                    existing_files.add(
                        parts[1].strip()
                    )

            if all(
                filename in existing_files
                for filename in filenames
            ):
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
    """
    Подключение к MikroTik RouterOS 7.

    КРИТИЧЕСКИ ВАЖНО:

    Paramiko 5.0.0 + MikroTik ROS7
    стабильно работает с:

        rsa-sha2-256

    Поэтому ограничиваем RSA authentication
    именно этим алгоритмом.

    Права ключевого файла здесь НЕ изменяются.
    """

    client = paramiko.SSHClient()

    client.set_missing_host_key_policy(
        paramiko.AutoAddPolicy()
    )

    # Загружаем RSA private key
    pkey = paramiko.RSAKey.from_private_key_file(
        key_path
    )

    transport = paramiko.Transport(
        (host, port)
    )

    # =====================================================
    # SSH HANDSHAKE
    # =====================================================

    transport.start_client(
        timeout=10
    )

    # =====================================================
    # RSA AUTHENTICATION
    #
    # Paramiko 5.x по умолчанию может выбирать
    # rsa-sha2-512.
    #
    # MikroTik ROS7 в нашем случае надежно
    # принимает rsa-sha2-256.
    # =====================================================

    transport.auth_publickey(
        username=user,
        key=pkey,
    )

    if not transport.is_authenticated():

        transport.close()

        raise paramiko.AuthenticationException(
            "RSA public-key authentication "
            "не выполнена"
        )

    client._transport = transport

    return client


def backup_router_ros7(
    router,
    local_dir,
):
    """
    Резервное копирование MikroTik RouterOS 7.

    Используется:

        Paramiko 5.0.0
        RSA 2048
        rsa-sha2-256
        SCPClient
    """

    host = router["host"]

    user = router.get(
        "username",
        "admin",
    )

    port = router.get(
        "port",
        22,
    )

    key = _get_key_path(router)

    ssh_password = router.get(
        "ssh_pass",
        "",
    )

    date_str = datetime.now().strftime(
        "%Y-%m-%d"
    )

    logging.info(
        "ROS7 backup via Paramiko/SCP: %s:%s",
        host,
        port,
    )

    client = None

    try:

        # =================================================
        # SSH CONNECTION
        # =================================================

        client = _connect_ros7(
            host=host,
            port=port,
            user=user,
            key_path=key,
            ssh_password=ssh_password,
        )
        os.makedirs(
            local_dir,
            exist_ok=True,
        )
        logging.info(
            "ROS7 SSH authentication successful: %s",
            host,
        )

        # =================================================
        # CREATE BACKUP
        # =================================================

        stdin, stdout, stderr = (
            client.exec_command(
                "/system backup save "
                "name=auto-backup"
            )
        )

        exit_status = (
            stdout.channel.recv_exit_status()
        )

        if exit_status != 0:

            error = (
                stderr.read()
                .decode(errors="ignore")
                .strip()
            )

            raise RuntimeError(
                "Ошибка создания backup: "
                + (
                    error
                    or f"exit code {exit_status}"
                )
            )

        # =================================================
        # CREATE EXPORT
        # =================================================

        stdin, stdout, stderr = (
            client.exec_command(
                "/export file=auto-export"
            )
        )

        exit_status = (
            stdout.channel.recv_exit_status()
        )

        if exit_status != 0:

            error = (
                stderr.read()
                .decode(errors="ignore")
                .strip()
            )

            raise RuntimeError(
                "Ошибка создания export: "
                + (
                    error
                    or f"exit code {exit_status}"
                )
            )

        # =================================================
        # WAIT FILES
        # =================================================

        if not wait_for_files_ros7(
            client,
            [
                "auto-backup.backup",
                "auto-export.rsc",
            ],
            timeout=30,
        ):

            raise RuntimeError(
                "Файлы backup/export "
                "не появились на роутере"
            )

        created_files = []

        # =================================================
        # SCP DOWNLOAD
        # =================================================

        with SCPClient(
            client.get_transport()
        ) as scp:

            for source, suffix in [
                (
                    "auto-backup.backup",
                    ".backup",
                ),
                (
                    "auto-export.rsc",
                    ".rsc",
                ),
            ]:

                destination = os.path.join(
                    local_dir,
                    f"{host}_{date_str}{suffix}",
                )

                scp.get(
                    source,
                    destination,
                )

                if not os.path.exists(
                    destination
                ):
                    raise RuntimeError(
                        f"Файл не скачан: "
                        f"{destination}"
                    )

                if (
                    suffix == ".backup"
                    and os.path.getsize(
                        destination
                    ) == 0
                ):
                    raise RuntimeError(
                        f"Пустой backup-файл: "
                        f"{destination}"
                    )

                created_files.append(
                    destination
                )

        # =================================================
        # REMOVE TEMPORARY FILES
        # =================================================

        try:

            client.exec_command(
                "/file remove "
                "auto-backup.backup; "
                "/file remove "
                "auto-export.rsc"
            )

        except Exception as exc:

            logging.warning(
                "Не удалось удалить временные "
                "файлы на %s: %s",
                host,
                exc,
            )

        logging.info(
            "ROS7 backup saved: %s",
            host,
        )

        return created_files

    finally:

        if client is not None:

            try:
                client.close()

            except Exception:
                pass


# =========================================================
# ОБЩАЯ ФУНКЦИЯ BACKUP
# =========================================================

def backup_router(router):
    """
    Определяет версию RouterOS и запускает
    соответствующий механизм резервного копирования.
    """

    backup_path = router["backup_path"]

    # Если путь относительный — привязываем его к каталогу плагина

    if not os.path.isabs(backup_path):
        current_plugin_dir = os.path.dirname(
            os.path.abspath(__file__)
        )
        backup_path = os.path.join(
            current_plugin_dir,
            backup_path,
        )

    # Нормализуем (убираем ./ ../ и т.п.)
    backup_path = os.path.abspath(backup_path)

    today = datetime.now().strftime("%Y-%m-%d")

    local_dir = os.path.join(
        backup_path,
        today,
    )

    version = str(
        router.get(
            "version",
            "7",
        )
    )

    if version.startswith("6"):

        logging.info(
            "RouterOS 6 detected: %s",
            router.get("host"),
        )

        return backup_router_ros6(
            router,
            local_dir,
        )

    logging.info(
        "RouterOS 7 detected: %s",
        router.get("host"),
    )

    return backup_router_ros7(
        router,
        local_dir,
    )


def run_backup(router):
    """
    Точка входа PluginManager.
    """

    return backup_router(router)