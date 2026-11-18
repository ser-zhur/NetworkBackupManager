# =========================================================
# tests/test_events.py — журнал событий
# =========================================================

import json

import pytest

from core import context, events
from core import plugin_manager as pm_module
from core.plugin_manager import PluginManager
from core.storage.base import PushResult


@pytest.fixture(autouse=True)
def journal(tmp_path, monkeypatch):
    monkeypatch.setattr(context, "DATA_DIR", tmp_path)
    monkeypatch.setattr(pm_module, "STATE_FILE", tmp_path / "state.json")
    events.reset()
    yield tmp_path / "events.jsonl"
    events.reset()


def test_emit_recent_and_file(journal):
    a = events.emit("x.one", "первое")
    b = events.emit("x.two", "второе", level="error", device_id="d1", n=5)

    assert b["id"] == a["id"] + 1 and b["data"] == {"n": 5}
    assert [e["kind"] for e in events.recent()] == ["x.two", "x.one"]
    assert events.recent(device_id="d1") == [b]
    assert events.recent(kinds=("x.one",)) == [a]
    assert events.recent(after_id=a["id"]) == [b]
    assert events.last("x.one") == a

    lines = journal.read_text(encoding="utf-8").splitlines()
    assert [json.loads(x)["message"] for x in lines] == ["первое", "второе"]


def test_survives_restart_and_continues_ids(journal):
    events.emit("x", "1")
    events.emit("x", "2")
    events.reset()                                   # «перезапуск»
    assert [e["message"] for e in events.recent()] == ["2", "1"]
    assert events.emit("x", "3")["id"] == 3


def test_file_is_trimmed(journal, monkeypatch):
    monkeypatch.setattr(events, "MAX_FILE_LINES", 10)
    monkeypatch.setattr(events, "MAX_KEEP", 4)
    events.reset()
    for i in range(25):
        events.emit("x", str(i))
    lines = journal.read_text(encoding="utf-8").splitlines()
    assert len(lines) <= 10
    assert json.loads(lines[-1])["message"] == "24"
    assert len(events.recent(limit=100)) == 4


def test_bad_subscriber_does_not_break_others(journal):
    got = []

    def bad(event):
        raise RuntimeError("boom")

    events.subscribe(bad)
    events.subscribe(got.append)
    try:
        events.emit("x", "y")
    finally:
        events.unsubscribe(bad)
        events.unsubscribe(got.append)
    assert [e["message"] for e in got] == ["y"]


def test_unknown_level_becomes_info(journal):
    assert events.emit("x", "y", level="loud")["level"] == "info"


# ---- события бэкапа ----

def _pm(tmp_path, func, n=1):
    pm = PluginManager()
    routers = [
        {"id": f"{i:012x}", "name": f"R{i}", "host": f"10.0.0.{i}",
         "backup_path": str(tmp_path / f"d{i}")}
        for i in range(n)
    ]
    pm._plugins["fake"] = {"func": func, "routers": routers}
    return pm, routers


def test_backup_failed_then_recovered(tmp_path):
    outcome = {"fail": True}

    def plugin(router):
        if outcome["fail"]:
            raise ConnectionError("timed out")
        return []

    pm, (r,) = _pm(tmp_path, plugin)
    pm.backup_device("fake", r, 0)
    pm.backup_device("fake", r, 0)
    outcome["fail"] = False
    pm.backup_device("fake", r, 0)
    pm.backup_device("fake", r, 0)                   # снова ок — без события

    kinds = [e["kind"] for e in reversed(events.recent())]
    assert kinds == [events.BACKUP_FAILED, events.BACKUP_FAILED, events.BACKUP_RECOVERED]
    failed, recovered = events.recent()[1], events.recent()[0]
    assert failed["data"]["failures"] == 2 and failed["data"]["name"] == "R0"
    assert failed["device_id"] == r["id"] and failed["level"] == "error"
    assert recovered["data"]["after_failures"] == 2


def test_storage_failed_then_recovered(tmp_path):
    dev = {"name": "R1", "host": "10.0.0.1", "type": "fake"}
    PluginManager._save_backend_state("d1", {"webdav": PushResult.failure("401")}, device=dev)
    PluginManager._save_backend_state("d1", {"webdav": PushResult.skipped("нет изменений")}, device=dev)
    PluginManager._save_backend_state("d1", {"webdav": PushResult.success("ok")}, device=dev)
    PluginManager._save_backend_state("d1", {"webdav": PushResult.success("ok")}, device=dev)

    got = [(e["kind"], e["data"].get("storage")) for e in reversed(events.recent())]
    assert got == [(events.STORAGE_FAILED, "webdav"), (events.STORAGE_RECOVERED, "webdav")]
    assert "R1: хранилище webdav — 401" == events.recent()[1]["message"]


def test_run_finished(tmp_path):
    def plugin(router):
        if router["host"].endswith(".1"):
            raise RuntimeError("x")
        return []

    pm, _ = _pm(tmp_path, plugin, n=3)
    pm.run_all_backups(source="schedule")

    run = events.last(events.RUN_FINISHED)
    assert run["level"] == "error"
    assert run["data"]["source"] == "schedule"
    assert run["data"]["total"] == 3 and run["data"]["failed"] == 1
    assert "успешно 2 из 3, ошибок 1" in run["message"]
