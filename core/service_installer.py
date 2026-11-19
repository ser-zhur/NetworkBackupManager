# =========================================================
# core/service_installer.py
# =========================================================

import ctypes
import base64
import logging
import os
import platform
import shlex
import shutil
import sys
import socket
import subprocess
import threading
import time

from pathlib import Path
from core.context import (
    BASE_DIR,
    CONFIG_FILE,
    LOGS_DIR,
    load_json,
)

NSSM_LOCAL_PATH = BASE_DIR / "bin" / "nssm.exe"

logger = logging.getLogger(__name__)

SERVICE_NAME = "NetworkBackupManager"
LINUX_UNIT = f"{SERVICE_NAME.lower()}.service"
DESCRIPTION = (
    "Служба автоматического резервного "
    "копирования сетевого оборудования"
)

IS_WINDOWS = platform.system() == "Windows"
IS_LINUX = platform.system() == "Linux"

# FIX: таймаут для внешних команд, чтобы не подвиснуть навсегда.
SUBPROCESS_TIMEOUT = 30


# =========================================================
# PRIVILEGES
# =========================================================

def is_admin() -> bool:
    """Windows: Administrator. Linux: root."""

    try:
        if IS_WINDOWS:
            return ctypes.windll.shell32.IsUserAnAdmin() != 0
        return os.geteuid() == 0
    except Exception:
        logger.debug("is_admin: не удалось определить права", exc_info=True)
        return False


# =========================================================
# ВНЕШНЕЕ УПРАВЛЕНИЕ (Docker, install.sh)
# =========================================================

def _managed_refusal() -> tuple[bool, str] | None:
    """
    Отказ для действий со службой, которой управляет не
    программа (Docker, служба install.sh). None — можно.
    """
    from core import settings

    manager = settings.service_manager()
    if manager == "docker":
        return False, "В контейнере службой управляет Docker (docker compose stop / restart)."
    if manager == "systemd":
        return False, (
            f"Служба установлена install.sh — управляйте ею на сервере: "
            f"sudo systemctl stop|start {LINUX_UNIT}; "
            f"удаление — sudo ./install.sh --uninstall."
        )
    return None


# =========================================================
# SERVICE PROCESS
# =========================================================

def is_service_process() -> bool:
    """True, если приложение запущено с аргументом --service."""
    return "--service" in sys.argv


# =========================================================
# PATHS
# =========================================================

def get_service_command() -> tuple[str, list[str], str]:
    """
    Возвращает (executable, arguments, working_dir).

    PyInstaller:  NetworkBackupManager.exe --service
    Python:       python main.py --service
    """

    work_dir = str(BASE_DIR.resolve())
    executable = os.path.abspath(sys.executable)

    if getattr(sys, "frozen", False):
        return executable, ["--service"], work_dir

    main_py = str((BASE_DIR / "main.py").resolve())
    return executable, [main_py, "--service"], work_dir


# =========================================================
# NSSM
# =========================================================

def _ensure_nssm() -> Path | None:
    """
    Возвращает путь к постоянному nssm.exe.

    Порядок поиска:

      1. BASE_DIR/bin/nssm.exe — если уже извлечён.
      2. sys._MEIPASS/bin/nssm.exe — если приложение заморожено,
         копируем в BASE_DIR/bin/nssm.exe и возвращаем путь.
      3. nssm в PATH — fallback для разработки.

    Возвращает None, если nssm.exe найти не удалось.
    """

    if NSSM_LOCAL_PATH.exists():
        return NSSM_LOCAL_PATH

    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            bundled = Path(meipass) / "bin" / "nssm.exe"
            if bundled.exists():
                try:
                    NSSM_LOCAL_PATH.parent.mkdir(
                        parents=True,
                        exist_ok=True,
                    )
                    shutil.copy2(bundled, NSSM_LOCAL_PATH)
                    logger.info(
                        "nssm.exe извлечён: %s",
                        NSSM_LOCAL_PATH,
                    )
                    return NSSM_LOCAL_PATH
                except Exception as exc:
                    logger.error(
                        "Не удалось извлечь nssm.exe: %s",
                        exc,
                    )
                    return None

    which = shutil.which("nssm")
    if which:
        return Path(which)

    return None


