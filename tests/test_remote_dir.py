# =========================================================
# tests/test_remote_dir.py
# =========================================================
#
# Каталог на внешнем сервере: подстановки {host}/{name}/
# {type} и защита от пересечения каталогов устройств.
#
# Сеть не используется; rclone — только тип local
# (папка на диске), если rclone установлен.
#
# =========================================================

import json
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
# Хранилище rclone: пересечения папок и путь для rclone
# =========================================================

from core.storage.rclone_remote import RcloneRemote, rclone_path  # noqa: E402

REMOTE = {"id": "r1", "name": "Тест", "type": "local", "options": {}, "base_dir": "/b"}


def test_same_local_folder_conflicts():
    r = RcloneRemote(REMOTE)
    assert r.target_key(REMOTE, host="10.0.0.1", folder="Office") == r.target_key(
        {**REMOTE, "base_dir": "b/"}, host="10.0.0.2", folder="office",
    )


def test_local_folder_separates_devices():
    r = RcloneRemote(REMOTE)
    assert r.target_key(REMOTE, host="x", folder="A") != r.target_key(
        REMOTE, host="x", folder="B",
    )


def test_different_remotes_never_conflict():
    a = RcloneRemote(REMOTE)
    b = RcloneRemote({**REMOTE, "id": "r2"})
    assert a.target_key(REMOTE, host="x", folder="A") != b.target_key(
        REMOTE, host="x", folder="A",
    )


def test_git_not_checked():
    from core.storage import StorageManager
    sm = StorageManager()
    sm.discover()
    assert sm.get("git").target_key({}, host="x") is None


def test_rclone_path_absolute_only_where_meaningful():
    # FTP/SFTP/local: ведущий '/' из базовой папки сохраняется
    assert rclone_path("ftp", "/home/ftptest", "/home/ftptest/Dev") == "/home/ftptest/Dev"
    assert rclone_path("sftp", "backups", "/backups/Dev") == "backups/Dev"
    # WebDAV, S3, облака: без ведущего '/'
    assert rclone_path("webdav", "/", "/Dev") == "Dev"
    assert rclone_path("s3", "/bucket", "/bucket/Dev") == "bucket/Dev"


# =========================================================
# Выгрузка настоящим rclone (тип local — без сети)
# =========================================================

def _rclone_available():
    from core import rclone as rclone_mod
    return rclone_mod.is_available() and rclone_mod.provider("local") is not None


def _ctx(local_dir):
    f = local_dir / "config.rsc"
    f.write_bytes(b"/system identity")
    return PushContext(
        device_id="mikrotik/0",
        device_host="10.0.0.7",
        device_type="mikrotik",
        backup_path=local_dir,
        files={"config.rsc": f},
        changed=True,
        run_time=datetime.now(),
    )


def test_push_to_local_with_real_rclone(tmp_path):
    if not _rclone_available():
        pytest.skip("rclone не найден")

    local = tmp_path / "Core_SW_local"
    local.mkdir()
    remote_root = tmp_path / "remote"
    cfg = {**REMOTE, "base_dir": str(remote_root).replace("\\", "/"), "mode": "all"}

    r = RcloneRemote(cfg).push(cfg, _ctx(local))

    assert r.ok, r.message
    # Папка на хранилище = имя локальной папки
    assert (remote_root / "Core_SW_local" / "config.rsc").read_bytes() == b"/system identity"


def test_test_reports_missing_base_dir_as_ok(tmp_path):
    if not _rclone_available():
        pytest.skip("rclone не найден")
    cfg = {**REMOTE, "base_dir": str(tmp_path / "not-yet").replace("\\", "/")}
    r = RcloneRemote(cfg).test(cfg)
    assert r.ok, r.message
    assert "будет создана" in r.message


def test_push_failure_reports_rclone_error(tmp_path, monkeypatch):
    from core import rclone as rclone_mod

    class Failed:
        returncode = 1
        stdout = ""
        stderr = "line1\nCRITICAL: permission denied"

    monkeypatch.setattr(rclone_mod, "is_available", lambda: True)
    monkeypatch.setattr(rclone_mod, "remote_env", lambda *a, **k: {})
    monkeypatch.setattr(rclone_mod, "run", lambda *a, **k: Failed())

    local = tmp_path / "Dev"
    local.mkdir()
    cfg = {**REMOTE, "mode": "all"}
    r = RcloneRemote(cfg).push(cfg, _ctx(local))
    assert not r.ok and "permission denied" in r.message


# =========================================================
# Сохранение устройства с конфликтующей папкой
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

    if not s.plugin_manager or s.storage_manager is None:
        pytest.skip("нет PluginManager")

    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps({"remotes": [
        {"id": "t-local", "name": "Т", "type": "local", "options": {}, "base_dir": "/backups"},
    ]}), encoding="utf-8")
    monkeypatch.setattr(global_config, "CONFIG_FILE", cfg_file)
    s.storage_manager.reload_remotes()

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
            "storage_t-local_enabled": "true",
        }, follow_redirects=False)

    try:
        assert post("192.0.2.1", tmp_path / "site1" / "Office").status_code == 303
        assert routers[0]["storage"]["t-local"] == {"enabled": True}
        r = post("192.0.2.2", tmp_path / "site2" / "Office")
        assert r.status_code == 400
        assert "192.0.2.1" in r.json()["detail"]
        assert len(routers) == 1                      # второе не добавлено

        # Другое имя локальной папки — сохраняется
        assert post("192.0.2.3", tmp_path / "site2" / "Office2").status_code == 303
        assert len(routers) == 2
    finally:
        s.app.dependency_overrides.clear()
        monkeypatch.undo()
        s.storage_manager.reload_remotes()
