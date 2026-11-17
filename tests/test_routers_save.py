# =========================================================
# tests/test_routers_save.py
# =========================================================
#
# Запись routers.json: атомарность, сериализация,
# привязка служебных полей к нужному устройству.
#
# Все тесты работают во временном каталоге — реальные
# plugins/*/routers.json не трогаются.
#
# =========================================================

import json
import threading

import pytest


@pytest.fixture
def pm(tmp_path):
    from core.plugin_manager import PluginManager

    manager = PluginManager()
    plugin_dir = tmp_path / "Mikrotik"
    plugin_dir.mkdir()
    manager._plugins["mikrotik"] = {
        "func": lambda r: [],
        "routers": [
            {"host": "10.0.0.1", "name": "A"},
            {"host": "10.0.0.2", "name": "B"},
            {"host": "10.0.0.3", "name": "C"},
        ],
        "dir": str(plugin_dir),
    }
    return manager


def _read(pm):
    path = pm._plugins["mikrotik"]["dir"] + "/routers.json"
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def test_save_routers_writes_list_without_tmp(pm, tmp_path):
    pm.save_routers("mikrotik")

    assert [r["name"] for r in _read(pm)] == ["A", "B", "C"]
    assert list((tmp_path / "Mikrotik").glob("*.tmp")) == []


def test_save_routers_unknown_plugin_raises(pm):
    with pytest.raises(KeyError):
        pm.save_routers("nope")


def test_concurrent_saves_leave_valid_json(pm):
    """Параллельные сохранения + изменения не портят файл."""
    routers = pm._plugins["mikrotik"]["routers"]
    errors = []

    def worker(n):
        try:
            for i in range(20):
                with pm.routers_lock:
                    routers[n % 3]["counter"] = f"{n}-{i}"
                    pm.save_routers("mikrotik")
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    data = _read(pm)  # не падает — JSON целый
    assert len(data) == 3
    # Файл совпадает с состоянием в памяти
    assert data == routers


def test_persist_service_fields_follows_object_not_index(pm):
    """
    Бэкап шёл для B (index 1). Пока он шёл, удалили A —
    B стал index 0. ros_version должен попасть в B, а не в C.
    """
    routers = pm._plugins["mikrotik"]["routers"]
    original_b = routers[1]

    routers.pop(0)  # удалили A

    pm.persist_router_service_fields(
        "mikrotik", 1, {"ros_version": "7"}, original=original_b,
    )

    by_name = {r["name"]: r for r in routers}
    assert by_name["B"].get("ros_version") == "7"
    assert "ros_version" not in by_name["C"]
    assert {r["name"]: r.get("ros_version") for r in _read(pm)} == {
        "B": "7", "C": None,
    }


def test_persist_service_fields_skips_removed_device(pm):
    routers = pm._plugins["mikrotik"]["routers"]
    original_b = routers[1]
    routers.remove(original_b)

    pm.persist_router_service_fields(
        "mikrotik", 1, {"ros_version": "6"}, original=original_b,
    )

    assert all("ros_version" not in r for r in routers)


# =========================================================
# Веб-обработчики: откат при ошибке записи
# =========================================================
#
# save_routers подменяется на «падающий» — на диск ничего
# не пишется, проверяем только, что память откатилась.

@pytest.fixture
def web(monkeypatch):
    pytest.importorskip("httpx2", reason="нужен httpx2 для TestClient")
    from fastapi.testclient import TestClient
    import web.server as s

    if not s.plugin_manager or not s.plugin_manager._plugins:
        pytest.skip("нет загруженных плагинов")

    def boom(device_type):
        raise OSError("диск недоступен")

    monkeypatch.setattr(s.plugin_manager, "save_routers", boom)
    s.app.dependency_overrides[s.require_auth] = lambda: "test"
    try:
        yield s, TestClient(s.app, raise_server_exceptions=False)
    finally:
        s.app.dependency_overrides.clear()


def test_toggle_pin_rolls_back_on_save_error(web):
    s, client = web
    device_type = next(iter(s.plugin_manager._plugins))
    routers = s.plugin_manager._plugins[device_type]["routers"]
    if not routers:
        pytest.skip("нет устройств")

    before = routers[0].get("pinned")
    r = client.post(f"/toggle-pin/{device_type}/0")

    assert r.status_code == 500
    assert routers[0].get("pinned") == before


def test_delete_device_rolls_back_on_save_error(web):
    s, client = web
    device_type = next(iter(s.plugin_manager._plugins))
    routers = s.plugin_manager._plugins[device_type]["routers"]
    if not routers:
        pytest.skip("нет устройств")

    snapshot = list(routers)
    r = client.post(f"/delete-device/{device_type}/0", data={})

    assert r.status_code == 500
    assert routers == snapshot
    assert routers[0] is snapshot[0]
