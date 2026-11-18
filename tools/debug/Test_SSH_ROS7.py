# ============================================================
# Test_SSH_ROS7.py
# ============================================================
# Диагностика public-key authentication на MikroTik RouterOS 6/7.
#
# Перебирает три варианта RSA-подписи userauth:
#   1) rsa-sha2-512   (самый современный, дефолт paramiko)
#   2) rsa-sha2-256   (может поддерживаться новыми ROS7)
#   3) ssh-rsa        (legacy SHA-1, гарантированно работает на всех ROS)
# Плюс отдельно — «standard paramiko» (без disabled_algorithms).
#
# Для каждого варианта — новое TCP-соединение, свой Transport.
# Результат пишется в paramiko_debug.log и на консоль.
# ============================================================

import base64
import hashlib
import logging
import socket
import sys
import traceback
from pathlib import Path

import paramiko


# ============================================================
# НАСТРОЙКИ — замените под нужное устройство
# ============================================================

HOST = "192.0.2.1"          # ← поменяйте
PORT = 22
USERNAME = "backup"         # ← поменяйте, если ходите под другим

KEY_PATH = Path(__file__).resolve().parents[2] / "data" / "keys" / "mikrotik" / "id_rsa.key"

DEBUG_LOG = Path(__file__).resolve().parent / "paramiko_debug.log"

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
# ВСПОМОГАТЕЛЬНОЕ
# ============================================================

def separator(char="=", length=72):
    print(char * length)


def print_title(text):
    print()
    separator()
    print(text)
    separator()


def fingerprint_md5(key) -> str:
    return ":".join(f"{b:02x}" for b in key.get_fingerprint())


def fingerprint_sha256(key) -> str:
    """
    Правильный SHA256 fingerprint в формате ssh-keygen.
    Для private RSAKey asbytes() возвращает blob публичной части —
    это именно то, что сервер видит как ключ пользователя.
    Для host key то же самое.
    """
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
        key = paramiko.RSAKey.from_private_key_file(str(KEY_PATH))
        print("[OK] RSA ключ успешно загружен")
        print(f"Bits:        {key.get_bits()}")
        print(f"Type:        {key.get_name()}")
        print(f"MD5:         {fingerprint_md5(key)}")
        print(f"SHA256:      {fingerprint_sha256(key)}")
        return key
    except Exception as exc:
        print("[FAIL] Не удалось загрузить RSA ключ")
        print(f"Ошибка: {exc}")
        return None


# ============================================================
# TRANSPORT
# ============================================================

def create_transport(disabled_pubkeys=None):
    """
    disabled_algorithms — ТОЛЬКО через конструктор.
    Присваивание transport.disabled_algorithms = {...} после
    создания НЕ работает: этот словарь читается один раз
    при инициализации AuthHandler.
    """
    sock = socket.create_connection((HOST, PORT), timeout=TIMEOUT)

    kwargs = {}
    if disabled_pubkeys:
        kwargs["disabled_algorithms"] = {"pubkeys": list(disabled_pubkeys)}

    transport = paramiko.Transport(sock, **kwargs)
    transport.banner_timeout = TIMEOUT
    transport.auth_timeout = TIMEOUT
    return transport


def do_handshake(transport):
    transport.start_client(timeout=TIMEOUT)
    if not transport.is_active():
        raise RuntimeError("Transport не активен после handshake")
    return transport.get_remote_server_key()


# ============================================================
# ИНФОРМАЦИЯ О TRANSPORT
# ============================================================