def _run_nssm(args: list[str]) -> subprocess.CompletedProcess:
    """
    Вызывает nssm.exe с указанными аргументами.

    Использует ту же кодировку, что и _run, и подавляет окно
    консоли через _windows_flags.
    """

    nssm = _ensure_nssm()

    if nssm is None:
        raise RuntimeError(
            "nssm.exe не найден. "
            "Положите его в bin/nssm.exe рядом с приложением."
        )

    logger.debug("nssm: %r", [str(nssm), *args])

    return subprocess.run(
        [str(nssm), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=SUBPROCESS_TIMEOUT,
        creationflags=_windows_flags(),
    )

# =========================================================
# SUBPROCESS
# =========================================================

def _console_encoding() -> str:
    """
    FIX: на Windows используем mbcs вместо устаревшего cp866.
    На Linux — utf-8.
    """
    if IS_WINDOWS:
        return "mbcs"
    return "utf-8"


def _run(cmd, **kwargs) -> subprocess.CompletedProcess:
    """
    Запускает команду, возвращает CompletedProcess.
    FIX: добавлен timeout и корректная кодировка.
    """

    logger.debug("run: %r", cmd)

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding=_console_encoding(),
        errors="replace",  # FIX: не глотаем кириллицу
        timeout=kwargs.pop("timeout", SUBPROCESS_TIMEOUT),
        **kwargs,
    )

    if result.returncode != 0:
        logger.debug(
            "run вернул %s: stdout=%r stderr=%r",
            result.returncode,
            (result.stdout or "")[:500],
            (result.stderr or "")[:500],
        )

    return result

# =========================================================
# POWERSHELL
# =========================================================

def _ps_quote(value: str) -> str:
    """
    Экранирует строку для встраивания в PowerShell-литерал
    в одинарных кавычках.

    В PowerShell внутри '...' единственная спецпоследовательность —
    это '' (двойная одинарная кавычка), которая даёт одну '.
    """
    return "'" + value.replace("'", "''") + "'"


def _run_powershell(script: str) -> subprocess.CompletedProcess:
    """
    Выполняет PowerShell-скрипт через -EncodedCommand.

    -EncodedCommand принимает base64 от UTF-16LE, это полностью
    устраняет проблемы с кавычками и экранированием в командной
    строке. На выходе форсируем UTF-8, чтобы не зависеть от
    OEM-кодировки консоли (cp866 на русской Windows и т.п.).

    Требуется Windows 8+ / Server 2012+.
    """
    if not IS_WINDOWS:
        raise RuntimeError("PowerShell доступен только на Windows")

    preamble = (
        "$ErrorActionPreference = 'Stop'; "
        "[Console]::OutputEncoding = "
        "[System.Text.Encoding]::UTF8; "
    )

    encoded = base64.b64encode(
        (preamble + script).encode("utf-16-le")
    ).decode("ascii")

    logger.debug("powershell: %s", script)

    return subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy", "Bypass",
            "-EncodedCommand",
            encoded,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=SUBPROCESS_TIMEOUT,
        creationflags=_windows_flags(),
    )


def _spawn_powershell_detached(script: str) -> bool:
    """
    Запускает PowerShell-скрипт в отдельном процессе,
    не привязываясь к текущему. Вывод не читается.
    """
    if not IS_WINDOWS:
        return False

    encoded = base64.b64encode(
        script.encode("utf-16-le")
    ).decode("ascii")

    return _spawn_detached([
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy", "Bypass",
        "-EncodedCommand",
        encoded,
    ])

def _spawn_independent(script: str) -> bool:
    """
    Запускает PowerShell-скрипт через WMI (Win32_Process.Create),
    чтобы созданный процесс был дочерним по отношению к
    WmiPrvSE.exe, а не к нам.

    Такой процесс переживает остановку нашей службы: NSSM убивает
    только дерево процессов приложения, а WMI-процесс к нему не
    относится.

    Используется для schedule_uninstall, где нужно выполнить
    действия ПОСЛЕ остановки службы.
    """
    if not IS_WINDOWS:
        return False

    encoded = base64.b64encode(
        script.encode("utf-16-le")
    ).decode("ascii")

    powershell_cmd = subprocess.list2cmdline([
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy", "Bypass",
        "-EncodedCommand",
        encoded,
    ])

    wmi_script = (
        f"$r = Invoke-CimMethod -ClassName Win32_Process "
        f"-MethodName Create -Arguments @{{ "
        f"CommandLine = {_ps_quote(powershell_cmd)} "
        f"}}; "
        f"if ($r.ReturnValue -ne 0) {{ exit $r.ReturnValue }}"
    )

    try:
        result = _run_powershell(wmi_script)
        if result.returncode != 0:
            logger.error(
                "WMI spawn вернул %s: %s",
                result.returncode,
                (result.stderr or result.stdout or "")[:500],
            )
            return False
        return True
    except Exception as exc:
        logger.error("WMI spawn failed: %s", exc)
        return False


