# =========================================================
# tests/test_smoke.py
# =========================================================


import pytest


# =========================================================
# INIT / FIRST COMMIT
# =========================================================

def test_first_commit_creates_repository(make_repo):
    repo_path, gs = make_repo(n=1)

    assert gs.is_initialized()
    assert (repo_path / ".git").is_dir()

    history = gs.get_history()
    assert len(history) == 1
    assert history[0]["message"] == "Backup #001"
    assert history[0]["author"].startswith("NetworkBackupManager")


def test_empty_repo_has_empty_history(tmp_path):
    from core.git_storage import GitStorage

    gs = GitStorage(tmp_path / "empty")

    assert not gs.is_initialized()
    assert gs.get_history() == []
    assert gs.list_files() == []
    assert gs.get_commit_count() == 0
    assert gs.get_last_commit() is None


# =========================================================
# MULTIPLE COMMITS / ORDER
# =========================================================

def test_multiple_commits_order_newest_first(make_repo):
    _, gs = make_repo(
        n=5,
        messages=["v1", "v2", "v3", "v4", "v5"],
    )

    history = gs.get_history()

    assert len(history) == 5
    assert history[0]["message"] == "v5"
    assert history[-1]["message"] == "v1"
    assert gs.get_commit_count() == 5


def test_commit_author_is_fixed(make_repo):
    _, gs = make_repo(n=3)

    for c in gs.get_history():
        assert c["author"] == "NetworkBackupManager <nbm@local>"


# =========================================================
# CHANGE DETECTION
# =========================================================

def test_identical_content_skips_commit(make_repo):
    repo_path, gs = make_repo(n=1)
    file_path = repo_path / "config.rsc"

    original_count = gs.get_commit_count()

    ok, created, _ = gs.commit_if_changed(
        files={"config.rsc": file_path},
        text_files=["config.rsc"],
        message="Backup duplicate",
        normalize=lambda name, data: data,
    )

    assert ok is True
    assert created is False
    assert gs.get_commit_count() == original_count


def test_changed_content_creates_commit(make_repo):
    repo_path, gs = make_repo(n=1)
    file_path = repo_path / "config.rsc"

    # write_bytes, не write_text
    file_path.write_bytes(b"version 999\n")

    ok, created, _ = gs.commit_if_changed(
        files={"config.rsc": file_path},
        text_files=["config.rsc"],
        message="Changed",
        normalize=lambda name, data: data,
    )

    assert ok is True
    assert created is True
    assert gs.get_commit_count() == 2
    assert gs.get_history()[0]["message"] == "Changed"


def test_normalize_hides_timestamp_changes(make_repo):
    repo_path, gs = make_repo(
        n=1,
        content=lambda i: "# generated at ts=1000\ndata\n",
    )
    file_path = repo_path / "config.rsc"

    # write_bytes, не write_text
    file_path.write_bytes(b"# generated at ts=9999\ndata\n")

    def strip_first_line(name, data):
        lines = data.splitlines(keepends=True)
        return b"".join(lines[1:]) if lines else data

    ok, created, _ = gs.commit_if_changed(
        files={"config.rsc": file_path},
        text_files=["config.rsc"],
        message="Should be skipped",
        normalize=strip_first_line,
    )

    assert ok is True
    assert created is False
    assert gs.get_commit_count() == 1


# =========================================================
# READ FILE AT COMMIT
# =========================================================

def test_get_file_at_returns_exact_bytes(make_repo):
    repo_path, gs = make_repo(
        n=3,
        content=lambda i: f"version {i}\n",
    )

    history = gs.get_history()

    data = gs.get_file_at(history[0]["sha"], "config.rsc")
    assert data == b"version 3\n"

    data = gs.get_file_at(history[-1]["sha"], "config.rsc")
    assert data == b"version 1\n"


def test_get_file_at_missing_returns_none(make_repo):
    _, gs = make_repo(n=1)
    sha = gs.get_history()[0]["sha"]

    assert gs.get_file_at(sha, "nonexistent.rsc") is None


