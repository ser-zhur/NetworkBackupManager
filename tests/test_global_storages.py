# =========================================================
# tests/test_global_storages.py
# =========================================================
#
# Внешние хранилища через rclone (config.json → remotes):
# общие для всех устройств, несколько одного типа, форма —
# из описания rclone. Перенос старых схем.
#
# config.json, state.json, routers.json — временные,
# устройства фиктивные. Сеть не используется; настоящий
# rclone — только с типом local (папка на диске).
#
# =========================================================

import json

import pytest

from core import rclone as rclone_mod
from core.storage import StorageManager, global_config
from core.storage.base import PushResult
from core.storage.rclone_remote import RcloneRemote

needs_rclone = pytest.mark.skipif(
    not (rclone_mod.is_available() and rclone_mod.provider("local")),
    reason="rclone не найден",
)


@pytest.fixture
def cfg_file(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"port": 8080}), encoding="utf-8")
    monkeypatch.setattr(global_config, "CONFIG_FILE", path)
    return path


def _local_remote(rid, base_dir, **extra):
    data = {"id": rid, "name": rid, "type": "local", "options": {},
            "base_dir": str(base_dir).replace("\\", "/"), "mode": "all"}
    data.update(extra)
    return data


# =========================================================
# Базовые функции
# =========================================================

def test_remotes_become_backends_several_of_one_type(cfg_file, tmp_path):
    global_config.save_remotes([
        _local_remote("a", tmp_path / "a"),
        _local_remote("b", tmp_path / "b"),
    ])
    sm = StorageManager()
    sm.discover()
    ids = [b.id for b in sm.all()]
    assert ids[0] == "git"                       # git первым
    assert ids[1:] == ["a", "b"]                 # два хранилища одного типа
    assert all(isinstance(sm.get(i), RcloneRemote) for i in ("a", "b"))


def test_is_configured_uses_rclone_required_options(cfg_file):
    if rclone_mod.provider("webdav") is None:
        pytest.skip("rclone не найден")
    b = RcloneRemote({"id": "w", "type": "webdav"})
    assert global_config.is_configured(b, {"w": {"type": "webdav", "options": {}}}) is False
    assert global_config.is_configured(
        b, {"w": {"type": "webdav", "options": {"url": "https://x"}}}) is True


def test_effective_config_and_toggle(cfg_file):
    b = RcloneRemote({"id": "w", "type": "local"})
    cfg = global_config.effective_config(
        {"storage": {"w": {"enabled": True}}}, b,
        {"w": {"type": "local", "base_dir": "/x"}},
    )
    assert cfg == {"type": "local", "base_dir": "/x", "enabled": True}
    assert global_config.is_enabled_for({}, b) is False


def test_new_remote_id_unique():
    ids = {global_config.new_remote_id("Nextcloud офис", set()) for _ in range(20)}
    assert len(ids) == 20
    assert all(i.startswith("nextcloud-") for i in ids)


# =========================================================
# Перенос старых схем
# =========================================================

def test_legacy_type_settings_to_remotes():
    w = global_config.legacy_storage_to_remote("webdav", {
        "url": "https://dav.test/x", "vendor": "nextcloud", "user": "u",
        "pass": "ENC", "remote_dir": "/", "mode": "changes_only",
    })
    assert w["id"] == "webdav" and w["type"] == "webdav"
    assert w["options"] == {"url": "https://dav.test/x", "vendor": "nextcloud",
                            "user": "u", "pass": "ENC"}
    assert w["base_dir"] == "/" and w["name"] == "WebDAV · dav.test"

    f = global_config.legacy_storage_to_remote("ftp", {
        "host": "ftp.test", "port": 21, "user": "u", "pass": "ENC",
        "use_tls": True, "remote_dir": "/home/ftptest",
    })
    assert f["options"]["explicit_tls"] is True
    assert f["options"]["no_check_certificate"] is True
    assert f["base_dir"] == "/home/ftptest"

    assert global_config.legacy_storage_to_remote("smb", {"host": "", "user": ""}) is None


