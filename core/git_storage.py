# =========================================================
# core/git_storage.py
# =========================================================
#
# Обёртка над dulwich для хранения конфигов устройств
# в git-репозитории.
#
# Репозиторий создаётся прямо в backup_path устройства.
# Файлы лежат плоско:
#
#     backup_path/
#     ├── .git/
#     ├── config.rsc
#     ├── config.backup
#     └── config.xml
#
# =========================================================

import difflib
import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

try:
    from dulwich import porcelain
    from dulwich.repo import Repo
    from dulwich.walk import Walker
    DULWICH_AVAILABLE = True
except ImportError:
    DULWICH_AVAILABLE = False


logger = logging.getLogger(__name__)


# Автор коммитов. У службы SYSTEM нет ~/.gitconfig,
# поэтому подпись передаётся явно в каждый коммит.
_AUTHOR_NAME = "NetworkBackupManager"
_AUTHOR_EMAIL = "nbm@local"

# Файл с пользовательскими аннотациями, хранится внутри .git/.
_NOTES_FILENAME = "nbm_notes.json"


# =========================================================
# БЛОКИРОВКИ
# =========================================================
#
# Параллельные бэкапы разных устройств пишут каждый в свой
# репозиторий, но два бэкапа одного устройства могут идти
# одновременно. Для каждого repo_path держим свой Lock.
# =========================================================

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _get_lock(repo_path: Path) -> threading.Lock:
    key = str(repo_path.resolve())
    with _locks_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _locks[key] = lock
        return lock


def is_available() -> bool:
    """True, если dulwich импортировался."""
    return DULWICH_AVAILABLE


# =========================================================
# ХЕЛПЕРЫ
# =========================================================

def _author_bytes() -> bytes:
    return f"{_AUTHOR_NAME} <{_AUTHOR_EMAIL}>".encode("utf-8")


def _to_local_str(ts: int) -> str:
    """commit_time → 'YYYY-MM-DD HH:MM:SS' в локальной зоне."""
    return (
        datetime.fromtimestamp(ts, tz=timezone.utc)
        .astimezone()
        .strftime("%Y-%m-%d %H:%M:%S")
    )


# =========================================================
# GIT STORAGE
# =========================================================