def test_get_file_at_bad_sha_returns_none(make_repo):
    _, gs = make_repo(n=1)

    assert gs.get_file_at("0" * 40, "config.rsc") is None


# =========================================================
# LIST FILES
# =========================================================

def test_list_files_contains_committed_files(make_repo):
    _, gs = make_repo(n=1)

    files = gs.list_files()
    assert "config.rsc" in files


# =========================================================
# NOTES
# =========================================================

def test_notes_round_trip(make_repo):
    _, gs = make_repo(n=3)
    sha = gs.get_history()[1]["sha"]

    assert gs.get_note(sha) == ""

    gs.set_note(sha, "Pre-update to 7.16")
    assert gs.get_note(sha) == "Pre-update to 7.16"

    notes = gs.get_notes()
    assert sha in notes
    assert notes[sha] == "Pre-update to 7.16"

    gs.set_note(sha, "")
    assert gs.get_note(sha) == ""
    assert sha not in gs.get_notes()


def test_note_is_persisted_across_instances(make_repo):
    from core.git_storage import GitStorage

    repo_path, gs = make_repo(n=2)
    sha = gs.get_history()[0]["sha"]
    gs.set_note(sha, "important")

    gs2 = GitStorage(repo_path)
    assert gs2.get_note(sha) == "important"


# =========================================================
# DIFF
# =========================================================

def test_get_diff_detects_textual_changes(make_repo):
    repo_path, gs = make_repo(
        n=2,
        content=lambda i: f"line A\nline B\n{'old' if i == 1 else 'new'}\n",
    )

    history = gs.get_history()
    newer = history[0]["sha"]
    older = history[1]["sha"]

    diffs = gs.get_diff(older, newer, ["config.rsc"])

    assert "config.rsc" in diffs
    text = diffs["config.rsc"]

    assert "-old" in text
    assert "+new" in text


def test_get_diff_binary_file_is_summarized(make_repo):
    """
    Файл с нулевыми байтами не разворачивается, а суммируется.

    Важно: commit_if_changed решает «изменилось ли», глядя
    только на text_files. Чтобы коммит с dump.bin создался,
    на том же шаге меняем и config.rsc — иначе dulwich
    пропустит коммит, и бинарный файл никогда не попадёт
    в историю.
    """
    repo_path, gs = make_repo(
        n=1,
        content=lambda i: "text\n",
    )

    # Меняем текстовый файл + добавляем бинарный
    (repo_path / "config.rsc").write_bytes(b"text changed\n")
    (repo_path / "dump.bin").write_bytes(b"\x00\x01\x02\x03")

    ok, created, _ = gs.commit_if_changed(
        files={
            "config.rsc": repo_path / "config.rsc",
            "dump.bin": repo_path / "dump.bin",
        },
        text_files=["config.rsc"],
        message="with binary",
        normalize=lambda name, data: data,
    )

    assert created is True

    history = gs.get_history()
    assert len(history) == 2

    diffs = gs.get_diff(
        history[1]["sha"],
        history[0]["sha"],
        ["dump.bin"],
    )

    assert "dump.bin" in diffs
    assert "Binary file" in diffs["dump.bin"]


# =========================================================
# HISTORY LIMIT
# =========================================================

def test_history_respects_limit(make_repo):
    _, gs = make_repo(n=30)

    assert len(gs.get_history(limit=10)) == 10
    assert len(gs.get_history(limit=100)) == 30


# =========================================================
# INTEGRATION: GIT CLI
# =========================================================

@pytest.mark.integration
def test_repo_is_readable_by_git_cli(make_repo):
    import shutil
    import subprocess

    if shutil.which("git") is None:
        pytest.skip("git не установлен в PATH")

    repo_path, gs = make_repo(n=3)

    result = subprocess.run(
        ["git", "-C", str(repo_path), "log", "--oneline"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    assert result.returncode == 0, f"git log упал:\n{result.stderr}"

    lines = [
        line for line in result.stdout.splitlines() if line.strip()
    ]
    assert len(lines) == 3
    assert "Backup #003" in result.stdout