import os
import sys
import time
import socket
import ctypes
import logging
import platform
import subprocess


from core.context import (
    BASE_DIR,
    CONFIG_FILE,
    load_json,
)


SERVICE_NAME = "NetworkBackupManager"

LINUX_UNIT = (
    f"{SERVICE_NAME.lower()}.service"
)

DESCRIPTION = (
    "Служба автоматического резервного "
    "копирования сетевого оборудования"
)


# =========================================================
# PRIVILEGES
# =========================================================

def is_admin() -> bool:
    """
    Проверяет административные права.

    Windows:
        Administrator

    Linux:
        root
    """

    try:

        if platform.system() == "Windows":

            return (
                ctypes.windll.shell32.IsUserAnAdmin()
                != 0
            )

        return os.geteuid() == 0

    except Exception:

        return False


# =========================================================
# SERVICE PROCESS
# =========================================================

def is_service_process() -> bool:
    """
    True, если приложение запущено
    с аргументом --service.
    """

    return "--service" in sys.argv


# =========================================================
# PATHS
# =========================================================

def get_application_executable() -> str:
    """
    Возвращает путь к исполняемому файлу
    приложения.

    Для PyInstaller:
        NetworkBackupManager.exe

    Для запуска из Python:
        python.exe
    """

    if getattr(
        sys,
        "frozen",
        False,
    ):

        return os.path.abspath(
            sys.executable
        )

    return os.path.abspath(
        sys.executable
    )


def get_service_command() -> tuple[
    str,
    list[str],
    str,
]:
    """
    Возвращает:

        executable
        arguments
        working directory

    В режиме PyInstaller:

        NetworkBackupManager.exe --service

    В режиме Python:

        python main.py --service
    """

    work_dir = str(
        BASE_DIR.resolve()
    )

    if getattr(
        sys,
        "frozen",
        False,
    ):

        return (
            os.path.abspath(
                sys.executable
            ),
            [
                "--service"
            ],
            work_dir,
        )

    return (
        os.path.abspath(
            sys.executable
        ),
        [
            str(
                (
                    BASE_DIR
                    / "main.py"
                ).resolve()
            ),
            "--service",
        ],
        work_dir,
    )


# =========================================================
# SUBPROCESS
# =========================================================

def _run(
    cmd,
    **kwargs,
):

    encoding = (
        "cp866"
        if platform.system() == "Windows"
        else "utf-8"
    )

    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding=encoding,
        errors="ignore",
        **kwargs,
    )


def _windows_flags():

    return (
        getattr(
            subprocess,
            "DETACHED_PROCESS",
            0x00000008,
        )
        |
        getattr(
            subprocess,
            "CREATE_NEW_PROCESS_GROUP",
            0x00000200,
        )
        |
        getattr(
            subprocess,
            "CREATE_NO_WINDOW",
            0x08000000,
        )
    )


def _spawn_detached(
    cmd,
):

    kwargs = {}

    if platform.system() == "Windows":

        kwargs["creationflags"] = (
            _windows_flags()
        )

        kwargs["close_fds"] = True

    else:

        kwargs["start_new_session"] = True

    subprocess.Popen(
        cmd,
        **kwargs,
    )


# =========================================================
# HTTP
# =========================================================

def wait_for_http(
    timeout: float = 20,
) -> bool:

    config = load_json(
        CONFIG_FILE,
        {},
    )

    host = config.get(
        "host",
        "127.0.0.1",
    )

    port = int(
        config.get(
            "port",
            8080,
        )
    )

    deadline = (
        time.time()
        + timeout
    )

    while (
        time.time() < deadline
    ):

        try:

            with socket.create_connection(
                (
                    host,
                    port,
                ),
                timeout=1,
            ):

                return True

        except OSError:

            time.sleep(0.4)

    return False


def is_http_up() -> bool:

    return wait_for_http(
        timeout=0.8
    )


# =========================================================
# WINDOWS TASK HELPERS
# =========================================================

