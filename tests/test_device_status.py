# =========================================================
# tests/test_device_status.py — исход каждой попытки бэкапа
# =========================================================

import json
from datetime import datetime, timedelta

import pytest

from core import backup_state, device_status
from core import plugin_manager as pm_module
from core.plugin_manager import PluginManager

T = device_status.TIME_FORMAT


def _ago(days):
    return (datetime.now() - timedelta(days=days)).strftime(T)


# ---- summarize ----

def test_no_state_is_none():
    s = device_status.summarize({})
    assert s["status"] == "none" and s["last_attempt"] is None
    assert s["last_attempt_ok"] is None and s["failures"] == 0


def test_legacy_entry_without_attempt():
    s = device_status.summarize({"last_run": _ago(1), "backends": {"git": {"ok": True}}})
    assert s["status"] == "ok" and s["last_attempt_ok"] is True
    assert s["last_attempt"] == s["last_success"]


def test_failed_attempt_makes_fresh_device_fail():
    entry = device_status.record_attempt({"last_run": _ago(1)}, False, "timed out")
    s = device_status.summarize(entry)
    assert s["status"] == "fail"
    assert s["last_error"] == "timed out" and s["failures"] == 1
    assert s["success_age_days"] == 1                 # успешный опрос — прежний


def test_failures_count_and_reset():
    e = device_status.record_attempt({}, False, "a")
    e = device_status.record_attempt(e, False, "b")
    assert device_status.summarize(e)["failures"] == 2
    e = device_status.record_attempt(e, True)
    assert e["attempt"]["failures"] == 0 and e["attempt"]["error"] == ""


def test_backend_error_is_reported():
    s = device_status.summarize({
        "last_run": _ago(0),
        "backends": {"git": {"ok": True}, "webdav": {"ok": False, "message": "401"}},
        "attempt": {"at": _ago(0), "ok": True, "failures": 0},
    })
    assert s["status"] == "fail" and s["backup_ok"] is True
    assert s["backends_ok"] is False and s["last_error"] == "webdav: 401"


def test_disabled_wins():
    e = device_status.record_attempt({}, False, "x")
    assert device_status.summarize(e, disabled=True)["status"] == "disabled"


@pytest.mark.parametrize("age, status", [(0, "ok"), (14, "ok"), (15, "warn"), (31, "fail")])
def test_age_thresholds(age, status):
    assert device_status.summarize({"last_run": _ago(age)})["status"] == status


# ---- запись в state.json ----

@pytest.fixture
def pm(tmp_path, monkeypatch):
    state = tmp_path / "state.json"
    monkeypatch.setattr(pm_module, "STATE_FILE", state)
    m = PluginManager()
    m.state_path = state
    return m


def _state(pm):
    return json.loads(pm.state_path.read_text(encoding="utf-8"))


def _add(pm, func, n=1, tmp=None):
    routers = [
        {"id": f"{i:012x}", "host": f"10.0.0.{i}", "backup_path": str(tmp / f"d{i}")}
        for i in range(n)
    ]
    pm._plugins["fake"] = {"func": func, "routers": routers}
    return routers


def test_backup_device_records_failure(pm, tmp_path):
    def boom(router):
        raise ConnectionError("No route to host")

    (router,) = _add(pm, boom, tmp=tmp_path)
    out = pm.backup_device("fake", router, 0)

    assert out["ok"] is False and out["error"] == "No route to host"
    attempt = _state(pm)[router["id"]]["attempt"]
    assert attempt["ok"] is False and attempt["failures"] == 1
    assert attempt["error"] == "No route to host"


def test_backup_device_success_and_plugin_none(pm, tmp_path):
    routers = _add(pm, lambda r: [], n=1, tmp=tmp_path)
    assert pm.backup_device("fake", routers[0], 0)["ok"] is True
    assert _state(pm)[routers[0]["id"]]["attempt"]["ok"] is True

    pm._plugins["fake"]["func"] = lambda r: None      # нарушение контракта
    out = pm.backup_device("fake", routers[0], 0)
    assert out["ok"] is False and "None" in out["error"]


