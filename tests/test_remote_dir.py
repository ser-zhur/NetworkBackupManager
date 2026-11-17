# =========================================================
# tests/test_remote_dir.py
# =========================================================
#
# Каталог на внешнем сервере: подстановки {host}/{name}/
# {type} и защита от пересечения каталогов устройств.
#
# Сеть и rclone не используются — всё подменяется.
#
# =========================================================

from datetime import datetime

import pytest

from core.storage.base import (
    PushContext,
    device_remote_dir,
    expand_remote_dir,
    normalize_remote_dir,
)


# =========================================================
# expand_remote_dir
# =========================================================

def test_expand_host_name_type():
    assert expand_remote_dir(
        "/b/{type}/{host}/{name}",
        host="10.0.0.1", name="Office", device_type="mikrotik",
    ) == "/b/mikrotik/10.0.0.1/Office"


def test_name_falls_back_to_host():
    assert expand_remote_dir("/b/{name}", host="10.0.0.1") == "/b/10.0.0.1"


def test_values_cannot_escape_directory():
    # '/' и '..' в названии не создают вложенных/соседних каталогов
    assert expand_remote_dir("/b/{name}", host="h", name="../x/y") == "/b/_x_y"
    assert expand_remote_dir("/b/{name}", host="h", name='a:b*c?"<>|') == "/b/a_b_c_____"


def test_no_placeholders_unchanged():
    assert expand_remote_dir("/home/ftptest", host="h") == "/home/ftptest"


def test_unknown_placeholder_kept():
    assert expand_remote_dir("/b/{site}/{host}", host="h") == "/b/{site}/h"


def test_malformed_braces_returned_as_is():
    assert expand_remote_dir("/b/{host", host="h") == "/b/{host"
    assert expand_remote_dir("/b/{}", host="h") == "/b/{}"


def test_device_dir_is_base_plus_local_folder():
    assert device_remote_dir("/", folder="192.0.2.1_Office") == "/192.0.2.1_Office"
    assert device_remote_dir("/home/ftptest/", folder="ZpStal") == "/home/ftptest/ZpStal"
    assert device_remote_dir("backup", folder="EU_DC") == "/backup/EU_DC"
    assert device_remote_dir("/b/{type}", folder="X", device_type="mikrotik") == "/b/mikrotik/X"
    assert device_remote_dir("/b", folder="") == "/b"


def test_normalize_remote_dir():
    assert normalize_remote_dir("a//b/") == "/a/b"
    assert normalize_remote_dir("\\a\\b") == "/a/b"
    assert normalize_remote_dir("") == "/"


# =========================================================
# target_key
# =========================================================

@pytest.fixture(scope="module")
def backends():
    from core.storage import StorageManager

    sm = StorageManager()
    sm.discover()
    return sm


def test_same_local_folder_conflicts(backends):
    ftp = backends.get("ftp")
    cfg = {"host": "ftp.test", "port": 21, "remote_dir": "/shared"}
    assert ftp.target_key(cfg, host="10.0.0.1", folder="Office") == ftp.target_key(
        {**cfg, "remote_dir": "shared/"}, host="10.0.0.2", folder="office",
    )


def test_local_folder_separates_devices(backends):
    ftp = backends.get("ftp")
    cfg = {"host": "ftp.test", "port": 21, "remote_dir": "/b"}
    assert ftp.target_key(cfg, host="x", folder="A") != ftp.target_key(
        cfg, host="x", folder="B",
    )


def test_different_server_no_conflict(backends):
    ftp = backends.get("ftp")
    a = {"host": "ftp.test", "port": 21, "remote_dir": "/shared"}
    assert ftp.target_key(a, host="x") != ftp.target_key(
        {**a, "port": 2121}, host="x",
    )
    webdav = backends.get("webdav")
    w = {"url": "https://a.test/dav/", "remote_dir": "/shared"}
    assert webdav.target_key(w, host="x") == webdav.target_key(
        {**w, "url": "https://A.test/dav"}, host="y",
    )


def test_git_not_checked(backends):
    assert backends.get("git").target_key({}, host="x") is None


def test_default_base_folder(backends):
    # Базовая папка по умолчанию; папка устройства — внутри
    for bid in ("ftp", "webdav"):
        field = next(f for f in backends.get(bid).fields if f.name == "remote_dir")
        assert field.default == "/network_backups"


# =========================================================
# push пишет в развёрнутый каталог
# =========================================================

