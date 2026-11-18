# =========================================================
# tests/test_monitoring.py — /api/health, /api/status, токен,
# расчёт следующего планового запуска
# =========================================================

import json
from datetime import datetime, timedelta

import pytest

from core import context, device_status, events, monitoring
from core.scheduler import BackupScheduler

T = device_status.TIME_FORMAT


# =========================================================
# Планировщик: next_run
# =========================================================

def _sched(**cfg):
    s = BackupScheduler(None)
    s.config = {"enabled": True, "mode": "daily", "daily_time": "03:00",
                "interval_hours": 24, "last_run": None, **cfg}
    return s


NOW = datetime(2026, 11, 18, 10, 0, 0)


def test_next_run_disabled():
    assert _sched(enabled=False).next_run(NOW) is None


def test_next_run_daily():
    today3 = datetime(2026, 11, 18, 3, 0)
    assert _sched(last_run="2026-11-18 03:00:05").next_run(NOW) == today3 + timedelta(days=1)
    assert _sched(last_run="2026-11-17 03:00:05").next_run(NOW) == today3      # пропущен — пора
    assert _sched(last_run=None).next_run(NOW) == today3
    early = datetime(2026, 11, 18, 2, 0)
    assert _sched(last_run="2026-11-10 03:00:00").next_run(early) == today3    # ждём сегодняшнего


def test_next_run_interval():
    s = _sched(mode="interval", interval_hours=6, last_run="2026-11-18 08:00:00")
    assert s.next_run(NOW) == datetime(2026, 11, 18, 14, 0)
    assert _sched(mode="interval").next_run(NOW) == NOW


def test_next_run_agrees_with_should_run(monkeypatch):
    """next_run в прошлом ⇔ _should_run() == True."""
    import core.scheduler as sm

    for last in (None, "2026-11-17 03:00:00", "2026-11-18 03:00:01"):
        for hour in (1, 3, 10, 23):
            now = datetime(2026, 11, 18, hour, 0)

            class FakeDT(datetime):
                @classmethod
                def now(cls, tz=None):
                    return now

            monkeypatch.setattr(sm, "datetime", FakeDT)
            s = _sched(last_run=last)
            assert (s.next_run(now) <= now) == s._should_run(), (last, hour)


# =========================================================
# Сводка
# =========================================================