def print_transport_info(transport, host_key):
    print()
    print("Transport:")
    print(f"  Remote version : {transport.remote_version}")
    print(f"  Active         : {transport.is_active()}")
    print(f"  Authenticated  : {transport.is_authenticated()}")
    print(f"  Host key type  : {transport.host_key_type}")

    print()
    print("Server host key:")
    print(f"  Type:     {host_key.get_name()}")
    print(f"  Bits:     {host_key.get_bits()}")
    print(f"  MD5:      {fingerprint_md5(host_key)}")
    print(f"  SHA256:   {fingerprint_sha256(host_key)}")

    print()
    print("Negotiated algorithms:")
    try:
        kex = transport.kex_engine.__class__.__name__ if transport.kex_engine else "?"
        print(f"  KEX         : {kex}")
    except Exception:
        print("  KEX         : <unknown>")
    for label, attr in (
        ("Cipher C->S", "local_cipher"),
        ("Cipher S->C", "remote_cipher"),
        ("MAC    C->S", "local_mac"),
        ("MAC    S->C", "remote_mac"),
        ("Comp   C->S", "local_compression"),
        ("Comp   S->C", "remote_compression"),
    ):
        try:
            print(f"  {label}: {getattr(transport, attr)}")
        except Exception:
            print(f"  {label}: <unknown>")

    # ВАЖНО: список алгоритмов подписи, которые paramiko будет
    # предъявлять серверу на USERAUTH. Именно его правим через
    # disabled_algorithms — не путать с host key algorithms.
    print()
    print("Userauth signature algorithms (_preferred_pubkeys):")
    try:
        preferred = getattr(transport, "_preferred_pubkeys", None)
        if preferred:
            for a in preferred:
                print(f"  - {a}")
        else:
            print("  <не удалось получить>")
    except Exception as exc:
        print(f"  <ошибка: {exc}>")


# ============================================================
# AUTH METHODS
# ============================================================

def get_auth_methods(transport):
    print_title("ДОСТУПНЫЕ AUTH METHODS (auth_none)")
    try:
        methods = transport.auth_none(USERNAME)
        print("[OK] auth_none неожиданно прошёл:", methods)
        return methods or []
    except paramiko.BadAuthenticationType as exc:
        methods = exc.allowed_types or []
        for m in methods:
            print(f"[SERVER] {m}")
        return methods
    except Exception as exc:
        print(f"[WARNING] Не удалось определить: {exc}")
        return []


# ============================================================
# ПОПЫТКА АУТЕНТИФИКАЦИИ
# ============================================================

