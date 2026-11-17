# =========================================================
# tests/test_batch_push.py
# =========================================================
#
# Выгрузка несколькими файлами за один запуск rclone
# (rclone copy --files-from-raw) и синхронизация всей истории
# устройства одной выгрузкой на хранилище.
#
# Настоящий rclone, хранилище типа local во временной папке.
#
# =========================================================

import subprocess
from datetime import datetime

import pytest

from core import rclone as rclone_mod
from core.storage import StorageManager, global_config
from core.storage.base import PushContext
from core.storage.rclone_remote import RcloneRemote

pytestmark = pytest.mark.skipif(
    not (rclone_mod.is_available() and rclone_mod.provider("local")),
    reason="rclone не найден",
)


def _local_remote(rid, base_dir):
    return {"id": rid, "name": rid, "type": "local", "options": {},
            "base_dir": str(base_dir).replace("\\", "/"), "mode": "all"}


@pytest.fixture
def rclone_calls(monkeypatch):
    calls = []
    real = rclone_mod.run

    def spy(args, *a, **kw):
        calls.append(args[0])
        return real(args, *a, **kw)

    monkeypatch.setattr(rclone_mod, "run", spy)
    return calls


def _ctx(folder, files):
    return PushContext(
        device_id="mikrotik/0", device_host="10.0.0.1", device_type="mikrotik",
        backup_path=folder, files=files, changed=True, run_time=datetime.now(),
    )


def test_files_of_one_folder_go_in_one_rclone_run(tmp_path, rclone_calls):
    local = tmp_path / "Dev"
    local.mkdir()
    files = {}
    for i in range(3):
        p = local / f"v{i}.rsc"
        p.write_bytes(b"x%d" % i)
        files[p.name] = p
    (local / "other.txt").write_bytes(b"not listed")       # не в списке — не выгружается

    remote = _local_remote("r", tmp_path / "remote")
    r = RcloneRemote(remote).push(remote, _ctx(local, files))

    assert r.ok and r.details["uploaded"] == 3, r.message
    assert rclone_calls == ["copy"]
    out = tmp_path / "remote" / "Dev"
    assert sorted(p.name for p in out.iterdir()) == ["v0.rsc", "v1.rsc", "v2.rsc"]


def test_renamed_file_is_uploaded_separately(tmp_path, rclone_calls):
    local = tmp_path / "Dev"
    local.mkdir()
    a = local / "a.rsc"
    a.write_bytes(b"a")
    b = local / "b.rsc"
    b.write_bytes(b"b")
    c = local / "raw.bin"
    c.write_bytes(b"c")

    remote = _local_remote("r", tmp_path / "remote")
    r = RcloneRemote(remote).push(remote, _ctx(local, {"a.rsc": a, "b.rsc": b, "dump.bin": c}))

    assert r.ok and r.details["uploaded"] == 3
    assert sorted(rclone_calls) == ["copy", "copyto"]
    assert (tmp_path / "remote" / "Dev" / "dump.bin").read_bytes() == b"c"


def test_partial_failure_names_failed_file(tmp_path, monkeypatch):
    local = tmp_path / "Dev"
    local.mkdir()
    files = {}
    for n in ("a.rsc", "b.rsc", "c.rsc"):
        (local / n).write_bytes(b"x")
        files[n] = local / n

    def fake_run(args, *a, **kw):
        return subprocess.CompletedProcess(args, 1, "", (
            "2026/11/17 20:00:00 INFO  : a.rsc: Copied (new)\n"
            "2026/11/17 20:00:01 ERROR : b.rsc.27514f6a.partial: Failed to copy: 507 Insufficient Storage\n"
            "2026/11/17 20:00:01 INFO  : c.rsc: Copied (new)\n"
            "2026/11/17 20:00:02 ERROR : Attempt 1/1 failed with 1 errors and: 507\n"
        ))

    monkeypatch.setattr(rclone_mod, "run", fake_run)
    remote = _local_remote("r", tmp_path / "remote")
    r = RcloneRemote(remote).push(remote, _ctx(local, files))

    assert r.ok and r.details == {"uploaded": 2, "failed": 1}
    assert "Загружено 2 из 3" in r.message and "b.rsc: Failed to copy: 507" in r.message


def test_general_failure_counts_all_files(tmp_path, monkeypatch):
    local = tmp_path / "Dev"
    local.mkdir()
    files = {n: local / n for n in ("a.rsc", "b.rsc")}
    for p in files.values():
        p.write_bytes(b"x")

    monkeypatch.setattr(rclone_mod, "run", lambda args, *a, **kw: subprocess.CompletedProcess(
        args, 1, "", "2026/11/17 20:00:00 NOTICE: Failed to copy: last error was: connection refused\n"))
    remote = _local_remote("r", tmp_path / "remote")
    r = RcloneRemote(remote).push(remote, _ctx(local, files))

    assert not r.ok and "2 файлов: connection refused" in r.message


