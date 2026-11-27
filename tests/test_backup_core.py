# =========================================================
# tests/test_backup_core.py — ядро бэкапа
# =========================================================
#
# git-история (ветка, сравнение файлов), выгрузка во
# внешние хранилища после сбоев, автоочистка, имена файлов.
# Хранилища — заглушки, сеть и rclone не используются.
#
# =========================================================

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

pytest.importorskip("dulwich")

from dulwich.repo import Repo  # noqa: E402

import core.git_storage as git_mod  # noqa: E402
import core.plugin_manager as pm_mod  # noqa: E402
from core.git_storage import GitStorage  # noqa: E402
from core.plugin_manager import PluginManager  # noqa: E402
from core.storage import global_config  # noqa: E402
from core.storage.base import PushContext, PushResult, StorageBackend  # noqa: E402
from storages.git import GitStorageBackend  # noqa: E402


def _commit(gs, folder, files: dict):
    paths = {}
    for name, data in files.items():
        (folder / name).write_bytes(data)
        paths[name] = folder / name
    return gs.commit_if_changed(paths, list(paths), "msg")


def _push_git(folder, files: dict):
    paths = {}
    for name, data in files.items():
        (folder / name).write_bytes(data)
        paths[name] = folder / name
    ctx = PushContext(
        device_id="d", device_host="h", device_type="ssh",
        backup_path=folder, files=paths, changed=None,
        run_time=datetime.now(),
    )
    return GitStorageBackend().push({}, ctx)


# =========================================================
# 1. Ветка по умолчанию
# =========================================================

def test_new_repo_uses_master_even_if_gitconfig_says_main(tmp_path, monkeypatch):
    real_init = git_mod.porcelain.init

    def init_main(path, **kw):                    # init.defaultBranch = main
        repo = real_init(path, **kw)
        repo.refs.set_symbolic_ref(b"HEAD", b"refs/heads/main")
        return repo

    monkeypatch.setattr(git_mod.porcelain, "init", init_main)
    gs = GitStorage(tmp_path)
    for _ in range(3):
        _commit(gs, tmp_path, {"config.rsc": b"same"})

    assert gs.get_commit_count() == 1
    assert len(gs.get_history()) == 1


def test_broken_repo_with_head_on_missing_branch_is_repaired(tmp_path):
    gs = GitStorage(tmp_path)
    _commit(gs, tmp_path, {"config.rsc": b"v1"})
    _commit(gs, tmp_path, {"config.rsc": b"v2"})
    repo = Repo(str(tmp_path))
    repo.refs.set_symbolic_ref(b"HEAD", b"refs/heads/main")   # как в 2.9.0 с main
    repo.close()

    assert gs.get_commit_count() == 2                          # история снова видна
    ok, created, _ = _commit(gs, tmp_path, {"config.rsc": b"v2"})
    assert ok and not created                                  # не «всегда новая»
    _commit(gs, tmp_path, {"config.rsc": b"v3"})
    assert gs.get_commit_count() == 3
    repo = Repo(str(tmp_path))
    assert repo.refs.read_ref(b"HEAD") == b"ref: refs/heads/master"
    repo.close()


# =========================================================
# 2. Сравниваются файлы с любым расширением
# =========================================================

@pytest.mark.parametrize("name", ["config.set", "config.commands", "startup-config"])
def test_unknown_extension_is_compared(tmp_path, name):
    verdicts = [_push_git(tmp_path, {name: b"set system host-name r1\n"}).details["changed"]
                for _ in range(3)]
    assert verdicts == [True, False, False]
    assert _push_git(tmp_path, {name: b"set system host-name r2\n"}).details["changed"] is True
    assert GitStorage(tmp_path).get_commit_count() == 2


def test_volatile_backup_does_not_decide(tmp_path):
    first = _push_git(tmp_path, {"config.rsc": b"/ip\n", "config.backup": b"\x00t1"})
    again = _push_git(tmp_path, {"config.rsc": b"/ip\n", "config.backup": b"\x00t2"})
    assert first.details["changed"] is True and again.details["changed"] is False


def test_file_set_change_is_a_change(tmp_path):
    _push_git(tmp_path, {"a.txt": b"1", "b.txt": b"2"})
    (tmp_path / "b.txt").unlink()
    assert _push_git(tmp_path, {"a.txt": b"1"}).details["changed"] is True


def test_git_failure_means_unknown_not_unchanged(tmp_path, monkeypatch):
    monkeypatch.setattr(GitStorage, "commit_if_changed",
                        lambda self, *a, **kw: (False, False, "Ошибка git: диск"))
    r = _push_git(tmp_path, {"config.rsc": b"x"})
    assert not r.ok and r.details["changed"] is None