def _get_windows_task_state():
    """
    Возвращает состояние задачи Windows.

    Возможные значения:

        running
        ready
        disabled
        not_found
        unknown
    """

    if platform.system() != "Windows":

        return "unknown"

    result = _run(
        [
            "schtasks",
            "/query",
            "/tn",
            SERVICE_NAME,
            "/fo",
            "LIST",
            "/v",
        ]
    )

    if result.returncode != 0:

        return "not_found"

    text = (
        f"{result.stdout}\n"
        f"{result.stderr}"
    ).lower()

    # Английская Windows
    if (
        "running" in text
        or "выполняется" in text
        or "запущена" in text
        or "работает" in text
    ):

        return "running"

    if (
        "ready" in text
        or "готова" in text
    ):

        return "ready"

    if (
        "disabled" in text
        or "отключена" in text
    ):

        return "disabled"

    return "unknown"


# =========================================================
# SERVICE STATUS
# =========================================================

def is_service_registered() -> bool:

    system = platform.system()

    # -----------------------------------------------------
    # WINDOWS
    # -----------------------------------------------------

    if system == "Windows":

        result = _run(
            [
                "schtasks",
                "/query",
                "/tn",
                SERVICE_NAME,
            ]
        )

        return (
            result.returncode == 0
        )

    # -----------------------------------------------------
    # LINUX
    # -----------------------------------------------------

    if system == "Linux":

        return os.path.exists(
            os.path.join(
                "/etc/systemd/system",
                LINUX_UNIT,
            )
        )

    return False


def is_os_service_running() -> bool:

    system = platform.system()

    # -----------------------------------------------------
    # WINDOWS
    # -----------------------------------------------------

    if system == "Windows":

        state = (
            _get_windows_task_state()
        )

        return (
            state == "running"
        )

    # -----------------------------------------------------
    # LINUX
    # -----------------------------------------------------

    if system == "Linux":

        result = _run(
            [
                "systemctl",
                "is-active",
                LINUX_UNIT,
            ]
        )

        return (
            (
                result.stdout
                or ""
            ).strip()
            == "active"
        )

    return False


def get_status() -> dict:
    """
    Полный статус службы для web-интерфейса.
    """

    registered = (
        is_service_registered()
    )

    running = (
        is_os_service_running()
        if registered
        else False
    )

    return {
        "registered":
            registered,

        "os_running":
            running,

        "http_up":
            is_http_up(),

        "service_mode":
            is_service_process(),

        "can_install":
            not registered,

        "can_restart":
            registered,

        "can_stop":
            registered
            or is_service_process(),

        "can_start":
            registered
            and not running,

        "can_uninstall":
            registered,

        "is_admin":
            is_admin(),

        "task_state":
            (
                _get_windows_task_state()
                if platform.system()
                == "Windows"
                else (
                    "active"
                    if running
                    else (
                        "installed"
                        if registered
                        else "not_found"
                    )
                )
            ),

        "service_name":
            SERVICE_NAME,

        "base_dir":
            str(
                BASE_DIR.resolve()
            ),
    }


# =========================================================
# ELEVATION
# =========================================================

def relaunch_elevated(
    args: list[str],
) -> bool:
    """
    Запускает новый процесс с правами
    администратора.

    Windows:
        UAC

    Linux:
        pkexec / sudo
    """

    system = platform.system()

    # =====================================================
    # WINDOWS
    # =====================================================

    if system == "Windows":

        if getattr(
            sys,
            "frozen",
            False,
        ):

            executable = os.path.abspath(
                sys.executable
            )

            parameters = (
                subprocess.list2cmdline(
                    args
                )
            )

        else:

            executable = os.path.abspath(
                sys.executable
            )

            parameters = (
                subprocess.list2cmdline(
                    [
                        str(
                            (
                                BASE_DIR
                                / "main.py"
                            ).resolve()
                        )
                    ]
                    + args
                )
            )

        result = (
            ctypes.windll.shell32
            .ShellExecuteW(
                None,
                "runas",
                executable,
                parameters,
                str(
                    BASE_DIR.resolve()
                ),
                1,
            )
        )

        return (
            int(result) > 32
        )

    # =====================================================
    # LINUX
    # =====================================================

    if system == "Linux":

        command = [
            sys.executable
        ]

        if not getattr(
            sys,
            "frozen",
            False,
        ):

            command.append(
                str(
                    (
                        BASE_DIR
                        / "main.py"
                    ).resolve()
                )
            )

        command.extend(
            args
        )

        for wrapper in (
            "pkexec",
            "sudo",
        ):

            try:

                result = subprocess.run(
                    [
                        wrapper,
                        *command,
                    ],
                    cwd=str(
                        BASE_DIR.resolve()
                    ),
                )

                return (
                    result.returncode
                    == 0
                )

            except FileNotFoundError:

                continue

    return False


