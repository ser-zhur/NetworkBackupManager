import os
import sys
import socket
import logging
import traceback
from pathlib import Path

import paramiko


# ============================================================
# НАСТРОЙКИ
# ============================================================

HOST = "192.0.2.3"
PORT = 22
USERNAME = "backup"

KEY_PATH = (
    Path(__file__).resolve().parent / "id_rsa.key"
)

DEBUG_LOG = (
    Path(__file__).resolve().parent / "paramiko_debug.log"
)

TIMEOUT = 10


# ============================================================
# ЛОГИРОВАНИЕ
# ============================================================

def setup_logging():
    """
    Полный DEBUG Paramiko в отдельный файл.
    """

    try:
        DEBUG_LOG.parent.mkdir(parents=True, exist_ok=True)

        # Старый лог удаляем, чтобы каждый запуск был чистым.
        if DEBUG_LOG.exists():
            try:
                DEBUG_LOG.unlink()
            except Exception:
                pass

        logging.basicConfig(
            filename=str(DEBUG_LOG),
            level=logging.DEBUG,
            format="%(levelname)s [%(asctime)s] %(name)s: %(message)s",
            force=True,
        )

        paramiko_logger = logging.getLogger("paramiko")
        paramiko_logger.setLevel(logging.DEBUG)

    except Exception as e:
        print(f"[WARNING] Не удалось настроить DEBUG LOG: {e}")


# ============================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ============================================================

def separator(char="=", length=70):
    print(char * length)


def print_title(text):
    print()
    separator()
    print(text)
    separator()


def fingerprint_md5(key):
    """
    MD5 fingerprint для удобства сравнения со старыми SSH-клиентами.
    """
    return ":".join(
        f"{b:02x}" for b in key.get_fingerprint()
    )


def fingerprint_sha256(key):
    """
    SHA256 fingerprint SSH host key.
    """
    import base64
    import hashlib

    digest = hashlib.sha256(key.asbytes()).digest()
    return base64.b64encode(digest).decode().rstrip("=")


# ============================================================
# ЗАГРУЗКА КЛЮЧА
# ============================================================

def load_private_key():
    print_title("ПРОВЕРКА RSA PRIVATE KEY")

    print(f"Файл: {KEY_PATH}")

    if not KEY_PATH.exists():
        print("[FAIL] Файл ключа не существует")
        return None

    print("[OK] Файл ключа существует")

    try:
        key = paramiko.RSAKey.from_private_key_file(
            str(KEY_PATH)
        )

        print("[OK] RSA ключ успешно загружен")
        print(f"Bits: {key.get_bits()}")
        print(f"Fingerprint MD5: {fingerprint_md5(key)}")

        return key

    except Exception as e:
        print("[FAIL] Не удалось загрузить RSA ключ")
        print(f"Ошибка: {e}")
        return None


# ============================================================
# СОЗДАНИЕ TRANSPORT
# ============================================================

def create_transport():
    """
    Создаёт новый Transport.

    Никакие внутренние security.key_types/ciphers/kex/digests
    здесь НЕ изменяются.

    Это принципиально важно для Paramiko 5.0.0.
    """

    sock = socket.create_connection(
        (HOST, PORT),
        timeout=TIMEOUT
    )

    transport = paramiko.Transport(sock)

    # Таймауты.
    transport.banner_timeout = TIMEOUT
    transport.auth_timeout = TIMEOUT

    return transport


# ============================================================
# HANDSHAKE
# ============================================================

def do_handshake(transport):
    """
    Выполняет SSH handshake.
    """

    transport.start_client(
        timeout=TIMEOUT
    )

    if not transport.is_active():
        raise RuntimeError(
            "Transport не активен после SSH handshake"
        )

    return transport.get_remote_server_key()


# ============================================================
# ИНФОРМАЦИЯ О TRANSPORT
# ============================================================