def test_backend_state_keeps_attempt(pm, tmp_path):
    (router,) = _add(pm, lambda r: [], tmp=tmp_path)
    pm.record_attempt(router["id"], False, "x")
    pm._save_backend_state(router["id"], {})
    entry = _state(pm)[router["id"]]
    assert entry["attempt"]["error"] == "x" and "last_run" in entry


def test_mass_run_summary_and_records(pm, tmp_path):
    calls = []

    def plugin(router):
        calls.append(router["host"])
        if router["host"].endswith(".1"):
            raise RuntimeError("auth failed")
        return []

    routers = _add(pm, plugin, n=3, tmp=tmp_path)
    routers.append({"id": "0000000000ff", "host": "x", "backup_path": "y", "disabled": True})

    summary = pm.run_all_backups()

    assert summary["total"] == 3 and summary["success"] == 2 and summary["failed"] == 1
    assert summary["skipped_disabled"] == 1 and summary["finished"]
    state = _state(pm)
    assert state[routers[1]["id"]]["attempt"]["error"] == "auth failed"
    assert state[routers[0]["id"]]["attempt"]["ok"] is True
    assert "0000000000ff" not in state
    assert not backup_state.is_backup_running()


def test_single_task_frees_slot_and_records(pm, tmp_path, monkeypatch):
    import web.server as server

    def boom(router):
        raise TimeoutError("timed out")

    (router,) = _add(pm, boom, tmp=tmp_path)
    monkeypatch.setattr(server, "plugin_manager", pm)

    assert backup_state.try_start_device(router["id"])
    server.execute_device_backup_task("fake", 0, router)

    assert backup_state.is_backup_running_for(router["id"]) is False
    assert _state(pm)[router["id"]]["attempt"]["error"] == "timed out"


def test_single_task_uses_id_not_index(pm, tmp_path, monkeypatch):
    """Индекс сдвинулся во время бэкапа — освобождается своё устройство."""
    import web.server as server

    routers = _add(pm, lambda r: [], n=2, tmp=tmp_path)
    monkeypatch.setattr(server, "plugin_manager", pm)
    target = routers[1]
    assert backup_state.try_start_device(target["id"])
    del routers[0]                                     # соседа удалили
    server.execute_device_backup_task("fake", 1, target)
    assert backup_state.is_backup_running_for(target["id"]) is False


def test_web_shows_failed_attempt(tmp_path, monkeypatch):
    s = pytest.importorskip("web.server")
    pytest.importorskip("httpx2", reason="нужен httpx2 для TestClient")
    from fastapi.testclient import TestClient

    if s.plugin_manager is None:
        pytest.skip("PluginManager не загружен")

    dev = {"id": "0000000000c1", "host": "10.0.0.1", "name": "Broken",
           "backup_path": str(tmp_path / "b")}
    (tmp_path / "b").mkdir()
    state = tmp_path / "state.json"
    entry = device_status.record_attempt({"last_run": _ago(2)}, False, "Authentication failed")
    entry = device_status.record_attempt(entry, False, "Authentication failed")
    state.write_text(json.dumps({dev["id"]: entry}), encoding="utf-8")

    monkeypatch.setattr(s, "STATE_FILE", state)
    monkeypatch.setattr(s.plugin_manager, "_plugins", {
        "fake": {"func": lambda r: [], "routers": [dev], "dir": str(tmp_path)},
    })
    s.app.dependency_overrides[s.require_auth] = lambda: "test"
    try:
        c = TestClient(s.app)
        card = c.get(f"/api/device/{dev['id']}").text
        page = c.get("/").text
    finally:
        s.app.dependency_overrides.clear()

    assert "Последняя попытка не удалась" in card
    assert "Authentication failed" in card and "неудач подряд: 2" in card
    assert 'title="Authentication failed"' in page