# =========================================================
# INSTALL SERVICE
# =========================================================

def install_service() -> tuple[
    bool,
    str,
]:

    if not is_admin():

        return (
            False,
            (
                "Для установки службы нужны "
                "права администратора "
                "(Windows) или root (Linux)."
            ),
        )

    if is_service_registered():

        return (
            True,
            "Служба уже зарегистрирована.",
        )

    system = platform.system()

    if system == "Windows":

        return (
            _install_windows_task()
        )

    if system == "Linux":

        return (
            _install_linux_service()
        )

    return (
        False,
        f"ОС {system} не поддерживается.",
    )


# =========================================================
# START SERVICE
# =========================================================

def start_service() -> tuple[
    bool,
    str,
]:

    if not is_service_registered():

        return (
            False,
            "Служба не зарегистрирована.",
        )

    system = platform.system()

    # =====================================================
    # WINDOWS
    # =====================================================

    if system == "Windows":

        result = _run(
            [
                "schtasks",
                "/run",
                "/tn",
                SERVICE_NAME,
            ]
        )

        if result.returncode == 0:

            return (
                True,
                "Служба запущена.",
            )

        return (
            False,
            (
                result.stdout
                or result.stderr
                or
                "Не удалось запустить "
                "задачу Windows."
            ),
        )

    # =====================================================
    # LINUX
    # =====================================================

    if system == "Linux":

        result = _run(
            [
                "systemctl",
                "start",
                LINUX_UNIT,
            ]
        )

        if result.returncode == 0:

            return (
                True,
                "Служба запущена.",
            )

        return (
            False,
            (
                result.stdout
                or result.stderr
                or
                "Не удалось запустить "
                "службу systemd."
            ),
        )

    return (
        False,
        "Неподдерживаемая ОС.",
    )


# =========================================================
# STOP REGISTERED SERVICE
# =========================================================

def stop_service() -> tuple[
    bool,
    str,
]:

    if not is_service_registered():

        return (
            False,
            "Служба не зарегистрирована.",
        )

    system = platform.system()

    # =====================================================
    # WINDOWS
    # =====================================================

    if system == "Windows":

        result = _run(
            [
                "schtasks",
                "/end",
                "/tn",
                SERVICE_NAME,
            ]
        )

        if result.returncode == 0:

            return (
                True,
                "Служба остановлена.",
            )

        return (
            False,
            (
                result.stdout
                or result.stderr
                or
                "Не удалось остановить "
                "задачу Windows."
            ),
        )

    # =====================================================
    # LINUX
    # =====================================================

    if system == "Linux":

        result = _run(
            [
                "systemctl",
                "stop",
                LINUX_UNIT,
            ]
        )

        if result.returncode == 0:

            return (
                True,
                "Служба остановлена.",
            )

        return (
            False,
            (
                result.stdout
                or result.stderr
                or
                "Не удалось остановить "
                "службу systemd."
            ),
        )

    return (
        False,
        "Неподдерживаемая ОС.",
    )


# =========================================================
# UNINSTALL SERVICE
# =========================================================

def uninstall_service() -> tuple[
    bool,
    str,
]:

    if not is_admin():

        return (
            False,
            (
                "Для удаления службы нужны "
                "права администратора или root."
            ),
        )

    system = platform.system()

    # =====================================================
    # WINDOWS
    # =====================================================

    if system == "Windows":

        # Сначала останавливаем задачу.
        _run(
            [
                "schtasks",
                "/end",
                "/tn",
                SERVICE_NAME,
            ]
        )

        time.sleep(1)

        result = _run(
            [
                "schtasks",
                "/delete",
                "/tn",
                SERVICE_NAME,
                "/f",
            ]
        )

        if result.returncode == 0:

            logging.info(
                "Windows Task %s удалена.",
                SERVICE_NAME,
            )

            return (
                True,
                "Служба Windows удалена.",
            )

        return (
            False,
            (
                result.stdout
                or result.stderr
                or
                "Ошибка удаления задачи."
            ),
        )

    # =====================================================
    # LINUX
    # =====================================================

    if system == "Linux":

        _run(
            [
                "systemctl",
                "stop",
                LINUX_UNIT,
            ]
        )

        _run(
            [
                "systemctl",
                "disable",
                LINUX_UNIT,
            ]
        )

        service_file = os.path.join(
            "/etc/systemd/system",
            LINUX_UNIT,
        )

        try:

            if os.path.exists(
                service_file
            ):

                os.remove(
                    service_file
                )

        except Exception as exc:

            return (
                False,
                (
                    "Ошибка удаления "
                    f"unit-файла: {exc}"
                ),
            )

        _run(
            [
                "systemctl",
                "daemon-reload",
            ]
        )

        return (
            True,
            "Служба Linux удалена.",
        )

    return (
        False,
        "Неподдерживаемая ОС.",
    )