def print_transport_info(transport, host_key):
    print()
    print("Transport:")
    print(f"Remote version : {transport.remote_version}")
    print(f"Active         : {transport.is_active()}")
    print(f"Authenticated  : {transport.is_authenticated()}")

    print()
    print("Server host key:")
    print(f"Type:        {host_key.get_name()}")
    print(f"Bits:        {host_key.get_bits()}")
    print(f"MD5:         {fingerprint_md5(host_key)}")
    print(f"SHA256:      {fingerprint_sha256(host_key)}")

    print()
    print("Negotiated algorithms:")

    try:
        print(
            f"KEX:         {transport.kex_engine.__class__.__name__}"
            if transport.kex_engine
            else "KEX:         <unknown>"
        )
    except Exception:
        print("KEX:         <unknown>")

    try:
        print(
            f"Cipher C->S: {transport.local_cipher}"
        )
    except Exception:
        print("Cipher C->S: <unknown>")

    try:
        print(
            f"Cipher S->C: {transport.remote_cipher}"
        )
    except Exception:
        print("Cipher S->C: <unknown>")

    try:
        print(
            f"MAC C->S:    {transport.local_mac}"
        )
    except Exception:
        print("MAC C->S:    <unknown>")

    try:
        print(
            f"MAC S->C:    {transport.remote_mac}"
        )
    except Exception:
        print("MAC S->C:    <unknown>")


# ============================================================
# AUTH METHODS
# ============================================================

def get_auth_methods(transport):
    """
    Узнаём доступные способы authentication через auth_none().
    """

    print_title("ДОСТУПНЫЕ AUTHENTICATION METHODS")

    try:
        methods = transport.auth_none(USERNAME)

        print("[OK] auth_none неожиданно прошёл")

        if methods:
            for method in methods:
                print(f"[SERVER] {method}")

        return methods

    except paramiko.BadAuthenticationType as e:
        methods = e.allowed_types or []

        for method in methods:
            print(f"[SERVER] {method}")

        return methods

    except Exception as e:
        print(f"[WARNING] Не удалось определить методы: {e}")
        return []


# ============================================================
# ПОПЫТКА АУТЕНТИФИКАЦИИ
# ============================================================

