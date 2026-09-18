import os
import logging

import paramiko
import socket


def backup_pfsense_ssh(router):
    host = router["host"]
    port = router.get("port", 22)
    username = router.get("username", "root")

    current_plugin_dir = os.path.dirname(os.path.abspath(__file__))
    key_filename = router.get(
        "ssh_key",
        router.get("private_key", "id_rsa.key"),
    )

    if os.path.isabs(key_filename):
        key_path = key_filename
    else:
        key_path = os.path.join(current_plugin_dir, key_filename)

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

    # Git-only: файл всегда пишется плоско в backup_path
    # с фиксированным именем config.xml.
    backup_dir = router["backup_path"]
    local_filename = "config.xml"

    os.makedirs(backup_dir, exist_ok=True)

    logging.info("📡 Подключение к pfSense %s:%s по SSH", host, port)

    try:
        key = paramiko.RSAKey.from_private_key_file(key_path)
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(
            hostname=host,
            port=port,
            username=username,
            pkey=key,
            timeout=10,
        )

        sftp = ssh.open_sftp()
        remote_file = "/cf/conf/config.xml"
        local_file = os.path.join(backup_dir, local_filename)

        sftp.get(remote_file, local_file)
        sftp.close()
        ssh.close()

        full_path = os.path.abspath(local_file)

        logging.info("✅ Бэкап pfSense сохранён: %s", full_path)
        return [full_path]

    except (paramiko.SSHException, socket.error) as e:
        logging.error(
            "❌ Сбой подключения к pfSense %s:%s: %s",
            host, port, e,
        )
        raise e


def run_backup(router_config):
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
    return backup_pfsense_ssh(router_config)