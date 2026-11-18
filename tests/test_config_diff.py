# =========================================================
# tests/test_config_diff.py — событие «конфигурация изменилась»
# =========================================================

import json
from types import SimpleNamespace

import pytest

from core import config_diff, context, events
from core.plugin_manager import PluginManager

git_storage = pytest.importorskip("core.git_storage")
if not git_storage.is_available():
    pytest.skip("dulwich не установлен", allow_module_level=True)


def _strip_date(name, data):
    """Как у MikroTik: первая строка — дата выгрузки."""
    return b"\n".join(data.split(b"\n")[1:]) if name.endswith(".rsc") else data


def _commit(repo, text, stamp):
    work = repo / "work"
    work.mkdir(parents=True, exist_ok=True)
    f = work / "config.rsc"
    f.write_text(f"# {stamp}\n{text}", encoding="utf-8")
    gs = git_storage.GitStorage(repo)
    ok, created, msg = gs.commit_if_changed(
        files={"config.rsc": f}, text_files=["config.rsc"],
        message=f"Backup {stamp}", normalize=_strip_date,
    )
    assert ok, msg
    return created


@pytest.fixture
def journal(tmp_path, monkeypatch):
    monkeypatch.setattr(context, "DATA_DIR", tmp_path / "data")
    events.reset()
    yield tmp_path / "data" / "events.jsonl"
    events.reset()


def test_first_version_is_not_a_change(tmp_path):
    repo = tmp_path / "r"
    assert _commit(repo, "a\nb\n", "t1")
    assert config_diff.summarize_change(repo, _strip_date) is None


def test_change_summary(tmp_path):
    repo = tmp_path / "r"
    _commit(repo, "/ip address add address=10.0.0.1\n/user set admin\n", "t1")
    assert _commit(repo, "/ip address add address=10.0.0.2\n/user set admin\n/x\n", "t2")

    ch = config_diff.summarize_change(repo, _strip_date)

    assert ch["files"] == ["config.rsc"]
    assert (ch["added"], ch["removed"]) == (2, 1)
    assert "+/ip address add address=10.0.0.2" in ch["diff"]
    assert "-/ip address add address=10.0.0.1" in ch["diff"]
    assert "# t2" not in ch["diff"]                      # дата нормализована


def test_diff_is_truncated(tmp_path):
    repo = tmp_path / "r"
    _commit(repo, "", "t1")
    _commit(repo, "".join(f"line {i}\n" for i in range(2000)), "t2")
    ch = config_diff.summarize_change(repo, _strip_date, max_chars=500)
    assert ch["added"] == 2000 and len(ch["diff"]) < 600 and "сокращены" in ch["diff"]


def test_event_keeps_diff_private(tmp_path, journal):
    repo = tmp_path / "r"
    _commit(repo, "secret-hash=AAA\n", "t1")
    _commit(repo, "secret-hash=BBB\n", "t2")

    got = []
    events.subscribe(got.append)
    try:
        ctx = SimpleNamespace(backup_path=repo, normalize=_strip_date, device_id="d1")
        PluginManager._emit_config_changed("mikrotik", {"name": "R1", "host": "10.0.0.1"}, ctx)
    finally:
        events.unsubscribe(got.append)

    (ev,) = got
    assert ev["kind"] == events.CONFIG_CHANGED and ev["device_id"] == "d1"
    assert "+1 −1" in ev["message"] and ev["data"]["files"] == ["config.rsc"]
    assert "secret-hash=BBB" in ev["private"]["diff"]

    stored = journal.read_text(encoding="utf-8")
    assert "secret-hash" not in stored and "private" not in stored
    assert "private" not in events.recent()[0]
    assert json.loads(stored.splitlines()[-1])["data"]["added"] == 1


def test_no_event_for_first_version(tmp_path, journal):
    repo = tmp_path / "r"
    _commit(repo, "x\n", "t1")
    ctx = SimpleNamespace(backup_path=repo, normalize=_strip_date, device_id="d1")
    PluginManager._emit_config_changed("mikrotik", {"name": "R1"}, ctx)
    assert events.recent() == []
