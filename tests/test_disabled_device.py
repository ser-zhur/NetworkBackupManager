# =========================================================
# tests/test_disabled_device.py
# =========================================================
#
# Статус «Отключено»: устройство выведено из эксплуатации,
# бэкапы не запускаются, история хранится и не очищается
# автоматически.
#
# Устройства фиктивные, devices.json не пишется.
#
# =========================================================

from pathlib import Path

import pytest

from core.plugin_manager import PluginManager, is_device_disabled


A = "0000000000aa"      # id активного устройства
R = "0000000000bb"      # id отключённого


def _routers(tmp_path):
    return [
        {"id": A, "host": "10.0.0.1", "name": "Active", "backup_path": str(tmp_path / "a")},
        {"id": R, "host": "10.0.0.2", "name": "Retired", "backup_path": str(tmp_path / "r"),
         "disabled": True},
    ]


def test_flag():
    assert is_device_disabled({"disabled": True}) is True
    assert is_device_disabled({}) is False


# =========================================================
# Ядро: массовый бэкап и автоочистка
# =========================================================

def test_mass_backup_skips_disabled(tmp_path):
    called = []
    pm = PluginManager()
    pm._plugins["fake"] = {
        "func": lambda r: called.append(r["host"]) or [],
        "routers": _routers(tmp_path),
        "dir": str(tmp_path),
    }
    pm.run_all_backups()
    assert called == ["10.0.0.1"]


def test_auto_cleanup_skips_disabled(tmp_path, monkeypatch):
    import core.context
    import core.maintenance as maint

    for r in _routers(tmp_path):
        Path(r["backup_path"]).mkdir()

    cleaned = []
    monkeypatch.setattr(maint, "apply_retention",
                        lambda path, cfg: cleaned.append(path.name) or [])
    monkeypatch.setattr(maint, "sweep_all_devices", lambda pm: {})
    monkeypatch.setattr(core.context, "load_json", lambda path, default=None: {
        "retention": {"enabled": True, "auto_sweep": True},
    })

    pm = PluginManager()
    pm._plugins["fake"] = {"func": None, "routers": _routers(tmp_path), "dir": str(tmp_path)}
    maint.post_backup_cleanup(pm)

    assert cleaned == ["a"]


# =========================================================
# Веб
# =========================================================

@pytest.fixture
def web(tmp_path, monkeypatch):
    s = pytest.importorskip("web.server")
    if s.plugin_manager is None:
        pytest.skip("PluginManager не загружен")

    routers = _routers(tmp_path)
    for r in routers:
        Path(r["backup_path"]).mkdir()
    monkeypatch.setattr(s.plugin_manager, "_plugins", {
        "fake": {"func": lambda r: [], "routers": routers, "dir": str(tmp_path)},
    })
    monkeypatch.setattr(s.plugin_manager, "save_routers", lambda *a: None)
    s.app.dependency_overrides[s.require_auth] = lambda: "test"
    yield s, routers
    s.app.dependency_overrides.clear()


def _client(s):
    pytest.importorskip("httpx2", reason="нужен httpx2 для TestClient")
    from fastapi.testclient import TestClient
    return TestClient(s.app)


def test_status_disabled_without_warnings(web):
    s, routers = web
    row = s._build_device_summary("fake", 1, routers[1], {
        R: {"last_run": "2020-01-01 00:00:00",
                   "backends": {"git": {"ok": False}}},
    })
    assert row["status"] == "disabled"
    assert row["overdue"] is False
    assert row["never_backed_up"] is False
    assert row["backends_error_count"] == 0

    card = s._build_device_payload("fake", 1, routers[1])
    assert card["status"] == "disabled" and card["disabled"] is True

    assert s._build_device_summary("fake", 0, routers[0], {})["status"] != "disabled"


