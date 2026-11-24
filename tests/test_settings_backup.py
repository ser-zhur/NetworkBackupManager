# =========================================================
# tests/test_settings_backup.py — копия настроек программы
# =========================================================

import contextlib
import io
import json
import subprocess
import zipfile

import pytest

from core import context, settings_backup
from core.storage.base import PushResult


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    d = tmp_path / "data"
    (d / "keys" / "mikrotik").mkdir(parents=True)
    (d / "devices.json").write_text('{"devices": []}', encoding="utf-8")
    (d / "secret.key").write_bytes(b"KEY")
    (d / "keys" / "mikrotik" / "r1.key").write_text("PRIVATE", encoding="utf-8")
    (d / "events.jsonl").write_text("{}\n", encoding="utf-8")
    (d / "config.json.bak-20261117-152303").write_text("{}", encoding="utf-8")
    (d / "migration-backup-20261118").mkdir()
    (d / "migration-backup-20261118" / "routers.json").write_text("[]", encoding="utf-8")
    monkeypatch.setattr(context, "DATA_DIR", d)
    return d


def test_collect_skips_logs_and_old_copies(data_dir):
    names = [p.as_posix() for p in settings_backup.collect_files()]
    assert names == ["devices.json", "keys/mikrotik/r1.key", "secret.key"]


def test_zip_without_password(data_dir, tmp_path):
    path = settings_backup.build(tmp_path)
    assert path.name.startswith("nbm-settings-") and path.suffix == ".zip"
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        assert {"data/devices.json", "data/secret.key", "data/keys/mikrotik/r1.key",
                settings_backup.MANIFEST_NAME, settings_backup.RESTORE_NAME} == names
        assert z.read("data/secret.key") == b"KEY"
        manifest = json.loads(z.read(settings_backup.MANIFEST_NAME))
    assert manifest["version"] and "data/secret.key" in manifest["files"]


def test_7z_with_password(data_dir, tmp_path):
    pytest.importorskip("py7zr")
    from core.storage.encryption import read_archive

    path = settings_backup.build(tmp_path, password="correct-horse")
    assert path.suffix == ".7z"
    with pytest.raises(Exception):
        read_archive(path, "wrong-password")
    files = read_archive(path, "correct-horse")
    assert files["data/keys/mikrotik/r1.key"] == b"PRIVATE"
    assert settings_backup.RESTORE_NAME in files


def test_empty_data_dir_is_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(context, "DATA_DIR", tmp_path / "nothing")
    with pytest.raises(settings_backup.SettingsBackupError):
        settings_backup.build(tmp_path)


def test_validate_password():
    assert settings_backup.validate_password("", "") is None
    assert "короче" in settings_backup.validate_password("short", "short")
    assert "не совпадают" in settings_backup.validate_password("longenough1", "longenough2")
    assert settings_backup.validate_password("longenough1", "longenough1") is None


@pytest.mark.parametrize("base, expected", [
    ("", "/_nbm_settings"),
    ("/", "/_nbm_settings"),
    ("backups/nbm", "/backups/nbm/_nbm_settings"),
    ("/srv/nbm/{type}/{name}", "/srv/nbm/_nbm_settings"),
    ("a\\b", "/a/b/_nbm_settings"),
])
def test_remote_dir(base, expected):
    assert settings_backup.remote_dir(base) == expected


def test_put_file_uses_copyto(tmp_path, monkeypatch):
    from core.storage import rclone_remote

    calls = []
    monkeypatch.setattr(rclone_remote.rclone_mod, "is_available", lambda: True)
    monkeypatch.setattr(
        rclone_remote.rclone_mod, "run",
        lambda args, env, timeout: calls.append(args) or subprocess.CompletedProcess(args, 0, "", ""),
    )
    backend = rclone_remote.RcloneRemote({"id": "s1", "type": "sftp"})
    monkeypatch.setattr(backend, "_rclone_env", lambda cfg: contextlib.nullcontext({}))
    f = tmp_path / "nbm-settings-1.7z"
    f.write_bytes(b"x")

    r = backend.put_file({"type": "sftp", "base_dir": "/srv"}, f, "/srv/_nbm_settings")
    assert r.ok
    args = calls[0]
    assert args[0] == "copyto" and args[-3] == "--"
    assert args[-1] == "NBM:/srv/_nbm_settings/nbm-settings-1.7z"