def authenticate_attempt(key, attempt_name, disabled_pubkeys=None):
    print()
    separator("-")
    print(f"ПОПЫТКА: {attempt_name}")
    separator("-")
    if disabled_pubkeys:
        print(f"disabled_algorithms.pubkeys = {disabled_pubkeys}")
    else:
        print("disabled_algorithms = <нет, дефолт paramiko>")

    transport = None
    try:
        transport = create_transport(disabled_pubkeys=disabled_pubkeys)
        host_key = do_handshake(transport)
        print("[OK] SSH handshake")
        print_transport_info(transport, host_key)

        print()
        print("[INFO] auth_publickey...")
        transport.auth_publickey(USERNAME, key)

        if not transport.is_authenticated():
            print("[FAIL] auth_publickey вернулся, но transport не аутентифицирован")
            return False

        print()
        print("[SUCCESS] public key authentication OK")

        # Проверка исполнения команды
        try:
            channel = transport.open_session()
            channel.settimeout(TIMEOUT)
            channel.exec_command("/system identity print")
            output = b""
            while True:
                chunk = channel.recv(4096)
                if not chunk:
                    break
                output += chunk
            channel.close()
            text = output.decode("utf-8", errors="replace")
            print()
            print("Ответ MikroTik:")
            print("-" * 72)
            print(text.rstrip())
            print("-" * 72)
        except Exception as exc:
            print(f"[WARN] Команда не выполнилась: {exc}")

        return True

    except paramiko.AuthenticationException as exc:
        print(f"[FAIL] AuthenticationException: {exc}")
        return False
    except paramiko.BadAuthenticationType as exc:
        print(f"[FAIL] BadAuthenticationType: allowed={exc.allowed_types}")
        return False
    except paramiko.SSHException as exc:
        print(f"[FAIL] SSHException: {exc}")
        return False
    except EOFError as exc:
        print(f"[FAIL] EOFError: {exc}")
        return False
    except OSError as exc:
        print(f"[FAIL] OS/network error: {exc}")
        return False
    except Exception as exc:
        print(f"[FAIL] {type(exc).__name__}: {exc}")
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
    print("DIAGNOSTIC: MikroTik ROS publickey authentication")
    separator()
    print(f"Paramiko: {paramiko.__version__}")
    print(f"Host:     {HOST}:{PORT}")
    print(f"User:     {USERNAME}")
    print(f"Key:      {KEY_PATH}")
    print(f"Debug:    {DEBUG_LOG}")

    key = load_private_key()
    if key is None:
        return 1

    # ---- Handshake без auth (чтобы узнать методы) ----
    print_title("HANDSHAKE (без auth)")
    transport = None
    try:
        transport = create_transport()
        host_key = do_handshake(transport)
        print("[OK] SSH handshake")
        print_transport_info(transport, host_key)
        get_auth_methods(transport)
    except Exception as exc:
        print(f"[FAIL] handshake/auth methods: {type(exc).__name__}: {exc}")
        traceback.print_exc()
        return 2
    finally:
        if transport is not None:
            try:
                transport.close()
            except Exception:
                pass

    # ---- Попытка 1: rsa-sha2-512 (только он, SHA256 отключён) ----
    # На MikroTik это обычно НЕ работает: ROS не поддерживает
    # SHA2-подписи для userauth. Но проверим.
    if authenticate_attempt(
        key,
        "rsa-sha2-512 (disable: rsa-sha2-256)",
        disabled_pubkeys=["rsa-sha2-256"],
    ):
        print_title("РЕЗУЛЬТАТ: rsa-sha2-512 работает")
        return 0

    # ---- Попытка 2: rsa-sha2-256 (только он) ----
    if authenticate_attempt(
        key,
        "rsa-sha2-256 (disable: rsa-sha2-512)",
        disabled_pubkeys=["rsa-sha2-512"],
    ):
        print_title("РЕЗУЛЬТАТ: rsa-sha2-256 работает")
        return 0

    # ---- Попытка 3: ssh-rsa (оба SHA2 отключены) ----
    if authenticate_attempt(
        key,
        "ssh-rsa (disable: rsa-sha2-256, rsa-sha2-512)",
        disabled_pubkeys=["rsa-sha2-256", "rsa-sha2-512"],
    ):
        print_title("РЕЗУЛЬТАТ: ssh-rsa работает")
        return 0

    # ---- Попытка 4: стандарт paramiko (для сравнения) ----
    if authenticate_attempt(
        key,
        "standard paramiko (без disabled_algorithms)",
        disabled_pubkeys=None,
    ):
        print_title("РЕЗУЛЬТАТ: standard paramiko работает")
        return 0

    print_title("ИТОГ: ни один вариант не прошёл")
    print("Смотрите", DEBUG_LOG)
    print("Строки, на которые смотреть:")
    print("  - Our pubkey algorithm list: [...]")
    print("  - Server did not send a server-sig-algs list")
    print("  - userauth: Attempting auth by publickey")
    print("  - Server accepts key: pkalg=...")
    return 3


if __name__ == "__main__":
    try:
        code = main()
    except KeyboardInterrupt:
        print("\n[INFO] Прервано")
        code = 130
    except Exception as exc:
        print("\nКРИТИЧЕСКАЯ ОШИБКА")
        print(f"{type(exc).__name__}: {exc}")
        traceback.print_exc()
        code = 99

    print()
    print("=" * 72)
    print(f"Код завершения: {code}")
    print("=" * 72)
    try:
        input("\nENTER для выхода...")
    except EOFError:
        pass
    sys.exit(code)
    