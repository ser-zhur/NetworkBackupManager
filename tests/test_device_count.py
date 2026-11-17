# =========================================================
# tests/test_device_count.py
# =========================================================
#
# Число версий в карточке устройства — точное (раньше
# «застревало» на 200) и совпадает со списком устройств.
#
# =========================================================

import pytest

s = pytest.importorskip("web.server")


@pytest.fixture
def device_with_history(make_repo, monkeypatch):
    if s.plugin_manager is None:
        pytest.skip("PluginManager не загружен")

    repo_path, gs = make_repo(n=205)
    history = gs.get_history(limit=10)
    gs.mark_deleted({history[5]["sha"], history[6]["sha"]})

    router = {"host": "10.0.0.9", "backup_path": str(repo_path)}
    monkeypatch.setattr(s.plugin_manager, "_plugins", {
        "mikrotik": {"func": lambda r: [], "routers": [router], "dir": str(repo_path)},
    })
    return router


def test_card_shows_exact_count_above_200(device_with_history):
    d = s._build_device_payload("mikrotik", 0, device_with_history)

    assert d["backup_count"] == 203          # 205 − 2 помеченных
    assert d["git_total"] == 203
    assert len(d["git_recent"]) == 3
    assert d["git_recent"][0]["message"] == "Backup #205"


def test_card_matches_device_list(device_with_history):
    card = s._build_device_payload("mikrotik", 0, device_with_history)
    row = s._build_device_summary("mikrotik", 0, device_with_history, {})

    assert row["backup_count"] == card["backup_count"]
