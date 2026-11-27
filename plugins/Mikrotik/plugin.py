import os
import re
import sys
import time
import subprocess
import logging

import paramiko
from scp import SCPClient

from core import host_keys
from core.ssh import CompatTransport

_debug_log = logging.getLogger("nbm.debug")

# =========================================================
# WINDOWS
# =========================================================

CREATE_NO_WINDOW = (
    subprocess.CREATE_NO_WINDOW
    if sys.platform == "win32"
    else 0
)

# =========================================================
# МАРКЕРЫ ОШИБОК ПОДКЛЮЧЕНИЯ
# =========================================================
#
# Нужны, чтобы отличить «не та версия RouterOS» от
# «не тот ключ». В первом случае есть смысл пробовать
# второй транспорт, во втором — нет: ключ и логин те же,
# результат будет таким же.
#
# =========================================================

class _VersionMismatch(Exception):
    """Текущая стратегия подключения не подходит устройству."""


class _AuthFailure(Exception):
    """Проблема аутентификации — смена версии не поможет."""


# Подстроки, по которым распознаём auth-ошибку в тексте
# subprocess-вывода ssh.exe (регистронезависимо).
_AUTH_MARKERS = (
    "permission denied",
    "authentication failed",
    "authentication exception",
    "invalid public key",
    "bad permissions",
    "unprotected private key",
    "no supported authentication",
    "publickey,password",
)


def _is_auth_error(exc: BaseException) -> bool:
    """True, если исключение — провал аутентификации."""
    if isinstance(exc, paramiko.AuthenticationException):
        return True
    text = str(exc).lower()
    return any(marker in text for marker in _AUTH_MARKERS)


# =========================================================
# ДИСПЕТЧЕРЫ ВЕРСИЙ
# =========================================================
#
# Каждая функция бросает либо _VersionMismatch (пробуем
# другую версию), либо _AuthFailure (пробовать бессмысленно),
# либо возвращает список файлов при успехе.
#
# =========================================================

def _do_ros7(router, backup_path):
    host = router["host"]
    try:
        client = _connect_ros7(
            host=host,
            port=router.get("port", 22),
            user=router.get("username", "admin"),
            key_path=_get_key_path(router),
            ssh_password=router.get("ssh_pass", ""),
            debug=bool(router.get("debug_connection")),
        )
    except Exception as exc:
        # Изменённый ключ хоста — не «не та версия ROS»:
        # пробовать другой путь подключения нельзя.
        if host_keys.is_host_key_error(exc):
            raise
        if _is_auth_error(exc):
            raise _AuthFailure(
                f"{type(exc).__name__}: {exc}"
            ) from exc
        raise _VersionMismatch(
            f"{type(exc).__name__}: {exc}"
        ) from exc

    logging.info("ROS7 detected: %s", host)

    try:
        return backup_router_ros7(router, backup_path, client=client)
    finally:
        try:
            client.close()
        except Exception:
            pass


def _do_ros6(router, backup_path):
    # ROS6 идёт через системный ssh.exe одной длинной
    # командой, поэтому «соединение установлено или нет»
    # и «версия подходит или нет» различимы только по
    # тексту ошибки.
    try:
        return backup_router_ros6(router, backup_path)
    except Exception as exc:
        if host_keys.is_host_key_error(exc):
            raise
        if _is_auth_error(exc):
            raise _AuthFailure(
                f"{type(exc).__name__}: {exc}"
            ) from exc
        raise _VersionMismatch(
            f"{type(exc).__name__}: {exc}"
        ) from exc

# =========================================================
# ОБЩИЕ ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# =========================================================

# =========================================================
# NORMALIZE FOR COMPARE
# =========================================================
#
# MikroTik в первую строку .rsc пишет дату экспорта.
# Форматы ROS 6 и 7 различаются:
#
#   ROS 6:  # sep/17/2026 12:45:17 by RouterOS 6.49.21
#   ROS 7:  # 2026-09-17 14:41:00 by RouterOS 7.24.2
#
# Эта строка меняется при каждом бэкапе, даже если конфиг
# не менялся. Нормализатор удаляет её — тогда сравнение
# в git-хранилище корректно определяет, есть ли реальные
# изменения. Сам файл в git сохраняется целиком, как
# скачан с роутера.
#
# =========================================================

