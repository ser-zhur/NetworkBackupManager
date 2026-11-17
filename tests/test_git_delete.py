# =========================================================
# tests/test_git_delete.py
# =========================================================
#
# Тесты механизма удаления версий из истории.
#
# =========================================================

from datetime import datetime, timedelta

import pytest


# =========================================================
# MARK / UNMARK
# =========================================================

def test_mark_deleted_returns_count(make_repo):
    _, gs = make_repo(n=5)
    history = gs.get_history()

    added = gs.mark_deleted(
        {history[0]["sha"], history[1]["sha"]}
    )
    assert added == 2

    # Повторная пометка тех же — 0 добавлено
    added = gs.mark_deleted({history[0]["sha"]})
    assert added == 0


def test_marked_shas_hidden_from_history(make_repo):
    _, gs = make_repo(n=5)
    history = gs.get_history()

    gs.mark_deleted({history[1]["sha"]})

    after = gs.get_history()
    assert len(after) == 4
    assert history[1]["sha"] not in {c["sha"] for c in after}


def test_unmark_restores_visibility(make_repo):
    _, gs = make_repo(n=5)
    history = gs.get_history()
    target = history[2]["sha"]

    gs.mark_deleted({target})
    assert len(gs.get_history()) == 4

    gs.unmark_deleted({target})
    assert len(gs.get_history()) == 5


def test_unmark_all_clears_list(make_repo):
    _, gs = make_repo(n=5)
    history = gs.get_history()

    gs.mark_deleted({c["sha"] for c in history[:3]})
    assert len(gs.get_deleted_shas()) == 3

    count = gs.unmark_deleted(None)
    assert count == 3
    assert gs.get_deleted_shas() == set()


def test_deleted_persists_across_instances(make_repo):
    from core.git_storage import GitStorage

    repo_path, gs = make_repo(n=3)
    sha = gs.get_history()[0]["sha"]
    gs.mark_deleted({sha})

    gs2 = GitStorage(repo_path)
    assert sha in gs2.get_deleted_shas()
    assert len(gs2.get_history()) == 2


def test_get_commit_count_respects_deleted(make_repo):
    _, gs = make_repo(n=5)
    assert gs.get_commit_count() == 5

    gs.mark_deleted({gs.get_history()[0]["sha"]})
    assert gs.get_commit_count() == 4


# =========================================================
# SWEEP (REBUILD)
# =========================================================

def test_sweep_noop_when_nothing_marked(make_repo):
    from core.maintenance import sweep_repo

    repo_path, gs = make_repo(n=3)

    ok, msg = sweep_repo(repo_path)
    assert ok is True
    assert "Нечего" in msg
    assert gs.get_commit_count() == 3


def test_sweep_removes_marked_commit(make_repo):
    from core.maintenance import sweep_repo

    repo_path, gs = make_repo(n=5)
    target_sha = gs.get_history()[2]["sha"]

    gs.mark_deleted({target_sha})
    assert gs.get_commit_count() == 4

    ok, msg = sweep_repo(repo_path)
    assert ok is True, msg
    assert "Удалено коммитов: 1" in msg

    assert gs.get_commit_count() == 4
    assert target_sha not in {
        c["sha"] for c in gs.get_history()
    }


def test_sweep_preserves_content(make_repo):
    """
    Ключевая проверка: после rebuild содержимое всех
    оставшихся версий не меняется.
    """
    from core.maintenance import sweep_repo

    repo_path, gs = make_repo(
        n=10,
        content=lambda i: f"version {i}\n",
    )

    # Снимок «до»: message → содержимое
    before = {}
    for c in gs.get_history():
        before[c["message"]] = gs.get_file_at(
            c["sha"], "config.rsc"
        )

    # Удаляем средние версии (v3, v5, v7)
    to_delete = {
        c["sha"] for c in gs.get_history()
        if c["message"] in {"Backup #003", "Backup #005", "Backup #007"}
    }
    gs.mark_deleted(to_delete)

    ok, _ = sweep_repo(repo_path)
    assert ok

    # Снимок «после» (sha изменились, message — нет)
    after = {}
    for c in gs.get_history():
        after[c["message"]] = gs.get_file_at(
            c["sha"], "config.rsc"
        )

    expected_kept = {
        f"Backup #{i:03d}"
        for i in range(1, 11)
        if i not in (3, 5, 7)
    }
    assert set(after.keys()) == expected_kept

    for msg in expected_kept:
        assert after[msg] == before[msg], (
            f"Содержимое {msg} разошлось после rebuild"
        )


def test_sweep_preserves_notes(make_repo):
    """
    Пересборка меняет sha коммитов выше удалённого.
    Заметки должны переехать на новые sha, а не потеряться.
    """
    from core.maintenance import sweep_repo

    repo_path, gs = make_repo(n=5)
    history = gs.get_history()

    # #005 (новейший) и #001 (ниже удаляемого) — с заметками
    gs.set_note(history[0]["sha"], "важная версия")
    gs.set_note(history[4]["sha"], "самая первая")

    gs.mark_deleted({history[2]["sha"]})  # удаляем #003
    ok, msg = sweep_repo(repo_path)
    assert ok is True, msg

    notes = gs.get_notes()
    by_message = {
        c["message"]: notes.get(c["sha"], "")
        for c in gs.get_history()
    }

    assert by_message["Backup #005"] == "важная версия"
    assert by_message["Backup #001"] == "самая первая"
    # Ни одной «осиротевшей» заметки
    live = {c["sha"] for c in gs.get_history()}
    assert set(notes) <= live


