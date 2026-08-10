import os
import sys
import ctypes
import platform
import subprocess

SERVICE_NAME = "NetworkBackupManager"
DISPLAY_NAME = "Network Backup Manager Service"
DESCRIPTION = "Служба автоматического резервного копирования сетевого оборудования"


def is_admin() -> bool:
    """Проверка наличия прав Администратора (Windows) или Root (Linux)"""
    try:
        if platform.system() == "Windows":
            return ctypes.windll.shell32.IsUserAnAdmin() != 0
        else:
            return os.geteuid() == 0
    except Exception:
        return False


def install_service():
    """Установка службы / фоновой задачи в систему"""
    if not is_admin():
        print("❌ Ошибка: Для установки службы требуются права Администратора (Windows) или sudo (Linux).")
        sys.exit(1)

    system = platform.system()
    if system == "Windows":
        _install_windows_task()
    elif system == "Linux":
        _install_linux_service()
    else:
        print(f"❌ ОС {system} не поддерживается для автоустановки службы.")


def uninstall_service():
    """Удаление службы / фоновой задачи из системы"""
    if not is_admin():
        print("❌ Ошибка: Для удаления службы требуются права Администратора (Windows) или sudo (Linux).")
        sys.exit(1)

    system = platform.system()
    if system == "Windows":
        _uninstall_windows_task()
    elif system == "Linux":
        _uninstall_linux_service()
    else:
        print(f"❌ ОС {system} не поддерживается.")


def _install_windows_task():
    """Регистрация фоновой автозагружаемой задачи в Windows через schtasks"""
    python_exe = sys.executable
    main_script = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "main.py"))

    # Формируем команду запуска
    task_cmd = f'"{python_exe}" "{main_script}" --service'

    print(f"⚙️ Регистрация фоновой задачи Windows: {SERVICE_NAME}...")

    # Создаем задачу, которая запускается при старте системы от имени SYSTEM
    cmd_create = [
        "schtasks", "/create",
        "/tn", SERVICE_NAME,
        "/tr", task_cmd,
        "/sc", "onstart",
        "/ru", "SYSTEM",
        "/f"
    ]

    res = subprocess.run(cmd_create, capture_output=True, text=True, encoding="cp866", errors="ignore")

    if res.returncode == 0:
        print("✅ Фоновая задача успешно зарегистрирована в Windows!")
        print("🚀 Запуск приложения...")
        start_res = subprocess.run(["schtasks", "/run", "/tn", SERVICE_NAME], capture_output=True, text=True, encoding="cp866", errors="ignore")
        if start_res.returncode == 0:
            print("✅ Приложение успешно запущено в фоне и будет стартовать автоматически при загрузке ПК!")
        else:
            print(f"⚠️ Задача создана, но не смогла стартовать сразу: {start_res.stdout or start_res.stderr}")
    else:
        print(f"❌ Ошибка создания задачи: {res.stdout or res.stderr}")


def _uninstall_windows_task():
    """Остановка и удаление задачи из Windows"""
    print(f"🛑 Остановка задачи {SERVICE_NAME}...")
    subprocess.run(["schtasks", "/end", "/tn", SERVICE_NAME], capture_output=True)

    print(f"🗑️ Удаление задачи {SERVICE_NAME}...")
    res = subprocess.run(["schtasks", "/delete", "/tn", SERVICE_NAME, "/f"], capture_output=True, text=True, encoding="cp866", errors="ignore")
    
    if res.returncode == 0:
        print("✅ Фоновая задача Windows успешно удалена!")
    else:
        print(f"❌ Ошибка удаления задачи: {res.stdout or res.stderr}")


def _install_linux_service():
    python_exe = sys.executable
    main_script = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "main.py"))
    work_dir = os.path.dirname(main_script)

    unit_content = f"""[Unit]
Description={DESCRIPTION}
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory={work_dir}
ExecStart={python_exe} {main_script} --service
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
"""

    service_filename = f"{SERVICE_NAME.lower()}.service"
    service_file = os.path.join("/etc/systemd/system", service_filename)

    print(f"⚙️ Создание systemd unit-файла: {service_file}...")

    try:
        with open(service_file, "w", encoding="utf-8") as f:
            f.write(unit_content)

        subprocess.run(["systemctl", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "enable", service_filename], check=True)
        subprocess.run(["systemctl", "start", service_filename], check=True)
        print("✅ Служба systemd успешно создана, включена в автозагрузку и запущена!")
    except Exception as e:
        print(f"❌ Ошибка установки службы Linux: {e}")


def _uninstall_linux_service():
    service_filename = f"{SERVICE_NAME.lower()}.service"
    service_file = os.path.join("/etc/systemd/system", service_filename)

    print(f"🛑 Остановка и отключение службы {service_filename}...")
    subprocess.run(["systemctl", "stop", service_filename], capture_output=True)
    subprocess.run(["systemctl", "disable", service_filename], capture_output=True)

    if os.path.exists(service_file):
        os.remove(service_file)
        print("🗑️ Unit-файл удален.")

    subprocess.run(["systemctl", "daemon-reload"], capture_output=True)
    print("✅ Служба Linux успешно удалена!")