def _windows_flags() -> int:
    """
    FIX: убрано противоречие DETACHED_PROCESS | CREATE_NO_WINDOW.
    Оставляем CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW.
    """

    flags = 0
    flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
    flags |= getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    return flags


def _spawn_detached(cmd) -> bool:
    """
    Запускает процесс, не привязанный к текущему.
    FIX: возвращает bool и логирует ошибки.
    """

    kwargs = {}

    try:
        if IS_WINDOWS:
            kwargs["creationflags"] = _windows_flags()
            kwargs["close_fds"] = True
        else:
            kwargs["start_new_session"] = True

        subprocess.Popen(cmd, **kwargs)
        return True

    except Exception as exc:
        logger.error("Не удалось запустить %r: %s", cmd, exc)
        return False


# =========================================================
# HTTP
# =========================================================

def get_web_config() -> tuple[str, int]:
    """(host, port) — см. core.settings.web_config."""
    from core import settings
    return settings.web_config()


def wait_for_http(timeout: float = 20) -> bool:
    """Ждёт, пока HTTP-порт начнёт принимать соединения."""

    host, port = get_web_config()
    deadline = time.time() + timeout

    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1):
                return True
        except OSError:
            time.sleep(0.4)

    return False


def is_http_up() -> bool:
    """Быстрая проверка доступности HTTP (для health-check)."""
    return wait_for_http(timeout=0.8)


# =========================================================
# WINDOWS TASK STATE
# =========================================================

def _get_windows_service_state() -> str:
    """
    Возвращает состояние службы Windows.

    Возможные значения:
        running, stopped, startpending, stoppending,
        not_found, unknown

    Использует PowerShell: Get-Service возвращает стабильные
    английские значения Status независимо от локали.
    """

    if not IS_WINDOWS:
        return "unknown"

    result = _run_powershell(
        f"(Get-Service -Name {_ps_quote(SERVICE_NAME)} "
        f"-ErrorAction SilentlyContinue).Status"
    )

    if result.returncode != 0:
        return "unknown"

    state = (result.stdout or "").strip().lower()

    if not state:
        return "not_found"

    return state

# =========================================================
# SERVICE STATE SNAPSHOT
# =========================================================
#
# На Windows get_status() раньше делал три PowerShell-вызова
# подряд: is_service_registered + is_os_service_running +
# _get_windows_service_state. Собираем всё за один вызов.
# =========================================================

def _get_service_state_snapshot() -> tuple[bool, bool, str]:
    """
    Возвращает (registered, running, task_state) за один опрос.

    task_state: running, stopped, startpending, stoppending,
                not_found, unknown, active, installed
    """
    if IS_WINDOWS:
        state = _get_windows_service_state()
        return (
            state != "not_found",
            state == "running",
            state,
        )

    if IS_LINUX:
        registered = os.path.exists(
            os.path.join("/etc/systemd/system", LINUX_UNIT)
        )
        if not registered:
            return (False, False, "not_found")

        result = _run(["systemctl", "is-active", LINUX_UNIT])
        active = (result.stdout or "").strip() == "active"
        return (True, active, "active" if active else "installed")

    return (False, False, "unknown")

# =========================================================
# SERVICE STATUS
# =========================================================

def is_service_registered() -> bool:

    if IS_WINDOWS:
        return _get_windows_service_state() != "not_found"

    if IS_LINUX:
        return os.path.exists(
            os.path.join("/etc/systemd/system", LINUX_UNIT)
        )

    return False


def is_os_service_running() -> bool:

    if IS_WINDOWS:
        return _get_windows_service_state() == "running"

    if IS_LINUX:
        result = _run(["systemctl", "is-active", LINUX_UNIT])
        return (result.stdout or "").strip() == "active"

    return False

# =========================================================
# STATUS CACHE
# =========================================================
#
# get_status() вызывается на каждой отрисовке главной и
# /service (которая ещё и перезагружается каждые 10 секунд).
# Кэшируем на STATUS_CACHE_TTL секунд — этого достаточно,
# чтобы убрать основную часть вызовов, и при этом
# пользователь не видит устаревших данных.
# =========================================================

STATUS_CACHE_TTL = 3.0

_status_cache_lock = threading.Lock()
_status_cache_value: dict | None = None
_status_cache_time: float = 0.0


