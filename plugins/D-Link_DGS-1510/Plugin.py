# =========================================================
# plugins/dlink/plugin.py
# =========================================================
# Плагин для резервного копирования D-Link (DGS-1510 и др.)
# через интерактивную SSH-сессию.
# =========================================================

import os
import re
import time
import logging
import paramiko

logger = logging.getLogger(__name__)

# Регулярка для удаления ANSI-escape последовательностей
ANSI_RE = re.compile(r'\x1b\[[0-9;?]*[a-zA-Z]')

# =========================================================
# ИЗВЛЕЧЕНИЕ КОНФИГА ИЗ "СЫРОГО" ВЫВОДА
# =========================================================

def _extract_config(raw: str) -> str:
    """Вырезает блок от 'Current configuration' до строки 'end'."""
    raw = ANSI_RE.sub('', raw).replace('\r', '')
    lines = raw.splitlines()
    
    start = next((i for i, l in enumerate(lines)
                  if l.strip().startswith("Current configuration")), None)
    
    if start is None:
        raise RuntimeError("не найден маркер 'Current configuration' в выводе коммутатора")
        
    end = next((i for i in range(start + 1, len(lines))
                if lines[i].strip() == "end"), len(lines) - 1)
                
    return "\n".join(lines[start:end + 1]) + "\n"


# =========================================================
# ОСНОВНАЯ ЛОГИКА
# =========================================================

def backup_dlink(router: dict) -> list[str]:
    host = router["host"]
    port = router.get("port", 22)
    user = router.get("username", "admin")
    
    # В проекте пароль хранится в поле ssh_pass и уже расшифрован к этому моменту
    password = router.get("ssh_pass", "")
    
    # Путь для сохранения
    backup_dir = router["backup_path"]
    os.makedirs(backup_dir, exist_ok=True)
    
    # Имя файла в Git-репозитории (как вы просили — config.json)
    local_filename = "config.json"
    local_path = os.path.join(backup_dir, local_filename)
    
    # Таймаут на подключение
    timeout = router.get("timeout", 10.0)

    logger.info("Подключение к D-Link %s:%s (user: %s)", host, port, user)

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    
    try:
        client.connect(
            hostname=host, 
            port=port, 
            username=user, 
            password=password,
            timeout=timeout, 
            allow_agent=False, 
            look_for_keys=False
        )

        # Открываем интерактивную сессию
        shell = client.invoke_shell()
        time.sleep(1.5)  # ждём приглашения
        
        if shell.recv_ready():
            shell.recv(65535)  # очищаем буфер от приглашения

        # Отключаем пагинацию (чтобы конфиг не прерывался на --More--)
        shell.send("terminal length 0\n")
        time.sleep(1)
        
        # Запрашиваем конфиг
        shell.send("show running-config\n")

        # Читаем вывод
        buf = ""
        deadline = time.time() + 30
        while time.time() < deadline:
            if shell.recv_ready():
                buf += shell.recv(65535).decode('utf-8', errors='ignore')
                # Ищем конец конфига
                if re.search(r'\bend\s*\n', buf):
                    time.sleep(0.5)
                    # Дочитываем остатки из буфера
                    while shell.recv_ready():
                        buf += shell.recv(65535).decode('utf-8', errors='ignore')
                    break
            else:
                time.sleep(0.3)
        else:
            logger.warning("Таймаут ожидания вывода 'end' от %s", host)

        shell.close()

        # Извлекаем чистый конфиг
        config = _extract_config(buf)

        # Сохраняем на диск
        with open(local_path, "w", encoding="utf-8") as f:
            f.write(config)

        logger.info("✅ Бэкап D-Link сохранён: %s (%d байт)", local_path, len(config))
        
        # Возвращаем список созданных файлов (контракт PluginManager)
        return [local_path]

    except paramiko.AuthenticationException as exc:
        logger.error("❌ Ошибка аутентификации D-Link %s: %s", host, exc)
        raise
    except Exception as exc:
        logger.error("❌ Ошибка подключения к D-Link %s: %s", host, exc)
        raise
    finally:
        client.close()


# =========================================================
# ТОЧКА ВХОДА (Контракт PluginManager)
# =========================================================

def run_backup(router: dict):
    """
    Точка входа для PluginManager.
    Возвращает список путей к созданным файлам.
    При ошибке — исключение.
    """
    return backup_dlink(router)