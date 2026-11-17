# =========================================================
# tests/test_encryption.py
# =========================================================
#
# Шифрование файлов на внешних хранилищах: архивы 7z
# (AES-256) с общим паролем, удаление открытых копий,
# повторная синхронизация без перешифрования, веб-настройки.
#
# Настоящий rclone (хранилище local во временной папке);
# config.json временный. Тесты архивов — только если
# установлена библиотека py7zr.
#
# =========================================================

import json
import subprocess
from datetime import datetime

import pytest

from core import rclone as rclone_mod
from core.storage import encryption, global_config
from core.storage.base import PushContext
from core.storage.rclone_remote import RcloneRemote

needs_rclone = pytest.mark.skipif(
    not (rclone_mod.is_available() and rclone_mod.provider("local")),
    reason="rclone не найден",
)
needs_7z = pytest.mark.skipif(not encryption.is_available(), reason="py7zr не установлен")

PASSWORD = "Секрет-пароль-123"


@pytest.fixture
def archiver(monkeypatch):
    """
    Настоящий 7z (py7zr), а если библиотеки нет — простая
    подделка: логика выгрузки, пропуска уже зашифрованного и
    удаления открытых копий проверяется и без py7zr.
    """
    if encryption.is_available():
        return "py7zr"

    def fake_make(src, dst, arcname, password):
        if not password:
            raise ValueError("пароль шифрования не задан")
        dst.write_bytes(b"FAKE7Z|" + password.encode() + b"|" + arcname.encode()
                        + b"|" + src.read_bytes())

    def fake_read(path, password):
        _, pw, name, data = path.read_bytes().split(b"|", 3)
        if pw.decode() != password:
            raise ValueError("wrong password")
        return {name.decode(): data}

    monkeypatch.setattr(encryption, "is_available", lambda: True)
    monkeypatch.setattr(encryption, "make_archive", fake_make)
    monkeypatch.setattr(encryption, "read_archive", fake_read)
    return "fake"