def invalidate_status_cache() -> None:
    """
    Сбросить кэш статуса службы.

    Вызывайте после действий, меняющих состояние службы
    (install/start/stop/restart/uninstall), чтобы UI сразу
    увидел свежие данные, а не ждал STATUS_CACHE_TTL.
    """
    global _status_cache_value, _status_cache_time

    with _status_cache_lock:
        _status_cache_value = None
        _status_cache_time = 0.0


def get_status(use_cache: bool = True) -> dict:
    """Полный статус службы для web-интерфейса.

    Результат кэшируется на STATUS_CACHE_TTL секунд.
    use_cache=False — получить свежие данные принудительно.
    """

    global _status_cache_value, _status_cache_time

    now = time.time()

    if use_cache:
        with _status_cache_lock:
            if (
                _status_cache_value is not None
                and (now - _status_cache_time) < STATUS_CACHE_TTL
            ):
                return _status_cache_value

    from core import settings

    manager = settings.service_manager()

    # В контейнере службой управляет Docker: кнопки службы
    # не показываем, systemctl/nssm не вызываем.
    if manager == "docker":
        result = {
            "managed_by": "docker",
            "registered": False,
            "os_running": False,
            "http_up": True,
            "service_mode": is_service_process(),
            "container": True,
            "can_install": False,
            "can_restart": False,
            "can_stop": False,
            "can_start": False,
            "can_uninstall": False,
            "is_admin": is_admin(),
            "task_state": "container",
            "service_name": SERVICE_NAME,
            "base_dir": str(BASE_DIR.resolve()),
        }
        with _status_cache_lock:
            _status_cache_value = result
            _status_cache_time = now
        return result

    # ---- один опрос вместо трёх ----
    registered, running, task_state = _get_service_state_snapshot()

    # Служба install.sh: состояние показываем, перезапуск —
    # самозавершением (systemd поднимет), остальное — systemctl.
    if manager == "systemd":
        result = {
            "managed_by": "systemd",
            "container": False,
            "registered": registered,
            "os_running": running,
            "http_up": True,
            "service_mode": True,
            "can_install": False,
            "can_restart": True,
            "can_stop": False,
            "can_start": False,
            "can_uninstall": False,
            "is_admin": is_admin(),
            "task_state": task_state,
            "service_name": LINUX_UNIT,
            "base_dir": str(BASE_DIR.resolve()),
        }
        with _status_cache_lock:
            _status_cache_value = result
            _status_cache_time = now
        return result

    result = {
        "managed_by": "",
        "container": False,
        "registered": registered,
        "os_running": running,
        "http_up": is_http_up(),
        "service_mode": is_service_process(),
        "can_install": not registered,
        "can_restart": registered,
        "can_stop": registered or is_service_process(),
        "can_start": registered and not running,
        "can_uninstall": registered,
        "is_admin": is_admin(),
        "task_state": task_state,
        "service_name": SERVICE_NAME,
        "base_dir": str(BASE_DIR.resolve()),
    }

    with _status_cache_lock:
        _status_cache_value = result
        _status_cache_time = now

    return result
# =========================================================
# ELEVATION
# =========================================================

def relaunch_elevated(args: list[str]) -> bool:
    """Запускает новый процесс с правами администратора."""

    # =====================================================
    # WINDOWS
    # =====================================================

    if IS_WINDOWS:

        executable = os.path.abspath(sys.executable)
        work_dir = str(BASE_DIR.resolve())

        if getattr(sys, "frozen", False):
            parameters = subprocess.list2cmdline(args)
        else:
            main_py = str((BASE_DIR / "main.py").resolve())
            parameters = subprocess.list2cmdline([main_py, *args])

        try:
            result = ctypes.windll.shell32.ShellExecuteW(
                None,
                "runas",
                executable,
                parameters,
                work_dir,
                1,
            )
            return int(result) > 32
        except Exception as exc:
            logger.error("ShellExecuteW failed: %s", exc)
            return False

    # =====================================================
    # LINUX
    # =====================================================

    if IS_LINUX:

        command = [sys.executable]
        if not getattr(sys, "frozen", False):
            command.append(str((BASE_DIR / "main.py").resolve()))
        command.extend(args)

        for wrapper in ("pkexec", "sudo"):
            try:
                result = subprocess.run(
                    [wrapper, *command],
                    cwd=str(BASE_DIR.resolve()),
                    timeout=SUBPROCESS_TIMEOUT,
                )
                return result.returncode == 0
            except FileNotFoundError:
                continue
            except subprocess.TimeoutExpired:
                logger.warning("%s превысил таймаут", wrapper)
                return False

    return False


