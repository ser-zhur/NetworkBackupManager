# =========================================================
# tests/test_settings_backup.py — копия настроек программы
# =========================================================

import contextlib
import io
import json
import subprocess
import zipfile
from datetime import datetime

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
        self.id, self.name, self.ok, self.uploaded, self.pruned = rid, rid.upper(), ok, [], []

    def put_file(self, cfg, path, directory):
        self.uploaded.append((path.name, directory, path.read_bytes()[:6]))
        return PushResult.success("готово") if self.ok else PushResult.failure("нет связи")

    def prune_files(self, cfg, directory, name_re, keep):
        self.pruned.append((directory, keep))
        return PushResult.success("", deleted=2)


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


# ---------------------------------------------------------- старые копии на хранилище

def test_prune_keeps_newest_and_ignores_foreign_files(monkeypatch):
    from core.storage import rclone_remote

    listing = "\n".join([
        "nbm-settings-20261101-040000.7z",
        "nbm-settings-20261103-040000.7z",
        "nbm-settings-20261102-040000.7z",
        "readme.txt",
        "nbm-settings-latest.7z",
    ]) + "\n"
    calls = []

    def run(args, env, timeout):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, listing if args[0] == "lsf" else "", "")

    monkeypatch.setattr(rclone_remote.rclone_mod, "run", run)
    backend = rclone_remote.RcloneRemote({"id": "s1", "type": "webdav"})
    monkeypatch.setattr(backend, "_rclone_env", lambda cfg: contextlib.nullcontext({}))

    r = backend.prune_files({"type": "webdav"}, "/nbm/_nbm_settings", settings_backup.ARCHIVE_RE, 2)
    assert r.ok and r.details["deleted"] == 1
    deleted = [a[-1] for a in calls if a[0] == "deletefile"]
    assert deleted == ["NBM:nbm/_nbm_settings/nbm-settings-20261101-040000.7z"]


# ---------------------------------------------------------- расписание

def test_next_run():
    cfg = dict(settings_backup.AUTO_DEFAULTS, enabled=True, storages=["s"], time="04:00", every_days=7)
    now = datetime(2026, 11, 24, 3, 0)
    assert settings_backup.next_run(cfg, now) == datetime(2026, 11, 24, 4, 0)
    assert settings_backup.next_run(cfg, datetime(2026, 11, 24, 9, 0)) == datetime(2026, 11, 24, 9, 0)
    cfg["last_run"] = "2026-11-20 06:30:00"                    # опоздала — от её даты
    assert settings_backup.next_run(cfg, now) == datetime(2026, 11, 27, 4, 0)
    assert settings_backup.next_run(dict(cfg, enabled=False), now) is None
    assert settings_backup.next_run(dict(cfg, storages=[]), now) is None


def test_validate_auto():
    ok = {"enabled": "1", "every_days": "1", "time": "04:00", "keep": "10", "storages": ["s"]}
    assert settings_backup.validate_auto(ok)[1] == ""
    assert "ЧЧ:ММ" in settings_backup.validate_auto(dict(ok, time="25:00"))[1]
    assert settings_backup.validate_auto(dict(ok, every_days="0"))[1]
    assert settings_backup.validate_auto(dict(ok, keep="-1"))[1]
    assert "хранилище" in settings_backup.validate_auto(dict(ok, storages=[]))[1]
    assert settings_backup.validate_auto(dict(ok, enabled=None, storages=[]))[1] == ""


def test_save_auto_keeps_last_result(data_dir):
    settings_backup._save_result(True, "готово", datetime(2026, 11, 24, 4, 0))
    settings_backup.save_auto({"enabled": True, "every_days": 2, "time": "05:00",
                               "keep": 3, "storages": ["s"]})
    cfg = settings_backup.load_auto()
    assert cfg["every_days"] == 2 and cfg["last_run"] == "2026-11-24 04:00:00" and cfg["last_ok"]


