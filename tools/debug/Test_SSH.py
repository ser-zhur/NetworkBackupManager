# ============================================================
# Test_SSH.py
# ============================================================
# Проверка полного цикла на MikroTik:
#   SSH handshake → publickey auth → exec → SCP download.
#
# В отличие от Test_SSH_ROS7.py здесь настраивается полный
# набор параметров Transport (kex, host key, cipher, MAC)
# и используется отключение SHA2-подписей для userauth —
# так, чтобы гарантированно сработать и на ROS6, и на ROS7.
# ============================================================

import logging
import sys
import time
import traceback
from pathlib import Path

import paramiko
from scp import SCPClient


# ============================================================
# НАСТРОЙКИ — замените под нужное устройство
# ============================================================

HOST = "192.0.2.1"          # ← поменяйте
PORT = 22
USERNAME = "backup"         # ← поменяйте, если ходите под другим

KEY_FILE = Path(__file__).resolve().parents[2] / "data" / "keys" / "mikrotik" / "id_rsa.key"

DEBUG_LOG = Path(__file__).resolve().with_name("paramiko_debug.log")

TIMEOUT = 15


# ============================================================
# ЛОГИРОВАНИЕ
# ============================================================

def setup_logging():
    try:
        if DEBUG_LOG.exists():
            try:
                DEBUG_LOG.unlink()
            except Exception:
                pass

        handler = logging.FileHandler(str(DEBUG_LOG), encoding="utf-8")
        handler.setFormatter(logging.Formatter(
            "%(levelname)s [%(asctime)s] %(name)s: %(message)s"
        ))

        root = logging.getLogger()
        for h in list(root.handlers):
            root.removeHandler(h)
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)

        logging.getLogger("paramiko").setLevel(logging.DEBUG)

    except Exception as exc:
        print(f"[WARNING] Не удалось настроить DEBUG LOG: {exc}")


# ============================================================
# КЛЮЧ
# ============================================================

def load_key():
    print("=" * 72)
    print("ЗАГРУЗКА RSA КЛЮЧА")
    print("=" * 72)
    print(f"Файл: {KEY_FILE}")

    if not KEY_FILE.exists():
        raise FileNotFoundError(f"Ключ не найден: {KEY_FILE}")

    key = paramiko.RSAKey.from_private_key_file(str(KEY_FILE))
    print(f"[OK] RSA ключ загружен")
    print(f"Bits: {key.get_bits()}")
    return key


# ============================================================
# TRANSPORT
# ============================================================

def create_transport():
    print()
    print("=" * 72)
    print("СОЗДАНИЕ SSH TRANSPORT")
    print("=" * 72)

    # disabled_algorithms для USERAUTH — обязательный аргумент
    # конструктора. MikroTik (6 и большинство 7) не принимает
    # подписи RSA-SHA2, поэтому отключаем оба, оставляя ssh-rsa.
    transport = paramiko.Transport(
        (HOST, PORT),
        disabled_algorithms={
            "pubkeys": ["rsa-sha2-512", "rsa-sha2-256"],
        },
    )

    security = transport.get_security_options()

    # KEX — широкий список, MikroTik выберет сам.
    # Включены и современные, и устаревшие DH-группы.
    security.kex = (
        "curve25519-sha256",
        "diffie-hellman-group-exchange-sha256",
        "diffie-hellman-group14-sha256",
        "diffie-hellman-group14-sha1",
        "diffie-hellman-group1-sha1",
    )

    # HOST KEY — то, чем сервер представляется клиенту.
    # Это НЕ то же, что подпись userauth.
    security.key_types = (
        "rsa-sha2-256",
        "ssh-rsa",
    )

    # Шифры
    security.ciphers = (
        "aes128-ctr",
        "aes192-ctr",
        "aes256-ctr",
    )

    # MAC — hmac-sha1 нужен для старых ROS; hmac-sha2-256 —
    # для ROS7 с strong-crypto=yes.
    security.digests = (
        "hmac-sha2-256",
        "hmac-sha1",
    )

    # (Опционально) разрешаем и SHA1, и SHA2 compress — нет,
    # compress оставляем как есть.

    print()
    print("Настройки (что предлагаем серверу):")
    print("  KEX      :", security.kex)
    print("  HostKey  :", security.key_types)
    print("  Cipher   :", security.ciphers)
    print("  MAC      :", security.digests)
    print("  disabled : {'pubkeys': ['rsa-sha2-512', 'rsa-sha2-256']}")

    try:
        preferred = getattr(transport, "_preferred_pubkeys", None)
        if preferred:
            print("  Userauth pubkey order:", list(preferred))
    except Exception:
        pass

    return transport


# ============================================================
# ИНФО
# ============================================================