# =========================================================
# INSTALL SERVICE
# =========================================================

def install_service() -> tuple[bool, str]:

    refusal = _managed_refusal()
    if refusal:
        return refusal

    if not is_admin():
        return (
            False,
            "Для установки службы нужны права администратора "
            "(Windows) или root (Linux).",
        )

    if is_service_registered():
        return True, "Служба уже зарегистрирована."

    if IS_WINDOWS:
        return _install_windows_service()

    if IS_LINUX:
        return _install_linux_service()

    return False, f"ОС {platform.system()} не поддерживается."


# =========================================================
# START SERVICE
# =========================================================

def start_service() -> tuple[bool, str]:

    refusal = _managed_refusal()
    if refusal:
        return refusal

    if not is_service_registered():
        return False, "Служба не зарегистрирована."

    # =====================================================
    # WINDOWS
    # =====================================================

    if IS_WINDOWS:
        result = _run_powershell(
            f"Start-Service -Name {_ps_quote(SERVICE_NAME)}"
        )
        if result.returncode == 0:
            return True, "Служба запущена."

        return (
            False,
            (result.stdout or result.stderr
             or "Не удалось запустить службу."),
        )

    # =====================================================
    # LINUX
    # =====================================================

    if IS_LINUX:
        result = _run([
            "systemctl", "start", LINUX_UNIT,
        ])

        if result.returncode == 0:
            return True, "Служба запущена."

        return (
            False,
            (result.stdout or result.stderr
             or "Не удалось запустить службу systemd."),
        )

    return False, "Неподдерживаемая ОС."


# =========================================================
# STOP REGISTERED SERVICE
# =========================================================

def stop_service() -> tuple[bool, str]:

    refusal = _managed_refusal()
    if refusal:
        return refusal

    if not is_service_registered():
        return False, "Служба не зарегистрирована."

    # =====================================================
    # WINDOWS
    # =====================================================

    if IS_WINDOWS:
        result = _run_powershell(
            f"Stop-Service -Name {_ps_quote(SERVICE_NAME)} -Force"
        )

        if result.returncode == 0:
            return True, "Служба остановлена."

        return (
            False,
            (result.stdout or result.stderr
             or "Не удалось остановить службу."),
        )

    # =====================================================
    # LINUX
    # =====================================================

    if IS_LINUX:
        result = _run([
            "systemctl", "stop", LINUX_UNIT,
        ])

        if result.returncode == 0:
            return True, "Служба остановлена."

        return (
            False,
            (result.stdout or result.stderr
             or "Не удалось остановить службу systemd."),
        )

    return False, "Неподдерживаемая ОС."


# =========================================================
# UNINSTALL SERVICE
# =========================================================

def uninstall_service() -> tuple[bool, str]:

    refusal = _managed_refusal()
    if refusal:
        return refusal

    if not is_admin():
        return (
            False,
            "Для удаления службы нужны права администратора "
            "или root.",
        )

    # =====================================================
    # WINDOWS
    # =====================================================
    
    if IS_WINDOWS:

        # Останавливаем службу, если запущена.
        _run_powershell(
            f"Stop-Service -Name {_ps_quote(SERVICE_NAME)} "
            f"-Force -ErrorAction SilentlyContinue"
        )
        time.sleep(1)

        try:
            result = _run_nssm(["remove", SERVICE_NAME, "confirm"])
        except RuntimeError as exc:
            return False, str(exc)

        if result.returncode == 0:
            logger.info("Служба %s удалена.", SERVICE_NAME)
            return True, "Служба Windows удалена."

        return (
            False,
            (result.stdout or result.stderr
             or "Ошибка удаления службы."),
        )
    
    # =====================================================
    # LINUX
    # =====================================================

    if IS_LINUX:

        _run(["systemctl", "stop", LINUX_UNIT])
        _run(["systemctl", "disable", LINUX_UNIT])

        service_file = os.path.join(
            "/etc/systemd/system", LINUX_UNIT,
        )

        try:
            if os.path.exists(service_file):
                os.remove(service_file)
        except Exception as exc:
            return False, f"Ошибка удаления unit-файла: {exc}"

        _run(["systemctl", "daemon-reload"])

        return True, "Служба Linux удалена."

    return False, "Неподдерживаемая ОС."


