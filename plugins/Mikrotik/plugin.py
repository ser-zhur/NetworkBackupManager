import os
import sys
import time
import subprocess
import logging

import paramiko
from datetime import datetime
from scp import SCPClient


# =========================================================
# Windows
# =========================================================

CREATE_NO_WINDOW = (
    subprocess.CREATE_NO_WINDOW
    if sys.platform == "win32"
    else 0
)


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
    Ожидание появления файлов на ROS6
    через системный SSH CLI.
    """

    start = time.time()

    ssh_cmd = [
        "ssh",
        "-p",
        str(port),
        "-i",
        key_path,

        "-oBatchMode=yes",
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
                creationflags=CREATE_NO_WINDOW,
            )

            output = result.stdout

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
                f in existing_files
                for f in filenames
            ):
                return True

        except Exception:
            pass

        time.sleep(1)

    return False


# =========================================================
# ROS7
# =========================================================

def wait_for_files(
    ssh_client,
    filenames,
    timeout=30,
):
    """
    Ожидание появления файлов на ROS7
    через Paramiko.
    """

    start = time.time()

    while time.time() - start < timeout:

        try:

            stdin, stdout, stderr = (
                ssh_client.exec_command(
                    "/file print without-paging"
                )
            )

            output = (
                stdout.read()
                .decode(errors="ignore")
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
                f in existing_files
                for f in filenames
            ):
                return True

        except Exception:
            pass

        time.sleep(1)

    return False


# =========================================================
# ROS6 backup
# =========================================================

def backup_router_ros6(
    router,
    local_dir,
):

    host = router["host"]

    user = router.get(
        "username",
        "admin",
    )

    port = router.get(
        "port",
        22,
    )

    key_filename = router.get(
        "private_key",
        "id_rsa.key",
    )

    current_plugin_dir = os.path.dirname(
        __file__
    )

    key = (
        key_filename
        if os.path.isabs(key_filename)
        else os.path.join(
            current_plugin_dir,
            key_filename,
        )
    )

    # ВАЖНО:
    #
    # Права на ключ здесь больше НЕ меняются.
    #
    # Они подготавливаются установщиком
    # при установке службы.
    #

    if not os.path.exists(key):

        raise Exception(
            f"Приватный SSH-ключ не найден: {key}"
        )

    date_str = datetime.now().strftime(
        "%Y-%m-%d"
    )

    # -----------------------------------------------------
    # SSH
    # -----------------------------------------------------

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

        "-oMACs=hmac-sha1,hmac-md5",

        "-oKexAlgorithms=+diffie-hellman-group1-sha1",

        "-oPubkeyAcceptedAlgorithms=+ssh-rsa",

        f"{user}@{host}",
    ]

    # -----------------------------------------------------
    # SCP
    # -----------------------------------------------------

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

        "-oMACs=hmac-sha1,hmac-md5",

        "-oKexAlgorithms=+diffie-hellman-group1-sha1",

        "-oPubkeyAcceptedAlgorithms=+ssh-rsa",
    ]

    # -----------------------------------------------------
    # Выполнение команды
    # -----------------------------------------------------

    def run_cmd(
        cmd,
        timeout=60,
    ):

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW,
        )

        if result.returncode != 0:

            raise Exception(
                result.stderr.strip()
                or
                "Команда завершилась с ошибкой"
            )

        return result.stdout

    logging.info(
        "🔐 ROS6 backup via system ssh/scp: "
        f"{host}:{port}"
    )

    # -----------------------------------------------------
    # Создание backup + export
    # -----------------------------------------------------

    combined_cmd = (
        "/system backup save "
        "name=auto-backup "
        "dont-encrypt=yes; "
        "/export file=auto-export"
    )

    run_cmd(
        ssh_base + [combined_cmd],
        timeout=90,
    )

    time.sleep(2)

    # -----------------------------------------------------
    # Проверяем наличие файлов
    # -----------------------------------------------------

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

        raise Exception(
            "Файлы backup/export "
            "не появились на роутере"
        )

    created_files = []

    # -----------------------------------------------------
    # Скачиваем файлы
    # -----------------------------------------------------

    for src, suffix in [
        (
            "auto-backup.backup",
            ".backup",
        ),
        (
            "auto-export.rsc",
            ".rsc",
        ),
    ]:

        dest = os.path.join(
            local_dir,
            f"{host}_{date_str}{suffix}",
        )

        run_cmd(
            scp_base
            + [
                f"{user}@{host}:{src}",
                dest,
            ],
            timeout=60,
        )

        if not os.path.exists(dest):

            raise Exception(
                f"Файл не скачан: {dest}"
            )

        if (
            suffix == ".backup"
            and
            os.path.getsize(dest) == 0
        ):

            raise Exception(
                f"Пустой backup-файл: {dest}"
            )

        created_files.append(dest)

    # -----------------------------------------------------
    # Удаляем временные файлы
    # -----------------------------------------------------

    try:

        run_cmd(
            ssh_base
            + [
                "/file remove "
                "auto-backup.backup; "
                "/file remove "
                "auto-export.rsc"
            ],
            timeout=20,
        )

    except Exception as e:

        logging.warning(
            "⚠ Не удалось удалить временные "
            f"файлы на {host}: {e}"
        )

    logging.info(
        f"📥 ROS6 backup saved: {host}"
    )

    return created_files


# =========================================================
# ROS7 backup
# =========================================================

def backup_router_ros7(
    router,
    local_dir,
):

    host = router["host"]

    user = router.get(
        "username",
        "admin",
    )

    port = router.get(
        "port",
        22,
    )

    key_filename = router.get(
        "private_key",
        "id_rsa.key",
    )

    ssh_password = router.get(
        "ssh_pass",
        "",
    )

    current_plugin_dir = os.path.dirname(
        __file__
    )

    key = (
        key_filename
        if os.path.isabs(key_filename)
        else os.path.join(
            current_plugin_dir,
            key_filename,
        )
    )

    if not os.path.exists(key):

        raise Exception(
            f"Приватный SSH-ключ не найден: {key}"
        )

    date_str = datetime.now().strftime(
        "%Y-%m-%d"
    )

    # -----------------------------------------------------
    # Paramiko
    # -----------------------------------------------------

    client = paramiko.SSHClient()

    client.set_missing_host_key_policy(
        paramiko.AutoAddPolicy()
    )

    pkey = (
        paramiko.RSAKey
        .from_private_key_file(key)
    )

    client.connect(
        host,
        port=port,
        username=user,
        pkey=pkey,
        password=(
            ssh_password
            if ssh_password
            else None
        ),
        timeout=10,
    )

    logging.info(
        "🔐 ROS7 backup via "
        f"Paramiko/SCP: {host}:{port}"
    )

    # -----------------------------------------------------
    # Создание backup
    # -----------------------------------------------------

    stdin, stdout, stderr = (
        client.exec_command(
            "/system backup save "
            "name=auto-backup"
        )
    )

    stdout.channel.recv_exit_status()

    # -----------------------------------------------------
    # Export
    # -----------------------------------------------------

    stdin, stdout, stderr = (
        client.exec_command(
            "/export file=auto-export"
        )
    )

    stdout.channel.recv_exit_status()

    # -----------------------------------------------------
    # Проверяем наличие файлов
    # -----------------------------------------------------

    if not wait_for_files(
        client,
        [
            "auto-backup.backup",
            "auto-export.rsc",
        ],
        timeout=30,
    ):

        raise Exception(
            "Файлы backup/export "
            "не появились на роутере"
        )

    created_files = []

    # -----------------------------------------------------
    # SCP
    # -----------------------------------------------------

    with SCPClient(
        client.get_transport()
    ) as scp:

        for src, suffix in [
            (
                "auto-backup.backup",
                ".backup",
            ),
            (
                "auto-export.rsc",
                ".rsc",
            ),
        ]:

            dest = os.path.join(
                local_dir,
                f"{host}_{date_str}{suffix}",
            )

            scp.get(
                src,
                dest,
            )

            if not os.path.exists(dest):

                raise Exception(
                    f"Файл не скачан: {dest}"
                )

            if (
                suffix == ".backup"
                and
                os.path.getsize(dest) == 0
            ):

                raise Exception(
                    f"Пустой backup-файл: {dest}"
                )

            created_files.append(dest)

    # -----------------------------------------------------
    # Удаляем временные файлы
    # -----------------------------------------------------

    client.exec_command(
        "/file remove auto-backup.backup; "
        "/file remove auto-export.rsc"
    )

    client.close()

    logging.info(
        f"📥 ROS7 backup saved: {host}"
    )

    return created_files


# =========================================================
# Общий backup
# =========================================================

def backup_router(router):

    backup_path = router[
        "backup_path"
    ]

    today = datetime.now().strftime(
        "%Y-%m-%d"
    )

    local_dir = os.path.join(
        backup_path,
        today,
    )

    os.makedirs(
        local_dir,
        exist_ok=True,
    )

    version = str(
        router.get(
            "version",
            "7",
        )
    )

    if version.startswith("6"):

        return backup_router_ros6(
            router,
            local_dir,
        )

    return backup_router_ros7(
        router,
        local_dir,
    )


# =========================================================
# Plugin entry point
# =========================================================

def run_backup(router):

    return backup_router(router)