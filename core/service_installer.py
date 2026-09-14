# =========================================================
# core/service_installer.py
# =========================================================

import ctypes
import base64
import logging
import os
import platform
import shlex
import socket
import subprocess
import sys
import time

from core.context import (
    BASE_DIR,
    CONFIG_FILE,
    load_json,
)


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

    Требуется Windows 8+ / Server 2012+ (модуль ScheduledTasks).
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
    """
    FIX: локальная копия get_web_config с валидацией.
    В идеале — вынести в core.context и переиспользовать.
    """

    config = load_json(CONFIG_FILE, {})

    host = config.get("host", "127.0.0.1")
    if not isinstance(host, str) or not host.strip():
        host = "127.0.0.1"

    raw_port = config.get("port", 8080)
    try:
        port = int(raw_port)
        if not (1 <= port <= 65535):
            raise ValueError("порт вне диапазона")
    except (TypeError, ValueError):
        logger.warning("Некорректный port в конфиге: %r", raw_port)
        port = 8080

    return host, port


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

def _get_windows_task_state() -> str:
    """
    Возвращает состояние задачи Windows.

    Возможные значения:
        running, ready, disabled, not_found, unknown

    Использует PowerShell: TaskState отдаёт стабильные
    английские значения (Running/Ready/Disabled/Queued)
    независимо от языка системы.
    """

    if not IS_WINDOWS:
        return "unknown"

    result = _run_powershell(
        f"(Get-ScheduledTask -TaskName {_ps_quote(SERVICE_NAME)} "
        f"-ErrorAction SilentlyContinue).State"
    )

    if result.returncode != 0:
        return "unknown"

    state = (result.stdout or "").strip().lower()

    if not state:
        return "not_found"

    if state in ("running", "queued"):
        return "running"
    if state == "ready":
        return "ready"
    if state == "disabled":
        return "disabled"

    return "unknown"


# =========================================================
# SERVICE STATUS
# =========================================================

def is_service_registered() -> bool:

    if IS_WINDOWS:
        result = _run_powershell(
            f"if (-not (Get-ScheduledTask "
            f"-TaskName {_ps_quote(SERVICE_NAME)} "
            f"-ErrorAction SilentlyContinue)) {{ exit 1 }}"
        )
        return result.returncode == 0

    if IS_LINUX:
        return os.path.exists(
            os.path.join("/etc/systemd/system", LINUX_UNIT)
        )

    return False


def is_os_service_running() -> bool:

    if IS_WINDOWS:
        return _get_windows_task_state() == "running"

    if IS_LINUX:
        result = _run(["systemctl", "is-active", LINUX_UNIT])
        return (result.stdout or "").strip() == "active"

    return False


