# =========================================================
# core/notifications.py — уведомления по событиям
# =========================================================
#
# Подписан на core/events. Каналы и их правила — в
# data/notifications.json:
#
#     {"channels": [{
#         "id": "a1b2c3d4", "type": "telegram", "name": "Админы",
#         "enabled": true,
#         "settings": {"bot_token": "<зашифровано>", "chat_id": "…"},
#         "rules": {"errors": true, "recovered": true,
#                   "run": "errors", "config": true,
#                   "include_diff": false, "app": false}
#     }]}
#
# Правила:
#   errors       бэкап устройства или выгрузка в хранилище не
#                удались, сбой планировщика
#   recovered    снова успешно после ошибки
#   run          итог массового прогона: always / errors / never
#   config       конфигурация устройства изменилась
#   include_diff прикладывать текст изменений (в нём могут быть
#                секреты конфигурации — по умолчанию нет)
#   app          программа запущена
#
# Группировка: события устройств во время массового прогона
# копятся и уходят ОДНИМ сообщением вместе с его итогом —
# ночной прогон с ошибками на 19 устройствах даёт одно
# сообщение, а не двадцать.
#
# Отправка — в отдельном потоке (бэкапы не ждут сети), до
# трёх попыток. Ошибки отправки — в лог и в состояние канала
# (видно в настройках); событий не порождают, чтобы не
# зациклиться.
#
# =========================================================

from __future__ import annotations

import logging
import queue
import threading
import time
import uuid
from datetime import datetime

from core import backup_state, context, events
from core.device_status import now_str
from core.notify_channels import CHANNELS, Message, NotifyError

logger = logging.getLogger(__name__)

DEFAULT_RULES = {
    "errors": True,
    "recovered": True,
    "run": "errors",
    "config": True,
    "include_diff": False,
    "app": False,
}
RUN_CHOICES = ("always", "errors", "never")

ERROR_KINDS = {events.BACKUP_FAILED, events.STORAGE_FAILED, events.SCHEDULER_ERROR}
RECOVERED_KINDS = {events.BACKUP_RECOVERED, events.STORAGE_RECOVERED}
# События устройств: во время массового прогона копятся до его итога.
DEVICE_KINDS = ERROR_KINDS - {events.SCHEDULER_ERROR} | RECOVERED_KINDS | {events.CONFIG_CHANGED}

ICONS = {
    events.BACKUP_FAILED: "❌",
    events.STORAGE_FAILED: "❌",
    events.SCHEDULER_ERROR: "⚠️",
    events.BACKUP_RECOVERED: "✅",
    events.STORAGE_RECOVERED: "✅",
    events.CONFIG_CHANGED: "✏️",
    events.RUN_FINISHED: "🏁",
    events.APP_STARTED: "▶️",
}

RETRY_DELAYS = (5, 30)          # между попытками, секунды
PENDING_MAX_AGE = 3 * 3600      # копленное без итога прогона — отправить


# =========================================================
# Настройки
# =========================================================

def _config_file():
    return context.DATA_DIR / "notifications.json"


def _storage():
    from core.secure_storage import get_secure_storage
    return get_secure_storage()


def load_config() -> dict:
    data = context.load_json(_config_file(), {})
    channels = data.get("channels") if isinstance(data, dict) else None
    out = []
    for ch in channels if isinstance(channels, list) else []:
        if isinstance(ch, dict) and ch.get("type") in CHANNELS and ch.get("id"):
            ch.setdefault("name", CHANNELS[ch["type"]].name)
            ch.setdefault("enabled", True)
            ch.setdefault("settings", {})
            ch["rules"] = {**DEFAULT_RULES, **(ch.get("rules") or {})}
            out.append(ch)
    return {"channels": out}


def save_config(config: dict) -> None:
    context.save_json(_config_file(), {"channels": config.get("channels", [])})


def get_channel(channel_id: str) -> dict | None:
    return next((c for c in load_config()["channels"] if c["id"] == channel_id), None)


