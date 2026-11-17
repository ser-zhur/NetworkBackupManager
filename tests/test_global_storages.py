# =========================================================
# tests/test_global_storages.py
# =========================================================
#
# Общие настройки хранилищ (одни на все устройства) и
# перенос со старой схемы, где они хранились в каждом
# устройстве.
#
# config.json, state.json и routers.json — временные,
# устройства фиктивные. Сеть не используется.
#
# =========================================================

import json
import pytest

from core.storage import StorageManager, global_config
from core.storage.base import PushResult


@pytest.fixture(scope="module")
def sm():
    m = StorageManager()
    m.discover()
    if m.get("ftp") is None or m.get("webdav") is None:
        pytest.skip("нет FTP/WebDAV backend")
    return m


@pytest.fixture
def cfg_file(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"port": 8080}), encoding="utf-8")
    monkeypatch.setattr(global_config, "CONFIG_FILE", path)
    return path


# =========================================================
# Базовые функции
# =========================================================

def test_is_configured_and_enabled(sm):
    ftp = sm.get("ftp")
    assert global_config.is_configured(ftp, {}) is False
    assert global_config.is_configured(ftp, {"ftp": {"host": "h", "user": "u"}}) is True
    assert global_config.is_configured(ftp, {"ftp": {"host": "h", "user": ""}}) is False

    assert global_config.is_enabled_for({"storage": {"ftp": {"enabled": True}}}, ftp)
    assert not global_config.is_enabled_for({}, ftp)
    assert global_config.is_enabled_for({}, sm.get("git"))       # git — всегда


def test_effective_config_merges_global_and_toggle(sm):
    cfg = global_config.effective_config(
        {"storage": {"ftp": {"enabled": True}}},
        sm.get("ftp"),
        {"ftp": {"host": "h", "remote_dir": "/x/{host}"}},
    )
    assert cfg == {"host": "h", "remote_dir": "/x/{host}", "enabled": True}


def test_common_dir():
    assert global_config._common_dir(["/A", "/B"]) == "/"
    assert global_config._common_dir(["/home/f", "/home/f/x"]) == "/home/f"
    assert global_config._common_dir(["/a/b/c"]) == "/a/b"
    assert global_config._common_dir([]) == "/"


def test_defaults_for_new_router_enable_configured(sm, cfg_file):
    global_config.save_global({"ftp": {"host": "h", "user": "u"}})
    d = sm.defaults_for_new_router()
    assert d["ftp"] == {"enabled": True}
    assert d["webdav"] == {"enabled": False}      # не настроено
    assert d["git"] == {"enabled": True}


# =========================================================
# Перенос
# =========================================================

def _old_router(host, dir_, *, enabled=True, vendor="nextcloud"):
    return {
        "host": host,
        "backup_path": f"/tmp/{host}",
        "storage": {
            "git": {"enabled": True, "mode": "changes_only"},
            "webdav": {
                "enabled": enabled, "url": "https://dav.test/x", "user": "u",
                "pass": "ENC", "vendor": vendor, "remote_dir": dir_,
                "mode": "changes_only",
            },
            "ftp": {"enabled": False, "host": "", "port": 21, "user": "",
                    "pass": "", "remote_dir": "/network_backups"},
        },
    }


def test_build_global_from_routers(sm):
    routers = [
        _old_router("10.0.0.1", "/Office1"),
        _old_router("10.0.0.2", "/Office2"),
        _old_router("10.0.0.3", "/Office3", vendor="other"),
    ]
    g = global_config.build_global_from_routers(sm, routers)

    assert g["webdav"]["url"] == "https://dav.test/x"
    assert g["webdav"]["vendor"] == "nextcloud"      # самый частый
    assert g["webdav"]["pass"] == "ENC"              # зашифрованный — как есть
    assert g["webdav"]["remote_dir"] == "/"          # базовая папка
    assert "enabled" not in g["webdav"]
    assert global_config.is_configured(sm.get("ftp"), g) is False


