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
import ctypes
import logging
import webbrowser
import threading
from pathlib import Path

logger = logging.getLogger("nbm.main")

# =========================================================
# ОСНОВНЫЕ ПУТИ
# =========================================================

# Пути — в core/context.py (BASE_DIR — код, HOME_DIR — данные).


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
from core.version import APP_NAME, COPYRIGHT, LICENSE_ID, SOURCE_URL, __version__

logger.info("%s %s", APP_NAME, __version__)

if rclone_mod.is_available():
    logger.info("rclone: %s", rclone_mod.get_version())
else:
    logger.info(
        "rclone: не настроен — storage-backend'ы "
        "на rclone недоступны."
    )


# =========================================================
# НАСТРОЙКИ И ПОЛЬЗОВАТЕЛЬ
# =========================================================
#
# Хост/порт и прочее — core/settings.py (окружение NBM_* важнее
# config.json). Пароль — core/auth.py.

from core import settings
from core.auth import hash_password, save_user, ensure_user_from_env


# =========================================================
# CREATE USER — КОНСОЛЬ (Linux без графики, Docker)
# =========================================================

def create_user_console() -> int:
    """
    Создание пользователя в терминале:
        python main.py --create-user
        docker exec -it nbm python main.py --create-user
    """
    import getpass

    ensure_directories()
    ensure_default_files()

    if not sys.stdin or not sys.stdin.isatty():
        print(
            "Нет терминала для ввода. Задайте NBM_ADMIN_USER и "
            "NBM_ADMIN_PASSWORD — пользователь создастся при запуске."
        )
        return 1

    print("Создание пользователя веб-интерфейса")
    username = input("Логин [admin]: ").strip() or "admin"
    if len(username) > 100:
        print("Слишком длинный логин.")
        return 1

    password = getpass.getpass("Пароль: ")
    if len(password) < 4:
        print("Пароль слишком короткий (минимум 4 символа).")
        return 1
    if getpass.getpass("Повторите пароль: ") != password:
        print("Пароли не совпадают.")
        return 1

    if AUTH_FILE.exists():
        answer = input("Пользователь уже есть. Перезаписать? [y/N]: ")
        if answer.strip().lower() not in ("y", "yes", "д", "да"):
            print("Отменено.")
            return 1

    save_user(username, password)
    print(f"Пользователь {username!r} сохранён в {AUTH_FILE}")
    return 0


def init_user_from_env() -> int:
    """
    --init-user: пользователь из NBM_ADMIN_USER / NBM_ADMIN_PASSWORD
    без окон и вопросов. Так его создаёт установщик Windows: служба
    запускается диспетчером служб и окружения установщика не видит.

    Существующего пользователя не меняет.
    Код выхода: 0 — создан или уже был; 2 — пароль не задан;
    1 — ошибка.
    """
    if AUTH_FILE.exists():
        return 0
    if settings.admin_from_env() is None:
        return 2
    try:
        ensure_directories()
        ensure_user_from_env()
    except Exception as exc:
        logger.error("--init-user: не удалось создать пользователя: %s", exc)
        return 1
    return 0 if AUTH_FILE.exists() else 1


def _has_gui() -> bool:
    if os.name == "nt":
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


# =========================================================
# CREATE USER — GUI
# =========================================================

def create_user() -> int:
    """
    Графическое создание пользователя.

    Работает в EXE, собранном с --noconsole. Без графики
    (Linux-сервер, Docker) — вопросы в терминале.
    """
    if not _has_gui():
        return create_user_console()

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
        if os.name != "nt":
            print(f"Графика недоступна ({exc}) — ввод в терминале.")
            return create_user_console()
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
    """(host, port) — см. core.settings.web_config."""
    return settings.web_config()


# =========================================================
# OPEN BROWSER
# =========================================================