def test_scheduled_run_uploads_and_records_result(data_dir, monkeypatch):
    pytest.importorskip("py7zr")
    from core.storage import encryption, global_config

    remote = _FakeRemote("s1")
    monkeypatch.setattr(global_config, "load_global", lambda: {"s1": {"base_dir": "/b"}})
    monkeypatch.setattr(global_config, "is_configured", lambda b, st=None: True)
    monkeypatch.setattr(encryption, "get_password", lambda: "storage-pass")
    settings_backup.save_auto({"enabled": True, "every_days": 1, "time": "00:00",
                               "keep": 5, "storages": ["s1"]})

    assert settings_backup.tick(lambda: [remote])
    for _ in range(200):                                    # поток копии
        if not settings_backup.is_running():
            break
        __import__("time").sleep(0.05)
    cfg = settings_backup.load_auto()
    assert cfg["last_ok"] and remote.uploaded and remote.pruned == [("/b/_nbm_settings", 5)]
    assert not settings_backup.tick(lambda: [remote])        # следующая — завтра


def test_scheduled_run_failure_is_an_event(data_dir, monkeypatch):
    from core import events
    from core.storage import encryption

    monkeypatch.setattr(encryption, "get_password", lambda: "")
    seen = []
    monkeypatch.setattr(events, "emit", lambda kind, msg, **kw: seen.append((kind, msg)))
    settings_backup._auto_running.acquire()
    settings_backup._run_auto({"storages": [], "keep": 0}, lambda: [])
    cfg = settings_backup.load_auto()
    assert cfg["last_run"] and cfg["last_ok"] is False
    assert seen and seen[0][0] == events.SCHEDULER_ERROR


# ---------------------------------------------------------- восстановление

def _zip(path, entries):
    with zipfile.ZipFile(path, "w") as z:
        for name, data in entries.items():
            z.writestr(name, data)
    return path


def test_restore_roundtrip(data_dir, tmp_path):
    archive = settings_backup.build(tmp_path)                  # снимок «до»
    (data_dir / "devices.json").write_text('{"devices": [{"id": "x"}]}', encoding="utf-8")
    (data_dir / "keys" / "mikrotik" / "new.key").write_text("NEW", encoding="utf-8")
    (data_dir / "secret.key").unlink()

    staged = tmp_path / "staged"
    staged.mkdir()
    info = settings_backup.stage_restore(archive, "", staged)
    assert info["files"] == 3 and info["devices"] == 0 and info["has_secret"]

    saved = settings_backup.apply_restore(staged)
    assert (data_dir / "secret.key").read_bytes() == b"KEY"
    assert (data_dir / "devices.json").read_text(encoding="utf-8") == '{"devices": []}'
    assert not (data_dir / "keys" / "mikrotik" / "new.key").exists()
    assert (saved / "keys" / "mikrotik" / "new.key").read_text(encoding="utf-8") == "NEW"
    assert (data_dir / "events.jsonl").exists()                # журнал не трогаем
    assert saved.name.startswith("restore-backup-")
    assert all("restore-backup" not in p.as_posix() for p in settings_backup.collect_files())


def test_restore_encrypted(data_dir, tmp_path):
    pytest.importorskip("py7zr")
    archive = settings_backup.build(tmp_path, password="long-password")
    for pw in ("", "wrong-password"):
        d = tmp_path / f"s{len(pw)}"
        d.mkdir()
        with pytest.raises(settings_backup.SettingsBackupError, match="не открылся"):
            settings_backup.stage_restore(archive, pw, d)
    d = tmp_path / "ok"
    d.mkdir()
    assert settings_backup.stage_restore(archive, "long-password", d)["has_secret"]


