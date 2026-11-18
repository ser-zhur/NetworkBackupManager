# =========================================================
# tests/test_log_tag.py
# =========================================================
#
# Тег устройства в app.log должен совпадать при записи
# (одиночный/массовый бэкап, синхронизация) и в фильтре
# /logs/device/... — иначе строки не видны в логе
# устройства.
#
# =========================================================

import pytest

from core.logger import device_log_tag, get_device_context

# host и ip намеренно разные — раньше массовый бэкап брал ip,
# а фильтр — host.
ROUTER = {"host": "gw.office", "ip": "10.0.0.1", "backup_path": "x"}
EXPECTED = "mikrotik/gw.office"


def test_device_log_tag_priority():
    assert device_log_tag("mikrotik", ROUTER) == EXPECTED
    assert device_log_tag("mikrotik", {"ip": "10.0.0.1"}) == "mikrotik/10.0.0.1"
    assert device_log_tag("mikrotik", {}) == "mikrotik/?"


def test_mass_backup_uses_same_tag(tmp_path, monkeypatch):
    from core.plugin_manager import PluginManager

    seen = []

    def plugin(router):
        seen.append(get_device_context())
        return []

    pm = PluginManager()
    pm._plugins["mikrotik"] = {
        "func": plugin,
        "routers": [{**ROUTER, "backup_path": str(tmp_path)}],
        "dir": str(tmp_path),
    }
    pm.run_all_backups()

    assert seen == [EXPECTED]


@pytest.fixture
def server(monkeypatch, tmp_path):
    s = pytest.importorskip("web.server")
    if s.plugin_manager is None:
        pytest.skip("PluginManager не загружен")
    monkeypatch.setattr(s.plugin_manager, "_plugins", {
        "mikrotik": {
            "func": lambda r: [],
            "routers": [dict(ROUTER, backup_path=str(tmp_path))],
            "dir": str(tmp_path),
        }
    })
    return s


def test_filter_uses_same_tag(server):
    assert server._device_filter_tag("Mikrotik", 0) == EXPECTED


def test_storage_sync_uses_same_tag(server, monkeypatch):
    seen = []

    def fake_sync(device_type, index):
        seen.append(get_device_context())
        return {"ok": True, "message": "", "errors": []}

    monkeypatch.setattr(server.plugin_manager, "sync_git_to_storages", fake_sync)
    server.execute_storage_sync_task("mikrotik", 0, "test")

    assert seen == [EXPECTED]


def test_single_backup_uses_same_tag(server, monkeypatch):
    seen = []
    server.plugin_manager._plugins["mikrotik"]["func"] = (
        lambda r: seen.append(get_device_context()) or []
    )
    monkeypatch.setattr(server.plugin_manager, "_push_to_storages", lambda **kw: None)

    from core import backup_state
    device_id = server.plugin_manager.device_id("mikrotik", 0)
    assert backup_state.try_start_device(device_id)
    server.execute_device_backup_task(
        "mikrotik", 0, dict(server.plugin_manager._plugins["mikrotik"]["routers"][0]),
    )

    assert seen == [EXPECTED]
    assert backup_state.is_backup_running_for(device_id) is False