def _ctx(tmp_path, host="10.0.0.7", name="Core SW"):
    # Локальная папка устройства — «Core_SW_local»
    local = tmp_path / "Core_SW_local"
    local.mkdir(exist_ok=True)
    f = local / "config.rsc"
    f.write_bytes(b"x")
    return PushContext(
        device_id="mikrotik/0",
        device_host=host,
        device_type="mikrotik",
        backup_path=local,
        files={"config.rsc": f},
        changed=True,
        run_time=datetime.now(),
        device_name=name,
    )


def test_ftp_push_uses_expanded_dir(backends, tmp_path, monkeypatch):
    ftp = backends.get("ftp")
    calls = []

    class FakeFTP:
        def cwd(self, d): calls.append(("cwd", d))
        def pwd(self): return "/"
        def storbinary(self, cmd, fp): calls.append(("stor", cmd))
        def quit(self): pass
        def close(self): pass

    monkeypatch.setattr(type(ftp), "_connect", staticmethod(lambda cfg: FakeFTP()))

    r = ftp.push(
        {"host": "ftp.test", "remote_dir": "/b", "mode": "all"},
        _ctx(tmp_path),
    )

    assert r.ok, r.message
    assert ("cwd", "/b/Core_SW_local") in calls
    assert ("stor", "STOR config.rsc") in calls


def test_webdav_push_uses_expanded_dir(backends, tmp_path, monkeypatch):
    from core import rclone as rclone_mod

    webdav = backends.get("webdav")
    targets = []

    class Done:
        returncode = 0
        stdout = stderr = ""

    def fake_run(args, env=None, timeout=0):
        targets.append(args[-1])
        return Done()

    cfg_file = tmp_path / "rclone.conf"
    cfg_file.write_text("")
    monkeypatch.setattr(rclone_mod, "is_available", lambda: True)
    monkeypatch.setattr(rclone_mod, "build_temp_config", lambda n, c: ({}, cfg_file))
    monkeypatch.setattr(rclone_mod, "run", fake_run)

    r = webdav.push(
        {"url": "https://dav.test/", "user": "u", "remote_dir": "/", "mode": "all"},
        _ctx(tmp_path),
    )

    assert r.ok, r.message
    # Папка на WebDAV = имя локальной папки
    assert targets == ["nbm_webdav:Core_SW_local/config.rsc"]


# =========================================================
# Сохранение устройства с конфликтующим каталогом
# =========================================================

def test_save_router_rejects_conflicting_dir(monkeypatch, tmp_path):
    """
    Папка на хранилище = имя локальной папки. Два устройства
    с одинаковым именем локальной папки (в разных каталогах)
    попали бы в одну удалённую папку → второе не сохраняется.
    """
    pytest.importorskip("httpx2", reason="нужен httpx2 для TestClient")
    from fastapi.testclient import TestClient
    import web.server as s
    from core.storage import global_config

    if not s.plugin_manager or s.storage_manager is None \
            or s.storage_manager.get("ftp") is None:
        pytest.skip("нет PluginManager или FTP backend")

    monkeypatch.setattr(global_config, "load_global", lambda: {
        "ftp": {"host": "ftp.nbm-test.invalid", "port": 21,
                "user": "u", "remote_dir": "/backups"},
    })
    routers = []
    monkeypatch.setattr(s.plugin_manager, "_plugins", {
        "mikrotik": {"func": None, "routers": routers, "dir": str(tmp_path)},
    })
    monkeypatch.setattr(s.plugin_manager, "save_routers", lambda t: None)
    s.app.dependency_overrides[s.require_auth] = lambda: "test"
    client = TestClient(s.app)

    def post(ip, backup_path):
        return client.post("/save-router", data={
            "device_type": "mikrotik",
            "ip": ip,
            "backup_path": str(backup_path),
            "storage_ftp_enabled": "true",
        }, follow_redirects=False)

    try:
        assert post("192.0.2.1", tmp_path / "site1" / "Office").status_code == 303
        r = post("192.0.2.2", tmp_path / "site2" / "Office")
        assert r.status_code == 400
        assert "192.0.2.1" in r.json()["detail"]
        assert len(routers) == 1                      # второе не добавлено

        # Другое имя локальной папки — сохраняется
        assert post("192.0.2.3", tmp_path / "site2" / "Office2").status_code == 303
        assert len(routers) == 2
    finally:
        s.app.dependency_overrides.clear()
