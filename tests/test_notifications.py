# =========================================================
# tests/test_notifications.py — правила, сборка сообщений,
# группировка за прогон, каналы (сеть подменена)
# =========================================================

import json

import pytest

from core import backup_state, context, events, notifications, notify_channels
from core.notifications import DEFAULT_RULES, Dispatcher, build_message, wants
from core.notify_channels import Message, NotifyError


def ev(kind, message="m", level=None, **data):
    level = level or ("error" if kind in notifications.ERROR_KINDS else "info")
    return {"id": 1, "at": "2026-11-18 03:00:00", "kind": kind, "level": level,
            "message": message, "device_id": "d1", "data": data}


# =========================================================
# Правила
# =========================================================

def test_wants_defaults():
    r = dict(DEFAULT_RULES)
    assert wants(r, ev(events.BACKUP_FAILED))
    assert wants(r, ev(events.STORAGE_RECOVERED))
    assert wants(r, ev(events.CONFIG_CHANGED))
    assert not wants(r, ev(events.APP_STARTED))
    assert wants(r, ev(events.RUN_FINISHED, level="error"))
    assert not wants(r, ev(events.RUN_FINISHED, level="info"))
    assert wants({**r, "run": "always"}, ev(events.RUN_FINISHED, level="info"))
    assert not wants({**r, "run": "never"}, ev(events.RUN_FINISHED, level="error"))
    assert not wants({**r, "errors": False}, ev(events.SCHEDULER_ERROR))


def test_normalize_rules():
    r = notifications.normalize_rules({"errors": "on", "run": "bogus"})
    assert r["errors"] is True and r["recovered"] is False and r["run"] == "errors"


# =========================================================
# Сообщения
# =========================================================

def test_single_event_message():
    m = build_message([ev(events.BACKUP_FAILED, "R1: бэкап не удался — timeout")], DEFAULT_RULES)
    assert m.subject == "❌ R1: бэкап не удался — timeout"
    assert m.level == "error" and m.text.endswith("2026-11-18 03:00:00")


def test_run_batch_message():
    batch = [
        ev(events.BACKUP_FAILED, "R1: бэкап не удался — timeout"),
        ev(events.CONFIG_CHANGED, "R2: конфигурация изменилась (+1 −1)"),
        ev(events.RUN_FINISHED, "Прогон завершён: успешно 18 из 19, ошибок 1",
           level="error", source="schedule", total=19, success=18, failed=1),
    ]
    m = build_message(batch, DEFAULT_RULES)
    assert m.subject == "🏁 Плановый бэкап: успешно 18 из 19, ошибок 1"
    assert "❌ R1" in m.text and "✏️ R2" in m.text and m.level == "error"
    assert len(m.events) == 3


def test_run_never_but_errors_wanted_keeps_context():
    batch = [ev(events.BACKUP_FAILED, "R1: x"),
             ev(events.RUN_FINISHED, "Прогон завершён: успешно 1 из 2, ошибок 1", level="error")]
    m = build_message(batch, {**DEFAULT_RULES, "run": "never"})
    assert m.subject.startswith("🏁 Бэкап всех устройств") and "❌ R1: x" in m.text
    assert [e["kind"] for e in m.events] == [events.BACKUP_FAILED]


def test_clean_run_only_when_always():
    run = ev(events.RUN_FINISHED, "Прогон завершён: успешно 3 из 3", total=3, success=3, failed=0)
    assert build_message([run], DEFAULT_RULES) is None
    m = build_message([run], {**DEFAULT_RULES, "run": "always"})
    assert "Всего 3, успешно 3, ошибок 0" in m.text


def test_diff_only_when_enabled():
    e = ev(events.CONFIG_CHANGED, "R1: изменилась")
    e["private"] = {"diff": "+secret"}
    assert "+secret" not in build_message([e], DEFAULT_RULES).text
    assert "+secret" in build_message([e], {**DEFAULT_RULES, "include_diff": True}).text


# =========================================================
# Рассыльщик
# =========================================================

@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(context, "DATA_DIR", tmp_path)
    return tmp_path


class Recorder(notify_channels.Channel):
    type = "rec"
    fields = []

    def __init__(self, fail_times=0):
        self.sent, self.fail_times = [], fail_times

    def send(self, settings, message):
        if self.fail_times:
            self.fail_times -= 1
            raise NotifyError("сеть недоступна")
        self.sent.append(message)


@pytest.fixture
def rec(home, monkeypatch):
    r = Recorder()
    monkeypatch.setitem(notify_channels.CHANNELS, "rec", r)
    monkeypatch.setattr(notifications, "CHANNELS", notify_channels.CHANNELS)
    ch = notifications.new_channel("rec")
    ch["rules"]["run"] = "always"
    notifications.save_config({"channels": [ch]})
    return r


def test_mass_run_is_one_message(rec):
    d = Dispatcher()
    d.process(ev(events.BACKUP_FAILED, "R1: x"), in_mass=True)
    d.process(ev(events.BACKUP_FAILED, "R2: y"), in_mass=True)
    assert rec.sent == []
    d.process(ev(events.RUN_FINISHED, "Прогон завершён: успешно 0 из 2, ошибок 2",
                 level="error"), in_mass=True)
    (m,) = rec.sent
    assert "R1: x" in m.text and "R2: y" in m.text