# =========================================================
# 3–4. Внешние хранилища после сбоя
# =========================================================

class _Remote(StorageBackend):
    id = "cloud"
    name = "cloud"
    fields = []

    def __init__(self):
        self.results = []
        self.calls = 0

    def test(self, config):
        return PushResult.success()

    def push(self, config, ctx):
        self.calls += 1
        return self.results.pop(0)


class _Git(StorageBackend):
    id = "git"
    name = "git"
    is_authority = True
    fields = []

    def __init__(self):
        self.result = None

    def test(self, config):
        return PushResult.success()

    def push(self, config, ctx):
        return self.result


class _SM:
    def __init__(self, *backends):
        self.backends = backends

    def enabled_for(self, router):
        return list(self.backends)


@pytest.fixture
def pushing(tmp_path, monkeypatch):
    monkeypatch.setattr(pm_mod, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(global_config, "load_global", lambda: {})
    monkeypatch.setattr(global_config, "is_configured", lambda b, s: True)
    monkeypatch.setattr(global_config, "effective_config",
                        lambda r, b, s: {"mode": "changes_only"})
    git, remote = _Git(), _Remote()
    pm = PluginManager(storage_manager=_SM(git, remote))
    router = {"id": "0123456789ab", "host": "192.0.2.1"}
    f = tmp_path / "config.rsc"
    f.write_bytes(b"cfg")

    def run(changed, *remote_results, git_ok=True):
        git.result = PushResult(git_ok, "", {"changed": changed})
        remote.results = list(remote_results)
        pm._push_to_storages("fake", 0, "192.0.2.1", router, tmp_path, [str(f)])
        state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
        return state[router["id"]]

    run.remote = remote
    return run


def test_failed_storage_is_retried_without_changes(pushing):
    entry = pushing(True, PushResult.failure("507"))
    assert entry["storage_failing"] == ["cloud"]

    entry = pushing(False, PushResult.success("ok"))     # изменений нет, но выгружаем
    assert pushing.remote.calls == 2
    assert entry["backends"]["cloud"]["ok"] and "storage_failing" not in entry

    pushing(False)                                       # теперь можно пропустить
    assert pushing.remote.calls == 2


def test_git_failure_does_not_skip_storages(pushing):
    entry = pushing(None, PushResult.success("ok"), git_ok=False)
    assert pushing.remote.calls == 1 and entry["backends"]["cloud"]["ok"]


def test_skip_does_not_hide_previous_storage_error(tmp_path, monkeypatch):
    monkeypatch.setattr(pm_mod, "STATE_FILE", tmp_path / "state.json")
    save = PluginManager._save_backend_state
    save("dev", {"cloud": PushResult.failure("507")})
    save("dev", {"cloud": PushResult.skipped("Изменений нет")})
    entry = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))["dev"]
    assert entry["backends"]["cloud"]["ok"] is False
    assert entry["storage_failing"] == ["cloud"]


# =========================================================
# 6. Автоочистка: текущая версия защищена
# =========================================================

def test_retention_keep_last_zero_keeps_current(tmp_path):
    from core.maintenance import apply_retention

    gs = GitStorage(tmp_path)
    for i in range(3):
        _commit(gs, tmp_path, {"config.rsc": f"v{i}".encode()})
    head = gs.get_history(limit=1)[0]["sha"]

    marked = apply_retention(tmp_path, {"keep_last": 0, "max_age_days": 1},
                             now=datetime.now() + timedelta(days=30))
    assert len(marked) == 2 and head not in marked
    ok, _ = gs.sweep()
    assert ok and gs.get_commit_count() == 1


# =========================================================
# 7–8. Файлы, которые вернул плагин
# =========================================================

def test_same_extension_files_do_not_collide(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    paths = [tmp_path / "running.xml", tmp_path / "a" / "x.xml", tmp_path / "b" / "x.xml"]
    for p in paths:
        p.write_bytes(p.parent.name.encode())
    files = PluginManager._normalize_created_files([str(p) for p in paths])
    assert files == {"config.xml": paths[0], "x.xml": paths[1], "x-2.xml": paths[2]}


def test_missing_files_fail_the_backup(tmp_path, monkeypatch):
    monkeypatch.setattr(pm_mod, "STATE_FILE", tmp_path / "state.json")
    pm = PluginManager()
    router = {"id": "0123456789ab", "host": "192.0.2.1", "backup_path": str(tmp_path)}
    pm._plugins["fake"] = {"func": lambda r: [str(Path(tmp_path) / "nope.rsc")],
                           "routers": [router]}
    out = pm.backup_device("fake", router, 0)
    assert out["ok"] is False and "не найдены" in out["error"]