# =========================================================
# STOP CURRENT PROCESS / SERVICE
# =========================================================
def schedule_stop() -> tuple[bool, str]:

    refusal = _managed_refusal()
    if refusal:
        return refusal

    # -----------------------------------------------------
    # Если текущий процесс — служба, останавливаем службу.
    # -----------------------------------------------------

    if is_service_process():

        if IS_WINDOWS:
            # Завершаем процесс с кодом 0. NSSM видит код 0
            # и, по правилу AppExit 0 Exit, останавливает службу.
            # Никаких Restart-Service из detached-процесса —
            # этот процесс был бы убит вместе с деревом службы.
            def _exit_for_stop():
                time.sleep(2)
                logging.info(
                    "Остановка службы через NSSM (exit code 0)"
                )
                logging.shutdown()
                os._exit(0)

            threading.Thread(
                target=_exit_for_stop,
                daemon=True,
            ).start()

            return (
                True,
                "Служба будет остановлена через несколько секунд. "
                "Панель станет недоступна.",
            )

        if IS_LINUX:
            ok = _spawn_detached([
                "/bin/bash", "-c",
                f"sleep 2; systemctl stop {LINUX_UNIT}",
            ])
            if not ok:
                return False, "Не удалось запустить отложенную остановку."

            return (
                True,
                "Служба будет остановлена через несколько секунд. "
                "Панель станет недоступна.",
            )

    # -----------------------------------------------------
    # Интерактивный процесс не может остановить службу напрямую.
    # server.py в этом случае должен идти через UAC.
    # -----------------------------------------------------

    if is_service_registered():
        return (
            False,
            "Остановка службы доступна только из режима службы "
            "или с повышенными правами.",
        )

    return False, "Служба не зарегистрирована."

# =========================================================
# RESTART
# =========================================================

def schedule_restart() -> tuple[bool, str]:

    from core import settings

    manager = settings.service_manager()
    if manager == "docker":
        return _managed_refusal()

    # Служба install.sh: программа работает не от root и
    # systemctl restart вызвать не может. Завершаемся с кодом 1 —
    # systemd (Restart=on-failure) запустит её заново.
    if manager == "systemd":

        def _exit_for_restart():
            time.sleep(2)
            logging.info("Перезапуск службы: выход с кодом 1, systemd запустит снова")
            logging.shutdown()
            os._exit(1)

        threading.Thread(target=_exit_for_restart, daemon=True).start()
        return (
            True,
            "Служба перезапускается. Подождите несколько секунд "
            "и обновите страницу.",
        )

    if not is_service_registered():
        return (
            False,
            "Служба не установлена — перезапуск недоступен.",
        )

    # =====================================================
    # WINDOWS
    # =====================================================

    if IS_WINDOWS:

        # -------------------------------------------------
        # Мы сами служба. Restart-Service из процесса службы
        # не работает: NSSM убьёт дерево процессов до того,
        # как Start-Service успеет выполниться.
        #
        # Вместо этого завершаем процесс с кодом 1.
        # NSSM по правилу AppExit Default Restart сам
        # перезапустит приложение, а служба останется Running.
        # -------------------------------------------------

        if is_service_process():

            def _exit_for_restart():
                time.sleep(2)
                logging.info(
                    "Перезапуск службы через NSSM (exit code 1)"
                )
                logging.shutdown()
                os._exit(1)

            threading.Thread(
                target=_exit_for_restart,
                daemon=True,
            ).start()

            return (
                True,
                "Служба перезапускается. Подождите несколько секунд "
                "и обновите страницу.",
            )

        # -------------------------------------------------
        # Мы внешний процесс (например, UAC-копия).
        # Можем спокойно вызвать Restart-Service — нас
        # при остановке службы никто не тронет.
        # -------------------------------------------------

        ok = _spawn_powershell_detached(
            f"Start-Sleep -Seconds 2; "
            f"Restart-Service "
            f"-Name {_ps_quote(SERVICE_NAME)} -Force"
        )

        if not ok:
            return False, "Не удалось запустить отложенный перезапуск."

        return (
            True,
            "Служба перезапускается. Подождите несколько секунд "
            "и обновите страницу.",
        )

    # =====================================================
    # LINUX
    # =====================================================

    if IS_LINUX:
        ok = _spawn_detached([
            "/bin/bash", "-c",
            f"sleep 2; systemctl restart {LINUX_UNIT}",
        ])
        if not ok:
            return False, "Не удалось запустить отложенный перезапуск."

        return (
            True,
            "Служба перезапускается. Подождите несколько секунд "
            "и обновите страницу.",
        )

    return False, "Неподдерживаемая ОС."


