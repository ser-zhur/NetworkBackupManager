import sys
import time
import traceback
from pathlib import Path

import paramiko
from scp import SCPClient


# ============================================================
# НАСТРОЙКИ
# ============================================================

HOST = "192.0.2.1"
PORT = 22
USERNAME = "backup"

KEY_FILE = Path(
    r"D:\MySoft\routerbackups\NetworkBackupManager\plugins\Mikrotik\id_rsa.key"
)

DEBUG_LOG = Path(__file__).with_name("paramiko_debug.log")


# ============================================================
# PARAMIKO DEBUG
# ============================================================

paramiko.util.log_to_file(
    str(DEBUG_LOG),
    level="DEBUG"
)


# ============================================================
# ЗАГРУЗКА КЛЮЧА
# ============================================================

def load_key():

    print("=" * 70)
    print("ЗАГРУЗКА RSA КЛЮЧА")
    print("=" * 70)

    print(f"Файл: {KEY_FILE}")

    if not KEY_FILE.exists():
        raise FileNotFoundError(
            f"Ключ не найден:\n{KEY_FILE}"
        )

    key = paramiko.RSAKey.from_private_key_file(
        str(KEY_FILE)
    )

    print()
    print("[OK] RSA ключ загружен")
    print(f"Bits: {key.get_bits()}")

    return key


# ============================================================
# TRANSPORT
# ============================================================

def create_transport():

    print()
    print("=" * 70)
    print("СОЗДАНИЕ SSH TRANSPORT")
    print("=" * 70)

    transport = paramiko.Transport(
        (HOST, PORT)
    )

    security = transport.get_security_options()

    # --------------------------------------------------------
    # KEX
    # --------------------------------------------------------

    security.kex = (
        "diffie-hellman-group-exchange-sha256",
    )

    # --------------------------------------------------------
    # HOST KEY
    # --------------------------------------------------------

    security.key_types = (
        "rsa-sha2-256",
    )

    # --------------------------------------------------------
    # CIPHER
    # --------------------------------------------------------

    security.ciphers = (
        "aes128-ctr",
    )

    # --------------------------------------------------------
    # MAC
    # --------------------------------------------------------

    security.digests = (
        "hmac-sha1",
    )

    print()
    print("Настройки:")

    print(
        "KEX      :",
        security.kex
    )

    print(
        "HostKey  :",
        security.key_types
    )

    print(
        "Cipher   :",
        security.ciphers
    )

    print(
        "MAC      :",
        security.digests
    )

    return transport


# ============================================================
# ИНФОРМАЦИЯ О TRANSPORT
# ============================================================

def show_transport_info(transport):

    print()
    print("=" * 70)
    print("ФАКТИЧЕСКОЕ SSH СОЕДИНЕНИЕ")
    print("=" * 70)

    print(
        "Remote version:",
        transport.remote_version
    )

    print(
        "Host key:",
        transport.host_key_type
    )

    print(
        "Local cipher:",
        transport.local_cipher
    )

    print(
        "Remote cipher:",
        transport.remote_cipher
    )

    print(
        "Local MAC:",
        transport.local_mac
    )

    print(
        "Remote MAC:",
        transport.remote_mac
    )


# ============================================================
# EXEC COMMAND
# ============================================================

def exec_command(transport, command):

    print()
    print("-" * 70)
    print("COMMAND")
    print("-" * 70)

    print(command)

    channel = transport.open_session()

    try:

        channel.exec_command(command)

        start = time.time()

        while not channel.exit_status_ready():

            if time.time() - start > 20:

                raise TimeoutError(
                    f"Timeout выполнения команды: {command}"
                )

            time.sleep(0.1)

        exit_code = channel.recv_exit_status()

        stdout = channel.makefile(
            "rb",
            -1
        ).read()

        stderr = channel.makefile_stderr(
            "rb",
            -1
        ).read()

        stdout = stdout.decode(
            "utf-8",
            errors="replace"
        )

        stderr = stderr.decode(
            "utf-8",
            errors="replace"
        )

        print()
        print("Exit code:", exit_code)

        if stdout.strip():

            print()
            print("STDOUT:")
            print(stdout)

        if stderr.strip():

            print()
            print("STDERR:")
            print(stderr)

        return exit_code, stdout, stderr

    finally:

        channel.close()


# ============================================================
# SCP TEST
# ============================================================