_RSC_TIMESTAMP_RE = re.compile(
    rb"^#\s+"
    rb"\S+"                    # дата: "sep/17/2026" или "2026-09-17"
    rb"\s+"
    rb"\d{1,2}:\d{2}:\d{2}"    # время: "12:45:17" или "9:12:34"
    rb"\s+by\s+RouterOS\b"
)


def normalize_for_compare(filename: str, data: bytes) -> bytes:
    """
    Возвращает содержимое файла, приведённое к виду,
    пригодному для сравнения «изменилось или нет».

    Для .rsc удаляет первую строку, если она содержит
    timestamp экспорта RouterOS.

    Для остальных файлов возвращает данные без изменений.
    """
    if not filename.endswith(".rsc"):
        return data

    if not data:
        return data

    lines = data.splitlines(keepends=True)

    if lines and _RSC_TIMESTAMP_RE.match(lines[0]):
        return b"".join(lines[1:])

    return data

def _run_command(cmd, timeout=60, verbose=False):
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        stdin=subprocess.DEVNULL,
        creationflags=CREATE_NO_WINDOW,
    )

    if result.returncode != 0:
        error = (
            result.stderr.strip()
            or result.stdout.strip()
            or "Команда завершилась с ошибкой"
        )
        raise RuntimeError(error)

    if verbose and result.stderr:
        for line in result.stderr.splitlines():
            _debug_log.debug("ssh[%s]: %s", cmd[0], line)

    return result.stdout


