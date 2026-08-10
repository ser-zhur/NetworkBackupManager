import os
import logging
from datetime import datetime
import paramiko
import socket

def backup_pfsense_ssh(router):
    host = router["host"]
    port = router.get("port", 22)
    username = router.get("username", "root")
    
    current_plugin_dir = os.path.dirname(os.path.abspath(__file__))
    key_filename = router.get("ssh_key", router.get("private_key", "id_rsa.key"))
    if not os.path.isabs(key_filename):
        key_path = os.path.join(current_plugin_dir, key_filename)
    else:
        key_path = key_filename

    backup_path = router["backup_path"]

    today = datetime.now().strftime("%Y-%m-%d")
    backup_dir = os.path.join(backup_path, today)
    os.makedirs(backup_dir, exist_ok=True)

    logging.info(f"📡 Подключение к pfSense {host}:{port} по SSH")

    try:
        key = paramiko.RSAKey.from_private_key_file(key_path)
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(hostname=host, port=port, username=username, pkey=key, timeout=10)

        sftp = ssh.open_sftp()
        remote_file = "/cf/conf/config.xml"
        local_file = os.path.join(backup_dir, f"{host.replace(':', '_')}_{today}.xml")

        sftp.get(remote_file, local_file)
        sftp.close()
        ssh.close()

        # Приводим к абсолютному пути
        full_path = os.path.abspath(local_file)

        logging.info(f"✅ Бэкап pfSense сохранён: {full_path}")
        return [full_path]
 

    except (paramiko.SSHException, socket.error) as e:
        logging.error(f"❌ Сбой подключения к pfSense {host}:{port}: {e}")
        raise e

def run_backup(router_config):
    """Единая точка входа для плагина pfSense"""
    return backup_pfsense_ssh(router_config)