def _old_router(host, dir_, *, enabled=True):
    return {
        "host": host,
        "backup_path": f"/tmp/{host}",
        "storage": {
            "git": {"enabled": True, "mode": "changes_only"},
            "webdav": {
                "enabled": enabled, "url": "https://dav.test/x", "user": "u",
                "pass": "ENC", "vendor": "nextcloud", "remote_dir": dir_,
                "mode": "changes_only",
            },
            "ftp": {"enabled": False, "host": "", "port": 21, "user": "",
                    "pass": "", "remote_dir": "/network_backups"},
        },
    }


def test_migrate_from_per_device_settings(cfg_file, tmp_path):
    from core.plugin_manager import PluginManager

    plugin_dir = tmp_path / "Plugin"
    plugin_dir.mkdir()
    routers = [_old_router("10.0.0.1", "/A"), _old_router("10.0.0.2", "/B")]
    (plugin_dir / "routers.json").write_text(json.dumps(routers), encoding="utf-8")
    original = (plugin_dir / "routers.json").read_bytes()

    sm = StorageManager()
    sm.discover()
    pm = PluginManager(storage_manager=sm)
    pm._plugins = {"fake": {"func": None, "routers": routers, "dir": str(plugin_dir)}}

    report = global_config.migrate(pm, sm)

    assert report["created_remotes"] == ["webdav"]           # ftp не был настроен
    remotes = global_config.load_remotes()
    assert remotes[0]["id"] == "webdav" and remotes[0]["base_dir"] == "/"
    assert remotes[0]["options"]["pass"] == "ENC"            # зашифрованный — как есть
    assert json.loads(cfg_file.read_text(encoding="utf-8"))["port"] == 8080
    assert sm.get("webdav") is not None                      # backend перечитан

    saved = json.loads((plugin_dir / "routers.json").read_text(encoding="utf-8"))
    assert saved[0]["storage"]["webdav"] == {"enabled": True}
    bak = list(plugin_dir.glob("routers.json.bak-*"))
    assert len(bak) == 1 and bak[0].read_bytes() == original

    assert global_config.migrate(pm, sm) is None             # повторно — ничего


def test_migrate_from_type_settings_keeps_ids_and_backs_up_config(cfg_file, tmp_path):
    from core.plugin_manager import PluginManager

    cfg_file.write_text(json.dumps({"port": 8080, "storages": {
        "webdav": {"url": "https://dav.test", "user": "u", "pass": "ENC", "remote_dir": "/"},
        "ftp": {"host": "ftp.test", "user": "u", "pass": "ENC2", "remote_dir": "/home/f"},
        "smb": {"host": "", "user": "", "remote_dir": ""},
    }}), encoding="utf-8")

    pm = PluginManager()
    pm._plugins = {}
    report = global_config.migrate(pm, None)

    assert report["created_remotes"] == ["webdav", "ftp"]
    data = json.loads(cfg_file.read_text(encoding="utf-8"))
    assert "storages" not in data and data["port"] == 8080
    assert len(list(tmp_path.glob("config.json.bak-*"))) == 1


# =========================================================
# Выгрузка через plugin_manager (настоящий rclone, local)
# =========================================================

@needs_rclone
def test_push_via_plugin_manager_to_two_local_remotes(cfg_file, tmp_path, monkeypatch):
    import core.plugin_manager as pm_mod
    from core.plugin_manager import PluginManager

    monkeypatch.setattr(pm_mod, "STATE_FILE", tmp_path / "state.json")
    global_config.save_remotes([
        _local_remote("one", tmp_path / "r1"),
        _local_remote("two", tmp_path / "r2"),
        {"id": "broken", "name": "Без url", "type": "webdav", "options": {},
         "base_dir": "/", "mode": "all"},
    ])
    sm = StorageManager()
    sm.discover()
    monkeypatch.setattr(sm.get("git"), "push",
                        lambda cfg, ctx: PushResult(True, "ok", {"changed": True}))
    pm = PluginManager(storage_manager=sm)

    local = tmp_path / "Dev_Local"
    local.mkdir()
    f = local / "config.rsc"
    f.write_bytes(b"cfg")
    router = {"host": "10.0.0.7", "storage": {
        "one": {"enabled": True}, "two": {"enabled": True}, "broken": {"enabled": True},
    }}

    pm._push_to_storages("mikrotik", 0, "10.0.0.7", router, local, [str(f)])

    assert (tmp_path / "r1" / "Dev_Local" / "config.rsc").read_bytes() == b"cfg"
    assert (tmp_path / "r2" / "Dev_Local" / "config.rsc").read_bytes() == b"cfg"
    state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    backends = state["mikrotik/0"]["backends"]
    assert backends["one"]["ok"] and backends["two"]["ok"]
    assert backends["broken"]["ok"] is False and "не настроено" in backends["broken"]["message"]