# ---------------------------------------------------------- веб

@pytest.fixture
def client(data_dir):
    s = pytest.importorskip("web.server")
    pytest.importorskip("httpx2", reason="нужен httpx2 для TestClient")
    from fastapi.testclient import TestClient
    s.app.dependency_overrides[s.require_auth] = lambda: "test"
    yield s, TestClient(s.app)
    s.app.dependency_overrides.clear()


def test_page_lists_tab(client):
    _, c = client
    page = c.get("/settings?tab=backup").text
    assert "Копия настроек программы" in page and "/settings/backup/download" in page


def test_download_zip(client):
    _, c = client
    r = c.post("/settings/backup/download", data={"password": "", "confirm": ""})
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"
    assert "nbm-settings-" in r.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        assert "data/secret.key" in z.namelist()


def test_download_bad_password_redirects(client):
    _, c = client
    r = c.post("/settings/backup/download", data={"password": "longenough1", "confirm": "x"},
               follow_redirects=False)
    assert r.status_code == 303 and "tab=backup&error=" in r.headers["location"]


class _FakeRemote:
    is_authority = False
    always_enabled = False
    icon = "☁"

    def __init__(self, rid, ok=True):
        self.id, self.name, self.ok, self.uploaded = rid, rid.upper(), ok, []

    def put_file(self, cfg, path, directory):
        self.uploaded.append((path.name, directory, path.read_bytes()[:6]))
        return PushResult.success("готово") if self.ok else PushResult.failure("нет связи")


@pytest.fixture
def remotes(client, monkeypatch):
    s, _ = client
    pytest.importorskip("py7zr")
    good, bad = _FakeRemote("good"), _FakeRemote("bad", ok=False)
    monkeypatch.setattr(s, "_external_backends", lambda: [good, bad])
    monkeypatch.setattr(s.global_config, "load_global",
                        lambda: {"good": {"base_dir": "/nbm/{name}"}, "bad": {}})
    monkeypatch.setattr(s.global_config, "is_configured", lambda b, st=None: True)
    monkeypatch.setattr(s.storage_encryption, "get_password", lambda: "storage-pass")
    return good, bad


def test_upload_encrypts_and_reports_each_storage(client, remotes):
    _, c = client
    good, bad = remotes
    r = c.post("/api/settings-backup/upload", json={"ids": ["good", "bad"]}).json()
    assert not r["ok"]
    assert [(x["id"], x["ok"]) for x in r["results"]] == [("good", True), ("bad", False)]
    name, directory, head = good.uploaded[0]
    assert name.endswith(".7z") and directory == "/nbm/_nbm_settings"
    assert head == b"7z\xbc\xaf\x27\x1c"          # сигнатура 7z


def test_upload_needs_encryption_password(client, remotes, monkeypatch):
    s, c = client
    monkeypatch.setattr(s.storage_encryption, "get_password", lambda: "")
    r = c.post("/api/settings-backup/upload", json={"ids": ["good"]}).json()
    assert not r["ok"] and "пароль шифрования" in r["message"]
    assert not remotes[0].uploaded


def test_upload_rejects_unknown_storage(client, remotes):
    _, c = client
    r = c.post("/api/settings-backup/upload", json={"ids": ["nope"]}).json()
    assert not r["ok"] and not remotes[0].uploaded
    assert not c.post("/api/settings-backup/upload", json={"ids": []}).json()["ok"]
