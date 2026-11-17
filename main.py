# =========================================================
# СТАНДАРТНЫЕ ИМПОРТЫ
# =========================================================

import multiprocessing

# FIX: freeze_support() вызывается сразу после импорта multiprocessing,
# до любых тяжёлых импортов (core.*, tkinter и т.п.).
# Это критично для PyInstaller-EXE, порождающих дочерние процессы.
if __name__ == "__main__":
    multiprocessing.freeze_support()


import os
import sys
import json
import base64
import hashlib
import ctypes
import logging
import webbrowser
import threading
from pathlib import Path

logger = logging.getLogger("nbm.main")

# =========================================================
# ОСНОВНЫЕ ПУТИ
# =========================================================

# FIX: APPLICATION_DIR вычисляется, но в этом файле не используется.
# Оставлен на случай, если понадобится в других модулях через import.
if getattr(sys, "frozen", False):
    APPLICATION_DIR = Path(sys.executable).resolve().parent
else:
    APPLICATION_DIR = Path(__file__).resolve().parent


# =========================================================
# КОНСОЛЬ ДЛЯ EXE С --noconsole
# =========================================================

def ensure_console() -> bool:
    """
    Для EXE, собранного с --noconsole.

    При запуске, например:
        NetworkBackupManager.exe --create-user

    автоматически создаёт консоль Windows, если её нет.
    Возвращает True, если консоль доступна (или уже была).
    """

    if os.name != "nt":
        return False

    try:
        if ctypes.windll.kernel32.GetConsoleWindow():
            return True

        result = ctypes.windll.kernel32.AllocConsole()

        if not result:
            return False

        sys.stdout = open(
            "CONOUT$",
            "w",
            encoding="utf-8",
            buffering=1,
        )

        sys.stderr = open(
            "CONOUT$",
            "w",
            encoding="utf-8",
            buffering=1,
        )

        sys.stdin = open(
            "CONIN$",
            "r",
            encoding="utf-8",
            buffering=1,
        )

        return True

    except Exception:
        # FIX: не глотаем молча — оставляем след в логе.
        logger.debug(
            "ensure_console: не удалось создать консоль",
            exc_info=True,
        )
        return False


# =========================================================
# ИМПОРТЫ ПРИЛОЖЕНИЯ
# =========================================================

from core.context import (
    DATA_DIR,
    AUTH_FILE,
    CONFIG_FILE,
    ensure_directories,
    ensure_default_files,
    ensure_web_files,
    load_json,
)

# =========================================================
# ЛОГИРОВАНИЕ
# =========================================================
# Все логгеры в проекте пишут в logs/app.log с форматом:
# время [УРОВЕНЬ] [device] имя_логгера: сообщение
#
# =========================================================

from core.logger import setup_logging

setup_logging()


from core.service_installer import (
    is_admin,
    is_service_process,
    is_service_registered,
    install_service,
    uninstall_service,
    start_service,
    stop_service,
    schedule_restart,
    relaunch_elevated,
    wait_for_http,
)



# -----------------------------------------------------
# rclone
# -----------------------------------------------------

from core import rclone as rclone_mod

if rclone_mod.is_available():
    logger.info("rclone: %s", rclone_mod.get_version())
else:
    logger.info(
        "rclone: не настроен — storage-backend'ы "
        "на rclone недоступны."
    )


# =========================================================
# НАСТРОЙКИ WEB
# =========================================================

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8080


# =========================================================
# ПАРОЛЬ
# =========================================================

PBKDF2_ITERATIONS = 260000


def hash_password(
    password: str,
    salt: bytes | None = None,
) -> str:

    if salt is None:
        salt = os.urandom(16)

    key = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        PBKDF2_ITERATIONS,
    )

    return (
        "pbkdf2_sha256$"
        f"{PBKDF2_ITERATIONS}$"
        f"{base64.b64encode(salt).decode()}$"
        f"{base64.b64encode(key).decode()}"
    )


# =========================================================
# CREATE USER — GUI
# =========================================================

