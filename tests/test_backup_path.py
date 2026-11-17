# =========================================================
# tests/test_backup_path.py
# =========================================================
#
# Единое правило backup_path: относительный путь всегда
# разрешается от BASE_DIR — в загрузке, формах, плагинах.
#
# =========================================================

from pathlib import Path

import pytest

from core.context import BASE_DIR, resolve_backup_path


def test_relative_is_resolved_from_base_dir():
    assert resolve_backup_path("Backups/dev1") == (
        BASE_DIR / "Backups" / "dev1"
    ).resolve()


def test_absolute_is_kept(tmp_path):
    assert resolve_backup_path(str(tmp_path)) == tmp_path.resolve()


def test_whitespace_is_stripped():
    assert resolve_backup_path("  Backups/x  ") == resolve_backup_path(
        "Backups/x"
    )


def test_result_does_not_depend_on_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert resolve_backup_path("Backups/dev1").is_relative_to(BASE_DIR)


def test_mass_backup_passes_absolute_path_to_plugin(tmp_path, monkeypatch):
    """
    Плагин получает абсолютный путь, даже если в памяти
    оказался относительный — и не зависит от cwd процесса.
    """
    from core.plugin_manager import PluginManager

    monkeypatch.chdir(tmp_path)  # «чужой» текущий каталог

    received = []

    def fake_plugin(router):
        received.append(router["backup_path"])
        return []

    pm = PluginManager()
    pm._plugins["fake"] = {
        "func": fake_plugin,
        "routers": [{"host": "10.0.0.1", "backup_path": "Backups/_nbm_test"}],
        "dir": str(tmp_path),
    }

    pm.run_all_backups()

    assert received == [str((BASE_DIR / "Backups" / "_nbm_test").resolve())]
    assert Path(received[0]).is_absolute()


def test_save_router_stores_absolute_path(monkeypatch):
    pytest.importorskip("httpx2", reason="нужен httpx2 для TestClient")
    from fastapi.testclient import TestClient
    import web.server as s

    if not s.plugin_manager or "mikrotik" not in s.plugin_manager._plugins:
        pytest.skip("плагин mikrotik не загружен")

    # Реальный routers.json не пишем
    monkeypatch.setattr(s.plugin_manager, "save_routers", lambda t: None)
    s.app.dependency_overrides[s.require_auth] = lambda: "test"

    routers = s.plugin_manager._plugins["mikrotik"]["routers"]
    before = len(routers)
    try:
        r = TestClient(s.app).post(
            "/save-router",
            data={
                "device_type": "mikrotik",
                "ip": "192.0.2.10",
                "backup_path": "Backups/_nbm_test_rel",
            },
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert len(routers) == before + 1
        assert routers[-1]["backup_path"] == str(
            (BASE_DIR / "Backups" / "_nbm_test_rel").resolve()
        )
    finally:
        del routers[before:]
        s.app.dependency_overrides.clear()
