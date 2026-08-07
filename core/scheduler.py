import os
import json
import time
import logging
import threading
from datetime import datetime, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_FILE = BASE_DIR / "scheduler_config.json"

DEFAULT_CONFIG = {
    "enabled": True,
    "mode": "daily",          # Режимы: "daily" (ежедневно) или "interval" (каждые N часов)
    "daily_time": "03:00",    # Время для ежедневного режима (ЧЧ:ММ)
    "interval_hours": 24,     # Интервал в часах для интервального режима
    "last_run": None          # Дата и время последнего успешного запуска
}

class BackupScheduler:
    def __init__(self, plugin_manager):
        self.plugin_manager = plugin_manager
        self.config = self.load_config()
        self._thread = None
        self._stop_event = threading.Event()

    def load_config(self):
        """Загрузка настроек планировщика из JSON"""
        if CONFIG_FILE.exists():
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                    return {**DEFAULT_CONFIG, **cfg}
            except Exception as e:
                logging.error(f"❌ Ошибка чтения конфигурации планировщика: {e}")
        return DEFAULT_CONFIG.copy()

    def save_config(self, new_config=None):
        """Сохранение настроек планировщика в JSON"""
        if new_config:
            self.config.update(new_config)
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(self.config, f, ensure_ascii=False, indent=4)
        except Exception as e:
            logging.error(f"❌ Ошибка сохранения конфигурации планировщика: {e}")

    def start(self):
        """Запуск фонового потока планировщика"""
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        logging.info("⏰ Фоновый планировщик задач запущен.")

    def stop(self):
        """Остановка фонового потока"""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=3)
        logging.info("⏰ Фоновый планировщик задач остановлен.")

    def _run_loop(self):
        """Главный цикл фоновой проверки времени"""
        while not self._stop_event.is_set():
            try:
                if self.config.get("enabled", True):
                    if self._should_run():
                        logging.info("⏰ [Планировщик] Наступило время автоматического бэкапа всех устройств...")
                        if self.plugin_manager:
                            # Выполняется бэкап ВСЕХ роутеров
                            self.plugin_manager.run_all_backups()
                        
                        self.config["last_run"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        self.save_config()

            except Exception as e:
                logging.error(f"❌ Ошибка в работе фонового планировщика: {e}")

            # Проверка условий каждые 30 секунд
            for _ in range(30):
                if self._stop_event.is_set():
                    break
                time.sleep(1)

    def _should_run(self) -> bool:
        """Проверка условия наступления времени бэкапа"""
        now = datetime.now()
        last_run_str = self.config.get("last_run")

        mode = self.config.get("mode", "daily")

        # Если бэкап еще ни разу не запускался
        if not last_run_str:
            if mode == "daily":
                return now.strftime("%H:%M") == self.config.get("daily_time", "03:00")
            return True

        try:
            last_run = datetime.strptime(last_run_str, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return True

        if mode == "daily":
            target_time = self.config.get("daily_time", "03:00")
            # Запуск происходит, если сегодня еще не запускали и время совпало
            if last_run.date() < now.date() and now.strftime("%H:%M") == target_time:
                return True
        elif mode == "interval":
            interval_h = int(self.config.get("interval_hours", 24))
            if now - last_run >= timedelta(hours=interval_h):
                return True

        return False

    def get_status_text(self) -> str:
        """Форматированное описание текущего статуса для веб-панели"""
        if not self.config.get("enabled", True):
            return "Автобэкап отключен"

        mode = self.config.get("mode", "daily")
        last_run = self.config.get("last_run") or "Никогда"

        if mode == "daily":
            return f"Ежедневно в {self.config.get('daily_time', '03:00')} (посл. запуск: {last_run})"
        else:
            return f"Каждые {self.config.get('interval_hours', 24)} ч. (посл. запуск: {last_run})"