class FakeScheduler:
    def __init__(self, alive=True, next_run=None, enabled=True):
        self.config = {"enabled": enabled, "mode": "daily", "daily_time": "03:00"}
        self._alive, self._next = alive, next_run

    def is_alive(self):
        return self._alive

    def is_job_running(self):
        return False

    def next_run(self, now=None):
        return self._next


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setattr(context, "DATA_DIR", tmp_path)
    monkeypatch.setattr(context, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.delenv("NBM_API_TOKEN", raising=False)
    events.reset()
    yield tmp_path
    events.reset()


def _pm(devs):
    from core.plugin_manager import PluginManager
    pm = PluginManager()
    pm._plugins["fake"] = {"func": lambda r: [], "routers": devs}
    return pm


def test_status_ok_and_problems(data):
    now = datetime.now()
    devs = [
        {"id": "00000000000a", "name": "Good", "host": "10.0.0.1"},
        {"id": "00000000000b", "name": "Bad", "host": "10.0.0.2"},
        {"id": "00000000000c", "name": "New", "host": "10.0.0.3"},
        {"id": "00000000000d", "name": "Off", "host": "10.0.0.4", "disabled": True},
    ]
    good = device_status.record_attempt({"last_run": now.strftime(T)}, True)
    bad = device_status.record_attempt({"last_run": now.strftime(T)}, False, "timed out")
    context.STATE_FILE.write_text(json.dumps({"00000000000a": good, "00000000000b": bad}),
                                  encoding="utf-8")

    st = monitoring.status(_pm(devs), FakeScheduler(next_run=now + timedelta(hours=5)), now=now)

    assert st["status"] == "error" and st["severity"] == 2
    assert st["counts"] == {"total": 4, "enabled": 3, "ok": 1, "warn": 0, "fail": 1,
                            "none": 1, "disabled": 1}
    assert st["problems"] == ["Bad: timed out"]
    assert st["warnings"] == ["New: бэкапов ещё не было"]
    by_id = {d["id"]: d for d in st["devices"]}
    assert by_id["00000000000b"]["severity"] == 2 and by_id["00000000000b"]["failures"] == 1
    assert by_id["00000000000d"]["status"] == "disabled"
    assert st["scheduler"]["overdue"] is False


def test_status_scheduler_problems(data):
    now = datetime.now()
    st = monitoring.status(_pm([]), FakeScheduler(alive=False, next_run=now - timedelta(hours=2)), now=now)
    assert st["status"] == "error"
    assert "Планировщик не работает" in st["problems"]
    assert any("не начался" in p for p in st["problems"])


def test_status_last_run_from_events(data):
    events.emit(events.RUN_FINISHED, "Прогон завершён: успешно 1 из 1", total=1, success=1)
    st = monitoring.status(_pm([]), None)
    assert st["last_run"]["total"] == 1 and st["last_run"]["message"].startswith("Прогон")
    assert st["status"] == "ok"


# =========================================================
# Токен
# =========================================================

def test_token_lifecycle(data, monkeypatch):
    assert monitoring.token_info()["configured"] is False
    assert monitoring.check_token("x") is False

    t1 = monitoring.generate_token()
    assert monitoring.check_token(t1) and not monitoring.check_token(t1 + "x")
    assert t1 not in (data / "monitoring.json").read_text(encoding="utf-8")   # только хэш

    t2 = monitoring.generate_token()
    assert monitoring.check_token(t2) and not monitoring.check_token(t1)

    monitoring.revoke_token()
    assert not monitoring.check_token(t2)

    monkeypatch.setenv("NBM_API_TOKEN", "from-env")
    assert monitoring.check_token("from-env") and monitoring.token_info()["from_env"]


# =========================================================
# HTTP
# =========================================================

@pytest.fixture
def client(data, monkeypatch):
    s = pytest.importorskip("web.server")
    pytest.importorskip("httpx2", reason="нужен httpx2 для TestClient")
    from fastapi.testclient import TestClient

    from core import auth
    auth_file = data / "auth.json"
    monkeypatch.setattr(context, "AUTH_FILE", auth_file)
    monkeypatch.setattr(s, "AUTH_FILE", auth_file)
    auth.save_user("admin", "pw")
    monkeypatch.setattr(s, "scheduler", FakeScheduler(next_run=datetime.now() + timedelta(hours=1)))
    if s.plugin_manager is not None:
        monkeypatch.setattr(s.plugin_manager, "_plugins", {})
    return s, TestClient(s.app)


def test_health_is_public(client, monkeypatch):
    s, c = client
    r = c.get("/api/health")
    assert r.status_code == 200 and r.json()["status"] == "ok" and r.json()["version"]

    monkeypatch.setattr(s, "scheduler", FakeScheduler(alive=False))
    r = c.get("/api/health")
    assert r.status_code == 503 and "Планировщик не работает" in r.json()["problems"]


def test_status_requires_token_or_password(client):
    s, c = client
    assert c.get("/api/status").status_code == 401
    assert c.get("/api/status", headers={"Authorization": "Bearer nope"}).status_code == 401

    token = monitoring.generate_token()
    r = c.get("/api/status", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200 and r.json()["app"]["version"]

    assert c.get("/api/status", auth=("admin", "pw")).status_code == 200
    assert c.get("/api/status", auth=("admin", "bad")).status_code == 401

    r = c.get("/api/events?limit=5", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200 and "events" in r.json()


def test_token_does_not_open_other_pages(client):
    s, c = client
    token = monitoring.generate_token()
    r = c.get("/settings", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401


def test_settings_tab_and_token_shown_once(client):
    s, c = client
    page = c.get("/settings?tab=monitoring", auth=("admin", "pw"))
    assert page.status_code == 200 and "Токен не создан" in page.text
    assert "/api/health" in page.text

    r = c.post("/settings/monitoring/token", auth=("admin", "pw"))
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    token = r.text.split('id="new-token"', 1)[1].split(">", 1)[1].split("<", 1)[0].strip()
    assert monitoring.check_token(token)

    page = c.get("/settings?tab=monitoring", auth=("admin", "pw")).text
    assert token not in page and "Токен создан" in page

    r = c.post("/settings/monitoring/token/revoke", auth=("admin", "pw"), follow_redirects=False)
    assert r.status_code == 303 and not monitoring.check_token(token)
