import sys
import logging
import webbrowser
import uvicorn

from core.context import (
    ensure_directories,
    ensure_default_files,
    load_json,
    CONFIG_FILE,
)
from core.logger import setup_logging
from core.service_installer import (
    is_admin,
    is_http_up,
    is_service_registered,
    install_service,
    relaunch_elevated,
    start_service,
    wait_for_http,
)


def bootstrap():
    ensure_directories()
    ensure_default_files()


def app_url() -> str:
    config = load_json(CONFIG_FILE, {})
    return f"http://127.0.0.1:{config.get('port', 8080)}"


def run_server():
    config = load_json(CONFIG_FILE, {})

    host = config.get("host", "127.0.0.1")
    port = config.get("port", 8080)

    uvicorn.run(
        "web.server:app",
        host=host,
        port=port,
        reload=False,
        workers=1,
        log_config=None,
        access_log=False
    )


def open_browser():
    config = load_json(CONFIG_FILE, {})

    if config.get("open_browser", True):
        try:
            webbrowser.open(app_url())
        except Exception:
            pass


def _install_and_start() -> bool:
    ok, msg = install_service()
    if ok:
        logging.info(msg)
    else:
        logging.error(msg)
        return False
    wait_for_http(timeout=25)
    return True


def handle_startup() -> bool:
    """
    Первый запуск ставит службу и выходит, если веб уже слушает порт.
    Возвращает True, если нужно поднять uvicorn в этом процессе.
    """
    if "--service" in sys.argv:
        return True

    if "--skip-service-install" in sys.argv:
        logging.info("Пропуск установки службы (--skip-service-install).")
        return True

    if "--install-and-exit" in sys.argv:
        if not is_admin():
            logging.error("Установка службы требует прав администратора.")
            sys.exit(1)
        _install_and_start()
        open_browser()
        sys.exit(0)

    if not is_service_registered():
        logging.info("Служба ещё не установлена — регистрация при первом запуске.")
        if is_admin():
            _install_and_start()
            open_browser()
            if is_http_up():
                sys.exit(0)
            logging.warning("Служба не ответила на порту — запуск в текущем процессе.")
            return True

        if relaunch_elevated(["--install-and-exit"]):
            logging.info("Ожидание запуска службы после повышения прав...")
            if wait_for_http(40):
                sys.exit(0)
            logging.warning("После запроса прав веб-сервер не ответил.")

        logging.warning(
            "Не удалось установить службу. "
            "Приложение будет работать без автозагрузки."
        )
        return True

    if not is_http_up():
        ok, msg = start_service()
        if ok:
            logging.info(msg)
            wait_for_http(timeout=25)
        else:
            logging.warning(msg)

    if is_http_up():
        open_browser()
        sys.exit(0)

    logging.warning("Служба установлена, но не запустилась — интерактивный режим.")
    return True


if __name__ == "__main__":
    bootstrap()
    setup_logging()

    should_run = handle_startup()
    if not should_run:
        sys.exit(0)

    if "--service" not in sys.argv:
        open_browser()

    run_server()
