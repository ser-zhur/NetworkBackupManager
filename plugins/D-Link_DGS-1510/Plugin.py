import os
import re
import socket
import time
import logging

import paramiko

from core import host_keys


# ANSI escape sequences (приглашение, цвета и т.п.)
ANSI_RE = re.compile(r'\x1b\[[0-9;?]*[a-zA-Z]')


def extract_config(raw: str) -> str:
    """
    Вырезает блок от 'Current configuration' до строки 'end'.
    """
    raw = ANSI_RE.sub('', raw).replace('\r', '')
    lines = raw.splitlines()

    start = next(
        (i for i, l in enumerate(lines)
         if l.strip().startswith("Current configuration")),
        None,
    )
    if start is None:
        raise RuntimeError(
            "не найден маркер 'Current configuration' в выводе D-Link"
        )

    end = next(
        (i for i in range(start + 1, len(lines))
         if lines[i].strip() == "end"),
        len(lines) - 1,
    )
    return "\n".join(lines[start:end + 1]) + "\n"


def backup_dlink_ssh(router):
    """
    Скачивает running-config с D-Link DGS-1510 по SSH
    через интерактивную сессию (invoke_shell).

    router (из routers.json, ssh_pass уже расшифрован):
        host / ip        : адрес коммутатора
        port             : SSH-порт (по умолчанию 22)
        username / user  : имя пользователя (по умолчанию "admin")
        ssh_pass         : пароль (расшифровывается plugin_manager'ом)
        backup_path      : каталог для сохранения
        timeout          : таймаут установки соединения, сек (10)
        read_timeout     : таймаут чтения конфига, сек (30)
        filename         : имя файла (по умолчанию "running-config.cfg")
    """
    host = router.get("host") or router.get("ip")
    if not host:
        raise ValueError("Не задан host/ip коммутатора D-Link")

    port = int(router.get("port", 22))
    username = router.get("username", router.get("user", "admin"))

    password = router.get("ssh_pass")
    if not password:
        raise ValueError(
            f"Не задан пароль SSH (поле 'ssh_pass') для D-Link {host}"
        )

    connect_timeout = float(router.get("timeout", 10.0))
    read_timeout = float(router.get("read_timeout", 30.0))

    backup_dir = router["backup_path"]
    local_filename = router.get("filename", "running-config.cfg")

    os.makedirs(backup_dir, exist_ok=True)
    local_file = os.path.join(backup_dir, local_filename)

    logging.info("📡 Подключение к D-Link %s:%s по SSH", host, port)

    client = paramiko.SSHClient()
    # Ключ хоста: запомнить при первом подключении, отказать
    # при изменённом (раньше AutoAddPolicy принимал любой).
    host_keys.prepare_client(client, port)

    try:
        client.connect(
            hostname=host,
            port=port,
            username=username,
            password=password,
            timeout=connect_timeout,
            allow_agent=False,
            look_for_keys=False,
        )

        # Интерактивная сессия — D-Link требует именно shell,
        # exec_command часто возвращает пустой вывод.
        shell = client.invoke_shell()
        time.sleep(1)  # ждём приглашения
        if shell.recv_ready():
            shell.recv(65535)  # сбрасываем приглашение

        # Отключаем пагинацию и запрашиваем конфиг
        shell.send("terminal length 0\n")
        time.sleep(1)
        shell.send("show running-config\n")

        # Читаем вывод, пока не увидим 'end' или не истечёт таймаут
        buf = ""
        deadline = time.time() + read_timeout
        while time.time() < deadline:
            if shell.recv_ready():
                buf += shell.recv(65535).decode(
                    'utf-8', errors='ignore'
                )
                if re.search(r'\bend\s*\n', buf):
                    time.sleep(0.5)
                    while shell.recv_ready():
                        buf += shell.recv(65535).decode(
                            'utf-8', errors='ignore'
                        )
                    break
            else:
                time.sleep(0.3)

        try:
            shell.close()
        except Exception:
            pass

        config = extract_config(buf)

        with open(local_file, "w", encoding="utf-8") as f:
            f.write(config)

        full_path = os.path.abspath(local_file)
        logging.info("✅ Бэкап D-Link сохранён: %s", full_path)
        return [full_path]

    except (paramiko.SSHException, socket.error, OSError) as e:
        logging.error(
            "❌ Сбой подключения к D-Link %s:%s: %s",
            host, port, e,
        )
        raise
    finally:
        try:
            client.close()
        except Exception:
            pass


def run_backup(router_config):
    """
    Точка входа PluginManager.

    Контракт с core.plugin_manager:
        • Возвращает list[str | Path] — пути к созданным файлам.
        • [] — «нет изменений» (успех, файлов для выгрузки нет).
        • Любая ошибка — исключение (глотать запрещено).

    Здесь всегда скачиваем конфиг и возвращаем путь к нему.
    """
    return backup_dlink_ssh(router_config)