def _get_key_path(router):
    # Ядро передаёт абсолютный путь в data/keys/mikrotik/
    # (core.devices.prepare_key_path). Относительное имя
    # бывает только при запуске плагина в обход ядра —
    # тогда ищем рядом с плагином.
    key_filename = router.get(
        "ssh_key",
        router.get("private_key", "id_rsa.key"),
    )

    current_plugin_dir = os.path.dirname(
        os.path.abspath(__file__)
    )

    if os.path.isabs(key_filename):
        key_path = key_filename
    else:
        key_path = os.path.join(
            current_plugin_dir, key_filename,
        )

    key_path = os.path.abspath(key_path)

    if not os.path.exists(key_path):
        raise FileNotFoundError(
            f"SSH-ключ не найден: {key_path}. "
            f"Положите файл ключа по этому пути или укажите "
            f"другое имя ключа в настройках устройства "
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

    return key_path


# =========================================================
# ROS6
# =========================================================
def backup_router_ros6(router, local_dir):
    host = router["host"]
    user = router.get("username", "admin")
    port = router.get("port", 22)
    key = _get_key_path(router)

    debug = bool(router.get("debug_connection"))
    ssh_verbose = ["-vvv"] if debug else []
    scp_verbose = ["-v"]   if debug else []

    # Ключ хоста: accept-new — запомнить при первом
    # подключении, отказать при изменённом (раньше
    # StrictHostKeyChecking=no принимал любой).
    host_key_opts = host_keys.ssh_exe_options()

    ssh_base = [
        "ssh", *ssh_verbose,
        "-p", str(port), "-i", key,
        "-T", "-n", "-oBatchMode=yes", "-oConnectTimeout=10",
        "-oServerAliveInterval=5", *host_key_opts,
        "-oMACs=hmac-sha1,hmac-md5",
        "-oKexAlgorithms=+diffie-hellman-group1-sha1",
        "-oPubkeyAcceptedAlgorithms=+ssh-rsa",
        # OpenSSH 8.8+ не берёт ключ хоста ssh-rsa по умолчанию,
        # а RouterOS 6 часто другого не предлагает.
        "-oHostKeyAlgorithms=+ssh-rsa",
        f"{user}@{host}",
    ]

    scp_base = [
        "scp", *scp_verbose,
        "-P", str(port), "-i", key,
        "-oBatchMode=yes", "-oConnectTimeout=10",
        *host_key_opts,
        "-oMACs=hmac-sha1,hmac-md5",
        "-oKexAlgorithms=+diffie-hellman-group1-sha1",
        "-oPubkeyAcceptedAlgorithms=+ssh-rsa",
        "-oHostKeyAlgorithms=+ssh-rsa",
    ]

    # 1. Очистка старых файлов
    try:
        _run_command(ssh_base + ["/file remove auto-backup.backup"], timeout=20, verbose=debug)
        _run_command(ssh_base + ["/file remove auto-export.rsc"], timeout=20, verbose=debug)
    except Exception:
        pass # Игнорируем, если файлов не было

    # 2. Создание файлов
    combined_command = (
        "/system backup save name=auto-backup dont-encrypt=yes; "
        "/export file=auto-export"
    )
    _run_command(ssh_base + [combined_command], timeout=90, verbose=debug)

    os.makedirs(local_dir, exist_ok=True)

    # 3. Скачивание с ретраями
    timeout = 90
    start_time = time.time()
    
    files_to_download = [
        ("auto-backup.backup", "config.backup"),
        ("auto-export.rsc", "config.rsc"),
    ]
    created_files = []

    while time.time() - start_time < timeout:
        success = True
        created_files = []

        for source, suffix in files_to_download:
            destination = os.path.join(local_dir, suffix)
            if os.path.exists(destination):
                os.remove(destination)

            try:
                _run_command(
                    scp_base + [f"{user}@{host}:{source}", destination],
                    timeout=30,
                    verbose=debug
                )
                if not os.path.exists(destination) or os.path.getsize(destination) == 0:
                    success = False
                    break
                created_files.append(destination)
            except Exception as exc:
                if debug:
                    _debug_log.debug("ros6 scp failed for %s: %s", source, exc)
                success = False
                break

        if success:
            break

        time.sleep(5)

    if not created_files or len(created_files) != 2:
        raise RuntimeError("Не удалось скачать файлы backup/export за 90 секунд.")

    # 4. Уборка на роутере
    try:
        _run_command(ssh_base + ["/file remove auto-backup.backup"], timeout=20, verbose=debug)
        _run_command(ssh_base + ["/file remove auto-export.rsc"], timeout=20, verbose=debug)
    except Exception as exc:
        logging.warning("Не удалось удалить временные файлы на %s: %s", host, exc)

    logging.info("ROS6 backup saved: %s", host)
    return created_files

# =========================================================
# ROS7
# =========================================================

def download_files_with_retry(
    ssh_client,
    local_dir,
    filenames_map,
    timeout=90,
    debug=False,
):
    """
    Пытается скачать файлы по SCP с повторными попытками.
    filenames_map: dict, где ключ - имя файла на роутере,
                   значение - локальное имя (куда сохранить).
    """
    start = time.time()
    attempt = 0

    while time.time() - start < timeout:
        attempt += 1
        
        if debug:
            _debug_log.debug(
                "ros7 download #%d: trying to download...", attempt
            )

        success = True
        
        try:
            with SCPClient(ssh_client.get_transport()) as scp:
                for remote_name, local_name in filenames_map.items():
                    local_path = os.path.join(local_dir, local_name)
                    
                    if os.path.exists(local_path):
                        os.remove(local_path)
                    
                    try:
                        scp.get(remote_name, local_path)
                    except Exception as scp_err:
                        success = False
                        if debug:
                            _debug_log.debug(
                                "ros7 download #%d: %s not ready (%s)",
                                attempt, remote_name, scp_err
                            )
                        break 
                        
                    if os.path.getsize(local_path) == 0:
                        success = False
                        if debug:
                            _debug_log.debug(
                                "ros7 download #%d: %s is 0 bytes, waiting...",
                                attempt, remote_name
                            )
                        break

            if success:
                if debug:
                    _debug_log.debug(
                        "ros7 download #%d: ALL FILES DOWNLOADED SUCCESSFULLY", attempt
                    )
                return True

        except Exception as e:
            if debug:
                _debug_log.debug(
                    "ros7 download #%d: transport error: %s: %s",
                    attempt, type(e).__name__, e
                )

        time.sleep(5)

    if debug:
        _debug_log.debug("ros7 download: TIMEOUT after %d attempts", attempt)

    return False

def _connect_ros7(
    host,
    port,
    user,
    key_path,
    ssh_password="",
    debug=False,
):
    """
    Подключение по SSH (paramiko) для MikroTik.

    MikroTik 6/7 не отправляют server-sig-algs в KEXINIT,
    поэтому paramiko не может выбрать алгоритм подписи
    по метаданным сервера. Дефолт paramiko 4.x —
    rsa-sha2-512, который MikroTik отвергает.

    Проверено на реальном ROS 6.49.x:
      • rsa-sha2-512  → Authentication failed
      • rsa-sha2-256  → OK
      • ssh-rsa       → OK (legacy, для очень старых ROS)

    Поэтому rsa-sha2-512 выключен. Подпись ssh-rsa (SHA-1)
    paramiko 5 не умеет — для таких ROS есть путь ROS6
    (системный ssh). Ключ хоста ssh-rsa и старый обмен
    ключами принимает CompatTransport.
    """
    pkey = paramiko.RSAKey.from_private_key_file(key_path)

    if debug:
        _debug_log.debug(
            "ros7 connect: host=%s port=%s user=%s",
            host, port, user,
        )
        _debug_log.debug(
            "ros7 key: type=%s fingerprint_md5=%s",
            pkey.get_name(),
            pkey.get_fingerprint().hex(),
        )

    def _attempt(disabled_pubkeys):
        transport = CompatTransport(
            (host, port),
            disabled_algorithms={
                "pubkeys": list(disabled_pubkeys),
            },
        )

        try:
            transport.start_client(timeout=10)

            # Ключ хоста — до аутентификации, чтобы не
            # предъявлять наш ключ подменённому серверу.
            host_keys.verify_transport(transport, host, port)

            if debug:
                try:
                    opts = transport.get_security_options()
                    _debug_log.debug(
                        "ros7 disabled_pubkeys: %s",
                        disabled_pubkeys,
                    )
                    _debug_log.debug(
                        "ros7 kex: %s", list(opts.kex),
                    )
                    _debug_log.debug(
                        "ros7 ciphers: %s", list(opts.ciphers),
                    )
                    _debug_log.debug(
                        "ros7 digests: %s", list(opts.digests),
                    )
                    _debug_log.debug(
                        "ros7 server banner: %s",
                        transport.remote_version,
                    )
                    _debug_log.debug(
                        "ros7 server key type: %s",
                        transport.host_key_type,
                    )
                    try:
                        preferred = list(
                            transport._preferred_pubkeys
                        )
                        actual = [
                            a for a in preferred
                            if a not in disabled_pubkeys
                        ]
                        _debug_log.debug(
                            "ros7 phase: disabled=%s, "
                            "active_pubkeys=%s",
                            disabled_pubkeys, actual,
                        )
                    except Exception:
                        pass
                except Exception:
                    pass

            transport.auth_publickey(username=user, key=pkey)

            if not transport.is_authenticated():
                raise paramiko.AuthenticationException(
                    "RSA public-key authentication не выполнена"
                )

            client = paramiko.SSHClient()
            client._transport = transport
            return client

        except Exception:
            try:
                transport.close()
            except Exception:
                pass
            raise

    return _attempt(["rsa-sha2-512"])


def backup_router_ros7(router, local_dir, client=None):
    host = router["host"]
    user = router.get("username", "admin")
    port = router.get("port", 22)
    key = _get_key_path(router)
    ssh_password = router.get("ssh_pass", "")

    debug = bool(router.get("debug_connection"))

    logging.info(
        "ROS7 backup via Paramiko/SCP: %s:%s",
        host, port,
    )

    own_client = client is None

    try:
        if own_client:
            client = _connect_ros7(
                host=host,
                port=port,
                user=user,
                key_path=key,
                ssh_password=ssh_password,
                debug=debug,
            )
            logging.info(
                "ROS7 SSH authentication successful: %s",
                host,
            )

        os.makedirs(local_dir, exist_ok=True)

        def _exec_logged(command: str) -> tuple[int, str, str]:
            stdin, stdout, stderr = client.exec_command(command)
            exit_status = stdout.channel.recv_exit_status()
            out = stdout.read().decode(errors="ignore").strip()
            err = stderr.read().decode(errors="ignore").strip()

            if debug:
                _debug_log.debug(
                    "ros7 exec: %r → exit=%d stdout=%r stderr=%r",
                    command, exit_status, out, err
                )
            return exit_status, out, err

        # 1. Очистка старых файлов
        _exec_logged("/file remove auto-backup.backup")
        _exec_logged("/file remove auto-export.rsc")

        # 2. Создание backup
        exit_status, _, err = _exec_logged(
            "/system backup save name=auto-backup"
        )
        if exit_status != 0 or err:
            raise RuntimeError(
                f"Ошибка создания backup: exit={exit_status}, stderr={err or '<пусто>'}"
            )

        # 3. Создание export
        exit_status, _, err = _exec_logged(
            "/export file=auto-export"
        )
        if exit_status != 0 or err:
            raise RuntimeError(
                f"Ошибка создания export: exit={exit_status}, stderr={err or '<пусто>'}"
            )

        # 4. Скачивание файлов
        files_to_download = {
            "auto-backup.backup": "config.backup",
            "auto-export.rsc": "config.rsc",
        }
        
        if not download_files_with_retry(
            client,
            local_dir,
            files_to_download,
            timeout=90,
            debug=debug
        ):
            raise RuntimeError(
                "Не удалось скачать файлы backup/export за 90 секунд."
            )

        created_files = [
            os.path.join(local_dir, "config.backup"),
            os.path.join(local_dir, "config.rsc"),
        ]

        # 5. Уборка на роутере
        try:
            _exec_logged("/file remove auto-backup.backup")
            _exec_logged("/file remove auto-export.rsc")
        except Exception as exc:
            logging.warning(
                "Не удалось удалить временные файлы на %s: %s",
                host, exc,
            )

        logging.info("ROS7 backup saved: %s", host)
        return created_files

    finally:
        if own_client and client is not None:
            try:
                client.close()
            except Exception:
                pass


# =========================================================
# ОБЩАЯ ФУНКЦИЯ BACKUP
# =========================================================

def backup_router(router):
    """
    Резервное копирование MikroTik с кэшем версии RouterOS.

    Если у устройства есть поле ros_version — начинаем с
    него. Иначе — с ROS7 (историческое поведение).
    При успехе плагин записывает определённую версию в
    переданный словарь; plugin_manager сохранит её на диск.

    Смена версии не производится при auth-ошибке: ключ и
    логин те же, повторная попытка только засорит лог и
    логи MikroTik.
    """

    # PluginManager передаёт уже абсолютный путь. На случай
    # прямого вызова — то же общее правило (от HOME_DIR),
    # а не от папки плагина.
    from core.context import resolve_backup_path

    backup_path = str(resolve_backup_path(router["backup_path"]))
    os.makedirs(backup_path, exist_ok=True)

    host = router.get("host", "unknown")
    preferred = str(router.get("ros_version") or "").strip()

    # Порядок попыток: сначала закэшированная версия.
    if preferred == "6":
        order = ("6", "7")
    elif preferred == "7":
        order = ("7", "6")
    else:
        order = ("7", "6")  # историческое поведение

    if preferred:
        logging.info(
            "Кэш версии: RouterOS %s для %s",
            preferred, host,
        )

    last_error: Exception | None = None
    auth_error: Exception | None = None

    for i, version in enumerate(order):
        is_last = (i == len(order) - 1)

        try:
            if version == "7":
                files = _do_ros7(router, backup_path)
            else:
                files = _do_ros6(router, backup_path)

        except _AuthFailure as exc:
            # Ключ/логин могут не подойти по-разному в двух путях:
            #
            #   • ssh.exe (ROS6) проверяет ACL ключа и падает
            #     с "bad permissions", даже если сам ключ верный.
            #     Paramiko ACL не проверяет — путь ROS7 мог бы
            #     пройти.
            #
            #   • ssh.exe и paramiko по-разному выбирают подпись
            #     (paramiko — только rsa-sha2-256). Устройство,
            #     принимающее только rsa-sha2-256, провалит
            #     ROS6-путь с _AuthFailure, но ROS7-путь прошёл бы.
            #
            # Поэтому при auth fail пробуем ДРУГОЙ путь, но
            # запоминаем ошибку, чтобы при провале обоих было
            # что показать в логе и в финальном raise.
            auth_error = exc
            logging.warning(
                "ROS%s: auth fail (%s)%s",
                version, exc,
                "" if is_last else " — пробуем другой путь",
            )
            if not is_last:
                continue
            raise

        except _VersionMismatch as exc:
            last_error = exc
            logging.info(
                "ROS%s не подошёл (%s)%s",
                version, exc,
                "" if is_last else " — пробуем другой путь",
            )
            continue

        # Успех.
        if preferred != version:
            # Плагин возвращает копию через secure_router;
            # plugin_manager увидит расхождение и сохранит.
            router["ros_version"] = version
            logging.info(
                "Определён путь подключения: %s (%s)",
                "paramiko" if version == "7" else "ssh.exe",
                host,
            )

        return files

    # Ни ROS6, ни ROS7 не подошли.
    raise last_error or auth_error or RuntimeError(
        "Не удалось подключиться ни по ROS6, ни по ROS7"
    )

# Описание для ядра (контракт v1, core/plugin_api.py)
PLUGIN = {
    "api": 1,
    "title": "MikroTik RouterOS",
    "description": (
        "Экспорт конфигурации (.rsc) и бинарный бэкап (.backup); "
        "RouterOS 6 и 7 определяются сами. Вход по SSH-ключу."
    ),
    "auth": ["key", "password"],
    "default_username": "nbm-backup",
    "setup": [
        "Ключ: нажмите «🔑 Создать пару ключей» под полем «Файл SSH-ключа». "
        "Закрытый ключ останется в программе (`data/keys/mikrotik/`). "
        "Нажмите «⬇ Скачать .pub» — файл открытого ключа понадобится в шаге 4. "
        "Свой закрытый RSA-ключ без пароля тоже подойдёт: положите файл в "
        "`data/keys/mikrotik/` и укажите его имя.",
        "Группа (Winbox: System → Users → Groups → «+», или в терминале): "
        "`/user group add name=nbm-backup policy=ssh,ftp,read,write,sensitive,policy,test`. "
        "Зачем права: `ssh` — вход; `ftp` — скачивание файлов по SCP; `read` — "
        "чтение конфигурации; `write` — создание и удаление временных файлов; "
        "`sensitive` — пароли и ключи в export и backup; `policy` и `test` — "
        "нужны для `/system backup save`. `api` не нужен: программа работает по SSH.",
        "Пользователь (System → Users → «+»): "
        "`/user add name=nbm-backup group=nbm-backup password=<надёжный пароль>`. "
        "Право `policy` позволяет менять права пользователей, поэтому заведите "
        "отдельного пользователя и входите только по ключу.",
        "Открытый ключ: перетащите скачанный файл .pub в Winbox → Files, затем "
        "System → Users → SSH Keys → «Import SSH Key»: пользователь `nbm-backup`, "
        "файл ключа. Или в терминале: "
        "`/user ssh-keys import public-key-file=id_rsa.key.pub user=nbm-backup` "
        "(имя файла — как у скачанного).",
        "Проверьте, что SSH включён: IP → Services → ssh (порт по умолчанию 22; "
        "другой — укажите в поле «Порт SSH»).",
        "В этой форме: «Пользователь SSH» — `nbm-backup`, «Файл SSH-ключа» — "
        "имя ключа из шага 1 (по умолчанию `id_rsa.key`). После сохранения "
        "запустите бэкап устройства, чтобы проверить подключение.",
    ],
}


def run_backup(router):
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
    return backup_router(router)