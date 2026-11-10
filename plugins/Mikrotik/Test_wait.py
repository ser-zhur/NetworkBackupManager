import time
import sys
import paramiko

# ============================================================
# НАСТРОЙКИ (замените под свой роутер)
# ============================================================
HOST = "192.0.2.2"
PORT = 22
USERNAME = "backup"
KEY_PATH = r"D:\MySoft\routerbackups\NetworkBackupManager\plugins\Mikrotik\id_rsa.key"

# ============================================================
# ФУНКЦИЯ ПРОВЕРКИ
# ============================================================
def test_wait():
    print(f"Подключение к {HOST}...")
    
    # Подключаемся (с обходом бага paramiko 4.x)
    transport = paramiko.Transport(
        (HOST, PORT),
        disabled_algorithms={"pubkeys": ["rsa-sha2-512"]}
    )
    transport.start_client(timeout=10)
    
    pkey = paramiko.RSAKey.from_private_key_file(KEY_PATH)
    transport.auth_publickey(USERNAME, pkey)

    if not transport.is_authenticated():
        print("[FAIL] Ошибка аутентификации")
        return

    print("[OK] Подключено.")
    client = paramiko.SSHClient()
    client._transport = transport

    # 1. Очищаем старые файлы
    print("\n--- Удаление старых файлов ---")
    stdin, stdout, stderr = client.exec_command(
        "/file remove auto-backup.backup auto-export.rsc"
    )
    stdout.read()
    print("Старые файлы удалены (если были)")

    # 2. Запускаем создание
    print("\n--- Запуск /system backup save ---")
    stdin, stdout, stderr = client.exec_command(
        "/system backup save name=auto-backup"
    )
    stdout.read()
    print("Команда отправлена.")

    print("\n--- Запуск /export ---")
    stdin, stdout, stderr = client.exec_command(
        "/export file=auto-export"
    )
    stdout.read()
    print("Команда отправлена.")

    # 3. Ждём появления
    print("\n--- Ожидание файлов ---")
    start = time.time()
    timeout = 30
    
    while time.time() - start < timeout:
        elapsed = time.time() - start
        print(f"[{elapsed:.1f} сек] Проверка...")
        
        missing = []
        for fn in ["auto-backup.backup", "auto-export.rsc"]:
            cmd = f'/file find where name="{fn}"'
            stdin, stdout, stderr = client.exec_command(cmd)
            
            raw_out = stdout.read()
            raw_err = stderr.read()
            exit_status = stdout.channel.recv_exit_status()
            
            out = raw_out.decode(errors="ignore").strip()
            err = raw_err.decode(errors="ignore").strip()
            
            print(f"  >> CMD: {cmd}")
            print(f"     EXIT: {exit_status}")
            print(f"     STDOUT: {repr(out)}")
            print(f"     STDERR: {repr(err)}")
            
            if not out:
                missing.append(fn)
                
        if not missing:
            print("\n[SUCCESS] Все файлы найдены!")
            break
            
        print(f"  Ждём: {missing}")
        time.sleep(1)
    else:
        print("\n[FAIL] Таймаут.")
        
    transport.close()

if __name__ == "__main__":
    test_wait()