def test_sweep_drops_note_of_deleted_commit(make_repo):
    from core.maintenance import sweep_repo

    repo_path, gs = make_repo(n=4)
    target = gs.get_history()[1]["sha"]

    gs.set_note(target, "удаляется вместе с версией")
    gs.mark_deleted({target})
    ok, msg = sweep_repo(repo_path)
    assert ok is True, msg

    assert target not in gs.get_notes()
    assert gs.get_notes() == {}


def test_retention_after_sweep_still_protects_notes(make_repo):
    """
    Сценарий автоочистки: retention → sweep → retention.
    Версия с заметкой не должна быть помечена во втором
    прогоне (раньше заметка терялась при sweep).
    """
    from core.maintenance import apply_retention, sweep_repo

    repo_path, gs = make_repo(n=6)
    history = gs.get_history()
    noted_msg = history[1]["message"]
    gs.set_note(history[1]["sha"], "не удалять")

    rules = {"keep_last": 0, "max_age_days": 1, "keep_with_notes": True}
    future = datetime.now() + timedelta(days=30)

    # Удаляем самую старую версию — sha выше неё меняются
    gs.mark_deleted({history[-1]["sha"]})
    ok, msg = sweep_repo(repo_path)
    assert ok is True, msg

    marked = apply_retention(repo_path, rules, now=future)

    noted_sha = next(
        c["sha"] for c in gs.get_history(include_marked=True)
        if c["message"] == noted_msg
    )
    assert noted_sha not in marked


def test_sweep_deletes_first_commit(make_repo):
    """Удаление самого старого коммита — новый корень."""
    from core.maintenance import sweep_repo

    repo_path, gs = make_repo(n=3)
    oldest = gs.get_history()[-1]["sha"]

    gs.mark_deleted({oldest})
    ok, _ = sweep_repo(repo_path)
    assert ok

    remaining = gs.get_history()
    assert len(remaining) == 2
    assert all(c["sha"] != oldest for c in remaining)


def test_sweep_deletes_head(make_repo):
    """Удаление HEAD — цепочка укорачивается, история цела."""
    from core.maintenance import sweep_repo

    repo_path, gs = make_repo(n=3)
    head = gs.get_history()[0]["sha"]

    gs.mark_deleted({head})
    ok, _ = sweep_repo(repo_path)
    assert ok

    remaining = gs.get_history()
    assert len(remaining) == 2
    assert remaining[0]["message"] == "Backup #002"


def test_sweep_cannot_delete_all(make_repo):
    from core.maintenance import sweep_repo

    repo_path, gs = make_repo(n=3)
    all_shas = {c["sha"] for c in gs.get_history()}

    gs.mark_deleted(all_shas)
    ok, msg = sweep_repo(repo_path)

    assert ok is False
    assert "всю историю" in msg

    # Репа не испорчена — история на месте
    gs2 = type(gs)(repo_path)
    assert len(gs2.get_deleted_shas()) == 3


# =========================================================
# RETENTION
# =========================================================

def test_retention_keeps_last_n(make_repo):
    """
    keep_last=N защищает последние N версий,
    даже если они старше max_age_days.
    """
    from core.maintenance import apply_retention

    repo_path, gs = make_repo(n=10)

    # Коммиты созданы только что. Сдвигаем «сейчас»
    # на 30 дней вперёд — тогда все коммиты старше 1 дня.
    future = datetime.now() + timedelta(days=30)

    config = {
        "keep_last": 4,
        "max_age_days": 1,
        "keep_with_notes": False,
    }

    marked = apply_retention(repo_path, config, now=future)
    assert len(marked) == 6   # 10 - 4

    remaining = gs.get_history()
    assert len(remaining) == 4
    # Защищены именно 4 самых свежих
    assert {c["message"] for c in remaining} == {
        "Backup #007", "Backup #008",
        "Backup #009", "Backup #010",
    }


def test_retention_keeps_notes(make_repo):
    """Версии с заметками не помечаются."""
    from core.maintenance import apply_retention

    repo_path, gs = make_repo(
        n=10,
        with_notes={3: "important", 6: "another"},
    )

    future = datetime.now() + timedelta(days=30)

    config = {
        "keep_last": 1,
        "max_age_days": 1,
        "keep_with_notes": True,
    }

    marked = apply_retention(repo_path, config, now=future)

    # Осталось: последний (keep_last=1) + два с заметками
    # Значит помечено 10 - 1 - 2 = 7
    assert len(marked) == 7


def test_retention_ignores_recent(make_repo):
    """
    Свежие версии не помечаются, даже без keep_last.

    Здесь now НЕ сдвигаем: коммиты только что созданы,
    max_age_days=365 — ни один не старше cutoff.
    """
    from core.maintenance import apply_retention

    repo_path, gs = make_repo(n=5)

    config = {
        "keep_last": 0,
        "max_age_days": 365,
        "keep_with_notes": False,
    }

    marked = apply_retention(repo_path, config)
    assert marked == []


def test_retention_empty_repo(tmp_path):
    from core.maintenance import apply_retention
    from core.git_storage import GitStorage

    empty = tmp_path / "empty"
    gs = GitStorage(empty)

    marked = apply_retention(empty, {"keep_last": 5})
    assert marked == []