def create_user() -> int:
    """
    Графическое создание пользователя.

    Работает в EXE, собранном с --noconsole.
    """

    # FIX: готовим каталоги и дефолтные файлы заранее,
    # чтобы AUTH_FILE гарантированно лежал в существующем каталоге.
    try:
        ensure_directories()
        ensure_default_files()
    except Exception as exc:
        # Если GUI не запустится — хотя бы консоль покажет причину.
        ensure_console()
        print(f"Не удалось подготовить каталоги данных: {exc}")
        return 1

    try:
        import tkinter as tk
        from tkinter import messagebox
    except Exception as exc:
        ensure_console()
        print(f"Не удалось загрузить tkinter: {exc}")
        return 1

    # -----------------------------------------------------
    # ROOT WINDOW
    # -----------------------------------------------------

    root = tk.Tk()
    root.title("NetworkBackupManager — создание пользователя")
    root.resizable(False, False)

    # -----------------------------------------------------
    # WINDOW SIZE / POSITION
    # -----------------------------------------------------

    width = 460
    height = 330

    screen_width = root.winfo_screenwidth()
    screen_height = root.winfo_screenheight()

    x = (screen_width - width) // 2
    y = (screen_height - height) // 2

    root.geometry(f"{width}x{height}+{x}+{y}")

    # -----------------------------------------------------
    # MAIN FRAME
    # -----------------------------------------------------

    frame = tk.Frame(root, padx=25, pady=20)
    frame.pack(fill="both", expand=True)

    # -----------------------------------------------------
    # TITLE
    # -----------------------------------------------------

    title = tk.Label(
        frame,
        text="Создание пользователя",
        font=("Segoe UI", 15, "bold"),
    )
    title.pack(pady=(0, 20))

    # -----------------------------------------------------
    # USERNAME
    # -----------------------------------------------------

    username_frame = tk.Frame(frame)
    username_frame.pack(fill="x", pady=5)

    tk.Label(
        username_frame,
        text="Имя пользователя:",
        width=20,
        anchor="w",
    ).pack(side="left")

    username_entry = tk.Entry(username_frame, width=30)
    username_entry.pack(side="left", fill="x", expand=True)

    # -----------------------------------------------------
    # PASSWORD
    # -----------------------------------------------------

    password_frame = tk.Frame(frame)
    password_frame.pack(fill="x", pady=5)

    tk.Label(
        password_frame,
        text="Пароль:",
        width=20,
        anchor="w",
    ).pack(side="left")

    password_entry = tk.Entry(password_frame, width=30, show="*")
    password_entry.pack(side="left", fill="x", expand=True)

    # -----------------------------------------------------
    # PASSWORD CONFIRM
    # -----------------------------------------------------

    confirm_frame = tk.Frame(frame)
    confirm_frame.pack(fill="x", pady=5)

    tk.Label(
        confirm_frame,
        text="Повторите пароль:",
        width=20,
        anchor="w",
    ).pack(side="left")

    confirm_entry = tk.Entry(confirm_frame, width=30, show="*")
    confirm_entry.pack(side="left", fill="x", expand=True)

    # -----------------------------------------------------
    # PASSWORD REQUIREMENTS
    # -----------------------------------------------------

    info = tk.Label(
        frame,
        text="Пароль должен содержать минимум 6 символов.",
        fg="gray",
    )
    info.pack(pady=(8, 15))

    # -----------------------------------------------------
    # CREATE
    # -----------------------------------------------------

    def save_user():

        username = username_entry.get().strip()
        password = password_entry.get()
        password2 = confirm_entry.get()

        # -------------------------------------------------
        # USERNAME
        # -------------------------------------------------

        if not username:
            messagebox.showerror(
                "Ошибка",
                "Имя пользователя не может быть пустым.",
                parent=root,
            )
            username_entry.focus_set()
            return

        if len(username) < 2 or len(username) > 64:
            messagebox.showerror(
                "Ошибка",
                "Имя пользователя должно содержать "
                "от 2 до 64 символов.",
                parent=root,
            )
            username_entry.focus_set()
            return

        # -------------------------------------------------
        # PASSWORD
        # -------------------------------------------------

        if not password:
            messagebox.showerror(
                "Ошибка",
                "Пароль не может быть пустым.",
                parent=root,
            )
            password_entry.focus_set()
            return

        if len(password) < 6:
            messagebox.showerror(
                "Ошибка",
                "Пароль должен содержать "
                "минимум 6 символов.",
                parent=root,
            )
            password_entry.focus_set()
            return

        # -------------------------------------------------
        # PASSWORD CONFIRMATION
        # -------------------------------------------------

        if password != password2:
            messagebox.showerror(
                "Ошибка",
                "Пароли не совпадают.",
                parent=root,
            )
            confirm_entry.focus_set()
            return

        # -------------------------------------------------
        # DATA DIRECTORY
        # -------------------------------------------------

        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            messagebox.showerror(
                "Ошибка",
                "Не удалось создать каталог данных:\n\n"
                f"{exc}",
                parent=root,
            )
            return

        # -------------------------------------------------
        # EXISTING USER
        # -------------------------------------------------

        if AUTH_FILE.exists():
            overwrite = messagebox.askyesno(
                "Пользователь уже существует",
                "Файл пользователя уже существует.\n\n"
                "Перезаписать пользователя?",
                parent=root,
            )
            if not overwrite:
                return

        # -------------------------------------------------
        # HASH
        # -------------------------------------------------

        try:
            password_hash = hash_password(password)

            data = {
                "username": username,
                "password_hash": password_hash,
            }

            AUTH_FILE.write_text(
                json.dumps(
                    data,
                    ensure_ascii=False,
                    indent=4,
                ),
                encoding="utf-8",
            )

        except Exception as exc:
            messagebox.showerror(
                "Ошибка",
                "Не удалось сохранить пользователя:\n\n"
                f"{exc}",
                parent=root,
            )
            return

        # -------------------------------------------------
        # SUCCESS
        # -------------------------------------------------

        messagebox.showinfo(
            "Готово",
            "Пользователь успешно создан.",
            parent=root,
        )
        root.destroy()

    # -----------------------------------------------------
    # BUTTONS
    # -----------------------------------------------------

    buttons = tk.Frame(frame)
    buttons.pack(pady=(5, 0))

    create_button = tk.Button(
        buttons,
        text="Создать пользователя",
        width=22,
        command=save_user,
    )
    create_button.pack(side="left", padx=5)

    cancel_button = tk.Button(
        buttons,
        text="Отмена",
        width=12,
        command=root.destroy,
    )
    cancel_button.pack(side="left", padx=5)

    # -----------------------------------------------------
    # ENTER / ESC
    # -----------------------------------------------------

    root.bind("<Return>", lambda event: save_user())
    root.bind("<Escape>", lambda event: root.destroy())

    username_entry.focus_set()

    # -----------------------------------------------------
    # RUN GUI
    # -----------------------------------------------------

    root.mainloop()

    return 0


