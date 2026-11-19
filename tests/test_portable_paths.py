# =========================================================
# tests/test_portable_paths.py — папку данных можно перенести
# =========================================================
#
# backup_path внутри папки данных хранится относительным;
# абсолютный путь прежней установки (другая папка, Windows →
# Docker) находит то же устройство в backups/ текущей.
#
# =========================================================

import json
import os

import pytest

from core import context, devices


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "backups").mkdir(parents=True)
    (home / "data").mkdir()
    monkeypatch.setattr(context, "HOME_DIR", home)
    monkeypatch.setattr(context, "BACKUPS_DIR", home / "backups")
    monkeypatch.setattr(context, "DATA_DIR", home / "data")
    monkeypatch.setattr(context, "DEVICES_FILE", home / "data" / "devices.json")
    return home.resolve()


def test_stored_relative_inside_home(home, tmp_path):
    assert devices.stored_backup_path(home / "backups" / "R1") == "backups/R1"
    assert devices.stored_backup_path("backups/R1") == "backups/R1"
    outside = (tmp_path / "elsewhere" / "R1").resolve()
    assert devices.stored_backup_path(outside) == str(outside)


def test_locate_relative(home):
    assert devices.locate_backup_path("backups/R1") == home / "backups" / "R1"


def test_locate_moved_installation(home, tmp_path):
    old = tmp_path / "old_install" / "backups" / "R1"
    # Папки нет ни там, ни здесь — путь не трогаем
    assert devices.locate_backup_path(str(old)) == old.resolve()
    # Перенесли папку данных — находим в backups/ текущей
    (home / "backups" / "R1").mkdir()
    assert devices.locate_backup_path(str(old)) == home / "backups" / "R1"
    # Существующий абсолютный путь — как есть
    old.mkdir(parents=True)
    assert devices.locate_backup_path(str(old)) == old.resolve()


@pytest.mark.skipif(os.name == "nt", reason="путь Windows на Linux")
def test_locate_windows_path_on_linux(home):
    assert devices.locate_backup_path(r"D:\MySoft\NBM\Backups\R1") == home / "backups" / "R1"
    assert devices.locate_backup_path("C:/NBM/backups/site/R2") == home / "backups" / "site" / "R2"
    assert devices.locate_backup_path(r"E:\archive\R3") == home / "backups" / "R3"


def test_devices_json_round_trip(home, tmp_path):
    from core.plugin_manager import PluginManager

    old = tmp_path / "old_install" / "backups" / "R1"
    (home / "backups" / "R1").mkdir()
    context.DEVICES_FILE.write_text(json.dumps({"devices": [
        {"id": "0123456789ab", "type": "pfsense", "host": "192.0.2.1", "backup_path": str(old)},
        {"id": "0123456789ac", "type": "pfsense", "host": "192.0.2.2", "backup_path": "backups/R2"},
    ]}), encoding="utf-8")

    pm = PluginManager()
    pm.discover_and_load_plugins()
    paths = [r["backup_path"] for r in pm._plugins["pfsense"]["routers"]]
    assert paths == [str(home / "backups" / "R1"), str(home / "backups" / "R2")]

    stored = json.loads(context.DEVICES_FILE.read_text(encoding="utf-8"))["devices"]
    assert [d["backup_path"] for d in stored] == ["backups/R1", "backups/R2"]
