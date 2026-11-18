# =========================================================
# tests/test_devices.py — data/devices.json, ключи, миграция
# =========================================================

import json

import pytest

from core import context, devices


@pytest.fixture
def home(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(context, "DATA_DIR", data)
    monkeypatch.setattr(context, "DEVICES_FILE", data / "devices.json")
    monkeypatch.setattr(context, "STATE_FILE", data / "state.json")
    monkeypatch.setattr(context, "KEYS_DIR", data / "keys")
    return tmp_path


def _legacy(plugins, name, routers, files=()):
    d = plugins / name
    d.mkdir(parents=True)
    (d / "routers.json").write_text(json.dumps(routers, ensure_ascii=False), encoding="utf-8")
    for fname in files:
        (d / fname).write_text(f"KEY {name}/{fname}", encoding="utf-8")
    return d


def test_migration_moves_devices_keys_and_state(home):
    plugins = home / "plugins"
    _legacy(plugins, "Mikrotik", [
        {"name": "R1", "host": "1.1.1.1", "private_key": "id_rsa.key"},
        {"name": "R2", "host": "1.1.1.2"},
    ], files=["id_rsa.key", "plugin.py"])
    _legacy(plugins, "D-Link_DGS-1510", [
        {"name": "S1", "host": "2.2.2.2", "ssh_key": "dlink.key"},
    ], files=["dlink.key"])
    context.STATE_FILE.write_text(json.dumps({
        "mikrotik/1": {"last": "ok"}, "d-link_dgs-1510/0": {"last": "fail"},
        "other": 1,
    }), encoding="utf-8")

    summary = devices.migrate_from_plugins(plugins)

    devs = devices.load_devices()
    assert [(d["type"], d["name"]) for d in devs] == [
        ("d-link_dgs-1510", "S1"), ("mikrotik", "R1"), ("mikrotik", "R2"),
    ]
    ids = {d["name"]: d["id"] for d in devs}
    assert all(devices.is_valid_id(i) for i in ids.values()) and len(set(ids.values())) == 3

    state = json.loads(context.STATE_FILE.read_text(encoding="utf-8"))
    assert state == {ids["R2"]: {"last": "ok"}, ids["S1"]: {"last": "fail"}, "other": 1}

    keys = context.KEYS_DIR
    assert (keys / "mikrotik" / "id_rsa.key").read_text(encoding="utf-8") == "KEY Mikrotik/id_rsa.key"
    assert (keys / "d-link_dgs-1510" / "dlink.key").is_file()
    assert not (keys / "mikrotik" / "plugin.py").exists()
    assert summary["devices"] == 3 and summary["state_entries"] == 2

    # Старые файлы на месте, копия сделана
    assert (plugins / "Mikrotik" / "routers.json").is_file()
    backup = list(context.DATA_DIR.glob("migration-backup-*"))
    assert len(backup) == 1 and (backup[0] / "Mikrotik.routers.json").is_file()

    # Повторно — ничего
    assert devices.migrate_from_plugins(plugins) is None
    assert devices.load_devices() == devs


def test_unreadable_key_does_not_stop_migration(home, monkeypatch):
    plugins = home / "plugins"
    _legacy(plugins, "Pfsense", [{"name": "FW", "ssh_key": "id_rsa.key"}], files=["id_rsa.key"])

    real_copy = devices.shutil.copy2

    def deny(src, dst, *a, **k):
        if str(src).endswith(".key"):
            raise PermissionError(13, "Permission denied")
        return real_copy(src, dst, *a, **k)

    monkeypatch.setattr(devices.shutil, "copy2", deny)
    summary = devices.migrate_from_plugins(plugins)

    assert [d["name"] for d in devices.load_devices()] == ["FW"]
    assert summary["keys_failed"] == ["pfsense/id_rsa.key"]


def test_migration_without_legacy_files_does_nothing(home):
    assert devices.migrate_from_plugins(home / "plugins") is None
    assert not context.DEVICES_FILE.exists()


def test_prepare_key_path(home):
    key = context.KEYS_DIR / "mikrotik" / "id_rsa.key"
    key.parent.mkdir(parents=True)
    key.write_text("k")

    r = {"private_key": "id_rsa.key"}
    devices.prepare_key_path("mikrotik", r)
    assert r["ssh_key"] == str(key.resolve())

    r = {}                                     # по умолчанию id_rsa.key
    devices.prepare_key_path("Mikrotik", r)
    assert r["ssh_key"] == str(key.resolve())

    r = {"ssh_key": "missing.key"}             # нет в data/keys — как было
    devices.prepare_key_path("mikrotik", r)
    assert r == {"ssh_key": "missing.key"}

    r = {"ssh_key": "../../secret.key"}
    devices.prepare_key_path("mikrotik", r)
    assert r == {"ssh_key": "../../secret.key"}


def test_ensure_ids_fixes_missing_and_duplicates():
    devs = [{"id": "aaaaaaaaaaaa"}, {"id": "aaaaaaaaaaaa"}, {}, {"id": "bad"}]
    assert devices.ensure_ids(devs) is True
    ids = [d["id"] for d in devs]
    assert ids[0] == "aaaaaaaaaaaa" and len(set(ids)) == 4
    assert all(devices.is_valid_id(i) for i in ids)
    assert devices.ensure_ids(devs) is False