def test_migrate_creates_global_strips_devices_and_backs_up(sm, cfg_file, tmp_path):
    from core.plugin_manager import PluginManager

    plugin_dir = tmp_path / "Plugin"
    plugin_dir.mkdir()
    routers = [_old_router("10.0.0.1", "/A"), _old_router("10.0.0.2", "/B")]
    (plugin_dir / "routers.json").write_text(json.dumps(routers), encoding="utf-8")
    original = (plugin_dir / "routers.json").read_bytes()

    pm = PluginManager(storage_manager=sm)
    pm._plugins["fake"] = {"func": None, "routers": routers, "dir": str(plugin_dir)}

    report = global_config.migrate(pm, sm)

    assert report["created_global"] is True
    assert report["devices_stripped"] == 2
    assert global_config.load_global()["webdav"]["remote_dir"] == "/"
    assert json.loads(cfg_file.read_text(encoding="utf-8"))["port"] == 8080   # не потеряно

    saved = json.loads((plugin_dir / "routers.json").read_text(encoding="utf-8"))
    assert saved[0]["storage"]["webdav"] == {"enabled": True}
    assert saved[0]["storage"]["ftp"] == {"enabled": False}

    backups = list(plugin_dir.glob("routers.json.bak-*"))
    assert len(backups) == 1 and backups[0].read_bytes() == original

    # Повторно — ничего
    assert global_config.migrate(pm, sm) is None


def test_migrate_keeps_existing_global(sm, cfg_file, tmp_path):
    from core.plugin_manager import PluginManager

    global_config.save_global({"webdav": {"url": "https://already.set", "user": "x"}})
    pm = PluginManager(storage_manager=sm)
    pm._plugins["fake"] = {"func": None, "routers": [_old_router("10.0.0.1", "/A")],
                           "dir": str(tmp_path)}
    pm.save_routers = lambda t: None

    global_config.migrate(pm, sm)
    assert global_config.load_global()["webdav"]["url"] == "https://already.set"


# =========================================================
# Выгрузка использует общие настройки
# =========================================================

def test_push_uses_global_settings(sm, cfg_file, tmp_path, monkeypatch):
    import core.plugin_manager as pm_mod
    from core.plugin_manager import PluginManager

    monkeypatch.setattr(pm_mod, "STATE_FILE", tmp_path / "state.json")

    pm = PluginManager(storage_manager=sm)
    global_config.save_global({
        "ftp": {"host": "ftp.test", "port": 21, "user": "u",
                "pass": pm.storage.encrypt("secret"),
                "remote_dir": "/b", "mode": "all"},
    })

    seen = {}

    class FakeFTP:
        def cwd(self, d): seen.setdefault("cwd", d)
        def pwd(self): return "/"
        def storbinary(self, cmd, fp): seen["stor"] = cmd
        def quit(self): pass
        def close(self): pass

    def fake_connect(cfg):
        seen["pass"] = cfg.get("pass")
        return FakeFTP()

    ftp = sm.get("ftp")
    monkeypatch.setattr(type(ftp), "_connect", staticmethod(fake_connect))
    monkeypatch.setattr(sm.get("git"), "push",
                        lambda cfg, ctx: PushResult(True, "ok", {"changed": True}))

    local = tmp_path / "Dev_Local"
    local.mkdir()
    f = local / "config.rsc"
    f.write_bytes(b"x")
    router = {"host": "10.0.0.7", "storage": {"ftp": {"enabled": True}, "webdav": {"enabled": True}}}

    pm._push_to_storages("mikrotik", 0, "10.0.0.7", router, local, [str(f)])

    assert seen["cwd"] == "/b/Dev_Local"                   # = имя локальной папки
    assert seen["pass"] == "secret"                       # расшифрован
    state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    webdav = state["mikrotik/0"]["backends"]["webdav"]
    assert webdav["ok"] is False and "не настроено" in webdav["message"]


# =========================================================
# Веб
# =========================================================

@pytest.fixture
def web(sm, cfg_file, tmp_path, monkeypatch):
    pytest.importorskip("httpx2", reason="нужен httpx2 для TestClient")
    from fastapi.testclient import TestClient
    import web.server as s

    if s.plugin_manager is None or s.storage_manager is None:
        pytest.skip("нет PluginManager / StorageManager")

    routers = [
        {"host": "10.0.0.1", "name": "A", "backup_path": str(tmp_path / "a"),
         "storage": {"webdav": {"enabled": True}}},
        {"host": "10.0.0.2", "name": "B", "backup_path": str(tmp_path / "b"),
         "storage": {"webdav": {"enabled": True}}},
    ]
    monkeypatch.setattr(s.plugin_manager, "_plugins", {
        "fake": {"func": None, "routers": routers, "dir": str(tmp_path)},
    })
    monkeypatch.setattr(s.plugin_manager, "save_routers", lambda t: None)
    s.app.dependency_overrides[s.require_auth] = lambda: "test"
    yield s, TestClient(s.app), routers
    s.app.dependency_overrides.clear()


