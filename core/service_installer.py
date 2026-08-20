import os
import sys
import time
import socket
import ctypes
import logging
import platform
import subprocess

from core.context import BASE_DIR, CONFIG_FILE, load_json

SERVICE_NAME = "NetworkBackupManager"
LINUX_UNIT = f"{SERVICE_NAME.lower()}.service"
DESCRIPTION = "Служба автоматического резервного копирования сетевого оборудования"


def is_admin() -> bool:
    try:
        if platform.system() == "Windows":
            return ctypes.windll.shell32.IsUserAnAdmin() != 0
        return os.geteuid() == 0
    except Exception:
        return False


def is_service_process() -> bool:
    return "--service" in sys.argv


def get_service_command() -> tuple[str, list[str], str]:
    """Исполняемый файл, аргументы и рабочий каталог для автозапуска."""
    work_dir = str(BASE_DIR)
    if getattr(sys, "frozen", False):
        return sys.executable, ["--service"], work_dir
    return sys.executable, [str(BASE_DIR / "main.py"), "--service"], work_dir


def _run(cmd, **kwargs):
    encoding = "cp866" if platform.system() == "Windows" else "utf-8"
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
        getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
        | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    )


def _spawn_detached(cmd):
    kwargs = {}
    if platform.system() == "Windows":
        kwargs["creationflags"] = _windows_flags()
        kwargs["close_fds"] = True
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(cmd, **kwargs)


def wait_for_http(timeout: float = 20) -> bool:
    config = load_json(CONFIG_FILE, {})
    host = "127.0.0.1"
    port = int(config.get("port", 8080))
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1):
                return True
        except OSError:
            time.sleep(0.4)
    return False


def is_http_up() -> bool:
    return wait_for_http(timeout=0.8)


def is_service_registered() -> bool:
    system = platform.system()
    if system == "Windows":
        res = _run(["schtasks", "/query", "/tn", SERVICE_NAME])
        return res.returncode == 0
    if system == "Linux":
        return os.path.exists(os.path.join("/etc/systemd/system", LINUX_UNIT))
    return False


def is_os_service_running() -> bool:
    system = platform.system()
    if system == "Windows":
        res = _run(["schtasks", "/query", "/tn", SERVICE_NAME, "/fo", "LIST", "/v"])
        if res.returncode != 0:
            return False
        text = f"{res.stdout}\n{res.stderr}".lower()
        return "running" in text or "выполняется" in text
    if system == "Linux":
        res = _run(["systemctl", "is-active", LINUX_UNIT])
        return (res.stdout or "").strip() == "active"
    return False


def get_status() -> dict:
    registered = is_service_registered()
    return {
        "registered": registered,
        "os_running": is_os_service_running() if registered else False,
        "http_up": True,
        "service_mode": is_service_process(),
        "can_restart": registered,
        "can_stop": True,
    }


def relaunch_elevated(args: list[str]) -> bool:
    """Повторный запуск с правами администратора / через pkexec."""
    system = platform.system()
    if system == "Windows":
        if getattr(sys, "frozen", False):
            params = subprocess.list2cmdline(args)
            exe = sys.executable
        else:
            params = subprocess.list2cmdline([str(BASE_DIR / "main.py")] + args)
            exe = sys.executable
        rc = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", exe, params, str(BASE_DIR), 1
        )
        return int(rc) > 32

    if system == "Linux":
        cmd = [sys.executable]
        if not getattr(sys, "frozen", False):
            cmd.append(str(BASE_DIR / "main.py"))
        cmd.extend(args)
        for wrapper in ("pkexec", "sudo"):
            try:
                res = subprocess.run([wrapper, *cmd], cwd=str(BASE_DIR))
                return res.returncode == 0
            except FileNotFoundError:
                continue
    return False


def install_service() -> tuple[bool, str]:
    if not is_admin():
        return False, "Для установки службы нужны права администратора (Windows) или root (Linux)."

    system = platform.system()
    if system == "Windows":
        return _install_windows_task()
    if system == "Linux":
        return _install_linux_service()
    return False, f"ОС {system} не поддерживается для автоустановки службы."


def start_service() -> tuple[bool, str]:
    system = platform.system()
    if system == "Windows":
        res = _run(["schtasks", "/run", "/tn", SERVICE_NAME])
        if res.returncode == 0:
            return True, "Служба запущена."
        return False, res.stdout or res.stderr or "Не удалось запустить задачу Windows."
    if system == "Linux":
        res = _run(["systemctl", "start", LINUX_UNIT])
        if res.returncode == 0:
            return True, "Служба запущена."
        return False, res.stdout or res.stderr or "Не удалось запустить службу systemd."
    return False, "Неподдерживаемая ОС."