def test_sync_uploads_whole_history_in_one_run(make_repo, tmp_path, monkeypatch, rclone_calls):
    from core.plugin_manager import PluginManager

    cfg = tmp_path / "config.json"
    cfg.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(global_config, "CONFIG_FILE", cfg)
    global_config.save_remotes([_local_remote("r", tmp_path / "remote")])

    repo_path, gs = make_repo(n=4)
    sm = StorageManager()
    sm.discover()
    pm = PluginManager(storage_manager=sm)
    pm._plugins = {"mikrotik": {"func": None, "dir": str(tmp_path), "routers": [
        {"host": "10.0.0.1", "backup_path": str(repo_path), "storage": {"r": {"enabled": True}}},
    ]}}

    result = pm.sync_git_to_storages("mikrotik", 0)

    assert result["ok"] and result["commits"] == 4, result
    assert result["files"] == 4 and result["backends"]["r"] == {"ok": 1, "err": 0, "skip": 0}
    assert rclone_calls == ["copy"]                         # один запуск на всю историю
    uploaded = list((tmp_path / "remote" / repo_path.name).iterdir())
    assert len(uploaded) == 4 and all(p.name.endswith("_config.rsc") for p in uploaded)
    assert "хранилищ без ошибок: 1 из 1" in result["message"]


def _many_devices(make_repo, tmp_path, monkeypatch, base_dir, types=("mikrotik",) * 3):
    from core.plugin_manager import PluginManager

    cfg = tmp_path / "config.json"
    cfg.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(global_config, "CONFIG_FILE", cfg)
    remote = _local_remote("r", tmp_path / "remote")
    remote["base_dir"] = remote["base_dir"] + base_dir
    global_config.save_remotes([remote])

    sm = StorageManager()
    sm.discover()
    pm = PluginManager(storage_manager=sm)
    pm._plugins = {}
    targets, repos = [], []
    for k, device_type in enumerate(types):
        repo_path, _ = make_repo(n=k + 2)                    # 2, 3, 4 версии
        repos.append(repo_path)
        routers = pm._plugins.setdefault(
            device_type, {"func": None, "dir": str(tmp_path), "routers": []},
        )["routers"]
        routers.append({"host": f"10.0.0.{k}", "backup_path": str(repo_path),
                        "storage": {"r": {"enabled": True}}})
        targets.append((device_type, len(routers) - 1))
    return pm, targets, repos


def test_sync_many_devices_in_one_rclone_run(make_repo, tmp_path, monkeypatch, rclone_calls):
    pm, targets, repos = _many_devices(make_repo, tmp_path, monkeypatch, "")
    prepared = []

    results = pm.sync_many_to_storages(targets, on_prepared=lambda t, i: prepared.append(i))

    assert rclone_calls == ["copy"]                          # все устройства — один запуск
    assert prepared == [0, 1, 2]
    for (t, i), repo, n in zip(targets, repos, (2, 3, 4)):
        r = results[f"{t}/{i}"]
        assert r["ok"] and r["files"] == n and r["backends"]["r"]["ok"] == 1, r
        assert len(list((tmp_path / "remote" / repo.name).iterdir())) == n


def test_sync_many_groups_by_substituted_base_dir(make_repo, tmp_path, monkeypatch, rclone_calls):
    pm, targets, repos = _many_devices(
        make_repo, tmp_path, monkeypatch, "/{type}", types=("mikrotik", "cisco", "mikrotik"),
    )
    results = pm.sync_many_to_storages(targets)

    assert rclone_calls == ["copy", "copy"]                  # две родительские папки
    assert all(r["ok"] and r["backends"]["r"]["ok"] == 1 for r in results.values())
    assert (tmp_path / "remote" / "cisco" / repos[1].name).is_dir()
    assert len(list((tmp_path / "remote" / "mikrotik").iterdir())) == 2


def test_sync_many_skips_busy_device(make_repo, tmp_path, monkeypatch, rclone_calls):
    pm, targets, repos = _many_devices(make_repo, tmp_path, monkeypatch, "")
    results = pm.sync_many_to_storages(targets, can_start=lambda t, i: i != 1)

    assert sorted(results) == ["mikrotik/0", "mikrotik/2"]
    assert not (tmp_path / "remote" / repos[1].name).exists()


def test_push_many_reports_failure_per_device(tmp_path, monkeypatch):
    devs = []
    for name in ("A", "B"):
        d = tmp_path / name
        d.mkdir()
        (d / "x.rsc").write_bytes(b"x")
        devs.append(_ctx(d, {"x.rsc": d / "x.rsc"}))

    def fake_run(args, *a, **kw):
        return subprocess.CompletedProcess(args, 1, "", (
            "2026/11/17 20:00:00 INFO  : A/x.rsc: Copied (new)\n"
            "2026/11/17 20:00:01 ERROR : B/x.rsc.0a1b2c3d.partial: Failed to copy: quota\n"
        ))

    monkeypatch.setattr(rclone_mod, "run", fake_run)
    remote = _local_remote("r", tmp_path / "remote")
    a, b = RcloneRemote(remote).push_many(remote, devs)

    assert a.ok and a.details == {"uploaded": 1}
    assert not b.ok and "x.rsc: Failed to copy: quota" in b.message