def new_channel(channel_type: str) -> dict:
    return {
        "id": uuid.uuid4().hex[:8],
        "type": channel_type,
        "name": CHANNELS[channel_type].name,
        "enabled": True,
        "settings": {},
        "rules": dict(DEFAULT_RULES),
    }


def encrypt_settings(channel_type: str, plain: dict, previous: dict | None = None) -> dict:
    """
    Значения из формы → хранимые. Секретные поля шифруются;
    пустое секретное поле — оставить прежнее значение.
    """
    previous = previous or {}
    secrets_ = set(CHANNELS[channel_type].secret_fields())
    out = {}
    for f in CHANNELS[channel_type].fields:
        value = plain.get(f.name, "")
        if f.name in secrets_:
            out[f.name] = _storage().encrypt(value) if value else previous.get(f.name, "")
        else:
            out[f.name] = value
    return out


def decrypt_settings(channel: dict) -> dict:
    settings = dict(channel.get("settings") or {})
    for name in CHANNELS[channel["type"]].secret_fields():
        if settings.get(name):
            settings[name] = _storage().decrypt(settings[name])
    return settings


def normalize_rules(raw: dict) -> dict:
    rules = dict(DEFAULT_RULES)
    for key in ("errors", "recovered", "config", "include_diff", "app"):
        rules[key] = bool(raw.get(key))
    run = raw.get("run")
    rules["run"] = run if run in RUN_CHOICES else DEFAULT_RULES["run"]
    return rules


# =========================================================
# Кому что
# =========================================================

def wants(rules: dict, event: dict) -> bool:
    kind = event.get("kind")
    if kind in ERROR_KINDS:
        return bool(rules.get("errors"))
    if kind in RECOVERED_KINDS:
        return bool(rules.get("recovered"))
    if kind == events.CONFIG_CHANGED:
        return bool(rules.get("config"))
    if kind == events.RUN_FINISHED:
        run = rules.get("run", "errors")
        return run == "always" or (run == "errors" and event.get("level") == "error")
    if kind == events.APP_STARTED:
        return bool(rules.get("app"))
    return False


def _line(event: dict) -> str:
    return f"{ICONS.get(event.get('kind'), '•')} {event.get('message', '')}"


def _diff_lines(event: dict, rules: dict) -> list[str]:
    diff = (event.get("private") or {}).get("diff")
    if event.get("kind") == events.CONFIG_CHANGED and rules.get("include_diff") and diff:
        return [diff]
    return []


def build_message(batch: list[dict], rules: dict) -> Message | None:
    """
    Сообщение для одного канала из пачки событий: одно событие
    или итог прогона с событиями устройств за прогон.
    None — каналу из этой пачки ничего не нужно.

    Если канал не хочет итог прогона, но хочет ошибки за
    прогон, заголовок всё равно — итог: так понятнее, к чему
    относятся строки.
    """
    picked = [e for e in batch if wants(rules, e)]
    if not picked:
        return None

    run = next((e for e in batch if e.get("kind") == events.RUN_FINISHED), None)
    details = [e for e in picked if e is not run]
    level = "error" if any(e.get("level") == "error" for e in picked) else "info"

    if run is not None:
        source = (run.get("data") or {}).get("source")
        what = "Плановый бэкап" if source == "schedule" else "Бэкап всех устройств"
        subject = f"{ICONS[events.RUN_FINISHED]} {what}: " + run["message"].removeprefix(
            "Прогон завершён: ")
    elif len(details) == 1:
        subject = _line(details[0])
    else:
        subject = f"Событий: {len(details)}"

    lines: list[str] = []
    if run is not None or len(details) > 1:
        for e in details:
            lines.append(_line(e))
            lines += _diff_lines(e, rules)
    else:
        lines += _diff_lines(details[0], rules)

    if run is not None and not details:
        d = run.get("data") or {}
        lines.append(
            f"Всего {d.get('total', 0)}, успешно {d.get('success', 0)}, "
            f"ошибок {d.get('failed', 0)}"
            + (f", снято по таймауту {d['timed_out']}" if d.get("timed_out") else "")
        )

    lines += ["", picked[-1].get("at") or now_str()]
    return Message(subject=subject, text="\n".join(lines).strip(), level=level, events=picked)