@pytest.fixture
def cfg_file(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(global_config, "CONFIG_FILE", path)
    return path


def _remote(tmp_path, encrypt=True):
    return {"id": "r", "name": "r", "type": "local", "options": {},
            "base_dir": str(tmp_path / "remote").replace("\\", "/"),
            "mode": "all", "encrypt": encrypt}


def _ctx(folder, files, immutable=False):
    return PushContext(
        device_id="mikrotik/0", device_host="10.0.0.1", device_type="mikrotik",
        backup_path=folder, files=files, changed=True, run_time=datetime.now(),
        immutable=immutable,
    )


def _device(tmp_path, name="Dev", files=("a.rsc", "b.rsc")):
    d = tmp_path / "local" / name
    d.mkdir(parents=True)
    out = {}
    for f in files:
        (d / f).write_bytes(f"data of {f}".encode())
        out[f] = d / f
    return d, out


# =========================================================
# Пароль
# =========================================================

def test_password_is_stored_encrypted(cfg_file):
    assert encryption.get_password() == "" and not encryption.is_password_set()
    encryption.set_password(PASSWORD)
    assert encryption.get_password() == PASSWORD
    raw = json.loads(cfg_file.read_text(encoding="utf-8"))[encryption.PASSWORD_KEY]
    assert PASSWORD not in raw


def test_password_validation():
    assert "короче" in encryption.validate_password("short", "short")
    assert "не совпадают" in encryption.validate_password("long-enough-1", "long-enough-2")
    assert encryption.validate_password("long-enough", "long-enough") is None


# =========================================================
# Отказы без пароля / без py7zr
# =========================================================

@needs_rclone
def test_push_refused_without_password(cfg_file, tmp_path, monkeypatch):
    monkeypatch.setattr(encryption, "is_available", lambda: True)
    d, files = _device(tmp_path)
    remote = _remote(tmp_path)
    r = RcloneRemote(remote).push(remote, _ctx(d, files))
    assert not r.ok and "пароль не задан" in r.message
    assert not (tmp_path / "remote").exists()                 # ничего не выгружено открытым


@needs_rclone
def test_push_refused_without_py7zr(cfg_file, tmp_path, monkeypatch):
    monkeypatch.setattr(encryption, "is_available", lambda: False)
    encryption.set_password(PASSWORD)
    d, files = _device(tmp_path)
    remote = _remote(tmp_path)
    r = RcloneRemote(remote).push(remote, _ctx(d, files))
    assert not r.ok and "py7zr" in r.message
    assert not (tmp_path / "remote").exists()


# =========================================================
# Шифрование (py7zr)
# =========================================================

@needs_rclone
def test_regular_push_uploads_archives_and_removes_plain_copies(cfg_file, tmp_path, archiver):
    encryption.set_password(PASSWORD)
    d, files = _device(tmp_path)
    out = tmp_path / "remote" / "Dev"
    out.mkdir(parents=True)
    (out / "a.rsc").write_bytes(b"old plain")                  # прежняя открытая копия
    (out / "other.txt").write_bytes(b"keep")                   # чужой файл — не трогаем

    remote = _remote(tmp_path)
    r = RcloneRemote(remote).push(remote, _ctx(d, files))

    assert r.ok and r.details["uploaded"] == 2 and r.details["encrypted"], r.message
    assert "Зашифровано" in r.message
    assert sorted(p.name for p in out.iterdir()) == ["a.rsc.7z", "b.rsc.7z", "other.txt"]
    assert encryption.read_archive(out / "a.rsc.7z", PASSWORD) == {"a.rsc": b"data of a.rsc"}
    with pytest.raises(Exception):
        encryption.read_archive(out / "a.rsc.7z", "wrong-password")


@needs_rclone
def test_sync_encrypts_once_and_keeps_orphans(cfg_file, tmp_path, monkeypatch, archiver):
    encryption.set_password(PASSWORD)
    d, files = _device(tmp_path, files=("v1_config.rsc", "v2_config.rsc"))
    out = tmp_path / "remote" / "Dev"
    out.mkdir(parents=True)
    (out / "v1_config.rsc").write_bytes(b"plain v1")           # будет зашифрована пара
    (out / "v0_config.rsc").write_bytes(b"pruned")             # версии уже нет локально
    (out / "config.rsc").write_bytes(b"plain latest")
    (out / "config.rsc.7z").write_bytes(b"x")                  # пара есть (обычный бэкап)

    remote = _remote(tmp_path)
    backend = RcloneRemote(remote)
    [r] = backend.push_many(remote, [_ctx(d, files, immutable=True)])

    assert r.ok and r.details["uploaded"] == 2, r.message
    assert r.details["plain_deleted"] == 2                     # v1_config.rsc, config.rsc
    assert r.details["plain_left"] == 1                        # v0 — без пары, оставлен
    names = sorted(p.name for p in out.iterdir())
    assert names == ["config.rsc.7z", "v0_config.rsc", "v1_config.rsc.7z", "v2_config.rsc.7z"]
    assert (tmp_path / "remote" / encryption.README_NAME).is_file()

    # Повторно: уже зашифрованное не шифруется и не перезаливается
    made = []
    real = encryption.make_archive
    monkeypatch.setattr(encryption, "make_archive",
                        lambda *a, **kw: made.append(a[2]) or real(*a, **kw))
    stamp = (out / "v2_config.rsc.7z").stat().st_mtime_ns
    [r2] = backend.push_many(remote, [_ctx(d, files, immutable=True)])
    assert r2.ok and r2.details["uploaded"] == 2 and made == []
    assert (out / "v2_config.rsc.7z").stat().st_mtime_ns == stamp


@needs_rclone
def test_failed_upload_keeps_plain_copy(cfg_file, tmp_path, monkeypatch, archiver):
    encryption.set_password(PASSWORD)
    d, files = _device(tmp_path, files=("a.rsc",))
    calls = []

    def fake_run(args, *a, **kw):
        calls.append(args[0])
        return subprocess.CompletedProcess(args, 1, "", "2026/11/17 20:00:00 NOTICE: Failed to copy: last error was: connection refused\n")

    monkeypatch.setattr(rclone_mod, "run", fake_run)
    remote = _remote(tmp_path)
    r = RcloneRemote(remote).push(remote, _ctx(d, files))
    assert not r.ok
    assert calls == ["copy"]                                   # удаления не было


@needs_rclone
def test_sync_all_devices_encrypted(cfg_file, tmp_path, make_repo, archiver):
    from core.plugin_manager import PluginManager
    from core.storage import StorageManager

    encryption.set_password(PASSWORD)
    global_config.save_remotes([_remote(tmp_path)])
    sm = StorageManager()
    sm.discover()
    pm = PluginManager(storage_manager=sm)
    repo, _ = make_repo(n=3)
    pm._plugins = {"mikrotik": {"func": None, "dir": str(tmp_path), "routers": [
        {"host": "10.0.0.1", "backup_path": str(repo), "storage": {"r": {"enabled": True}}},
    ]}}

    result = pm.sync_git_to_storages("mikrotik", 0)
    assert result["ok"] and result["files"] == 3, result
    archives = sorted((tmp_path / "remote" / repo.name).iterdir())
    assert len(archives) == 3 and all(p.name.endswith("_config.rsc.7z") for p in archives)
    # Коммиты в одну секунду — порядок имён не гарантирован
    contents = sorted(
        data for p in archives for data in encryption.read_archive(p, PASSWORD).values()
    )
    assert contents == [b"version 1\n", b"version 2\n", b"version 3\n"]


@needs_7z
def test_real_7z_archive_roundtrip_and_hidden_names(tmp_path):
    src = tmp_path / "config.rsc"
    src.write_bytes(b"/ip address add address=10.0.0.1/24\n" * 50)
    dst = tmp_path / "config.rsc.7z"

    encryption.make_archive(src, dst, "config.rsc", PASSWORD)

    raw = dst.read_bytes()
    assert raw[:6] == b"7z\xbc\xaf\x27\x1c"                    # формат 7z
    assert b"config.rsc" not in raw and b"10.0.0.1" not in raw  # ни имени, ни данных
    assert encryption.read_archive(dst, PASSWORD) == {"config.rsc": src.read_bytes()}
    with pytest.raises(Exception):
        encryption.read_archive(dst, "неверный-пароль")


# =========================================================
# Веб
# =========================================================

@pytest.fixture
def web(cfg_file, monkeypatch):
    pytest.importorskip("httpx2", reason="нужен httpx2 для TestClient")
    from fastapi.testclient import TestClient
    import web.server as s

    if rclone_mod.provider("local") is None:
        pytest.skip("rclone не найден")
    s.storage_manager.reload_remotes()
    s.app.dependency_overrides[s.require_auth] = lambda: "test"
    yield s, TestClient(s.app)
    s.app.dependency_overrides.clear()
    monkeypatch.undo()
    s.storage_manager.reload_remotes()


def test_web_set_and_change_password(web):
    s, c = web
    page = c.get("/settings?tab=storage").text
    assert "🔒 Шифрование" in page and "Пароль не задан" in page

    r = c.post("/settings/storage/encryption", data={"password": "short", "confirm": "short"},
               follow_redirects=False)
    assert "error=" in r.headers["location"] and not encryption.is_password_set()

    r = c.post("/settings/storage/encryption", data={"password": PASSWORD, "confirm": PASSWORD},
               follow_redirects=False)
    assert "encryption_set" in r.headers["location"] and encryption.get_password() == PASSWORD

    r = c.post("/settings/storage/encryption", data={"password": "Другой-пароль-9", "confirm": "Другой-пароль-9"},
               follow_redirects=False)
    assert "encryption_changed" in r.headers["location"]
    page = c.get(r.headers["location"]).text
    assert "уже выгруженные остаются со старым" in page and "✓ Пароль задан" in page


def _local_form(tmp_path, encrypt):
    data = {"id": "", "type": "local", "name": "Диск", "mode": "all",
            "base_dir": str(tmp_path / "remote").replace("\\", "/")}
    if encrypt:
        data["encrypt"] = "true"
    return data


def test_web_encrypt_needs_password(web, tmp_path, monkeypatch):
    s, c = web
    monkeypatch.setattr(encryption, "is_available", lambda: True)
    r = c.post("/settings/storage/save", data=_local_form(tmp_path, True), follow_redirects=False)
    assert r.status_code == 400 and "задайте пароль" in r.text
    assert global_config.load_remotes() == []


def test_web_encrypt_flag_saved_and_shown(web, tmp_path, monkeypatch):
    s, c = web
    monkeypatch.setattr(encryption, "is_available", lambda: True)
    encryption.set_password(PASSWORD)
    r = c.post("/settings/storage/save", data=_local_form(tmp_path, True), follow_redirects=False)
    assert r.status_code == 303
    remote = global_config.load_remotes()[0]
    assert remote["encrypt"] is True

    assert "🔒 шифрование" in c.get("/settings?tab=storage").text
    edit = c.get(f"/settings/storage/edit?id={remote['id']}").text
    assert 'name="encrypt" value="true"' in edit and "checked" in edit.split('name="encrypt"', 1)[1][:80]

    # Выключение
    form = _local_form(tmp_path, False)
    form["id"] = remote["id"]
    c.post("/settings/storage/save", data=form, follow_redirects=False)
    assert global_config.load_remotes()[0]["encrypt"] is False