# =========================================================
# UNINSTALL (отложенное удаление)
# =========================================================

def schedule_uninstall() -> tuple[bool, str]:
    """
    Отложенное удаление службы.

    Используется, когда команду удаления нужно выполнить из
    самого процесса службы: прямая остановка задачи убила бы
    нас до отправки HTTP-ответа.
    """

    refusal = _managed_refusal()
    if refusal:
        return refusal

    if not is_service_registered():
        return False, "Служба не зарегистрирована."

    # =====================================================
    # WINDOWS
    # =====================================================

    if IS_WINDOWS:
        nssm = _ensure_nssm()

        if nssm is None:
            return False, "nssm.exe не найден."

        # Используем WMI, потому что остановка службы убьёт всё
        # дерево процессов приложения. Обычный detached-PowerShell
        # до команды `nssm remove` дожить не успеет.
        ok = _spawn_independent(
            f"Start-Sleep -Seconds 3; "
            f"Stop-Service "
            f"-Name {_ps_quote(SERVICE_NAME)} "
            f"-Force -ErrorAction SilentlyContinue; "
            f"Start-Sleep -Seconds 2; "
            f"& '{str(nssm)}' remove {SERVICE_NAME} confirm"
        )
        if not ok:
            return False, "Не удалось запустить отложенное удаление."

        return (
            True,
            "Служба будет удалена через несколько секунд. "
            "Панель станет недоступна.",
        )
    # =====================================================
    # LINUX
    # =====================================================

    if IS_LINUX:
        ok = _spawn_detached([
            "/bin/bash", "-c",
            (
                "sleep 2; "
                f"systemctl stop {LINUX_UNIT}; "
                f"systemctl disable {LINUX_UNIT}; "
                f"rm -f /etc/systemd/system/{LINUX_UNIT}; "
                "systemctl daemon-reload"
            ),
        ])
        if not ok:
            return False, "Не удалось запустить отложенное удаление."

        return (
            True,
            "Служба будет удалена через несколько секунд. "
            "Панель станет недоступна.",
        )

    return False, "Неподдерживаемая ОС."

# =========================================================
# WINDOWS SERVICE INSTALL (NSSM)
# =========================================================

def _install_windows_service() -> tuple[bool, str]:

    nssm = _ensure_nssm()

    if nssm is None:
        return False, (
            "nssm.exe не найден. "
            "Положите его в bin/nssm.exe рядом с приложением."
        )

    executable, args, work_dir = get_service_command()

    logger.info("Установка службы через NSSM: %s", executable)
    logger.info("Arguments: %s", args)
    logger.info("Working directory: %s", work_dir)

    # -----------------------------------------------------
    # Регистрация службы
    # -----------------------------------------------------

    try:
        r = _run_nssm([
            "install",
            SERVICE_NAME,
            str(executable),
            *args,
        ])
    except RuntimeError as exc:
        return False, str(exc)

    if r.returncode != 0:
        message = (
            (r.stderr or r.stdout or "").strip()
            or "nssm install failed"
        )
        logger.error("Ошибка nssm install: %s", message)
        return False, message

    # -----------------------------------------------------
    # Настройка параметров службы
    # -----------------------------------------------------

    logs_dir = str(LOGS_DIR.resolve())

    # Критичные параметры: если не удалось — откатываем регистрацию.
    critical_settings = [
        ("AppDirectory", work_dir),
        ("Start", "SERVICE_AUTO_START"),
        # Exit code 0 = нормальное завершение, служба останавливается.
        ("AppExit", "0", "Exit"),
        # Любой другой exit code = перезапустить приложение.
        ("AppExit", "Default", "Restart"),
    ]

    # Некритичные: только предупреждение.
    optional_settings = [
        ("Description", DESCRIPTION),
        ("AppRestartDelay", "5000"),
        ("AppThrottle", "1500"),
        ("AppStdout", os.path.join(logs_dir, "service_stdout.log")),
        ("AppStderr", os.path.join(logs_dir, "service_stderr.log")),
        ("AppRotateFiles", "1"),
        ("AppRotateBytes", str(10 * 1024 * 1024)),
        ("AppRotateOnline", "1"),
    ]

    for entry in critical_settings:
        try:
            rr = _run_nssm(["set", SERVICE_NAME, *entry])
        except RuntimeError as exc:
            _run_nssm(["remove", SERVICE_NAME, "confirm"])
            return False, f"nssm set {entry[0]}: {exc}"

        if rr.returncode != 0:
            _run_nssm(["remove", SERVICE_NAME, "confirm"])
            return False, (
                f"nssm set {entry[0]}: "
                + (rr.stderr or rr.stdout or "").strip()
            )

    for entry in optional_settings:
        try:
            rr = _run_nssm(["set", SERVICE_NAME, *entry])
        except RuntimeError as exc:
            logger.warning("nssm set %s: %s", entry[0], exc)
            continue

        if rr.returncode != 0:
            logger.warning(
                "nssm set %s: %s",
                entry[0],
                (rr.stderr or rr.stdout or "").strip(),
            )

    # -----------------------------------------------------
    # Запуск службы
    # -----------------------------------------------------

    ok, start_message = start_service()

    if not ok:
        logger.warning(
            "Служба создана, но не запущена: %s",
            start_message,
        )
        return (
            True,
            f"Служба создана, но не запущена: {start_message}",
        )

    time.sleep(1)

    if is_os_service_running():
        return (
            True,
            "Служба Windows зарегистрирована и запущена.",
        )

    return (
        True,
        "Служба Windows зарегистрирована. "
        "Команда запуска отправлена.",
    )