# =========================================================
# STOP CURRENT PROCESS / SERVICE
# =========================================================

def schedule_stop() -> tuple[
    bool,
    str,
]:

    # -----------------------------------------------------
    # Если текущий процесс является службой,
    # останавливаем именно задачу.
    # -----------------------------------------------------

    if is_service_process():

        if platform.system() == "Windows":

            _spawn_detached(
                [
                    "cmd",
                    "/c",
                    (
                        "timeout /t 2 /nobreak >nul "
                        "& schtasks /end /tn "
                        f"\"{SERVICE_NAME}\""
                    ),
                ]
            )

            return (
                True,
                (
                    "Служба будет остановлена "
                    "через несколько секунд. "
                    "Панель станет недоступна."
                ),
            )

        if platform.system() == "Linux":

            _spawn_detached(
                [
                    "/bin/bash",
                    "-c",
                    (
                        "sleep 2; "
                        f"systemctl stop {LINUX_UNIT}"
                    ),
                ]
            )

            return (
                True,
                (
                    "Служба будет остановлена "
                    "через несколько секунд. "
                    "Панель станет недоступна."
                ),
            )

    # -----------------------------------------------------
    # Если это интерактивный процесс.
    # -----------------------------------------------------

    _spawn_self_exit()

    return (
        True,
        (
            "Текущий процесс будет остановлен "
            "через несколько секунд."
        ),
    )


# =========================================================
# RESTART
# =========================================================

def schedule_restart() -> tuple[
    bool,
    str,
]:

    if not is_service_registered():

        return (
            False,
            (
                "Служба не установлена — "
                "перезапуск недоступен."
            ),
        )

    system = platform.system()

    # =====================================================
    # WINDOWS
    # =====================================================

    if system == "Windows":

        _spawn_detached(
            [
                "cmd",
                "/c",
                (
                    "timeout /t 2 /nobreak >nul "
                    "& schtasks /end /tn "
                    f"\"{SERVICE_NAME}\" "
                    "& timeout /t 2 /nobreak >nul "
                    "& schtasks /run /tn "
                    f"\"{SERVICE_NAME}\""
                ),
            ]
        )

        return (
            True,
            (
                "Служба перезапускается. "
                "Подождите несколько секунд "
                "и обновите страницу."
            ),
        )

    # =====================================================
    # LINUX
    # =====================================================

    if system == "Linux":

        _spawn_detached(
            [
                "/bin/bash",
                "-c",
                (
                    "sleep 2; "
                    f"systemctl restart {LINUX_UNIT}"
                ),
            ]
        )

        return (
            True,
            (
                "Служба перезапускается. "
                "Подождите несколько секунд "
                "и обновите страницу."
            ),
        )

    return (
        False,
        "Неподдерживаемая ОС.",
    )


# =========================================================
# SELF EXIT
# =========================================================

def _spawn_self_exit():

    pid = os.getpid()

    if platform.system() == "Windows":

        _spawn_detached(
            [
                "cmd",
                "/c",
                (
                    "timeout /t 2 /nobreak >nul "
                    "& taskkill /PID "
                    f"{pid} /F"
                ),
            ]
        )

    else:

        _spawn_detached(
            [
                "/bin/bash",
                "-c",
                (
                    "sleep 2; "
                    f"kill {pid}"
                ),
            ]
        )


# =========================================================
# WINDOWS TASK COMMAND
# =========================================================