def _webdav_form(remote_dir, password=""):
    return {
        "storage_webdav_url": "https://dav.test/x",
        "storage_webdav_vendor": "nextcloud",
        "storage_webdav_user": "u",
        "storage_webdav_pass": password,
        "storage_webdav_remote_dir": remote_dir,
        "storage_webdav_mode": "changes_only",
    }


def test_settings_save_encrypts_and_keeps_password(web):
    s, c, _ = web
    r = c.post("/settings/storages/save", data=_webdav_form("/{host}", "pw1"),
               follow_redirects=False)
    assert r.status_code == 303 and "storages_saved" in r.headers["location"]
    enc = global_config.load_global()["webdav"]["pass"]
    assert enc and enc != "pw1" and s.storage.decrypt(enc) == "pw1"

    # Пустой пароль — остаётся прежний
    c.post("/settings/storages/save", data=_webdav_form("/{host}"), follow_redirects=False)
    assert global_config.load_global()["webdav"]["pass"] == enc


def test_settings_save_rejects_same_local_folder(web, tmp_path):
    s, c, routers = web
    # Два устройства с одинаковым именем локальной папки
    routers[1]["backup_path"] = str(tmp_path / "other" / "a")
    r = c.post("/settings/storages/save", data=_webdav_form("/", "pw"),
               follow_redirects=False)
    assert r.status_code == 303 and "error=" in r.headers["location"]
    assert "webdav" not in global_config.load_global()        # не сохранено

    routers[1]["backup_path"] = str(tmp_path / "b")
    r = c.post("/settings/storages/save", data=_webdav_form("/", "pw"),
               follow_redirects=False)
    assert "storages_saved" in r.headers["location"]


def test_device_form_only_toggles(web):
    s, c, routers = web
    global_config.save_global({"webdav": {"url": "https://dav.test/x", "user": "u",
                                          "remote_dir": "/{host}"}})
    page = c.get("/edit/fake/0").text

    assert 'name="storage_webdav_enabled"' in page
    assert 'name="storage_webdav_url"' not in page              # полей нет
    assert "/10.0.0.1/a" in page          # итоговая папка = база + имя локальной
    assert 'name="storage_ftp_enabled"' in page and "Не настроено" in page

    add = c.get("/add").text
    assert 'name="storage_webdav_enabled"' in add
    assert 'name="storage_webdav_url"' not in add


def test_device_cannot_enable_unconfigured(web):
    s, c, routers = web
    global_config.save_global({"webdav": {"url": "https://dav.test/x", "user": "u",
                                          "remote_dir": "/{host}"}})
    r = c.post("/update-router/fake/0", data={
        "ip": "10.0.0.1", "backup_path": routers[0]["backup_path"],
        "storage_webdav_enabled": "true", "storage_ftp_enabled": "true",
    }, follow_redirects=False)
    assert r.status_code == 303
    st = s.plugin_manager._plugins["fake"]["routers"][0]["storage"]
    assert st["webdav"] == {"enabled": True}
    assert st["ftp"] == {"enabled": False}                        # не настроено
    assert st["git"] == {"enabled": True}


def test_storage_test_uses_saved_password(web, sm, monkeypatch):
    s, c, _ = web
    global_config.save_global({"webdav": {"url": "https://dav.test/x", "user": "u",
                                          "pass": s.storage.encrypt("saved-pw")}})
    seen = {}
    monkeypatch.setattr(s.storage_manager.get("webdav"), "test",
                        lambda cfg: seen.update(cfg) or PushResult(True, "ok"))
    r = c.post("/api/storage/test/webdav", json={"url": "https://dav.test/x", "user": "u", "pass": ""})
    assert r.json()["ok"] is True
    assert seen["pass"] == "saved-pw"
