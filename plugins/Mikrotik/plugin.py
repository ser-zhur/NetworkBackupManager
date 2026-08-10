import os
import time
import subprocess
import logging
from datetime import datetime
import paramiko
from scp import SCPClient

# --- Ожидание файлов для ROS 6.x ---
def wait_for_files_ros6(ip, username, key, port, required_files, forbidden_files=None, timeout=20):
    if forbidden_files is None:
        forbidden_files = []

    ssh_base = [
        "ssh", "-i", key,
        "-oMACs=hmac-sha1,hmac-md5",
        "-oKexAlgorithms=+diffie-hellman-group1-sha1",
        "-oPubkeyAcceptedAlgorithms=+ssh-rsa",
        "-p", str(port),
        f"{username}@{ip}"
    ]

    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            result = subprocess.run(
                ssh_base + ["/file print without-paging"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
            )
            output = result.stdout
            lines = output.splitlines()
            existing_files = set()

            for line in lines:
                parts = line.split()
                if len(parts) >= 2:
                    existing_files.add(parts[1].strip())

            missing_required = [f for f in required_files if f not in existing_files]
            present_forbidden = [f for f in forbidden_files if f in existing_files]

            if not missing_required and not present_forbidden:
                logging.info("✅ Все нужные файлы MikroTik 6 найдены.")
                return True

        except Exception as e:
            logging.warning(f"⚠️ Ошибка при проверке файлов ROS 6: {e}")

        time.sleep(1)

    logging.error(f"⏱️ Таймаут: файлы ROS 6 не появились за {timeout} секунд.")
    return False

# --- Ожидание файлов для ROS 7.x ---
def wait_for_files(ssh_client, required_files, forbidden_files=None, timeout=20):
    if forbidden_files is None:
        forbidden_files = []

    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            stdin, stdout, stderr = ssh_client.exec_command("/file print without-paging")
            file_list_raw = stdout.read().decode("utf-8")

            lines = file_list_raw.splitlines()
            existing_files = set()

            for line in lines:
                parts = line.split()
                if len(parts) >= 2:
                    existing_files.add(parts[1].strip())

            missing_required = [f for f in required_files if f not in existing_files]
            present_forbidden = [f for f in forbidden_files if f in existing_files]

            if not missing_required and not present_forbidden:
                logging.info("✅ Все нужные файлы MikroTik 7 найдены.")
                return True

        except Exception as e:
            logging.warning(f"⚠️ Ошибка при проверке файлов ROS 7: {e}")

        time.sleep(1)

    logging.error(f"⏱️ Таймаут: файлы ROS 7 не появились за {timeout} секунд.")
    return False


def backup_router_ros6(router, local_dir):
    """Бэкап RouterOS 6.x с объединением команд для защиты от заморозки SSH-сессий"""
    ip = router["ip"]
    username = router["username"]
    port = str(router.get("port", 22))

    current_plugin_dir = os.path.dirname(os.path.abspath(__file__))
    key_filename = router.get("private_key", "id_rsa.key")
    key = key_filename if os.path.isabs(key_filename) else os.path.join(current_plugin_dir, key_filename)

    null_host_file = "NUL" if os.name == "nt" else "/dev/null"

    ssh_base = [
        "ssh",
        "-T",                       # Отключение TTY
        "-n",
        "-oBatchMode=yes",
        "-oConnectTimeout=10",
        "-oServerAliveInterval=5",
        "-oLogLevel=ERROR",
        "-i", key,
        "-oMACs=hmac-sha1,hmac-md5",
        "-oKexAlgorithms=+diffie-hellman-group1-sha1",
        "-oPubkeyAcceptedAlgorithms=+ssh-rsa",
        "-oStrictHostKeyChecking=no",
        f"-oUserKnownHostsFile={null_host_file}",
        "-p", port,
        f"{username}@{ip}"
    ]

    scp_opts = [
        "scp",
        "-oBatchMode=yes",
        "-oConnectTimeout=10",
        "-oLogLevel=ERROR",
        "-i", key,
        "-oMACs=hmac-sha1,hmac-md5",
        "-oKexAlgorithms=+diffie-hellman-group1-sha1",
        "-oPubkeyAcceptedAlgorithms=+ssh-rsa",
        "-oStrictHostKeyChecking=no",
        f"-oUserKnownHostsFile={null_host_file}",
        "-P", port
    ]

    def run_cmd(cmd_list, timeout=40):
        res = subprocess.run(
            cmd_list,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL
        )
        if res.returncode != 0:
            err_msg = res.stderr.strip() or res.stdout.strip() or f"Exit Code {res.returncode}"
            raise Exception(f"SSH CLI Error: {err_msg}")
        return res.stdout

    try:
        logging.info(f"📡 Создание бэкапа на MikroTik ROS 6 ({ip})...")

        # 🌟 Объединяем команды в ОДИН SSH-запрос через точку с запятой
        cmd_combined = "/system backup save name=auto-backup dont-encrypt=yes; /export file=auto-export"
        try:
            run_cmd(ssh_base + [cmd_combined], timeout=40)
        except Exception:
            # Если dont-encrypt не поддерживается старой версией ROS 6
            cmd_fallback = "/system backup save name=auto-backup; /export file=auto-export"
            run_cmd(ssh_base + [cmd_fallback], timeout=40)

        # ⏳ Пауза 2 секунды, чтобы SSH-демон ROS 6 успел корректно закрыть сокет
        time.sleep(2)

        today = datetime.now().strftime("%Y-%m-%d")
        created_files = []

        # Скачиваем созданные файлы
        for src, suffix in [("auto-backup.backup", ".backup"), ("auto-export.rsc", ".rsc")]:
            dest = os.path.join(local_dir, f"{ip}_{today}{suffix}")
            run_cmd(scp_opts + [f"{username}@{ip}:{src}", dest], timeout=30)
            logging.info(f"📥 Получен файл: {dest}")
            if os.path.exists(dest):
                created_files.append(dest)
            time.sleep(1)

        # Удаляем временные файлы с роутера одной объединенной командой
        try:
            run_cmd(ssh_base + ["/file remove auto-backup.backup; /file remove auto-export.rsc"], timeout=10)
        except Exception:
            pass

        return created_files

    except Exception as e:
        logging.error(f"❌ Ошибка бэкапа MikroTik 6.x ({ip}): {e}")
        raise e



# --- Бэкап RouterOS 7.x ---
def backup_router_ros7(router, local_dir):
    ip = router["ip"]
    username = router["username"]
    
    # Получаем расшифрованный ядром пароль (или кодовую фразу ключа)
    ssh_password = router.get("ssh_pass", "")
    
    current_plugin_dir = os.path.dirname(os.path.abspath(__file__))
    key_filename = router.get("private_key", "id_rsa.key")
    if not os.path.isabs(key_filename):
        key = os.path.join(current_plugin_dir, key_filename)
    else:
        key = key_filename
    port = router.get("port", 22)
    
    try:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        
        if os.path.exists(key):
            pkey = paramiko.RSAKey(filename=key)
            client.connect(ip, username=username, pkey=pkey, password=ssh_password if ssh_password else None, port=port, timeout=10)
        else:
            client.connect(ip, username=username, password=ssh_password, port=port, timeout=10)

        client.exec_command("/system backup save name=auto-backup")
        client.exec_command("/export file=auto-export")

        wait_for_files(
            client,
            required_files=["auto-backup.backup", "auto-export.rsc"],
            forbidden_files=["auto-export.rsc.in_progress"]
        )

        with SCPClient(client.get_transport()) as scp:
            for suffix in [".backup", ".rsc"]:
                src = "auto-backup.backup" if suffix == ".backup" else "auto-export.rsc"
                dest = os.path.join(local_dir, f"{ip}_{datetime.now().date()}{suffix}")
                scp.get(src, dest)
                # Получаем абсолютный путь для логирования
                full_path = os.path.abspath(dest)
                logging.info(f"📥 Получен файл: {dest}")

        client.exec_command("/file remove auto-backup.backup")
        client.exec_command("/file remove auto-export.rsc")
        client.close()
        
    except Exception as e:
        logging.error(f"❌ Ошибка при бэкапе RouterOS 7.x: {e}")
        raise e
    
# --- Главная функция-диспетчер плагина ---
def backup_router(router):
    ip = router["ip"]
    backup_path = router["backup_path"]
    version = str(router.get("version", "7"))

    today = datetime.now().strftime("%Y-%m-%d")
    local_dir = os.path.join(backup_path, today)
    os.makedirs(local_dir, exist_ok=True)

    logging.info(f"📡 Бэкап MikroTik {ip} (ROS {version})")
    
    created_files = []
    try:
        if version.startswith("6"):
            backup_router_ros6(router, local_dir)
        else:
            backup_router_ros7(router, local_dir)

        for suffix in [".backup", ".rsc"]:
            file_path = os.path.join(local_dir, f"{ip}_{today}{suffix}")
            if os.path.exists(file_path):
                created_files.append(file_path)
                
        return created_files

    except Exception as e:
        logging.error(f"❌ Ошибка при бэкапе MikroTik {ip}: {e}")
        raise e
    
def run_backup(router_config):
    """
    Точка входа для плагина MikroTik.
    """
    return backup_router(router_config)