@pytest.mark.parametrize("entries, message", [
    ({"data/../evil.txt": "x"}, "посторонний"),
    ({"/etc/passwd": "x"}, "посторонний"),
    ({"other/file.txt": "x"}, "посторонний"),
    ({"data/devices.json": "{}"}, "NBM_SETTINGS"),
    ({settings_backup.MANIFEST_NAME: '{"program": "Other"}', "data/a.json": "{}"}, "не копия"),
    ({settings_backup.MANIFEST_NAME: json.dumps({"program": settings_backup.PROGRAM})}, "нет файлов"),
])
def test_restore_rejects_bad_archives(tmp_path, entries, message):
    archive = _zip(tmp_path / "a.zip", entries)
    staged = tmp_path / "staged"
    staged.mkdir()
    with pytest.raises(settings_backup.SettingsBackupError, match=message):
        settings_backup.stage_restore(archive, "", staged)
    assert not (tmp_path / "evil.txt").exists()


def test_restore_rejects_not_an_archive(tmp_path):
    f = tmp_path / "x.zip"
    f.write_bytes(b"hello")
    with pytest.raises(settings_backup.SettingsBackupError, match="не архив"):
        settings_backup.stage_restore(f, "", tmp_path)


def test_web_restore_flow(client, data_dir, tmp_path, monkeypatch):
    s, c = client
    from core import backup_state

    archive = settings_backup.build(tmp_path)
    (data_dir / "secret.key").write_bytes(b"CHANGED")
    restarted = []
    monkeypatch.setattr(s, "_restart_after_restore", lambda: restarted.append(1) or (True, "перезапуск"))
    monkeypatch.setattr(s.scheduler, "stop", lambda: None)

    page = c.post("/settings/backup/restore/upload", data={"password": ""},
                  files={"file": (archive.name, archive.read_bytes(), "application/zip")}).text
    assert "Архив проверен" in page and "устройств: 0" in page
    token = page.split('name="token" value="', 1)[1].split('"', 1)[0]

    r = c.post("/settings/backup/restore/apply", data={"token": token}, follow_redirects=False)
    assert r.status_code == 303 and "error=" in r.headers["location"]          # без подтверждения

    try:
        r = c.post("/settings/backup/restore/apply", data={"token": token, "confirm": "1"})
        assert r.status_code == 200 and "Настройки восстановлены" in r.text
        assert (data_dir / "secret.key").read_bytes() == b"KEY" and restarted
    finally:
        backup_state.finish_mass()

    r = c.post("/settings/backup/restore/apply", data={"token": token, "confirm": "1"},
               follow_redirects=False)
    assert "error=" in r.headers["location"]                                    # токен одноразовый


def test_web_restore_bad_file(client):
    _, c = client
    r = c.post("/settings/backup/restore/upload", data={"password": ""},
               files={"file": ("x.zip", b"not an archive", "application/zip")},
               follow_redirects=False)
    assert r.status_code == 303 and "error=" in r.headers["location"]


def test_web_auto_settings(client, remotes):
    _, c = client
    r = c.post("/settings/backup/auto", data={"enabled": "1", "every_days": "3", "time": "02:30",
                                               "keep": "4", "storages": ["good"]},
               follow_redirects=False)
    assert r.headers["location"].endswith("message=auto_saved")
    cfg = settings_backup.load_auto()
    assert cfg["enabled"] and cfg["every_days"] == 3 and cfg["storages"] == ["good"]

    r = c.post("/settings/backup/auto", data={"enabled": "1", "every_days": "1", "time": "02:30",
                                               "keep": "4", "storages": ["nope"]},
               follow_redirects=False)
    assert "error=" in r.headers["location"]
    page = c.get("/settings?tab=backup").text
    assert "По расписанию" in page and "Восстановить из архива" in page


def test_unexpected_error_text_stays_in_log(client, remotes, monkeypatch):
    s, c = client

    def boom(*a, **kw):
        raise OSError("/srv/secret/path: permission denied")

    monkeypatch.setattr(s.settings_backup, "upload", boom)
    r = c.post("/api/settings-backup/upload", json={"ids": ["good"]}).json()
    assert not r["ok"] and "/srv/secret" not in r["message"] and "журнал" not in r["message"]
    assert "общем логе" in r["message"]

    monkeypatch.setattr(s.settings_backup, "build", boom)
    r = c.post("/settings/backup/download", data={"password": "", "confirm": ""},
               follow_redirects=False)
    assert "secret" not in r.headers["location"]