def show_transport_info(transport):
    print()
    print("=" * 72)
    print("ФАКТИЧЕСКОЕ SSH СОЕДИНЕНИЕ")
    print("=" * 72)
    print("Remote version:", transport.remote_version)
    print("Host key type :", transport.host_key_type)
    print("Cipher C->S   :", transport.local_cipher)
    print("Cipher S->C   :", transport.remote_cipher)
    print("MAC    C->S   :", transport.local_mac)
    print("MAC    S->C   :", transport.remote_mac)


# ============================================================
# EXEC
# ============================================================

def exec_command(transport, command):
    print()
    print("-" * 72)
    print("COMMAND:", command)
    print("-" * 72)

    channel = transport.open_session()
    try:
        channel.exec_command(command)
        start = time.time()
        while not channel.exit_status_ready():
            if time.time() - start > TIMEOUT:
                raise TimeoutError(f"Timeout: {command}")
            time.sleep(0.1)

        code = channel.recv_exit_status()
        stdout = channel.makefile("rb", -1).read().decode("utf-8", errors="replace")
        stderr = channel.makefile_stderr("rb", -1).read().decode("utf-8", errors="replace")

        print("Exit code:", code)
        if stdout.strip():
            print("STDOUT:")
            print(stdout)
        if stderr.strip():
            print("STDERR:")
            print(stderr)
        return code, stdout, stderr
    finally:
        channel.close()


# ============================================================
# SCP
# ============================================================

def test_scp(transport, remote_file):
    print()
    print("=" * 72)
    print("SCP TEST")
    print("=" * 72)
    print(f"Пробуем скачать: {remote_file}")

    local_file = Path(__file__).with_name("test_download.rsc")
    if local_file.exists():
        try:
            local_file.unlink()
        except Exception:
            pass

    try:
        with SCPClient(transport) as scp:
            scp.get(remote_file, str(local_file))

        if not local_file.exists():
            raise RuntimeError("SCP не создал локальный файл")

        size = local_file.stat().st_size
        print(f"[SUCCESS] SCP работает")
        print(f"  Файл:   {local_file}")
        print(f"  Размер: {size} bytes")
        return True
    except Exception as exc:
        print(f"[FAILED] SCP: {type(exc).__name__}: {exc}")
        return False


# ============================================================
# MAIN
# ============================================================

def main():
    setup_logging()

    print()
    print("=" * 72)
    print("PARAMIKO TEST ДЛЯ MIKROTIK ROS")
    print("=" * 72)
    print("Paramiko:", paramiko.__version__)
    print("Host:    ", HOST)
    print("User:    ", USERNAME)
    print("Key:     ", KEY_FILE)
    print("Debug:   ", DEBUG_LOG)

    transport = None
    try:
        key = load_key()

        transport = create_transport()

        print()
        print("=" * 72)
        print("SSH HANDSHAKE")
        print("=" * 72)
        transport.start_client(timeout=TIMEOUT)
        print("[OK] SSH handshake")

        server_key = transport.get_remote_server_key()
        print()
        print("Server host key:")
        print("  Type:", server_key.get_name())
        print("  Fingerprint (MD5):",
              ":".join(f"{b:02x}" for b in server_key.get_fingerprint()))

        show_transport_info(transport)

        print()
        print("=" * 72)
        print("PUBLIC KEY AUTHENTICATION")
        print("=" * 72)
        transport.auth_publickey(USERNAME, key)
        if not transport.is_authenticated():
            raise RuntimeError("Paramiko не прошёл authentication")
        print("[SUCCESS] Authentication OK")

        # Проверка exec
        code, stdout, stderr = exec_command(
            transport, "/system identity print"
        )
        if code != 0:
            raise RuntimeError("/system identity print failed")
        print("[SUCCESS] SSH command works")

        # Список файлов
        exec_command(transport, "/file print without-paging")

        # SCP — только если файл есть
        code, stdout, _ = exec_command(
            transport, '/file print where name="auto-export.rsc"'
        )
        if "auto-export.rsc" in stdout:
            test_scp(transport, "auto-export.rsc")
        else:
            print()
            print("[SKIP] auto-export.rsc отсутствует на роутере")

        print()
        print("=" * 72)
        print("ИТОГ")
        print("=" * 72)
        print("Paramiko подключился, auth OK, exec OK.")
        print("DEBUG LOG:", DEBUG_LOG)
        return 0

    except Exception as exc:
        print()
        print("=" * 72)
        print("ОШИБКА")
        print("=" * 72)
        print("Тип:    ", type(exc).__name__)
        print("Ошибка: ", exc)
        print("TRACEBACK:")
        traceback.print_exc()
        print("DEBUG LOG:", DEBUG_LOG)
        return 1

    finally:
        if transport is not None:
            try:
                transport.close()
            except Exception:
                pass


if __name__ == "__main__":
    code = main()
    print()
    try:
        input("Нажмите ENTER для выхода...")
    except EOFError:
        pass
    sys.exit(code)