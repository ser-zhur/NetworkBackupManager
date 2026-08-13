import sys
import webbrowser
import uvicorn

from core.context import (
    ensure_directories,
    ensure_default_files,
    load_json,
    CONFIG_FILE,
)
from core.logger import setup_logging


def bootstrap():
    ensure_directories()
    ensure_default_files()


def run_server():
    config = load_json(CONFIG_FILE)

    host = config.get("host", "127.0.0.1")
    port = config.get("port", 8080)

    uvicorn.run(
        "web.server:app",
        host=host,
        port=port,
        reload=False,
        workers=1,
    )


def open_browser():
    config = load_json(CONFIG_FILE)

    if config.get("open_browser", True):
        url = f"http://127.0.0.1:{config.get('port', 8080)}"
        try:
            webbrowser.open(url)
        except Exception:
            pass


if __name__ == "__main__":
    bootstrap()
    setup_logging()

    service_mode = "--service" in sys.argv

    if not service_mode:
        open_browser()

    run_server()