# =========================================================
# INSTALL SERVICE CLI
# =========================================================

def install_service_cli() -> int:

    ensure_directories()
    ensure_default_files()

    if not is_admin():
        print("Для установки службы нужны права администратора.")
        return 1

    ok, message = install_service()
    print(message)

    # После успешной регистрации служба уже запущена
    # (см. _install_windows_service → start_service).
    # Открываем браузер из этой же elevated-копии:
    # исходный launcher уже завершился и сделать это
    # не может.
    if ok:
        open_browser_when_ready()

    return 0 if ok else 1


# =========================================================
# UNINSTALL SERVICE CLI
# =========================================================

def uninstall_service_cli() -> int:

    ensure_console()

    if not is_admin():
        print("Для удаления службы нужны права администратора.")
        return 1

    ok, message = uninstall_service()
    print(message)

    return 0 if ok else 1


# =========================================================
# START SERVICE CLI
# =========================================================

def start_service_cli() -> int:

    ensure_console()

    if not is_admin():
        print("Для запуска службы нужны права администратора.")
        return 1

    if not is_service_registered():
        print("Служба не зарегистрирована.")
        return 1

    ok, message = start_service()
    print(message)

    if ok:
        open_browser_when_ready()

    return 0 if ok else 1


# =========================================================
# STOP SERVICE CLI
# =========================================================