def authenticate_attempt(
    key,
    attempt_name,
    forced_signature_algorithm=None,
):
    """
    Отдельная SSH-сессия для каждой попытки.

    forced_signature_algorithm:
        None
            стандартный Paramiko

        rsa-sha2-256
            принудительная RSA SHA256

        ssh-rsa
            принудительная legacy RSA/SHA1
    """

    print()
    separator("-")
    print(f"ПОПЫТКА: {attempt_name}")
    separator("-")

    if forced_signature_algorithm:
        print(
            f"Требуемый RSA signature algorithm: "
            f"{forced_signature_algorithm}"
        )
    else:
        print(
            "RSA signature algorithm: "
            "СТАНДАРТНЫЙ PARAMIKO"
        )

    transport = None

    try:
        # ----------------------------------------------------
        # Новый Transport
        # ----------------------------------------------------

        transport = create_transport()

        # ----------------------------------------------------
        # Для ssh-rsa / rsa-sha2-256.
        #
        # Paramiko выбирает алгоритм подписи RSA внутри
        # AuthHandler. Поэтому не используем
        # security.key_types.
        #
        # Вместо этого применяем отключение альтернативных
        # RSA signature algorithms через disabled_algorithms.
        # ----------------------------------------------------

        if forced_signature_algorithm == "rsa-sha2-256":

            # Оставляем rsa-sha2-256.
            #
            # Отключаем rsa-sha2-512.
            #
            # ssh-rsa не отключаем — сервер всё равно сможет
            # выбрать rsa-sha2-256 благодаря server-sig-algs.
            transport.disabled_algorithms = {
                "pubkeys": [
                    "rsa-sha2-512",
                ]
            }

        elif forced_signature_algorithm == "ssh-rsa":

            # Для legacy ssh-rsa необходимо убрать
            # rsa-sha2-* из списка предпочтительных
            # алгоритмов подписи.
            #
            # В Paramiko внутренние названия для disabled
            # algorithms зависят от версии. Поэтому сначала
            # попробуем штатный механизм.
            transport.disabled_algorithms = {
                "pubkeys": [
                    "rsa-sha2-512",
                    "rsa-sha2-256",
                ]
            }

        # ----------------------------------------------------
        # Handshake
        # ----------------------------------------------------

        print("[INFO] Выполняем SSH handshake...")

        host_key = do_handshake(transport)

        print("[OK] SSH handshake")

        print_transport_info(
            transport,
            host_key
        )

        # ----------------------------------------------------
        # Server signature algorithms
        # ----------------------------------------------------

        print()
        print("Проверка server-sig-algs:")

        try:
            # AuthHandler хранит информацию о сервере.
            auth_handler = transport.auth_handler

            if auth_handler is not None:
                server_sig_algs = getattr(
                    auth_handler,
                    "server_sig_algs",
                    None
                )

                print(
                    f"server_sig_algs: "
                    f"{server_sig_algs}"
                )

        except Exception as e:
            print(
                f"[INFO] Не удалось получить "
                f"server-sig-algs напрямую: {e}"
            )

        # ----------------------------------------------------
        # Authentication
        # ----------------------------------------------------

        print()
        print("[INFO] Выполняем publickey authentication...")

        print(f"Key type: {key.get_name()}")
        print(f"Bits:     {key.get_bits()}")
        print(
            f"MD5:      {fingerprint_md5(key)}"
        )

        result = transport.auth_publickey(
            USERNAME,
            key
        )

        # ----------------------------------------------------
        # Успешная authentication
        # ----------------------------------------------------

        if transport.is_authenticated():

            print()
            print("[SUCCESS] PUBLIC KEY AUTHENTICATION УСПЕШНА!")

            print(f"Auth result: {result}")

            # ------------------------------------------------
            # Выполняем простую команду ROS7.
            # ------------------------------------------------

            print()
            print("ПРОВЕРКА SSH COMMAND EXECUTION")

            channel = transport.open_session()

            channel.settimeout(TIMEOUT)

            channel.exec_command(
                "/system identity print"
            )

            output = channel.recv(65535)

            try:
                text = output.decode(
                    "utf-8",
                    errors="replace"
                )
            except Exception:
                text = str(output)

            print()
            print("Ответ MikroTik:")
            print("-" * 70)
            print(text.rstrip())
            print("-" * 70)

            channel.close()

            return True

        print(
            "[FAIL] auth_publickey вернул управление, "
            "но transport не authenticated."
        )

        return False

    except paramiko.AuthenticationException as e:

        print()
        print(
            "[FAIL] AuthenticationException"
        )
        print(f"Ошибка: {e}")

        return False

    except paramiko.BadAuthenticationType as e:

        print()
        print(
            "[FAIL] BadAuthenticationType"
        )

        print(
            f"Разрешённые методы: "
            f"{e.allowed_types}"
        )

        return False

    except paramiko.SSHException as e:

        print()
        print("[FAIL] SSHException")
        print(f"Ошибка: {e}")

        return False

    except EOFError as e:

        print()
        print("[FAIL] EOFError")
        print(f"Ошибка: {e}")

        return False

    except OSError as e:

        print()
        print("[FAIL] OS/network error")
        print(f"Ошибка: {e}")

        return False

    except Exception as e:

        print()
        print("[FAIL] Неожиданная ошибка")
        print(f"Тип: {type(e).__name__}")
        print(f"Ошибка: {e}")

        traceback.print_exc()

        return False

    finally:

        if transport is not None:

            try:
                transport.close()
            except Exception:
                pass


# ============================================================
# MAIN
# ============================================================