# =========================================================
# Веб
# =========================================================

@pytest.fixture
def web(cfg_file, tmp_path, monkeypatch):
    pytest.importorskip("httpx2", reason="нужен httpx2 для TestClient")
    from fastapi.testclient import TestClient
    import web.server as s

    if s.plugin_manager is None or s.storage_manager is None:
        pytest.skip("нет PluginManager / StorageManager")
    if rclone_mod.provider("webdav") is None:
        pytest.skip("rclone не найден")

    routers = [
        {"host": "10.0.0.1", "name": "A", "backup_path": str(tmp_path / "a"), "storage": {}},
        {"host": "10.0.0.2", "name": "B", "backup_path": str(tmp_path / "b"), "storage": {}},
    ]
    monkeypatch.setattr(s.plugin_manager, "_plugins", {
        "fake": {"func": None, "routers": routers, "dir": str(tmp_path)},
    })
    monkeypatch.setattr(s.plugin_manager, "save_routers", lambda t: None)
    s.storage_manager.reload_remotes()
    s.app.dependency_overrides[s.require_auth] = lambda: "test"
    yield s, TestClient(s.app), routers
    s.app.dependency_overrides.clear()
    monkeypatch.undo()
    s.storage_manager.reload_remotes()


def _webdav_form(rid="", name="Nextcloud", password="", url="https://dav.test/x", base="/"):
    return {
        "id": rid, "type": "webdav", "name": name, "mode": "changes_only",
        "base_dir": base, "opt_url": url, "opt_vendor": "nextcloud",
        "opt_user": "u", "opt_pass": password,
    }


def test_new_page_lists_popular_types(web):
    s, c, _ = web
    page = c.get("/settings/storage/new").text
    for t in ("webdav", "ftp", "sftp", "s3", "onedrive", "drive"):
        assert f"/settings/storage/edit?type={t}" in page
    assert "Вход в учётную запись через браузер" in page     # OAuth-типы помечены


def test_edit_page_built_from_rclone_description(web):
    s, c, _ = web
    page = c.get("/settings/storage/edit?type=webdav").text
    assert 'name="opt_url"' in page and 'name="opt_pass"' in page
    assert "Дополнительные параметры" in page

    s3 = c.get("/settings/storage/edit?type=s3").text
    assert 'id="rclone-provider"' in s3                       # фильтр по провайдеру
    assert 'data-provider="' in s3


def test_save_encrypts_password_and_supports_two_of_one_type(web):
    s, c, _ = web
    r = c.post("/settings/storage/save", data=_webdav_form(name="Первый", password="pw1"),
               follow_redirects=False)
    assert r.status_code == 303 and "remote_saved" in r.headers["location"]
    r = c.post("/settings/storage/save", data=_webdav_form(name="Второй", password="pw2",
                                                           url="https://other.test"),
               follow_redirects=False)
    assert r.status_code == 303

    remotes = global_config.load_remotes()
    assert [x["name"] for x in remotes] == ["Первый", "Второй"]
    assert remotes[0]["id"] != remotes[1]["id"]
    enc = remotes[0]["options"]["pass"]
    assert enc != "pw1" and s.storage.decrypt(enc) == "pw1"
    assert [b.id for b in s.storage_manager.all()][1:] == [x["id"] for x in remotes]

    # Изменение без пароля — пароль прежний
    rid = remotes[0]["id"]
    c.post("/settings/storage/save", data=_webdav_form(rid=rid, name="Первый!"),
           follow_redirects=False)
    again = global_config.load_global()[rid]
    assert again["name"] == "Первый!" and again["options"]["pass"] == enc


