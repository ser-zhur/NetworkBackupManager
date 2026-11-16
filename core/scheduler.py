import time
import logging
import threading
from datetime import datetime, timedelta

from core.context import (
    SCHEDULER_FILE,
    load_json,
    save_json,
)


DEFAULT_CONFIG = {
    "enabled": False,
    "mode": "daily",
    "daily_time": "03:00",
    "interval_hours": 24,
    "last_run": None,
}


class BackupScheduler:
    def __init__(self, plugin_manager):
        self.plugin_manager = plugin_manager
        self.config = self.load_config()
        self._thread = None
        self._stop_event = threading.Event()
        self._job_running = threading.Event()

        # Защита от retry-storm: если save_config упал,
        # не пытаемся сразу заново. Ждём 5 минут.
        self._last_attempt_ts = None
        self._RETRY_COOLDOWN = timedelta(minutes=5)

    def load_config(self):
        """Загрузка настроек планировщика из data/scheduler.json"""
        return load_json(SCHEDULER_FILE, DEFAULT_CONFIG.copy())

    def save_config(self, new_config=None):
        """Сохранение настроек планировщика в data/scheduler.json"""
        if new_config:
            self.config.update(new_config)

        save_json(SCHEDULER_FILE, self.config)

    def start(self):
        """Запуск фонового потока планировщика"""
        if self._thread and self._thread.is_alive():
            return

        self._stop_event.clear()

        self._thread = threading.Thread(
            target=self._run_loop,
            daemon=True,
            name="BackupScheduler",
        )

        self._thread.start()

        logging.info("⏰ Фоновый планировщик задач запущен.")

    def stop(self):
        """Остановка фонового потока"""
        self._stop_event.set()

        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)

        logging.info("⏰ Фоновый планировщик задач остановлен.")

    def _run_loop(self):
        """Главный цикл планировщика"""

        while not self._stop_event.is_set():

            try:
                loaded = self.load_config()

                # ---- Защита от потери last_run при сбое save ----
                # Если в памяти last_run новее, чем на диске —
                # значит предыдущий save_config упал. Не даём
                # себе повторно запускать бэкап из-за этого.
                mem_last = self.config.get("last_run") if self.config else None
                if mem_last:
                    try:
                        mem_dt = datetime.strptime(mem_last, "%Y-%m-%d %H:%M:%S")
                        disk_last = loaded.get("last_run")
                        disk_dt = (
                            datetime.strptime(disk_last, "%Y-%m-%d %H:%M:%S")
                            if disk_last else datetime.min
                        )
                        if mem_dt > disk_dt:
                            loaded["last_run"] = mem_last
                    except ValueError:
                        pass

                self.config = loaded

                if self.config.get("enabled", False):

                    # ---- Cooldown после неудачной попытки ----
                    if (
                        self._last_attempt_ts is not None
                        and (datetime.now() - self._last_attempt_ts)
                            < self._RETRY_COOLDOWN
                    ):
                        self._stop_event.wait(30)
                        continue

                    if self._should_run():

                        if self._job_running.is_set():
                            logging.warning(
                                "⚠ Предыдущее задание ещё выполняется, пропуск."
                            )
                        else:
                            self._job_running.set()
                            self._last_attempt_ts = datetime.now()

                            try:
                                logging.info(
                                    "🚀 Запуск автоматического бэкапа "
                                    "всех устройств."
                                )

                                if self.plugin_manager:
                                    self.plugin_manager.run_all_backups()

                                self.config["last_run"] = (
                                    datetime.now().strftime(
                                        "%Y-%m-%d %H:%M:%S"
                                    )
                                )

                                try:
                                    self.save_config()
                                except Exception as save_exc:
                                    # НЕ пробрасываем — иначе бэкап
                                    # будет запускаться снова каждые
                                    # 30 секунд. last_run в памяти
                                    # сохранён, cooldown активен.
                                    logging.error(
                                        "❌ Не удалось сохранить "
                                        "scheduler.json: %s",
                                        save_exc,
                                    )

                                logging.info(
                                    "✅ Автоматический бэкап завершён."
                                )

                                try:
                                    from core.maintenance import (
                                        post_backup_cleanup,
                                    )
                                    post_backup_cleanup(
                                        self.plugin_manager
                                    )
                                except Exception as cleanup_exc:
                                    logging.error(
                                        "❌ Ошибка автоочистки: %s",
                                        cleanup_exc,
                                        exc_info=True,
                                    )

                            except Exception as e:
                                logging.error(
                                    f"❌ Ошибка автоматического бэкапа: {e}"
                                )

                            finally:
                                self._job_running.clear()

            except Exception as e:
                logging.error(
                    f"❌ Ошибка в цикле планировщика: {e}"
                )

            self._stop_event.wait(30)


    def _should_run(self) -> bool:
        """Проверка условия наступления времени бэкапа."""

        now = datetime.now()
        last_run_str = self.config.get("last_run")
        mode = self.config.get("mode", "daily")

        # ---- Разбор last_run ----
        last_run = None
        if last_run_str:
            try:
                last_run = datetime.strptime(
                    last_run_str,
                    "%Y-%m-%d %H:%M:%S",
                )
            except ValueError:
                logging.warning(
                    "Некорректный last_run в scheduler.json: %r; "
                    "считаем, что бэкапов ещё не было",
                    last_run_str,
                )
                last_run = None

        # ---- ЕЖЕДНЕВНЫЙ РЕЖИМ ----
        if mode == "daily":

            target_time_str = self.config.get("daily_time", "03:00")

            try:
                target_time = datetime.strptime(
                    target_time_str,
                    "%H:%M",
                ).time()
            except ValueError:
                logging.warning(
                    "Некорректный daily_time в scheduler.json: %r; "
                    "использую 03:00",
                    target_time_str,
                )
                target_time = datetime.strptime("03:00", "%H:%M").time()

            target_dt = datetime.combine(now.date(), target_time)

            # Пока цель не наступила — не запускаем.
            if now < target_dt:
                return False

            # Первый запуск: цель наступила, last_run ещё нет.
            if last_run is None:
                return True

            # Догоняющий запуск: если приложение было выключено
            # в момент цели (или было занято), бэкап отработает
            # при первой возможности, а не пропустится до завтра.
            return last_run < target_dt

        # ---- ИНТЕРВАЛЬНЫЙ РЕЖИМ ----
        if mode == "interval":

            try:
                interval_h = int(self.config.get("interval_hours", 24))
            except (TypeError, ValueError):
                logging.warning(
                    "Некорректный interval_hours в scheduler.json: %r; "
                    "использую 24",
                    self.config.get("interval_hours"),
                )
                interval_h = 24

            if interval_h < 1:
                interval_h = 1

            if last_run is None:
                return True

            return (now - last_run) >= timedelta(hours=interval_h)

        return False

    def get_status_text(self) -> str:
        """Описание статуса для веб-интерфейса"""

        if not self.config.get("enabled", False):
            return "Автобэкап отключён"

        if self._job_running.is_set():
            return "Выполняется бэкап..."

        mode = self.config.get("mode", "daily")
        last_run = self.config.get("last_run") or "никогда"

        if mode == "daily":
            return (
                f"Ежедневно в {self.config.get('daily_time', '03:00')} "
                f"(последний запуск: {last_run})"
            )

        return (
            f"Каждые {self.config.get('interval_hours', 24)} ч. "
            f"(последний запуск: {last_run})"
        )