def test_single_device_run_is_immediate(rec):
    d = Dispatcher()
    d.process(ev(events.BACKUP_FAILED, "R1: x"), in_mass=False)
    assert len(rec.sent) == 1


def test_pending_flushed_if_run_ended_without_summary(rec):
    d = Dispatcher()
    d.process(ev(events.BACKUP_FAILED, "R1: x"), in_mass=True)
    d.flush_stale()                                   # прогона уже нет
    assert len(rec.sent) == 1 and "R1: x" in rec.sent[0].subject


def test_disabled_channel_and_retries(rec, home):
    cfg = notifications.load_config()
    cfg["channels"][0]["enabled"] = False
    notifications.save_config(cfg)
    d = Dispatcher()
    d.process(ev(events.BACKUP_FAILED), in_mass=False)
    assert rec.sent == []

    cfg["channels"][0]["enabled"] = True
    notifications.save_config(cfg)
    rec.fail_times = 1
    ok, _ = d.send(cfg["channels"][0], Message("s", "t"), retries=(0,))
    assert ok and len(rec.sent) == 1

    rec.fail_times = 5
    ok, err = d.send(cfg["channels"][0], Message("s", "t"), retries=(0, 0))
    assert not ok and err == "сеть недоступна"
    assert d.status[cfg["channels"][0]["id"]]["ok"] is False


def test_subscription_end_to_end(rec):
    d = Dispatcher()
    d.start()
    try:
        assert backup_state.get_backup_status()["mass"] is False
        events.reset()
        events.emit(events.BACKUP_FAILED, "R9: z", level="error")
        d.stop()                                     # дождаться очереди
    finally:
        d.stop()
    assert any("R9: z" in m.subject for m in rec.sent)


def test_settings_secrets_encrypted(home):
    plain = {"bot_token": "123:SECRET", "chat_id": "42"}
    stored = notifications.encrypt_settings("telegram", plain)
    assert stored["chat_id"] == "42" and "SECRET" not in stored["bot_token"]
    ch = {"type": "telegram", "settings": stored}
    assert notifications.decrypt_settings(ch)["bot_token"] == "123:SECRET"
    again = notifications.encrypt_settings("telegram", {"bot_token": "", "chat_id": "43"}, stored)
    assert again["bot_token"] == stored["bot_token"]           # пустое — прежнее


# =========================================================
# Каналы (сеть подменена)
# =========================================================

@pytest.fixture
def http(monkeypatch):
    calls = []

    def fake(url, payload, headers=None):
        calls.append((url, payload, headers or {}))
        return fake.reply

    fake.reply = (200, b'{"ok": true}')
    monkeypatch.setattr(notify_channels, "_http_post", fake)
    return calls, fake


def test_telegram(http):
    calls, fake = http
    ch = notify_channels.CHANNELS["telegram"]
    ch.send({"bot_token": "T0K", "chat_id": "42"}, Message("Заголовок", "x" * 5000))
    url, payload, _ = calls[0]
    assert url.endswith("/botT0K/sendMessage") and payload["chat_id"] == "42"
    assert payload["text"].startswith("Заголовок") and len(payload["text"]) <= 4000

    fake.reply = (401, b'{"ok": false, "description": "Unauthorized"}')
    with pytest.raises(NotifyError) as e:
        ch.send({"bot_token": "T0K", "chat_id": "42"}, Message("s", "t"))
    assert "неверный токен" in str(e.value) and "T0K" not in str(e.value)

    assert ch.validate({"bot_token": "", "chat_id": "1"}).startswith("Не заполнено")


def test_webhook(http):
    calls, fake = http
    ch = notify_channels.CHANNELS["webhook"]
    ch.send({"url": "https://hook", "header_name": "X-Key", "header_value": "k"},
            Message("S", "body", level="error", events=[ev(events.BACKUP_FAILED, "R1")]))
    url, payload, headers = calls[0]
    assert payload["text"] == "S\n\nbody" and payload["level"] == "error"
    assert payload["events"][0]["kind"] == events.BACKUP_FAILED and headers == {"X-Key": "k"}
    assert "private" not in json.dumps(payload)

    fake.reply = (500, b"oops")
    with pytest.raises(NotifyError, match="HTTP 500"):
        ch.send({"url": "https://hook"}, Message("s", "t"))
    assert "http" in ch.validate({"url": "ftp://x"})


def test_email(monkeypatch):
    sent = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout=None, context=None):
            sent["addr"] = (host, port)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self, context=None):
            sent["tls"] = True

        def login(self, user, password):
            sent["login"] = (user, password)

        def send_message(self, msg):
            sent["msg"] = msg

    monkeypatch.setattr(notify_channels.smtplib, "SMTP", FakeSMTP)
    ch = notify_channels.CHANNELS["email"]
    settings = {"host": "smtp.example.com", "port": "587", "security": "starttls",
                "username": "u", "password": "p", "sender": "nbm@example.com",
                "recipients": "a@example.com; b@example.com"}
    assert ch.validate(settings) == ""
    ch.send(settings, Message("Тема", "Текст"))
    msg = sent["msg"]
    assert sent["tls"] and sent["login"] == ("u", "p") and sent["addr"] == ("smtp.example.com", 587)
    assert msg["Subject"] == "[NBM] Тема" and msg["To"] == "a@example.com, b@example.com"
    assert "Текст" in msg.get_content()
    assert ch.validate({**settings, "port": "0"}) == "Некорректный порт"