# =========================================================
# Рассылка
# =========================================================

class Dispatcher:
    def __init__(self):
        self._queue: queue.Queue = queue.Queue()
        self._pending: list[dict] = []
        self._pending_since: float | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.status: dict[str, dict] = {}     # id канала → последняя отправка

    # ---- жизненный цикл ----

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        # Свежая очередь: в старой мог остаться сигнал остановки
        # от повторного stop() — он завершил бы новый поток сразу.
        self._queue = queue.Queue()
        events.subscribe(self.on_event)
        self._thread = threading.Thread(target=self._run, name="Notifications", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        events.unsubscribe(self.on_event)
        self._stop.set()
        self._queue.put(None)
        if self._thread:
            self._thread.join(timeout=5)

    # ---- приём событий (в потоке того, кто вызвал emit) ----

    def on_event(self, event: dict) -> None:
        in_mass = backup_state.get_backup_status()["mass"]
        self._queue.put((event, in_mass))

    # ---- поток рассылки ----

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                item = self._queue.get(timeout=30)
            except queue.Empty:
                item = ()
            if item is None:
                break
            try:
                self.process(*item) if item else self.flush_stale()
            except Exception as exc:
                logger.error("Уведомления: %s", exc, exc_info=True)

    def process(self, event: dict, in_mass: bool) -> None:
        """Одно событие: копить до итога прогона или отправить."""
        kind = event.get("kind")
        if kind in DEVICE_KINDS and in_mass:
            if not self._pending:
                self._pending_since = time.monotonic()
            self._pending.append(event)
            return
        if kind == events.RUN_FINISHED:
            batch, self._pending = self._pending + [event], []
            self.deliver(batch)
            return
        # Прогон оборвался без итога — накопленное не теряем.
        if self._pending and not backup_state.get_backup_status()["mass"]:
            self.flush_pending()
        self.deliver([event])

    def flush_pending(self) -> None:
        batch, self._pending = self._pending, []
        if batch:
            self.deliver(batch)

    def flush_stale(self) -> None:
        if not self._pending:
            return
        too_old = time.monotonic() - (self._pending_since or 0) > PENDING_MAX_AGE
        if too_old or not backup_state.get_backup_status()["mass"]:
            self.flush_pending()

    def deliver(self, batch: list[dict]) -> None:
        for channel in load_config()["channels"]:
            if not channel.get("enabled"):
                continue
            message = build_message(batch, channel["rules"])
            if message is not None:
                self.send(channel, message)

    def send(self, channel: dict, message: Message, retries=RETRY_DELAYS) -> tuple[bool, str]:
        impl = CHANNELS[channel["type"]]
        settings = decrypt_settings(channel)
        error = impl.validate(settings)
        attempts = 0
        while not error:
            attempts += 1
            try:
                impl.send(settings, message)
                self._set_status(channel, True, "")
                return True, "Отправлено"
            except NotifyError as exc:
                error = str(exc)
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
            if attempts > len(retries) or self._stop.is_set():
                break
            logger.warning("Уведомления: «%s» — %s; повтор через %s с",
                           channel["name"], error, retries[attempts - 1])
            if self._stop.wait(retries[attempts - 1]):
                break
            error = ""
        logger.error("Уведомления: «%s» не отправлено — %s", channel["name"], error)
        self._set_status(channel, False, error)
        return False, error

    def send_test(self, channel: dict) -> tuple[bool, str]:
        """Проверочное сообщение сразу, без повторов (кнопка в настройках)."""
        message = Message(
            subject="Проверка уведомлений",
            text=f"Канал «{channel['name']}» настроен верно.\n\n{now_str()}",
            events=[],
        )
        return self.send(channel, message, retries=())

    def _set_status(self, channel: dict, ok: bool, error: str) -> None:
        with self._lock:
            self.status[channel["id"]] = {
                "ok": ok, "error": error,
                "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }


dispatcher = Dispatcher()