def stop_service_cli() -> int:

    ensure_console()

    if not is_admin():
        print("Для остановки службы нужны права администратора.")
        return 1

    if not is_service_registered():
        print("Служба не зарегистрирована.")
        return 1

    ok, message = stop_service()
    print(message)

    return 0 if ok else 1


# =========================================================
# RESTART SERVICE CLI
# =========================================================

def restart_service_cli() -> int:

    ensure_console()

    if not is_admin():
        print("Для перезапуска службы нужны права администратора.")
        return 1

    if not is_service_registered():
        print("Служба не зарегистрирована.")
        return 1

    ok, message = schedule_restart()
    print(message)

    # schedule_restart из внешнего процесса запускает
    # отложенный Restart-Service в detached-PowerShell.
    # Дадим службе успеть подняться, прежде чем
    # открывать браузер.
    if ok:
        open_browser_when_ready()

    return 0 if ok else 1


# =========================================================
# CONFIG
# =========================================================

def get_web_config() -> tuple[str, int]:
    """
    Возвращает (host, port) с защитой от некорректных значений.
    """

    config = load_json(CONFIG_FILE, {})

    # host
    host = config.get("host", DEFAULT_HOST)
    if not isinstance(host, str) or not host.strip():
        logger.warning(
            "Некорректный host в конфиге (%r), использую %s",
            host,
            DEFAULT_HOST,
        )
        host = DEFAULT_HOST

    # port
    raw_port = config.get("port", DEFAULT_PORT)
    try:
        port = int(raw_port)
        if not (1 <= port <= 65535):
            raise ValueError("порт вне диапазона 1..65535")
    except (TypeError, ValueError) as exc:
        logger.warning(
            "Некорректный port в конфиге (%r): %s; использую %s",
            raw_port,
            exc,
            DEFAULT_PORT,
        )
        port = DEFAULT_PORT

    return host, port


# =========================================================
# OPEN BROWSER
# =========================================================

def open_browser_when_ready() -> None:

    try:
        if not wait_for_http(20):
            logger.warning(
                "Веб-сервер не ответил за 20 секунд — браузер не открыт."
            )
            return

        host, port = get_web_config()
        url = f"http://{host}:{port}"

        logger.info("Открытие браузера: %s", url)
        webbrowser.open(url, new=2)

    except Exception as exc:
        logger.error("Не удалось открыть браузер: %s", exc)


def start_browser_thread() -> None:
    """
    Запускает ожидание сервера и открытие браузера в фоне,
    чтобы не блокировать основной поток.
    """
    threading.Thread(
        target=open_browser_when_ready,
        daemon=True,
    ).start()


# =========================================================
# START WEB SERVER
# =========================================================

def run_web_server() -> None:

    import uvicorn

    host, port = get_web_config()

    logger.info(
        "Запуск NetworkBackupManager на %s:%s",
        host,
        port,
    )

    uvicorn.run(
        "web.server:app",
        host=host,
        port=port,
        reload=False,
        log_config=None,
    )


# =========================================================
# START NORMAL APPLICATION
# =========================================================

