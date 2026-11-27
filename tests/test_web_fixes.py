# =========================================================
# tests/test_web_fixes.py — веб-интерфейс
# =========================================================
#
# Устройство по id, а не по сдвинувшемуся номеру; проверка
# адреса и порта; расписание; удалённое хранилище в
# расписании копии настроек; журнал устройства после
# ротации; общая блокировка config.json.
#
# =========================================================

import json
import socket
import threading

import pytest

s = pytest.importorskip("web.server")
pytest.importorskip("httpx2", reason="нужен httpx2 для TestClient")

from starlette.requests import Request  # noqa: E402

from tests.test_delete_safety import client, fake_devices  # noqa: E402,F401


def _shift_list_during_form(monkeypatch, action):
    """Пока обработчик ждёт форму, выполнить action (другой запрос)."""
    real = Request._get_form

    async def patched(self, **kw):
        action()
        return await real(self, **kw)

    monkeypatch.setattr(Request, "_get_form", patched)


# ---- 1. удаление по id ----

def test_delete_takes_device_by_id_after_list_shift(client, fake_devices, tmp_path, monkeypatch):
    dirs = {}
    for name in ("A", "B", "C"):
        dirs[name] = tmp_path / name
        dirs[name].mkdir()
        (dirs[name] / "config.rsc").write_text(name)
        fake_devices(name, dirs[name])
    routers = s.plugin_manager._plugins["fake"]["routers"]
    ids = [s.plugin_manager.device_id("fake", i) for i in range(3)]

    # Пока разбирается форма удаления B, удаляют A: B → номер 0
    _shift_list_during_form(monkeypatch, lambda: routers.pop(0))
    r = client.post(f"/device/{ids[1]}/delete", data={}, follow_redirects=False)

    assert r.status_code == 303
    assert [d["name"] for d in routers] == ["C"]       # удалено именно B
    assert not dirs["B"].exists() and dirs["C"].exists()


def test_delete_of_device_removed_meanwhile_is_404(client, fake_devices, tmp_path, monkeypatch):
    fake_devices("A", tmp_path / "A")
    routers = s.plugin_manager._plugins["fake"]["routers"]
    dev_id = s.plugin_manager.device_id("fake", 0)
    _shift_list_during_form(monkeypatch, lambda: routers.pop(0))
    r = client.post(f"/device/{dev_id}/delete", data={}, follow_redirects=False)
    assert r.status_code == 404


def test_toggle_pin_by_id(client, fake_devices, tmp_path):
    fake_devices("A", tmp_path / "A")
    fake_devices("B", tmp_path / "B")
    routers = s.plugin_manager._plugins["fake"]["routers"]
    b_id = s.plugin_manager.device_id("fake", 1)
    routers.pop(0)                                    # номер B сдвинулся до запроса
    r = client.post(f"/device/{b_id}/toggle-pin", follow_redirects=False)
    assert r.status_code == 303 and routers[0]["pinned"] is True


# ---- 2. адрес и порт ----

@pytest.fixture
def cfg(tmp_path, monkeypatch):
    from core import context, settings as nbm_settings

    path = tmp_path / "config.json"
    path.write_text(json.dumps({"host": "127.0.0.1", "port": 8080}), encoding="utf-8")
    monkeypatch.setattr(s, "CONFIG_FILE", path)
    monkeypatch.setattr(context, "CONFIG_FILE", path)
    monkeypatch.setattr(nbm_settings, "env", lambda name, default="": "")
    return path


def _general(client, **fields):
    data = {"host": "127.0.0.1", "port": "8080", **fields}
    return client.post("/settings/general/save", data=data, follow_redirects=False)


def test_foreign_address_is_rejected(client, cfg):
    r = _general(client, host="192.0.2.77")
    assert r.status_code == 303 and "error=" in r.headers["location"]
    assert json.loads(cfg.read_text(encoding="utf-8"))["host"] == "127.0.0.1"


def test_busy_port_is_rejected(client, cfg):
    busy = socket.socket()
    busy.bind(("127.0.0.1", 0))
    busy.listen()
    try:
        r = _general(client, port=str(busy.getsockname()[1]))
    finally:
        busy.close()
    assert "error=" in r.headers["location"]
    assert json.loads(cfg.read_text(encoding="utf-8"))["port"] == 8080


def test_bad_port_is_error_not_8080(client, cfg):
    r = _general(client, port="99999")
    assert "error=" in r.headers["location"]