def uninstall_service() -> tuple[bool, str]:
    if not is_admin():
        return False, "Для удаления службы нужны права администратора или root."

    system = platform.system()
    if system == "Windows":
        _run(["schtasks", "/end", "/tn", SERVICE_NAME])
        res = _run(["schtasks", "/delete", "/tn", SERVICE_NAME, "/f"])
        if res.returncode == 0:
            return True, "Фоновая задача Windows удалена."
        return False, res.stdout or res.stderr or "Ошибка удаления задачи."
    if system == "Linux":
        _run(["systemctl", "stop", LINUX_UNIT])
        _run(["systemctl", "disable", LINUX_UNIT])
        service_file = os.path.join("/etc/systemd/system", LINUX_UNIT)
        if os.path.exists(service_file):
            os.remove(service_file)
        _run(["systemctl", "daemon-reload"])
        return True, "Служба Linux удалена."
    return False, "Неподдерживаемая ОС."


def schedule_stop() -> tuple[bool, str]:
    """Остановка с задержкой, чтобы успел уйти HTTP-ответ."""
    if is_service_registered():
        system = platform.system()
        if system == "Windows":
            _spawn_detached([
                "cmd", "/c",
                f"timeout /t 2 /nobreak >nul & schtasks /end /tn {SERVICE_NAME}",
            ])
            return True, "Служба будет остановлена через несколько секунд. Панель станет недоступна."
        if system == "Linux":
            _spawn_detached(["/bin/bash", "-c", f"sleep 2; systemctl stop {LINUX_UNIT}"])
            return True, "Служба будет остановлена через несколько секунд. Панель станет недоступна."
    _spawn_self_exit()
    return True, "Процесс будет остановлен через несколько секунд. Панель станет недоступна."


def schedule_restart() -> tuple[bool, str]:
    if not is_service_registered():
        return False, "Служба не установлена — перезапуск из панели недоступен."

    system = platform.system()
    if system == "Windows":
        _spawn_detached([
            "cmd", "/c",
            f"timeout /t 2 /nobreak >nul & schtasks /end /tn {SERVICE_NAME} "
            f"& timeout /t 2 /nobreak >nul & schtasks /run /tn {SERVICE_NAME}",
        ])
        return True, "Служба перезапускается. Обновите страницу через несколько секунд."
    if system == "Linux":
        _spawn_detached(["/bin/bash", "-c", f"sleep 2; systemctl restart {LINUX_UNIT}"])
        return True, "Служба перезапускается. Обновите страницу через несколько секунд."
    return False, "Неподдерживаемая ОС."


def _spawn_self_exit():
    if platform.system() == "Windows":
        pid = os.getpid()
        _spawn_detached(["cmd", "/c", f"timeout /t 2 /nobreak >nul & taskkill /PID {pid} /F"])
    else:
        pid = os.getpid()
        _spawn_detached(["/bin/bash", "-c", f"sleep 2; kill {pid}"])


def _windows_task_tr() -> str:
    exe, args, work_dir = get_service_command()
    inner = subprocess.list2cmdline([exe] + args)
    return f'cmd /c cd /d "{work_dir}" && {inner}'


def _install_windows_task() -> tuple[bool, str]:
    logging.info("Регистрация фоновой задачи Windows: %s", SERVICE_NAME)
    res = _run([
        "schtasks", "/create",
        "/tn", SERVICE_NAME,
        "/tr", _windows_task_tr(),
        "/sc", "onstart",
        "/ru", "SYSTEM",
        "/f",
    ])
    if res.returncode != 0:
        msg = res.stdout or res.stderr or "Ошибка создания задачи."
        logging.error("Ошибка создания задачи: %s", msg)
        return False, msg

    ok, start_msg = start_service()
    if ok:
        logging.info("Фоновая задача Windows зарегистрирована и запущена.")
        return True, "Фоновая задача Windows зарегистрирована и запущена."
    logging.warning("Задача создана, но не стартовала: %s", start_msg)
    return True, f"Задача создана, но не стартовала сразу: {start_msg}"


def _install_linux_service() -> tuple[bool, str]:
    exe, args, work_dir = get_service_command()
    quoted = " ".join(_shlex_quote(part) for part in [exe] + args)
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
    service_file = os.path.join("/etc/systemd/system", LINUX_UNIT)
    logging.info("Создание systemd unit-файла: %s", service_file)
    try:
        with open(service_file, "w", encoding="utf-8") as f:
            f.write(unit_content)
        _run(["systemctl", "daemon-reload"], check=False)
        enable = _run(["systemctl", "enable", LINUX_UNIT])
        start = _run(["systemctl", "start", LINUX_UNIT])
        if enable.returncode != 0 or start.returncode != 0:
            detail = enable.stderr or start.stderr or "ошибка systemctl"
            logging.error("Ошибка установки службы Linux: %s", detail)
            return False, detail
        logging.info("Служба systemd создана, включена в автозагрузку и запущена.")
        return True, "Служба systemd создана, включена в автозагрузку и запущена."
    except Exception as e:
        logging.error("Ошибка установки службы Linux: %s", e)
        return False, str(e)


def _shlex_quote(value: str) -> str:
    if platform.system() == "Windows":
        return subprocess.list2cmdline([value])
    import shlex
    return shlex.quote(value)