def test_single_backup_refused(web, monkeypatch):
    s, _ = web
    started = []
    monkeypatch.setattr(s, "execute_device_backup_task", lambda *a: started.append(a))
    c = _client(s)

    r = c.post(f"/device/{R}/run", headers={"X-Requested-With": "XMLHttpRequest"})
    assert r.status_code == 409
    assert started == []
    assert s.is_backup_running_for(R) is False   # слот не занят

    from core import backup_state
    r = c.post(f"/device/{A}/run", headers={"X-Requested-With": "XMLHttpRequest"})
    assert r.status_code == 204
    assert len(started) == 1
    backup_state.finish_device(A)


def test_prune_all_skips_disabled(web, monkeypatch):
    s, _ = web
    cleaned = []
    monkeypatch.setattr(s, "apply_retention",
                        lambda path, cfg: cleaned.append(path.name) or [])
    monkeypatch.setattr(s, "load_json", lambda path, default=None: {
        "retention": {"enabled": True},
    })
    r = _client(s).post("/maintenance/prune-all", follow_redirects=False)
    assert r.status_code == 303
    assert cleaned == ["a"]


def _toggle_checked(html: str) -> bool:
    """Стоит ли checked у ползунка name="active"."""
    tag = html.split('name="active"')[0].rsplit("<input", 1)[1] +           html.split('name="active"')[1].split(">", 1)[0]
    return "checked" in tag


def test_toggle_switch(web):
    s, routers = web
    c = _client(s)
    form = {"ip": "10.0.0.1", "backup_path": routers[0]["backup_path"]}
    current = lambda: s.plugin_manager._plugins["fake"]["routers"][0]

    # Ползунок выключен: маркер есть, чекбокса нет → отключено
    r = c.post(f"/device/{A}/update", data={**form, "active_field": "1"},
               follow_redirects=False)
    assert r.status_code == 303
    assert current()["disabled"] is True

    # Ползунок включён → активно, ключа нет
    r = c.post(f"/device/{A}/update",
               data={**form, "active_field": "1", "active": "true"},
               follow_redirects=False)
    assert r.status_code == 303
    assert "disabled" not in current()

    # Запрос без поля статуса устройство НЕ отключает
    r = c.post(f"/device/{A}/update", data=form, follow_redirects=False)
    assert r.status_code == 303
    assert "disabled" not in current()


def test_toggle_rendered_state(web):
    s, _ = web
    c = _client(s)
    active = c.get(f"/device/{A}/edit").text
    retired = c.get(f"/device/{R}/edit").text

    assert 'name="active_field"' in active
    assert _toggle_checked(active) is True        # по умолчанию включён
    assert _toggle_checked(retired) is False
    assert "Устройство активно" in active and "Устройство отключено" in active


def test_edit_sets_and_clears_flag(web):
    s, routers = web
    c = _client(s)
    form = {"ip": "10.0.0.1", "backup_path": routers[0]["backup_path"]}

    r = c.post(f"/device/{A}/update", data={**form, "disabled": "true"},
               follow_redirects=False)
    assert r.status_code == 303
    assert s.plugin_manager._plugins["fake"]["routers"][0]["disabled"] is True

    r = c.post(f"/device/{A}/update", data=form, follow_redirects=False)
    assert r.status_code == 303
    assert "disabled" not in s.plugin_manager._plugins["fake"]["routers"][0]


def test_pages_render(web):
    s, _ = web
    c = _client(s)

    page = c.get(f"/?selected={R}").text
    assert f'data-row-run-backup="{A}"' in page          # активное — кнопка есть
    assert f'data-row-run-backup="{R}"' not in page      # отключённое — нет
    assert 'data-filter-status="disabled"' in page
    assert 'data-disabled="1"' in page

    card = c.get(f"/api/device/{R}").text
    assert "⏸ Отключено" in card
    assert 'id="run-backup-form"' not in card

    edit = c.get(f"/device/{R}/edit").text
    assert 'name="active"' in edit and _toggle_checked(edit) is False