def test_valid_address_saved_and_error_shown(client, cfg):
    r = _general(client, host="0.0.0.0")
    assert r.headers["location"].endswith("message=saved")
    assert json.loads(cfg.read_text(encoding="utf-8"))["host"] == "0.0.0.0"
    page = client.get("/settings?tab=general&error=" + "тест")
    assert "Не сохранено: тест" in page.text


# ---- 6. расписание ----

@pytest.mark.parametrize("fields", [
    {"mode": "weekly"},
    {"daily_time": "25:99"},
    {"interval_hours": "0"},
    {"interval_hours": "abc"},
])
def test_scheduler_rejects_bad_values(client, monkeypatch, fields):
    saved = []
    monkeypatch.setattr(s.scheduler, "save_config", lambda c: saved.append(c))
    data = {"enabled": "true", "mode": "daily", "daily_time": "03:00", "interval_hours": "24", **fields}
    r = client.post("/scheduler/save", data=data, follow_redirects=False)
    assert "error=" in r.headers["location"] and saved == []


def test_scheduler_saves_good_values(client, monkeypatch):
    saved = []
    monkeypatch.setattr(s.scheduler, "save_config", lambda c: saved.append(c))
    r = client.post("/scheduler/save", data={"mode": "interval", "daily_time": "04:30",
                                             "interval_hours": "6"}, follow_redirects=False)
    assert r.headers["location"].endswith("message=saved")
    assert saved[0]["interval_hours"] == 6


# ---- 4. удалённое хранилище и копия настроек ----

def test_deleted_storage_leaves_settings_backup_schedule(client, cfg, monkeypatch):
    from core import settings_backup
    from core.storage import global_config

    monkeypatch.setattr(global_config, "CONFIG_FILE", cfg)
    auto_file = cfg.parent / "settings_backup.json"
    monkeypatch.setattr(settings_backup, "_auto_file", lambda: auto_file)
    global_config.save_remotes([
        {"id": "r1", "name": "one", "type": "local", "options": {}},
        {"id": "r2", "name": "two", "type": "local", "options": {}},
    ])
    settings_backup.save_auto({"enabled": True, "storages": ["r1", "r2"]})
    if s.storage_manager is not None:
        monkeypatch.setattr(s.storage_manager, "reload_remotes", lambda: None)

    r = client.post("/settings/storage/r1/delete", follow_redirects=False)
    assert r.status_code == 303
    assert settings_backup.load_auto()["storages"] == ["r2"]


# ---- 8. журнал устройства после ротации ----

def test_device_log_reads_rotated_files(tmp_path):
    log = tmp_path / "app.log"
    (tmp_path / "app.log.2").write_text("old [dev1] первая\n", encoding="utf-8")
    (tmp_path / "app.log.1").write_text("x [dev1] вторая\nx [other] чужая\n", encoding="utf-8")
    log.write_text("y [other] только чужие\n", encoding="utf-8")

    text, shown, more, _ = s.read_log_tail(log, "100", filter_tag="dev1")
    assert text == "old [dev1] первая\nx [dev1] вторая\n" and shown == 2 and not more

    text, shown, more, _ = s.read_log_tail(log, "1", filter_tag="dev1")
    assert text == "x [dev1] вторая\n" and more

    text, _, _, _ = s.read_log_tail(log, "100")            # общий журнал — только текущий
    assert "первая" not in text


# ---- 7. config.json под общей блокировкой ----

def test_config_writers_share_lock(cfg, monkeypatch):
    from core import context, rclone, tls_store
    from core.storage import global_config

    monkeypatch.setattr(rclone, "CONFIG_FILE", cfg)
    monkeypatch.setattr(global_config, "CONFIG_FILE", cfg)

    # Пока блокировка занята, запись из другого потока ждёт
    done = []
    with context.CONFIG_LOCK:
        t = threading.Thread(target=lambda: (rclone.set_rclone_path("/x/rclone"), done.append(1)))
        t.start()
        t.join(0.3)
        assert done == []
    t.join(5)
    assert done == [1]

    threads = [
        threading.Thread(target=rclone.set_rclone_path, args=("/y/rclone",)),
        threading.Thread(target=global_config.save_remotes, args=([{"id": "a", "type": "local"}],)),
        threading.Thread(target=tls_store.unassign),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert data["rclone"]["binary_path"] == "/y/rclone"
    assert data["remotes"] == [{"id": "a", "type": "local"}]
    assert data["port"] == 8080
