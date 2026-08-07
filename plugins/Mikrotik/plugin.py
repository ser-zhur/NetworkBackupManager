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

# --- Бэкап RouterOS 6.x ---
def backup_router_ros6(router, local_dir):
    ip = router["ip"]
    username = router["username"]
    current_plugin_dir = os.path.dirname(os.path.abspath(__file__))
    key_filename = router.get("private_key", "id_rsa.key")
    if not os.path.isabs(key_filename):
        key = os.path.join(current_plugin_dir, key_filename)
    else:
        key = key_filename
    port = router.get("port", 22)

    ssh_base = [
        "ssh", "-i", key,
        "-oMACs=hmac-sha1,hmac-md5",
        "-oKexAlgorithms=+diffie-hellman-group1-sha1",
        "-oPubkeyAcceptedAlgorithms=+ssh-rsa",
        "-p", str(port),
        f"{username}@{ip}"
    ]

    try:
        subprocess.run(ssh_base + ["/system backup save name=auto-backup"], check=True)
        subprocess.run(ssh_base + ["/export file=auto-export"], check=True)

        if not wait_for_files_ros6(
            ip, username, key, port,
            required_files=["auto-backup.backup", "auto-export.rsc"],
            forbidden_files=["auto-export.rsc.in_progress"]
        ):
            raise Exception("Файлы ROS 6 не появились вовремя")

        for suffix in [".backup", ".rsc"]:
            dest = os.path.join(local_dir, f"{ip}_{datetime.now().date()}{suffix}")
            src = "auto-backup.backup" if suffix == ".backup" else "auto-export.rsc"
            scp_cmd = ["scp", "-i", key,
                       "-oMACs=hmac-sha1,hmac-md5",
                       "-oKexAlgorithms=+diffie-hellman-group1-sha1",
                       "-oPubkeyAcceptedAlgorithms=+ssh-rsa",
                       "-P", str(port),
                       f"{username}@{ip}:{src}", dest]
            subprocess.run(scp_cmd, check=True)
            logging.info(f"📥 Получен файл: {dest}")

        subprocess.run(ssh_base + ["/file remove auto-backup.backup"], check=True)
        subprocess.run(ssh_base + ["/file remove auto-export.rsc"], check=True)

    except Exception as e:
        logging.error(f"❌ Ошибка при бэкапе MikroTik 6.x: {e}")
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