def _windows_task_tr() -> str:
    """
    Формирует команду для Task Scheduler.

    ВАЖНО:

    Рабочий каталог задаётся отдельно через /TR,
    поэтому приложение не должно начинать работу
    из C:\\Windows\\System32.

    Никаких изменений ACL здесь нет.
    """

    executable, args, work_dir = (
        get_service_command()
    )

    command = (
        subprocess.list2cmdline(
            [
                executable,
                *args,
            ]
        )
    )

    return (
        f'cmd.exe /d /c '
        f'cd /d "{work_dir}" '
        f'&& {command}'
    )


# =========================================================
# WINDOWS TASK INSTALL
# =========================================================

def _install_windows_task() -> tuple[
    bool,
    str,
]:

    logging.info(
        "Регистрация Windows Task: %s",
        SERVICE_NAME,
    )

    executable, args, work_dir = (
        get_service_command()
    )

    logging.info(
        "Executable: %s",
        executable,
    )

    logging.info(
        "Arguments: %s",
        args,
    )

    logging.info(
        "Working directory: %s",
        work_dir,
    )

    task_command = (
        _windows_task_tr()
    )

    result = _run(
        [
            "schtasks",
            "/create",

            "/tn",
            SERVICE_NAME,

            "/tr",
            task_command,

            "/sc",
            "onstart",

            "/ru",
            "SYSTEM",

            "/rl",
            "HIGHEST",

            "/f",
        ]
    )

    if result.returncode != 0:

        message = (
            result.stdout
            or result.stderr
            or
            "Ошибка создания задачи Windows."
        )

        logging.error(
            "Ошибка создания Windows Task: %s",
            message,
        )

        return (
            False,
            message,
        )

    logging.info(
        "Windows Task %s успешно создана.",
        SERVICE_NAME,
    )

    ok, start_message = (
        start_service()
    )

    if not ok:

        logging.warning(
            "Задача создана, но не запущена: %s",
            start_message,
        )

        return (
            True,
            (
                "Задача создана, но не запущена: "
                f"{start_message}"
            ),
        )

    # -----------------------------------------------------
    # Ждём, чтобы убедиться, что процесс действительно
    # начал работу.
    # -----------------------------------------------------

    time.sleep(1)

    if is_os_service_running():

        return (
            True,
            (
                "Служба Windows зарегистрирована "
                "и запущена от SYSTEM."
            ),
        )

    return (
        True,
        (
            "Служба Windows зарегистрирована. "
            "Команда запуска отправлена."
        ),
    )


# =========================================================
# LINUX SYSTEMD
# =========================================================

def _install_linux_service() -> tuple[
    bool,
    str,
]:

    executable, args, work_dir = (
        get_service_command()
    )

    quoted = " ".join(
        _shlex_quote(part)
        for part in [
            executable,
            *args,
        ]
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
        "/etc/systemd/system",
        LINUX_UNIT,
    )

    logging.info(
        "Создание systemd unit: %s",
        service_file,
    )

    try:

        with open(
            service_file,
            "w",
            encoding="utf-8",
        ) as file:

            file.write(
                unit_content
            )

        daemon = _run(
            [
                "systemctl",
                "daemon-reload",
            ]
        )

        if daemon.returncode != 0:

            return (
                False,
                (
                    daemon.stderr
                    or daemon.stdout
                    or
                    "Ошибка "
                    "systemctl daemon-reload."
                ),
            )

        enable = _run(
            [
                "systemctl",
                "enable",
                LINUX_UNIT,
            ]
        )

        if enable.returncode != 0:

            return (
                False,
                (
                    enable.stderr
                    or enable.stdout
                    or
                    "Ошибка "
                    "systemctl enable."
                ),
            )

        start = _run(
            [
                "systemctl",
                "start",
                LINUX_UNIT,
            ]
        )

        if start.returncode != 0:

            return (
                False,
                (
                    start.stderr
                    or start.stdout
                    or
                    "Ошибка "
                    "systemctl start."
                ),
            )

        return (
            True,
            (
                "Служба systemd создана, "
                "включена в автозагрузку "
                "и запущена."
            ),
        )

    except Exception as exc:

        logging.error(
            "Ошибка установки службы Linux: %s",
            exc,
        )

        return (
            False,
            str(exc),
        )


# =========================================================
# SHELL QUOTING
# =========================================================

def _shlex_quote(
    value: str,
) -> str:

    if platform.system() == "Windows":

        return subprocess.list2cmdline(
            [value]
        )

    import shlex

    return shlex.quote(value)