def run_application(skip_service_install: bool = False) -> int:

    ensure_directories()
    ensure_web_files() 
    ensure_default_files()

    # -----------------------------------------------------
    # РЕЖИМ СЛУЖБЫ
    # -----------------------------------------------------

    if is_service_process():
        logger.info("Приложение запущено в режиме службы.")
        run_web_server()
        return 0

    # -----------------------------------------------------
    # --skip-service-install
    # -----------------------------------------------------

    if skip_service_install:
        logger.info(
            "Запуск без установки службы (--skip-service-install)."
        )
        start_browser_thread()
        run_web_server()
        return 0

    # -----------------------------------------------------
    # ОБЫЧНЫЙ ЗАПУСК
    # -----------------------------------------------------

    if not is_service_registered():
        logger.info("Служба не зарегистрирована.")

        if not is_admin():
            print("Для первого запуска требуются права администратора.")
            print("Запрашиваем повышение прав...")

            ok = relaunch_elevated(["--install-service"])
            if not ok:
                print("Не удалось получить права администратора.")
                return 1

            logger.info(
                "Launcher завершает работу: запущена "
                "копия с повышенными правами."
            )
            return 0

        ok, message = install_service()
        if not ok:
            print("Ошибка установки службы:")
            print(message)
            return 1

        logger.info("Служба установлена: %s", message)

    else:
        # -------------------------------------------------
        # Служба уже зарегистрирована.
        # Убедимся, что она запущена.
        # -------------------------------------------------

        try:
            from core.service_installer import is_os_service_running

            if not is_os_service_running():
                logger.info(
                    "Служба зарегистрирована, но не запущена."
                )
                start_service()

        except Exception as exc:
            logger.error("Ошибка запуска службы: %s", exc)

    # -----------------------------------------------------
    # После установки/запуска службы открываем браузер.
    # Сам launcher завершается.
    # -----------------------------------------------------

    open_browser_when_ready()

    return 0


# =========================================================
# MAIN
# =========================================================

# FIX: список известных CLI-флагов, чтобы предупреждать о неизвестных.
_KNOWN_FLAGS = {
    "--create-user",
    "--service",
    "--install-service",
    "--uninstall-service",
    "--stop-service",
    "--start-service",
    "--restart-service",
    "--skip-service-install",
}


def main() -> int:

    args = sys.argv[1:]

    # -----------------------------------------------------
    # Проверка неизвестных аргументов
    # -----------------------------------------------------

    unknown = [a for a in args if a.startswith("-") and a not in _KNOWN_FLAGS]
    if unknown:
        ensure_console()
        print("Неизвестные аргументы командной строки:")
        for a in unknown:
            print(f"  {a}")
        print()
        print("Известные флаги:")
        for flag in sorted(_KNOWN_FLAGS):
            print(f"  {flag}")
        return 2

    # -----------------------------------------------------
    # CREATE USER
    # -----------------------------------------------------

    if "--create-user" in args:
        return create_user()

    # -----------------------------------------------------
    # INSTALL SERVICE
    # -----------------------------------------------------

    if "--install-service" in args:
        ensure_console()
        return install_service_cli()

    # -----------------------------------------------------
    # UNINSTALL SERVICE
    # -----------------------------------------------------

    if "--uninstall-service" in args:
        return uninstall_service_cli()

    # -----------------------------------------------------
    # STOP SERVICE
    # -----------------------------------------------------

    if "--stop-service" in args:
        return stop_service_cli()

    # -----------------------------------------------------
    # RESTART SERVICE
    # -----------------------------------------------------

    if "--restart-service" in args:
        return restart_service_cli()

    # -----------------------------------------------------
    # START SERVICE
    # -----------------------------------------------------

    if "--start-service" in args:
        return start_service_cli()
    
    # -----------------------------------------------------
    # SKIP SERVICE INSTALL
    # -----------------------------------------------------

    skip_service_install = "--skip-service-install" in args

    return run_application(skip_service_install=skip_service_install)


# =========================================================
# ENTRY POINT
# =========================================================

if __name__ == "__main__":

    try:
        sys.exit(main())

    except KeyboardInterrupt:
        sys.exit(130)

    except Exception:

        # FIX: теперь критичная ошибка гарантированно попадает
        # в app.log с полным traceback.
        logger.exception("Критическая ошибка в main()")

        try:
            ensure_console()

            import traceback

            print()
            print("КРИТИЧЕСКАЯ ОШИБКА:")
            print(traceback.format_exc())
            print()

            input("Нажмите Enter...")

        except Exception:
            pass

        sys.exit(1)