# =========================================================
# SHELL QUOTING
# =========================================================

def _shell_quote(value: str) -> str:
    """Квотирование аргумента для командной строки."""

    if IS_WINDOWS:
        return subprocess.list2cmdline([value])

    return shlex.quote(value)


# =========================================================
# LINUX SYSTEMD
# =========================================================

def _unit_environment() -> str:
    """
    Environment= для переменных NBM_*, заданных при установке
    (NBM_HOME, NBM_PORT, …): служба запускается с теми же
    настройками, что и установщик. Пароль не переносим —
    он нужен только при первом запуске.
    """
    lines = []
    for name in sorted(os.environ):
        if not name.startswith("NBM_") or name == "NBM_ADMIN_PASSWORD":
            continue
        value = (
            os.environ[name]
            .replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("%", "%%")          # спецификаторы systemd
        )
        lines.append(f'Environment="{name}={value}"\n')
    return "".join(lines)


def _install_linux_service() -> tuple[bool, str]:

    executable, args, work_dir = get_service_command()

    # FIX: проверяем, что рабочий каталог существует.
    if not os.path.isdir(work_dir):
        return False, f"Рабочий каталог не существует: {work_dir}"

    quoted = " ".join(
        _shell_quote(part) for part in [executable, *args]
    )

    # KillMode=process — при остановке службы systemd убивает
    # только главный процесс, а не всю cgroup. Это критично для
    # отложенных действий (schedule_stop, schedule_restart): они
    # запускают bash-скрипт, который должен пережить остановку
    # основного приложения и вызвать systemctl stop/restart.
    #
    # Без KillMode=process systemd отправит SIGTERM всей cgroup
    # и убьёт отложенный скрипт до того, как он сработает.
    unit_content = f"""[Unit]
Description={DESCRIPTION}
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory={work_dir}
{_unit_environment()}ExecStart={quoted}
Restart=on-failure
RestartSec=5
KillMode=process

[Install]
WantedBy=multi-user.target
"""

    service_file = os.path.join(
        "/etc/systemd/system", LINUX_UNIT,
    )

    logger.info("Создание systemd unit: %s", service_file)

    try:
        with open(service_file, "w", encoding="utf-8") as f:
            f.write(unit_content)

        daemon = _run(["systemctl", "daemon-reload"])
        if daemon.returncode != 0:
            # FIX: откатываем unit-файл при неудаче.
            try:
                os.remove(service_file)
            except OSError:
                pass
            return (
                False,
                (daemon.stderr or daemon.stdout
                 or "Ошибка systemctl daemon-reload."),
            )

        enable = _run(["systemctl", "enable", LINUX_UNIT])
        if enable.returncode != 0:
            try:
                os.remove(service_file)
                _run(["systemctl", "daemon-reload"])
            except OSError:
                pass
            return (
                False,
                (enable.stderr or enable.stdout
                 or "Ошибка systemctl enable."),
            )

        start = _run(["systemctl", "start", LINUX_UNIT])
        if start.returncode != 0:
            return (
                False,
                (start.stderr or start.stdout
                 or "Ошибка systemctl start."),
            )

        return (
            True,
            "Служба systemd создана, включена в автозагрузку "
            "и запущена.",
        )

    except Exception as exc:
        logger.error("Ошибка установки службы Linux: %s", exc)
        return False, str(exc)