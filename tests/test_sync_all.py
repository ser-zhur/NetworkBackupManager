# =========================================================
# tests/test_sync_all.py
# =========================================================
#
# «Синхронизировать хранилища» на всех устройствах
# (Настройки → Хранилища).
#
# Устройства фиктивные, config.json временный; подготовка
# файлов устройства и само хранилище подменены — сеть и git
# не используются.
#
# =========================================================

import json

import pytest

from core import backup_state
from core.storage import global_config

s = pytest.importorskip("web.server")


def ID(n: int) -> str:
    """id фиктивного устройства №n."""
    return f"{n:012x}"


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
        {"id": ID(0), "host": "10.0.0.1", "storage": {"webdav": {"enabled": True}}},
        {"id": ID(1), "host": "10.0.0.2", "storage": {"webdav": {"enabled": False}}},   # нечего
        {"id": ID(2), "host": "10.0.0.3", "storage": {"webdav": {"enabled": True}}},
        {"id": ID(3), "host": "10.0.0.4", "storage": {"webdav": {"enabled": True}}},
    ]
    monkeypatch.setattr(s.plugin_manager, "_plugins", {
        "fake": {"func": None, "routers": routers, "dir": str(tmp_path)},
    })

    synced = []
    pushes = []

    from datetime import datetime

    from core.plugin_manager import _sync_result
    from core.storage.base import PushContext, PushResult

    class FakeBackend:
        id = "webdav"
        name = "Т"
        always_enabled = False

        def push_many(self, cfg, contexts):
            pushes.append([c.device_id for c in contexts])
            # Во время выгрузки слоты устройств уже свободны
            assert backup_state.get_sync_status()["devices"] == []
            return [PushResult.failure("нет места") if c.device_id == ID(3)
                    else PushResult.success("ok", uploaded=1) for c in contexts]

    backend = FakeBackend()

    def fake_prepare(device_type, index, folder, storages):
        synced.append(index)
        # Во время подготовки устройство занято
        assert backup_state.is_sync_running_for(ID(index))
        result = _sync_result()
        result["backends"]["webdav"] = {"ok": 0, "err": 0, "skip": 0}
        result["commits"] = 1
        ctx = PushContext(
            device_id=ID(index), device_host="h", device_type=device_type,
            backup_path=tmp_path / str(index), files={"f": tmp_path / "f"},
            changed=True, run_time=datetime.now(),
        )
        return {"result": result, "ctx": ctx, "backends": [backend], "router": routers[index]}

    monkeypatch.setattr(s.plugin_manager, "_prepare_sync", fake_prepare)
    monkeypatch.setattr(s.plugin_manager, "_decrypt_backend_config", lambda b, cfg: dict(cfg))
    yield synced, pushes

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


def test_prepares_all_then_pushes_once_skips_busy_and_counts(env):
    synced, pushes = env
    backup_state.try_start_device(ID(2))            # идёт бэкап устройства 2

    with s._sync_all_lock:
        s._sync_all_running = True
    stats = s.execute_sync_all_task("test")

    assert synced == [0, 3]
    assert pushes == [[ID(0), ID(3)]]                 # одна выгрузка на хранилище
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