def test_scp(transport):

    print()
    print("=" * 70)
    print("SCP TEST")
    print("=" * 70)

    test_file = "auto-export.rsc"

    local_file = Path(__file__).with_name(
        "test_download.rsc"
    )

    print()
    print(
        f"Пробуем скачать: {test_file}"
    )

    try:

        with SCPClient(
            transport
        ) as scp:

            scp.get(
                test_file,
                str(local_file)
            )

        if not local_file.exists():

            raise RuntimeError(
                "SCP не создал локальный файл"
            )

        size = local_file.stat().st_size

        print()
        print("[SUCCESS] SCP работает")
        print(
            f"Файл: {local_file}"
        )
        print(
            f"Размер: {size} bytes"
        )

        return True

    except Exception as e:

        print()
        print("[FAILED] SCP")
        print(
            f"{type(e).__name__}: {e}"
        )

        return False


# ============================================================
# MAIN TEST
# ============================================================

def main():

    print()
    print("=" * 70)
    print("PARAMIKO TEST ДЛЯ MIKROTIK ROS")
    print("=" * 70)

    print()
    print(
        "Paramiko:",
        paramiko.__version__
    )

    print(
        "Host:",
        HOST
    )

    print(
        "User:",
        USERNAME
    )

    print(
        "Key:",
        KEY_FILE
    )

    transport = None

    try:

        # ----------------------------------------------------
        # 1. KEY
        # ----------------------------------------------------

        key = load_key()

        # ----------------------------------------------------
        # 2. TRANSPORT
        # ----------------------------------------------------

        transport = create_transport()

        # ----------------------------------------------------
        # 3. START SSH
        # ----------------------------------------------------

        print()
        print("=" * 70)
        print("SSH HANDSHAKE")
        print("=" * 70)

        transport.start_client(
            timeout=10
        )

        print()
        print("[OK] SSH handshake")

        # ----------------------------------------------------
        # 4. HOST KEY
        # ----------------------------------------------------

        server_key = transport.get_remote_server_key()

        print()
        print("Server host key:")
        print(
            "Type:",
            server_key.get_name()
        )

        print(
            "Fingerprint:",
            server_key.get_fingerprint().hex()
        )

        # ----------------------------------------------------
        # 5. USER AUTH
        # ----------------------------------------------------

        print()
        print("=" * 70)
        print("PUBLIC KEY AUTHENTICATION")
        print("=" * 70)

        print()
        print(
            "Пробуем RSA private key..."
        )

        transport.auth_publickey(
            USERNAME,
            key
        )

        if not transport.is_authenticated():

            raise RuntimeError(
                "Paramiko не прошёл authentication"
            )

        print()
        print("[SUCCESS] Authentication OK")

        # ----------------------------------------------------
        # 6. SHOW NEGOTIATED PARAMETERS
        # ----------------------------------------------------

        show_transport_info(
            transport
        )

        # ----------------------------------------------------
        # 7. TEST COMMAND
        # ----------------------------------------------------

        code, stdout, stderr = exec_command(
            transport,
            "/system identity print"
        )

        if code != 0:

            raise RuntimeError(
                "Команда /system identity print "
                "завершилась ошибкой"
            )

        print()
        print("[SUCCESS] SSH command works")

        # ----------------------------------------------------
        # 8. FILE LIST
        # ----------------------------------------------------

        exec_command(
            transport,
            "/file print without-paging"
        )

        print()
        print("[SUCCESS] /file print works")

        # ----------------------------------------------------
        # 9. SCP
        # ----------------------------------------------------

        # SCP проверяем только если файл уже есть
        #
        # Если auto-export.rsc отсутствует,
        # это НЕ ошибка SSH.

        print()
        print(
            "Проверяем наличие auto-export.rsc..."
        )

        code, stdout, stderr = exec_command(
            transport,
            '/file print where name="auto-export.rsc"'
        )

        if "auto-export.rsc" in stdout:

            test_scp(
                transport
            )

        else:

            print()
            print(
                "[SKIP] auto-export.rsc "
                "отсутствует на роутере"
            )

        # ----------------------------------------------------
        # SUCCESS
        # ----------------------------------------------------

        print()
        print("=" * 70)
        print("ИТОГ")
        print("=" * 70)

        print()
        print(
            "Paramiko успешно подключился "
            "к MikroTik."
        )

        print()
        print(
            "SSH authentication: OK"
        )

        print(
            "SSH commands: OK"
        )

        print()
        print(
            f"DEBUG LOG:\n{DEBUG_LOG}"
        )

        return 0

    except Exception as e:

        print()
        print("=" * 70)
        print("ОШИБКА")
        print("=" * 70)

        print()
        print(
            "Тип:",
            type(e).__name__
        )

        print(
            "Ошибка:",
            e
        )

        print()
        print("TRACEBACK:")

        traceback.print_exc()

        print()
        print(
            "DEBUG LOG:"
        )

        print(
            DEBUG_LOG
        )

        return 1

    finally:

        if transport:

            try:
                transport.close()
            except Exception:
                pass


if __name__ == "__main__":

    exit_code = main()

    print()
    input(
        "Нажмите ENTER для выхода..."
    )

    sys.exit(
        exit_code
    )