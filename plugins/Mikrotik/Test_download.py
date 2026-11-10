import time
import sys
import os
import paramiko
from scp import SCPClient

# ============================================================
# НАСТРОЙКИ (замените под свой роутер)
# ============================================================
HOST = "192.0.2.2"
PORT = 22
USERNAME = "backup"
KEY_PATH = r"D:\MySoft\routerbackups\NetworkBackupManager\plugins\Mikrotik\id_rsa.key"

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
LOCAL_BACKUP = os.path.join(TEST_DIR, "test_config.backup")
LOCAL_EXPORT = os.path.join(TEST_DIR, "test_config.rsc")

def main():
    print(f"Подключение к {HOST}:{PORT}...")
    
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

    def exec_cmd(cmd):
        print(f"\n>>> EXEC: {cmd}")
        stdin, stdout, stderr = client.exec_command(cmd)
        exit_status = stdout.channel.recv_exit_status()
        out = stdout.read().decode(errors="ignore").strip()
        err = stderr.read().decode(errors="ignore").strip()
        print(f"    EXIT: {exit_status}")
        if out: print(f"    STDOUT: {repr(out)}")
        if err: print(f"    STDERR: {repr(err)}")
        return exit_status, out, err

    # Вспомогательная функция для правильного удаления
    def remove_remote_files():
        # Удаляем по одному, чтобы не было "bad parameter"
        exec_cmd("/file remove auto-backup.backup")
        exec_cmd("/file remove auto-export.rsc")

    try:
        # 1. Удаляем старые файлы
        print("\n--- Уборка перед стартом ---")
        remove_remote_files()

        # 2. Создаем новые файлы
        exec_cmd("/system backup save name=auto-backup")
        exec_cmd("/export file=auto-export")

        # 3. Пытаемся скачать с ретраями
        print("\n" + "="*50)
        print("НАЧИНАЕМ ПОПЫТКИ СКАЧИВАНИЯ ЧЕРЕЗ SCP")
        print("="*50)

        start_time = time.time()
        timeout = 60
        attempt = 0

        for f in (LOCAL_BACKUP, LOCAL_EXPORT):
            if os.path.exists(f):
                os.remove(f)

        while time.time() - start_time < timeout:
            attempt += 1
            print(f"\n--- Попытка #{attempt} ({time.time() - start_time:.1f} сек) ---")
            
            success = True
            
            try:
                with SCPClient(transport) as scp:
                    # Пробуем скачать бэкап
                    try:
                        print(f"  Качаем auto-backup.backup...")
                        scp.get("auto-backup.backup", LOCAL_BACKUP)
                        size = os.path.getsize(LOCAL_BACKUP)
                        print(f"    [OK] Скачан ({size} байт)")
                        if size == 0:
                            print("    [WARN] Файл 0 байт! Ждем...")
                            success = False
                    except Exception as e:
                        print(f"    [FAIL] Ошибка SCP (backup): {e}")
                        success = False

                    # Пробуем скачать экспорт
                    try:
                        print(f"  Качаем auto-export.rsc...")
                        scp.get("auto-export.rsc", LOCAL_EXPORT)
                        size = os.path.getsize(LOCAL_EXPORT)
                        print(f"    [OK] Скачан ({size} байт)")
                        if size == 0:
                            print("    [WARN] Файл 0 байт! Ждем...")
                            success = False
                    except Exception as e:
                        print(f"    [FAIL] Ошибка SCP (export): {e}")
                        success = False

                if success:
                    print("\n[SUCCESS] Все файлы успешно скачаны!")
                    break

            except Exception as e:
                print(f"  [CRITICAL] Ошибка SCP-сессии: {type(e).__name__}: {e}")
            
            print("  Ждем 5 секунд...")
            time.sleep(5)
        else:
            print("\n[FAIL] Таймаут ожидания файлов.")

        # 4. Уборка на роутере
        print("\n--- Уборка после завершения ---")
        remove_remote_files()

    finally:
        transport.close()
        print("\nСоединение закрыто.")

if __name__ == "__main__":
    main()