def open_browser_when_ready() -> None:

    # В контейнере и при NBM_OPEN_BROWSER=0 / open_browser=false
    if not settings.open_browser():
        return

    try:
        if not wait_for_http(20):
            logger.warning(
                "Веб-сервер не ответил за 20 секунд — браузер не открыт."
            )
            return

        host, port = get_web_config()
        try:
            scheme = "https" if settings.tls_files() else "http"
        except settings.TlsConfigError:
            return
        url = f"{scheme}://{host}:{port}"

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

    extra = {}
    proxies = settings.trusted_proxies()
    if proxies:
        # Реальный адрес клиента и схема (https) из X-Forwarded-*
        extra["forwarded_allow_ips"] = proxies

    # HTTPS: сертификат задан, но не годится — не запускаемся,
    # а не уходим молча на HTTP (пароль шёл бы открытым)
    try:
        tls = settings.tls_files()
    except settings.TlsConfigError as exc:
        logger.error("HTTPS: %s. Веб-сервер не запущен.", exc)
        raise SystemExit(2)
    if tls:
        cert, key = tls
        extra["ssl_certfile"] = str(cert)
        if key:
            extra["ssl_keyfile"] = str(key)
        logger.info("HTTPS включён: %s", cert)
        expires = settings.cert_expires(cert)
        if expires:
            from datetime import datetime, timezone
            days = (expires - datetime.now(timezone.utc)).days
            if days < 0:
                logger.warning("Сертификат HTTPS истёк %s — браузеры его не примут", expires.date())
            elif days <= 30:
                logger.warning("Сертификат HTTPS истекает через %s дн. (%s)", days, expires.date())

    uvicorn.run(
        "web.server:app",
        host=host,
        port=port,
        reload=False,
        log_config=None,
        proxy_headers=True,
        **extra,
    )


# =========================================================
# START NORMAL APPLICATION
# =========================================================

def run_application(
    skip_service_install: bool = False,
    foreground: bool = False,
) -> int:

    ensure_directories()
    ensure_web_files()
    ensure_default_files()
    ensure_user_from_env()

    # -----------------------------------------------------
    # РЕЖИМ СЛУЖБЫ
    # -----------------------------------------------------

    if is_service_process():
        logger.info("Приложение запущено в режиме службы.")
        run_web_server()
        return 0

    # -----------------------------------------------------
    # --foreground / контейнер: только веб-сервер, без
    # установки службы и без браузера (Docker, systemd
    # с ExecStart вручную, отладка)
    # -----------------------------------------------------

    if foreground or settings.in_container():
        logger.info("Запуск на переднем плане (без службы ОС).")
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

def print_version() -> None:
    """
    --version. EXE собран с --noconsole: своей консоли нет,
    поэтому пишем в консоль, из которой запустили (cmd,
    PowerShell). Новая консоль закрылась бы сразу вместе
    с выводом.
    """
    # Первая строка — имя и версия (её разбирает install.sh)
    text = (
        f"{APP_NAME} {__version__}\n"
        f"{COPYRIGHT}\n"
        f"Лицензия {LICENSE_ID}: программа предоставляется «как есть», "
        f"без каких-либо гарантий.\n"
        f"Исходный код: {SOURCE_URL}"
    )

    if os.name == "nt" and sys.stdout is None:
        try:
            # ATTACH_PARENT_PROCESS = -1
            if ctypes.windll.kernel32.AttachConsole(-1):
                with open("CONOUT$", "w", encoding="utf-8") as out:
                    out.write("\n" + text + "\n")
                return
        except Exception:
            pass
        # Запуск не из консоли (например, ярлык) — своё окно
        if ensure_console():
            print(text)
            input("Нажмите Enter...")
        return

    print(text)


# FIX: список известных CLI-флагов, чтобы предупреждать о неизвестных.
_KNOWN_FLAGS = {
    "--create-user",
    "--init-user",
    "--service",
    "--install-service",
    "--uninstall-service",
    "--stop-service",
    "--start-service",
    "--restart-service",
    "--skip-service-install",
    "--version",
    "--foreground",
}


def tolerant_stdio() -> None:
    """
    Вывод в канал или файл в кодировке без кириллицы (cp1252 на
    английской Windows, LANG=C в Linux): непредставимые символы
    заменяются на «?», а не роняют программу UnicodeEncodeError.
    В окне консоли Windows Python пишет Юникодом — там не влияет.
    """
    # strict, а в канал на Windows — surrogateescape: оба падают
    # на символе, которого нет в кодировке
    lenient = {"replace", "backslashreplace", "xmlcharrefreplace", "namereplace", "ignore"}
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is not None and stream.errors not in lenient:
                stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass


def main() -> int:

    tolerant_stdio()

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
    # VERSION
    # -----------------------------------------------------

    if "--version" in args:
        print_version()
        return 0

    # -----------------------------------------------------
    # CREATE USER
    # -----------------------------------------------------

    if "--create-user" in args:
        return create_user()

    if "--init-user" in args:
        return init_user_from_env()

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

    return run_application(
        skip_service_install=skip_service_install,
        foreground="--foreground" in args,
    )


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