# =========================================================
# tests/conftest.py
# =========================================================
#
# Общие pytest-фикстуры для всех тестов проекта.
#
# =========================================================

import shutil
import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


dulwich = pytest.importorskip(
    "dulwich",
    reason="dulwich не установлен — тесты git_storage пропущены",
)


FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    if not FIXTURES_DIR.exists():
        pytest.skip(f"Каталог fixtures не найден: {FIXTURES_DIR}")
    return FIXTURES_DIR


@pytest.fixture(scope="session")
def sample_old(fixtures_dir: Path) -> str:
    path = fixtures_dir / "sample-old.rsc"
    if not path.exists():
        pytest.skip(f"Нет образца: {path}")
    return path.read_text(encoding="utf-8")


@pytest.fixture(scope="session")
def sample_new(fixtures_dir: Path) -> str:
    path = fixtures_dir / "sample-new.rsc"
    if not path.exists():
        pytest.skip(f"Нет образца: {path}")
    return path.read_text(encoding="utf-8")


@pytest.fixture
def make_repo(tmp_path):
    """
    Фабрика тестовых репозиториев.

        repo_path, gs = make_repo(n=20)
    """
    from core.git_storage import GitStorage

    def _make(
        n: int = 10,
        content=None,
        messages=None,
        with_notes=None,
    ):
        repo_path = tmp_path / f"repo-{n}-{id(content) if content else 0}"
        repo_path.mkdir(parents=True, exist_ok=True)

        gs = GitStorage(repo_path)

        if content is None:
            content = lambda i: f"version {i}\n"

        file_path = repo_path / "config.rsc"

        for i in range(1, n + 1):
            # ВАЖНО: write_bytes, а не write_text.
            # На Windows write_text превращает \n в \r\n,
            # что ломает побайтовое сравнение в тестах.
            # Реальные файлы от плагинов приходят в бинарном
            # виде (sftp.get, scp.get), платформа не влияет.
            file_path.write_bytes(content(i).encode("utf-8"))

            msg = (
                messages[i - 1]
                if messages and i - 1 < len(messages)
                else f"Backup #{i:03d}"
            )

            ok, created, _ = gs.commit_if_changed(
                files={"config.rsc": file_path},
                text_files=["config.rsc"],
                message=msg,
                normalize=lambda name, data: data,
            )

            if not ok:
                raise RuntimeError(f"Не удалось создать коммит #{i}")

        if with_notes:
            history = gs.get_history()
            for idx, note in with_notes.items():
                if 0 <= idx < len(history):
                    gs.set_note(history[idx]["sha"], note)

        return repo_path, gs

    return _make