class GitStorage:
    """Тонкая обёртка для одного репозитория."""

    def __init__(self, repo_path):
        self.repo_path = Path(repo_path).resolve()
        self._lock = _get_lock(self.repo_path)

    # -----------------------------------------------------
    # INIT
    # -----------------------------------------------------

    def is_initialized(self) -> bool:
        return (self.repo_path / ".git").is_dir()

    def _open_repo(self) -> "Repo":
        """Открывает репо, при необходимости создаёт его."""
        if not self.is_initialized():
            self.repo_path.mkdir(parents=True, exist_ok=True)
            porcelain.init(str(self.repo_path))
            logger.info(
                "git: инициализирован репозиторий %s",
                self.repo_path,
            )
        return Repo(str(self.repo_path))

    def _head_sha(self, repo: "Repo") -> bytes | None:
        """SHA HEAD или None, если история пуста."""
        try:
            head = repo.head()
            return head or None
        except (KeyError, ValueError):
            return None

    def _read_blob(
        self,
        repo: "Repo",
        commit_sha: bytes,
        filename: str,
    ) -> bytes | None:
        """
        Содержимое filename из коммита commit_sha.

        В dulwich Tree.__getitem__ возвращает кортеж
        (mode, sha). Раньше (до 1.x) — просто sha.
        Унифицируем оба варианта.
        """
        try:
            commit = repo[commit_sha]
            tree = repo[commit.tree]

            entry = tree[filename.encode()]

            # dulwich ≥ 1.x: entry = (mode, sha)
            # dulwich < 1.x:  entry = sha
            if isinstance(entry, tuple):
                _, blob_sha = entry
            else:
                blob_sha = entry

            blob = repo[blob_sha]
            return blob.data

        except Exception:
            # Ключ отсутствует, битый объект, что угодно —
            # для нас это «файла в этом коммите нет».
            return None

    def _list_files_at(
        self,
        repo: "Repo",
        commit_sha: bytes,
    ) -> list[str]:
        """Список файлов (regular files) в дереве коммита."""
        try:
            commit = repo[commit_sha]
            tree = repo[commit.tree]
            result = []
            for name, mode, _sha in tree.iteritems():
                # mode для regular file = 0o100644 (или 0o100755)
                if (mode & 0o170000) == 0o100000:
                    result.append(name.decode())
            return sorted(result)
        except Exception:
            return []

    # -----------------------------------------------------
    # COMMIT
    # -----------------------------------------------------

    def commit_if_changed(
        self,
        files: dict,
        text_files: Iterable[str],
        message: str,
    ) -> tuple[bool, bool, str]:
        """
        Сравнивает текстовые файлы с HEAD и, если есть отличия,
        копирует ВСЕ файлы в репо и делает коммит.

        Возвращает (ok, created, human_message):

            ok=True,  created=True   — коммит создан
            ok=True,  created=False  — изменений нет (норма)
            ok=False, created=False  — ошибка git
        """ 

        if not DULWICH_AVAILABLE:
            return False, False, "git: dulwich недоступен"
    
        text_files = set(text_files)

        with self._lock:
            repo = self._open_repo()
            try:
                head_sha = self._head_sha(repo)

                # Первый коммит — всегда
                if head_sha is None:
                    changed = True
                else:
                    changed = False
                    for name in text_files:
                        src = files.get(name)
                        if src is None or not src.exists():
                            continue
                        new_data = src.read_bytes()
                        old_data = self._read_blob(
                            repo, head_sha, name
                        )
                        if old_data != new_data:
                            changed = True
                            break

                if not changed:
                    logger.info(
                        "git[%s]: изменений нет, коммит пропущен",
                        self.repo_path.name,
                    )
                    return True, False, "Изменений не обнаружено"

                # Копируем рабочие файлы в репо
                written: list[str] = []
                for name, src in files.items():
                    if not src.exists():
                        continue
                    dst = self.repo_path / name
                    if dst.resolve() != src.resolve():
                        dst.write_bytes(src.read_bytes())
                    written.append(name)

                if not written:
                    return False, False, "Нет файлов для коммита"

                porcelain.add(str(self.repo_path), paths=written)

                author = _author_bytes()
                porcelain.commit(
                    str(self.repo_path),
                    message=message.encode("utf-8"),
                    author=author,
                    committer=author,
                )

                new_sha = self._head_sha(repo)
                short = new_sha.decode()[:7] if new_sha else "?"
                logger.info(
                    "git[%s]: коммит %s — %s",
                    self.repo_path.name,
                    short,
                    message,
                )
                return True, True, f"Создан коммит {short}"

            except Exception as exc:
                logger.error(
                    "git[%s]: ошибка коммита: %s",
                    self.repo_path.name,
                    exc,
                    exc_info=True,
                )
                return False, False, f"Ошибка git: {exc}"
            finally:
                repo.close()

    # -----------------------------------------------------
    # HISTORY
    # -----------------------------------------------------

    def get_history(self, limit: int = 50) -> list:
        """
        Список коммитов (свежие сверху):

        [{
            "sha": "abc...",
            "short": "abc1234",
            "message": "...",
            "author": "...",
            "time": "2026-09-16 03:00:00",
            "ts": 1757980000,
            "files": ["config.rsc", "config.backup"],
        }, ...]
        """

        if not self.is_initialized():
            return []

        with self._lock:
            repo = Repo(str(self.repo_path))
            try:
                head_sha = self._head_sha(repo)
                if head_sha is None:
                    return []

                walker = Walker(
                    repo.object_store,
                    [head_sha],
                    max_entries=limit,
                )

                result = []
                for entry in walker:
                    commit = entry.commit
                    sha = commit.id.decode()
                    result.append({
                        "sha": sha,
                        "short": sha[:7],
                        "message": commit.message.decode(
                            "utf-8", errors="replace"
                        ).strip(),
                        "author": commit.author.decode(
                            "utf-8", errors="replace"
                        ),
                        "time": _to_local_str(commit.commit_time),
                        "ts": commit.commit_time,
                        "files": self._list_files_at(repo, commit.id),
                    })

                return result
            finally:
                repo.close()

    # -----------------------------------------------------
    # FILE AT COMMIT
    # -----------------------------------------------------

    def get_file_at(self, sha: str, filename: str) -> bytes | None:
        """Байты файла из указанного коммита."""
        if not self.is_initialized():
            return None

        with self._lock:
            repo = Repo(str(self.repo_path))
            try:
                return self._read_blob(
                    repo, sha.encode(), filename
                )
            except Exception as exc:
                logger.error(
                    "git: не удалось получить %s@%s: %s",
                    filename,
                    sha[:7],
                    exc,
                )
                return None
            finally:
                repo.close()

    def list_files(self) -> list[str]:
        """Список файлов в HEAD."""
        if not self.is_initialized():
            return []

        with self._lock:
            repo = Repo(str(self.repo_path))
            try:
                head_sha = self._head_sha(repo)
                if head_sha is None:
                    return []
                return self._list_files_at(repo, head_sha)
            finally:
                repo.close()

    # -----------------------------------------------------
    # DIFF
    # -----------------------------------------------------

    def get_diff(
        self,
        sha_a: str,
        sha_b: str,
        filenames: Iterable[str],
    ) -> dict:
        """
        Unified diff по каждому файлу.

        Возвращает {filename: diff_text}.
        """
        result = {}
        for name in filenames:
            old = self.get_file_at(sha_a, name) or b""
            new = self.get_file_at(sha_b, name) or b""

            # Бинарные — просто пометим
            if b"\x00" in old or b"\x00" in new:
                result[name] = (
                    f"Binary file "
                    f"(old: {len(old)} bytes, "
                    f"new: {len(new)} bytes)"
                )
                continue

            old_lines = old.decode(
                "utf-8", errors="replace"
            ).splitlines(keepends=True)
            new_lines = new.decode(
                "utf-8", errors="replace"
            ).splitlines(keepends=True)

            diff = difflib.unified_diff(
                old_lines,
                new_lines,
                fromfile=f"{name}@{sha_a[:7]}",
                tofile=f"{name}@{sha_b[:7]}",
                n=3,
            )
            result[name] = "".join(diff)

        return result

    # -----------------------------------------------------
    # NOTES (пользовательские комментарии)
    # -----------------------------------------------------
    #
    # Хранятся в .git/nbm_notes.json:
    #
    #     {"<sha>": "комментарий", ...}
    #
    # =====================================================

    def _notes_path(self) -> Path:
        return self.repo_path / ".git" / _NOTES_FILENAME

    def get_notes(self) -> dict:
        path = self._notes_path()
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return {}
            return {str(k): str(v) for k, v in data.items()}
        except Exception as exc:
            logger.warning(
                "git: не удалось прочитать notes: %s", exc
            )
            return {}

    def set_note(self, sha: str, note: str) -> None:
        with self._lock:
            notes = self.get_notes()
            if note.strip():
                notes[sha] = note.strip()
            else:
                notes.pop(sha, None)

            path = self._notes_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(notes, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

    def get_note(self, sha: str) -> str:
        return self.get_notes().get(sha, "")

    # -----------------------------------------------------
    # СВОДКА
    # -----------------------------------------------------

    def get_last_commit(self) -> dict | None:
        history = self.get_history(limit=1)
        return history[0] if history else None

    def get_commit_count(self) -> int:
        if not self.is_initialized():
            return 0
        with self._lock:
            repo = Repo(str(self.repo_path))
            try:
                head_sha = self._head_sha(repo)
                if head_sha is None:
                    return 0
                count = 0
                walker = Walker(
                    repo.object_store,
                    [head_sha],
                )
                for _ in walker:
                    count += 1
                return count
            finally:
                repo.close()