def get_status() -> dict:
    """Полный статус службы для web-интерфейса."""

    registered = is_service_registered()
    running = is_os_service_running() if registered else False

    # FIX: task_state вычисляем один раз.
    if IS_WINDOWS:
        task_state = _get_windows_task_state()
    else:
        if running:
            task_state = "active"
        elif registered:
            task_state = "installed"
        else:
            task_state = "not_found"

    return {
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

    if not is_admin():
        return (
            False,
            "Для установки службы нужны права администратора "
            "(Windows) или root (Linux).",
        )

    if is_service_registered():
        return True, "Служба уже зарегистрирована."

    if IS_WINDOWS:
        return _install_windows_task()

    if IS_LINUX:
        return _install_linux_service()

    return False, f"ОС {platform.system()} не поддерживается."


# =========================================================
# START SERVICE
# =========================================================

def start_service() -> tuple[bool, str]:

    if not is_service_registered():
        return False, "Служба не зарегистрирована."

    # =====================================================
    # WINDOWS
    # =====================================================

    if IS_WINDOWS:
        result = _run_powershell(
            f"Start-ScheduledTask "
            f"-TaskName {_ps_quote(SERVICE_NAME)}"
        )
        if result.returncode == 0:
            return True, "Служба запущена."

        return (
            False,
            (result.stdout or result.stderr
             or "Не удалось запустить задачу Windows."),
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

    if not is_service_registered():
        return False, "Служба не зарегистрирована."

    # =====================================================
    # WINDOWS
    # =====================================================

    if IS_WINDOWS:
        result = _run_powershell(
            f"Stop-ScheduledTask "
            f"-TaskName {_ps_quote(SERVICE_NAME)}"
        )

        if result.returncode == 0:
            return True, "Служба остановлена."

        return (
            False,
            (result.stdout or result.stderr
             or "Не удалось остановить задачу Windows."),
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

        # Останавливаем задачу (может уже быть остановлена —
        # -ErrorAction SilentlyContinue подавляет ошибку).
        _run_powershell(
            f"Stop-ScheduledTask "
            f"-TaskName {_ps_quote(SERVICE_NAME)} "
            f"-ErrorAction SilentlyContinue"
        )
        time.sleep(1)

        result = _run_powershell(
            f"Unregister-ScheduledTask "
            f"-TaskName {_ps_quote(SERVICE_NAME)} "
            f"-Confirm:$false"
        )

        if result.returncode == 0:
            logger.info("Windows Task %s удалена.", SERVICE_NAME)
            return True, "Служба Windows удалена."

        return (
            False,
            (result.stdout or result.stderr
             or "Ошибка удаления задачи."),
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

    # -----------------------------------------------------
    # Если текущий процесс является службой,
    # останавливаем именно задачу.
    # -----------------------------------------------------

    if is_service_process():

        if IS_WINDOWS:
            ok = _spawn_powershell_detached(
                f"Start-Sleep -Seconds 2; "
                f"Stop-ScheduledTask "
                f"-TaskName {_ps_quote(SERVICE_NAME)}"
            )
            
            if not ok:
                return False, "Не удалось запустить отложенную остановку."

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
    # Если это интерактивный процесс.
    # -----------------------------------------------------

    if not _spawn_self_exit():
        return False, "Не удалось запустить отложенное завершение."

    return (
        True,
        "Текущий процесс будет остановлен через несколько секунд.",
    )


# =========================================================
# RESTART
# =========================================================

def schedule_restart() -> tuple[bool, str]:

    if not is_service_registered():
        return (
            False,
            "Служба не установлена — перезапуск недоступен.",
        )

    # =====================================================
    # WINDOWS
    # =====================================================

    if IS_WINDOWS:
        ok = _spawn_powershell_detached(
            f"Start-Sleep -Seconds 2; "
            f"Stop-ScheduledTask "
            f"-TaskName {_ps_quote(SERVICE_NAME)}; "
            f"Start-Sleep -Seconds 2; "
            f"Start-ScheduledTask "
            f"-TaskName {_ps_quote(SERVICE_NAME)}"
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

    if not is_service_registered():
        return False, "Служба не зарегистрирована."

    # =====================================================
    # WINDOWS
    # =====================================================

    if IS_WINDOWS:
        ok = _spawn_powershell_detached(
            f"Start-Sleep -Seconds 2; "
            f"Stop-ScheduledTask "
            f"-TaskName {_ps_quote(SERVICE_NAME)}; "
            f"Start-Sleep -Seconds 2; "
            f"Unregister-ScheduledTask "
            f"-TaskName {_ps_quote(SERVICE_NAME)} "
            f"-Confirm:$false"
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
# SELF EXIT
# =========================================================

def _spawn_self_exit() -> bool:

    pid = os.getpid()

    if IS_WINDOWS:
        return _spawn_powershell_detached(
            f"Start-Sleep -Seconds 2; "
            f"Stop-Process -Id {pid} -Force"
        )

    return _spawn_detached([
        "/bin/bash", "-c",
        f"sleep 2; kill {pid}",
    ])


# =========================================================
# WINDOWS TASK INSTALL
# =========================================================

def _install_windows_task() -> tuple[bool, str]:
    """
    Регистрирует задачу через Register-ScheduledTask.
    относительные пути внутри процесса
    службы будут резолвиться от каталога приложения, а не от
    C:\\Windows\\System32.
    """

    logger.info("Регистрация Windows Task: %s", SERVICE_NAME)

    executable, args, work_dir = get_service_command()

    logger.info("Executable: %s", executable)
    logger.info("Arguments: %s", args)
    logger.info("Working directory: %s", work_dir)

    exe_q = _ps_quote(executable)
    wd_q = _ps_quote(work_dir)
    name_q = _ps_quote(SERVICE_NAME)

    if args:
        arg_str = subprocess.list2cmdline(args)
        arg_part = f"-Argument {_ps_quote(arg_str)} "
    else:
        arg_part = ""

    script = (
        f"$action = New-ScheduledTaskAction "
        f"-Execute {exe_q} {arg_part}"
        f"-WorkingDirectory {wd_q}; "
        f"$trigger = New-ScheduledTaskTrigger -AtStartup; "
        f"$principal = New-ScheduledTaskPrincipal "
        f"-UserId 'SYSTEM' "
        f"-LogonType ServiceAccount "
        f"-RunLevel Highest; "
        f"$settings = New-ScheduledTaskSettingsSet "
        f"-AllowStartIfOnBatteries "
        f"-DontStopIfGoingOnBatteries "
        f"-StartWhenAvailable "
        f"-ExecutionTimeLimit ([TimeSpan]::Zero); "
        f"Register-ScheduledTask "
        f"-TaskName {name_q} "
        f"-Action $action "
        f"-Trigger $trigger "
        f"-Principal $principal "
        f"-Settings $settings "
        f"-Force | Out-Null"
    )

    result = _run_powershell(script)

    if result.returncode != 0:
        message = (
            (result.stderr or result.stdout or "").strip()
            or "Ошибка создания задачи Windows."
        )
        logger.error("Ошибка создания Windows Task: %s", message)
        return False, message

    logger.info("Windows Task %s успешно создана.", SERVICE_NAME)

    ok, start_message = start_service()

    if not ok:
        logger.warning(
            "Задача создана, но не запущена: %s", start_message,
        )
        return (
            True,
            f"Задача создана, но не запущена: {start_message}",
        )

    time.sleep(1)

    if is_os_service_running():
        return (
            True,
            "Служба Windows зарегистрирована и запущена от SYSTEM.",
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

def _install_linux_service() -> tuple[bool, str]:

    executable, args, work_dir = get_service_command()

    # FIX: проверяем, что рабочий каталог существует.
    if not os.path.isdir(work_dir):
        return False, f"Рабочий каталог не существует: {work_dir}"

    quoted = " ".join(
        _shell_quote(part) for part in [executable, *args]
    )

    unit_content = f"""[Unit]
Description={DESCRIPTION}
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory={work_dir}
ExecStart={quoted}
Restart=on-failure
RestartSec=5

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