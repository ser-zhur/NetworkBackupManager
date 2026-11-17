# =========================================================
# tests/test_sync_all.py
# =========================================================
#
# «Синхронизировать хранилища» на всех устройствах
# (Настройки → Хранилища).
#
# Устройства фиктивные, config.json временный; сама
# синхронизация одного устройства подменена — сеть и git
# не используются.
#
# =========================================================

import json

import pytest

from core import backup_state
from core.storage import global_config

s = pytest.importorskip("web.server")


@pytest.fixture
def env(tmp_path, monkeypatch):
    if s.plugin_manager is None or s.storage_manager is None:
        pytest.skip("нет PluginManager / StorageManager")

    # Хранилище rclone (тип local — настроено без параметров)
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"remotes": [
        {"id": "webdav", "name": "Т", "type": "local", "options": {}, "base_dir": "/"},
    ]}), encoding="utf-8")
    monkeypatch.setattr(global_config, "CONFIG_FILE", cfg)
    s.storage_manager.reload_remotes()

    routers = [
        {"host": "10.0.0.1", "storage": {"webdav": {"enabled": True}}},
        {"host": "10.0.0.2", "storage": {"webdav": {"enabled": False}}},   # нечего
        {"host": "10.0.0.3", "storage": {"webdav": {"enabled": True}}},
        {"host": "10.0.0.4", "storage": {"webdav": {"enabled": True}}},
    ]
    monkeypatch.setattr(s.plugin_manager, "_plugins", {
        "fake": {"func": None, "routers": routers, "dir": str(tmp_path)},
    })

    synced = []

    def fake_sync(device_type, index):
        synced.append(index)
        # Во время синхронизации устройство занято
        assert backup_state.is_sync_running_for(f"{device_type}/{index}")
        ok = index != 3
        return {"ok": ok, "message": "", "errors": [],
                "backends": {"webdav": {"ok": 1, "err": 0 if ok else 1, "skip": 0}}}

    monkeypatch.setattr(s.plugin_manager, "sync_git_to_storages", fake_sync)
    yield synced

    for d in backup_state.get_sync_status()["devices"]:
        backup_state.finish_sync(d)
    for d in backup_state.get_backup_status()["devices"]:
        backup_state.finish_device(d)
    with s._sync_all_lock:
        s._sync_all_running = False
    monkeypatch.undo()
    s.storage_manager.reload_remotes()


def test_targets_only_devices_with_enabled_storage(env):
    assert [i for _, i, _ in s._sync_all_targets()] == [0, 2, 3]


def test_syncs_sequentially_skips_busy_and_counts(env):
    synced = env
    backup_state.try_start_device("fake/2")            # идёт бэкап устройства 2

    with s._sync_all_lock:
        s._sync_all_running = True
    stats = s.execute_sync_all_task("test")

    assert synced == [0, 3]
    assert stats == {"devices": 3, "ok": 1, "errors": 1, "skipped": 1}
    assert s.is_sync_all_running() is False                 # флаг сброшен
    assert backup_state.get_sync_status()["devices"] == []  # слоты освобождены


def test_endpoint_starts_once(env, monkeypatch):
    pytest.importorskip("httpx2", reason="нужен httpx2 для TestClient")
    from fastapi.testclient import TestClient

    started = []
    monkeypatch.setattr(s, "execute_sync_all_task", lambda user: started.append(user))
    s.app.dependency_overrides[s.require_auth] = lambda: "test"
    try:
        c = TestClient(s.app)
        r = c.post("/maintenance/sync-all", follow_redirects=False)
        assert r.status_code == 303 and "sync_started" in r.headers["location"]
        assert started == ["test"]

        # Заглушка не сбрасывает флаг — как будто ещё идёт
        r = c.post("/maintenance/sync-all", follow_redirects=False)
        assert "sync_running" in r.headers["location"]
        assert started == ["test"]

        page = c.get("/settings?tab=storage").text
        assert "Синхронизация идёт" in page
    finally:
        s.app.dependency_overrides.clear()


def test_button_shown_with_device_count(env):
    pytest.importorskip("httpx2", reason="нужен httpx2 для TestClient")
    from fastapi.testclient import TestClient

    s.app.dependency_overrides[s.require_auth] = lambda: "test"
    try:
        page = TestClient(s.app).get("/settings?tab=storage").text
    finally:
        s.app.dependency_overrides.clear()
    assert 'action="/maintenance/sync-all"' in page
    assert "устройств: 3" in page