def test_save_missing_required_rerenders_form_with_values(web):
    s, c, _ = web
    r = c.post("/settings/storage/save", data=_webdav_form(name="Без url", url=""),
               follow_redirects=False)
    assert r.status_code == 400
    assert "не заполнены обязательные параметры: url" in r.text
    assert 'value="Без url"' in r.text                        # введённое не потеряно
    assert global_config.load_remotes() == []


def test_save_rejects_same_local_folder(web, tmp_path):
    s, c, routers = web
    routers[0]["storage"] = {"x": {"enabled": True}}
    routers[1]["storage"] = {"x": {"enabled": True}}
    routers[1]["backup_path"] = str(tmp_path / "other" / "a")   # то же имя «a»
    global_config.save_remotes([_local_remote("x", tmp_path / "r")])
    s.storage_manager.reload_remotes()

    form = {"id": "x", "type": "local", "name": "x", "mode": "all", "base_dir": "/new"}
    r = c.post("/settings/storage/save", data=form, follow_redirects=False)
    assert r.status_code == 400 and "одинаковое имя локальной папки" in r.text
    assert global_config.load_global()["x"]["base_dir"].endswith("/r")   # прежнее


def test_delete_disables_on_devices(web, tmp_path):
    s, c, routers = web
    global_config.save_remotes([_local_remote("x", tmp_path / "r")])
    s.storage_manager.reload_remotes()
    routers[0]["storage"] = {"x": {"enabled": True}, "git": {"enabled": True}}

    r = c.post("/settings/storage/x/delete", follow_redirects=False)
    assert r.status_code == 303 and "remote_deleted" in r.headers["location"]
    assert global_config.load_remotes() == []
    assert routers[0]["storage"] == {"git": {"enabled": True}}
    assert s.storage_manager.get("x") is None


def test_storage_tab_lists_remotes(web, tmp_path):
    s, c, routers = web
    global_config.save_remotes([_local_remote("x", tmp_path / "r", name="Мой диск")])
    s.storage_manager.reload_remotes()
    routers[0]["storage"] = {"x": {"enabled": True}}
    page = c.get("/settings?tab=storage").text
    assert "Мой диск" in page and "включено у устройств: 1" in page
    assert "/settings/storage/edit?id=x" in page


def test_device_form_toggles_for_remotes(web, tmp_path):
    s, c, routers = web
    global_config.save_remotes([
        _local_remote("x", tmp_path / "r"),
        {"id": "w", "name": "Без url", "type": "webdav", "options": {}, "base_dir": "/"},
    ])
    s.storage_manager.reload_remotes()
    page = c.get("/edit/fake/0").text
    assert 'name="storage_x_enabled"' in page
    assert "/a" in page                                        # итоговая папка
    assert "Не настроено" in page                              # webdav без url

    r = c.post("/update-router/fake/0", data={
        "ip": "10.0.0.1", "backup_path": routers[0]["backup_path"],
        "storage_x_enabled": "true", "storage_w_enabled": "true",
    }, follow_redirects=False)
    assert r.status_code == 303
    st = s.plugin_manager._plugins["fake"]["routers"][0]["storage"]
    assert st["x"] == {"enabled": True}
    assert st["w"] == {"enabled": False}                       # ненастроенное нельзя


def test_api_test_uses_saved_password(web, monkeypatch):
    s, c, _ = web
    global_config.save_remotes([{
        "id": "w", "name": "W", "type": "webdav", "base_dir": "/",
        "options": {"url": "https://dav.test", "user": "u",
                    "pass": s.storage.encrypt("saved-pw")},
    }])
    seen = {}

    def fake_test(self, cfg):
        seen.update(cfg)
        return PushResult(True, "ok")

    monkeypatch.setattr(RcloneRemote, "test", fake_test)
    r = c.post("/api/remotes/test", json={
        "id": "w", "type": "webdav", "base_dir": "/",
        "options": {"url": "https://dav.test", "user": "u", "pass": ""},
    })
    assert r.json()["ok"] is True
    assert s.storage.decrypt(seen["options"]["pass"]) == "saved-pw"

    r = c.post("/api/remotes/test", json={"id": "w", "saved": True})
    assert r.json()["ok"] is True