def main():

    setup_logging()

    print()
    separator()
    print("PARAMIKO TEST ДЛЯ MIKROTIK ROS7")
    separator()

    print(f"Paramiko: {paramiko.__version__}")
    print(f"Host:     {HOST}")
    print(f"Port:     {PORT}")
    print(f"User:     {USERNAME}")
    print(f"Key:      {KEY_PATH}")
    print(f"Debug:    {DEBUG_LOG}")

    # ========================================================
    # Проверяем версию Paramiko
    # ========================================================

    if paramiko.__version__ != "5.0.0":

        print()
        print(
            "[WARNING] Этот тест рассчитан именно "
            "на Paramiko 5.0.0"
        )

        print(
            f"Установлена версия: "
            f"{paramiko.__version__}"
        )

    # ========================================================
    # Загружаем ключ
    # ========================================================

    key = load_private_key()

    if key is None:
        print()
        print("[FAIL] Тест остановлен.")
        return 1

    # ========================================================
    # Сначала отдельное соединение для определения методов
    # ========================================================

    print_title("ПРОВЕРКА SSH HANDSHAKE")

    transport = None

    try:

        transport = create_transport()

        host_key = do_handshake(
            transport
        )

        print("[OK] SSH handshake")

        print_transport_info(
            transport,
            host_key
        )

        get_auth_methods(
            transport
        )

    except Exception as e:

        print()
        print("[FAIL] SSH handshake/auth methods")
        print(f"Тип: {type(e).__name__}")
        print(f"Ошибка: {e}")

        print()
        print("TRACEBACK:")
        traceback.print_exc()

        return 2

    finally:

        if transport is not None:
            try:
                transport.close()
            except Exception:
                pass

    # ========================================================
    # ПОПЫТКА №1
    # rsa-sha2-256
    # ========================================================

    print_title(
        "ПОПЫТКА №1: RSA-SHA2-256"
    )

    success = authenticate_attempt(
        key=key,
        attempt_name="RSA-SHA2-256",
        forced_signature_algorithm="rsa-sha2-256",
    )

    if success:

        print_title("РЕЗУЛЬТАТ")

        print(
            "[SUCCESS] MikroTik ROS7 принимает "
            "RSA public key через Paramiko."
        )

        print(
            "[SUCCESS] Рабочий вариант: rsa-sha2-256"
        )

        print()
        print(
            "Именно этот вариант рекомендуется "
            "использовать в plugin.py."
        )

        print()
        print(f"DEBUG LOG: {DEBUG_LOG}")

        return 0

    # ========================================================
    # ПОПЫТКА №2
    # ssh-rsa
    # ========================================================

    print_title(
        "ПОПЫТКА №2: LEGACY SSH-RSA"
    )

    success = authenticate_attempt(
        key=key,
        attempt_name="SSH-RSA",
        forced_signature_algorithm="ssh-rsa",
    )

    if success:

        print_title("РЕЗУЛЬТАТ")

        print(
            "[SUCCESS] MikroTik ROS7 принимает "
            "RSA public key."
        )

        print(
            "[SUCCESS] Рабочий вариант: ssh-rsa"
        )

        print()
        print(
            "ROS7 совместим с Paramiko только "
            "при использовании legacy ssh-rsa."
        )

        print()
        print(
            "Это менее предпочтительный вариант, "
            "чем rsa-sha2-256."
        )

        print()
        print(f"DEBUG LOG: {DEBUG_LOG}")

        return 0

    # ========================================================
    # ПОПЫТКА №3
    # стандартный Paramiko
    # ========================================================

    print_title(
        "ПОПЫТКА №3: STANDARD PARAMIKO RSA"
    )

    success = authenticate_attempt(
        key=key,
        attempt_name="STANDARD PARAMIKO RSA",
        forced_signature_algorithm=None,
    )

    if success:

        print_title("РЕЗУЛЬТАТ")

        print(
            "[SUCCESS] Обычная RSA-аутентификация "
            "Paramiko 5.0.0 работает."
        )

        print(
            "Дополнительные настройки не нужны."
        )

        print()
        print(f"DEBUG LOG: {DEBUG_LOG}")

        return 0

    # ========================================================
    # ВСЕ ПОПЫТКИ НЕУДАЧНЫ
    # ========================================================

    print_title("РЕЗУЛЬТАТ")

    print(
        "[FAIL] Ни один вариант RSA authentication "
        "не прошёл."
    )

    print()
    print(
        "SSH handshake с MikroTik ROS7 работает."
    )

    print(
        "Private RSA key успешно загружается."
    )

    print(
        "Проблема находится именно на этапе "
        "public-key authentication."
    )

    print()
    print(
        "Необходимо посмотреть полный DEBUG LOG:"
    )

    print(
        str(DEBUG_LOG)
    )

    return 3


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":

    try:
        exit_code = main()

    except KeyboardInterrupt:

        print()
        print("[INFO] Прервано пользователем.")
        exit_code = 130

    except Exception as e:

        print()
        print("=" * 70)
        print("КРИТИЧЕСКАЯ ОШИБКА")
        print("=" * 70)

        print(
            f"Тип: {type(e).__name__}"
        )

        print(
            f"Ошибка: {e}"
        )

        traceback.print_exc()

        exit_code = 99

    print()
    print("=" * 70)
    print(f"Код завершения: {exit_code}")
    print("=" * 70)

    input("\nНажмите ENTER для выхода...")

    sys.exit(exit_code)