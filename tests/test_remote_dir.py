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


def test_same_server_same_dir_conflicts(backends):
    ftp = backends.get("ftp")
    cfg = {"host": "ftp.test", "port": 21, "remote_dir": "/shared"}
    assert ftp.target_key(cfg, host="10.0.0.1") == ftp.target_key(
        {**cfg, "remote_dir": "shared/"}, host="10.0.0.2",
    )


def test_host_placeholder_separates_devices(backends):
    ftp = backends.get("ftp")
    cfg = {"host": "ftp.test", "port": 21, "remote_dir": "/b/{host}"}
    assert ftp.target_key(cfg, host="10.0.0.1") != ftp.target_key(
        cfg, host="10.0.0.2",
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


def test_new_device_defaults_use_host(backends):
    defaults = backends.defaults_for_new_router()
    assert defaults["ftp"]["remote_dir"] == "/network_backups/{host}"
    assert defaults["webdav"]["remote_dir"] == "/network_backups/{host}"


# =========================================================
# push пишет в развёрнутый каталог
# =========================================================

def _ctx(tmp_path, host="10.0.0.7", name="Core SW"):
    f = tmp_path / "config.rsc"
    f.write_bytes(b"x")
    return PushContext(
        device_id="mikrotik/0",
        device_host=host,
        device_type="mikrotik",
        backup_path=tmp_path,
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
        {"host": "ftp.test", "remote_dir": "/b/{host}/{name}", "mode": "all"},
        _ctx(tmp_path),
    )

    assert r.ok, r.message
    assert ("cwd", "/b/10.0.0.7/Core SW") in calls
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
        {"url": "https://dav.test/", "user": "u", "remote_dir": "/b/{host}", "mode": "all"},
        _ctx(tmp_path),
    )

    assert r.ok, r.message
    assert targets == ["nbm_webdav:b/10.0.0.7/config.rsc"]


# =========================================================
# Сохранение устройства с конфликтующим каталогом
# =========================================================

def test_save_router_rejects_conflicting_dir(monkeypatch):
    pytest.importorskip("httpx2", reason="нужен httpx2 для TestClient")
    from fastapi.testclient import TestClient
    import web.server as s

    if not s.plugin_manager or "mikrotik" not in s.plugin_manager._plugins:
        pytest.skip("плагин mikrotik не загружен")
    if s.storage_manager is None or s.storage_manager.get("ftp") is None:
        pytest.skip("FTP backend не загружен")

    # Реальный routers.json не пишем
    monkeypatch.setattr(s.plugin_manager, "save_routers", lambda t: None)
    s.app.dependency_overrides[s.require_auth] = lambda: "test"

    routers = s.plugin_manager._plugins["mikrotik"]["routers"]
    before = len(routers)
    client = TestClient(s.app)

    def post(ip, remote_dir):
        return client.post(
            "/save-router",
            data={
                "device_type": "mikrotik",
                "ip": ip,
                "backup_path": f"Backups/_nbm_test_{ip}",
                "storage_ftp_enabled": "true",
                "storage_ftp_host": "ftp.nbm-test.invalid",
                "storage_ftp_port": "21",
                "storage_ftp_remote_dir": remote_dir,
            },
            follow_redirects=False,
        )

    try:
        assert post("192.0.2.1", "/nbm_shared").status_code == 303
        r = post("192.0.2.2", "/nbm_shared/")
        assert r.status_code == 400
        assert "192.0.2.1" in r.json()["detail"]
        assert len(routers) == before + 1          # второе не добавлено

        # С подстановкой {host} — каталоги разные, сохраняется
        assert post("192.0.2.3", "/nbm_shared/{host}").status_code == 303
        assert len(routers) == before + 2
    finally:
        del routers[before:]
        s.app.dependency_overrides.clear()
