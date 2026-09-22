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
# GitStorage ничего не знает про форматы конкретных
# устройств. Нормализация содержимого для сравнения
# приходит снаружи — в commit_if_changed(normalize=...).
#
# =========================================================

import difflib
import json
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

try:
    from dulwich import porcelain
    from dulwich.objects import Blob, Commit, Tree
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

# Файл со списком sha, помеченных к удалению (soft-delete).
# Хранится в .git/ рядом с notes. Физическое удаление из
# истории делается отдельным шагом (rebuild_history),
# который вызывается вручную или scheduler'ом.
_DELETED_FILENAME = "nbm_deleted.json"


# =========================================================
# NORMALIZE
# =========================================================
#
# Локальная реализация identity — чтобы модуль не зависел
# от core.storage (там уже импортируется GitStorage).
# =========================================================

def _identity(filename: str, data: bytes) -> bytes:
    return data


# =========================================================
# БЛОКИРОВКИ
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
        if not self.is_initialized():
            self.repo_path.mkdir(parents=True, exist_ok=True)
            porcelain.init(str(self.repo_path))
            logger.info(
                "git: инициализирован репозиторий %s",
                self.repo_path,
            )
        return Repo(str(self.repo_path))

    def _head_sha(self, repo: "Repo") -> bytes | None:
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
        try:
            commit = repo[commit_sha]
            tree = repo[commit.tree]
            entry = tree[filename.encode()]

            if isinstance(entry, tuple):
                _, blob_sha = entry
            else:
                blob_sha = entry

            blob = repo[blob_sha]
            return blob.data

        except Exception as exc:
            logger.warning(
                "git._read_blob(%s, %s): FAILED (%s): %s",
                commit_sha.decode(errors="replace")
                if isinstance(commit_sha, bytes)
                else str(commit_sha),
                filename,
                type(exc).__name__,
                exc,
            )
            return None

    def _list_files_at(
        self,
        repo: "Repo",
        commit_sha: bytes,
    ) -> list[str]:
        try:
            commit = repo[commit_sha]
            tree = repo[commit.tree]
            result = []
            for name, mode, _sha in tree.iteritems():
                if (mode & 0o170000) == 0o100000:
                    result.append(name.decode())
            return sorted(result)
        except Exception:
            return []

    # -----------------------------------------------------
    # DELETED SHAS (soft-delete markers)
    # -----------------------------------------------------

    def _deleted_path(self) -> Path:
        return self.repo_path / ".git" / _DELETED_FILENAME

    def _load_deleted(self) -> set[str]:
        """
        Читает список помеченных sha.

        Возвращает множество строк (hex sha). Если файла нет
        или он повреждён — пустое множество без ошибки.
        """
        path = self._deleted_path()
        if not path.exists():
            return set()

        try:
            data = json.loads(
                path.read_text(encoding="utf-8")
            )
        except Exception as exc:
            logger.warning(
                "git: не удалось прочитать %s: %s",
                path, exc,
            )
            return set()

        if not isinstance(data, list):
            return set()

        return {str(s) for s in data}

    def _save_deleted(self, shas: set[str]) -> None:
        """
        Сохраняет список помеченных sha.

        Пустой список = удаление файла (не держим мусор).
        """
        path = self._deleted_path()
        path.parent.mkdir(parents=True, exist_ok=True)

        if not shas:
            if path.exists():
                try:
                    path.unlink()
                except OSError as exc:
                    logger.warning(
                        "git: не удалось удалить %s: %s",
                        path, exc,
                    )
            return

        try:
            path.write_text(
                json.dumps(
                    sorted(shas),
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning(
                "git: не удалось записать %s: %s",
                path, exc,
            )

    def get_deleted_shas(self) -> set[str]:
        """Возвращает копию множества помеченных sha."""
        return set(self._load_deleted())

    def mark_deleted(self, shas: set[str]) -> int:
        """
        Помечает sha к удалению.

        Возвращает количество ДОБАВЛЕННЫХ sha (уже помеченные
        не считаются).
        """
        if not shas:
            return 0

        with self._lock:
            current = self._load_deleted()
            before = len(current)
            current.update(str(s) for s in shas)
            self._save_deleted(current)
            return len(current) - before

    def unmark_deleted(
        self,
        shas: set[str] | None = None,
    ) -> int:
        """
        Снимает пометку.

        shas=None — очищает весь список.
        Возвращает количество снятых пометок.
        """
        with self._lock:
            current = self._load_deleted()

            if shas is None:
                count = len(current)
                self._save_deleted(set())
                return count

            to_remove = {str(s) for s in shas}
            removed = current & to_remove
            current -= to_remove
            self._save_deleted(current)
            return len(removed)

    # -----------------------------------------------------
    # COMMIT
    # -----------------------------------------------------

    def commit_if_changed(
        self,
        files: dict,
        text_files: Iterable[str],
        message: str,
        normalize: Callable[[str, bytes], bytes] | None = None,
    ) -> tuple[bool, bool, str]:
        """
        Сравнивает текстовые файлы с HEAD и, если есть
        отличия (с учётом нормализации), коммитит содержимое
        рабочего дерева.

        normalize — функция (filename, data) -> data_for_compare.
        Применяется ТОЛЬКО для сравнения. В git попадают
        исходные байты.

        Возвращает (ok, created, human_message).
        """

        if not DULWICH_AVAILABLE:
            return False, False, "git: dulwich недоступен"

        if normalize is None:
            normalize = _identity

        text_files = set(text_files)

        with self._lock:
            repo = self._open_repo()
            try:
                head_sha = self._head_sha(repo)

                if head_sha is None:
                    changed = True
                else:
                    # Отбираем текстовые файлы, которые
                    # реально существуют на диске. Сравнивать
                    # имеет смысл только их.
                    comparable = [
                        name for name in text_files
                        if name in files and files[name].exists()
                    ]

                    if not comparable:
                        # Нет ни одного текстового файла для
                        # сравнения — значит устройство отдаёт
                        # binary-only либо текст по какой-то
                        # причине отсутствует.
                        #
                        # Политика: коммитим всегда. Binary-файлы
                        # (например, MikroTik .backup) содержат
                        # внутренние timestamp-ы и почти всегда
                        # уникальны, а git дедуплицирует blob-ы —
                        # накладные расходы малы (один commit
                        # ~200 байт).
                        logger.info(
                            "git[%s]: нет текстовых файлов "
                            "для сравнения (%d файлов всего) — "
                            "коммит создаётся безусловно",
                            self.repo_path.name,
                            len(files),
                        )
                        changed = True
                    else:
                        changed = False
                        for name in comparable:
                            src = files[name]
                            new_data = src.read_bytes()
                            old_data = self._read_blob(
                                repo, head_sha, name
                            )

                            new_cmp = normalize(name, new_data)
                            old_cmp = (
                                normalize(name, old_data)
                                if old_data is not None
                                else None
                            )

                            logger.info(
                                "git[%s]: compare %s: "
                                "old=%s bytes, new=%d bytes, "
                                "equal=%s",
                                self.repo_path.name,
                                name,
                                len(old_data)
                                if old_data is not None
                                else "None",
                                len(new_data),
                                old_cmp == new_cmp,
                            )

                            if old_cmp != new_cmp:
                                changed = True
                                break

                if not changed:
                    logger.info(
                        "git[%s]: изменений нет, коммит пропущен",
                        self.repo_path.name,
                    )
                    return True, False, "Изменений не обнаружено"

                # -------------------------------------------------
                # Строим tree и commit явно, без porcelain.
                # Каждый файл попадает в дерево ровно в том виде,
                # в котором он лежит на диске (raw, без нормализации).
                # -------------------------------------------------

                blob_ids: dict[str, bytes] = {}

                for name, src in files.items():
                    if not src.exists():
                        continue

                    data = src.read_bytes()
                    blob = Blob.from_string(data)
                    repo.object_store.add_object(blob)
                    blob_ids[name] = blob.id

                    logger.info(
                        "git[%s]: staged %s (%d bytes, blob=%s)",
                        self.repo_path.name,
                        name,
                        len(data),
                        blob.id.decode()[:7],
                    )

                if not blob_ids:
                    return False, False, "Нет файлов для коммита"

                tree = Tree()
                for name, blob_id in blob_ids.items():
                    tree.add(
                        name.encode("utf-8"),
                        0o100644,
                        blob_id,
                    )

                repo.object_store.add_object(tree)

                now = int(time.time())
                author_bytes = _author_bytes()

                commit = Commit()
                commit.tree = tree.id
                commit.parents = [head_sha] if head_sha else []
                commit.author = author_bytes
                commit.committer = author_bytes
                commit.commit_time = now
                commit.author_time = now
                commit.commit_timezone = 0
                commit.author_timezone = 0
                commit.message = message.encode("utf-8")
                commit.encoding = b"UTF-8"

                repo.object_store.add_object(commit)

                try:
                    repo.refs[b"refs/heads/master"] = commit.id
                except Exception as exc:
                    logger.error(
                        "git[%s]: не удалось обновить "
                        "refs/heads/master: %s",
                        self.repo_path.name,
                        exc,
                    )
                    return False, False, (
                        f"Ошибка обновления HEAD: {exc}"
                    )

                short = commit.id.decode()[:7]
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
    
    def get_history(
        self,
        limit: int = 50,
        include_marked: bool = False,
    ) -> list:
        """
        История коммитов.

        include_marked=False (default) — помеченные к удалению
        исключаются. Используется на главной странице, в
        retention и на странице diff: пользователю там не
        нужны «полуудалённые» версии.

        include_marked=True — все коммиты, включая помеченные.
        Каждый получает флаг 'marked'. Используется на странице
        /git/.../, где пользователь управляет пометками и
        должен видеть, что уже отмечено.

        Walker не имеет собственного лимита — останавливаемся
        после сбора limit валидных записей.
        """
        if not self.is_initialized():
            return []

        with self._lock:
            repo = Repo(str(self.repo_path))
            try:
                head_sha = self._head_sha(repo)
                if head_sha is None:
                    return []

                deleted = self._load_deleted()

                walker = Walker(
                    repo.object_store,
                    [head_sha],
                )

                result = []
                for entry in walker:
                    commit = entry.commit
                    sha = commit.id.decode()
                    is_marked = sha in deleted

                    if is_marked and not include_marked:
                        continue

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
                        "files": self._list_files_at(
                            repo, commit.id
                        ),
                        "marked": is_marked,
                    })

                    if len(result) >= limit:
                        break

                return result
            finally:
                repo.close()

    # -----------------------------------------------------
    # FILE AT COMMIT
    # -----------------------------------------------------

    def get_file_at(self, sha: str, filename: str) -> bytes | None:
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
        result = {}
        for name in filenames:
            old = self.get_file_at(sha_a, name) or b""
            new = self.get_file_at(sha_b, name) or b""

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
    # NOTES
    # -----------------------------------------------------

    def _notes_path(self) -> Path:
        return self.repo_path / ".git" / _NOTES_FILENAME

    def get_notes(self) -> dict:
        path = self._notes_path()
        if not path.exists():
            return {}
        try:
            data = json.loads(
                path.read_text(encoding="utf-8")
            )
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
                json.dumps(
                    notes, ensure_ascii=False, indent=2
                ),
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
        """Общее число коммитов без помеченных к удалению."""
        if not self.is_initialized():
            return 0
        with self._lock:
            repo = Repo(str(self.repo_path))
            try:
                head_sha = self._head_sha(repo)
                if head_sha is None:
                    return 0

                deleted = self._load_deleted()

                count = 0
                walker = Walker(
                    repo.object_store,
                    [head_sha],
                )
                for entry in walker:
                    sha = entry.commit.id.decode()
                    if sha in deleted:
                        continue
                    count += 1
                return count
            finally:
                repo.close()
                
        # -----------------------------------------------------
    # REBUILD HISTORY (hard-delete)
    # -----------------------------------------------------

    def _walk_all(self, repo: "Repo", head_sha: bytes) -> list:
        """
        Все коммиты от HEAD до корня (в порядке от новых к старым).

        Используется в rebuild_history. Для обычного чтения
        истории — get_history().
        """
        walker = Walker(repo.object_store, [head_sha])
        return [entry.commit for entry in walker]

    def rebuild_history(
        self,
        shas_to_delete: set[str],
    ) -> tuple[bool, str]:
        """
        Физически удаляет коммиты из истории.

        Создаёт новые commit-объекты для всех, что выше
        удалённых, с перепривязкой parent-указателей.
        Верхние коммиты получат новые SHA (как в git rebase).

        Содержимое файлов (blob'ы) сохраняется полностью,
        пересоздаются только лёгкие commit-объекты (~200 байт).

        Возвращает (ok, message).

        Побочный эффект: очищает .git/nbm_deleted.json —
        после успешной пересборки список больше не нужен.

        Нельзя удалить все коммиты — вернёт (False, ...).
        """
        if not shas_to_delete:
            return True, "Список удаляемых пуст"

        with self._lock:
            repo = self._open_repo()
            try:
                head_sha = self._head_sha(repo)
                if head_sha is None:
                    return True, "Репозиторий пуст"

                all_commits = self._walk_all(repo, head_sha)
                if not all_commits:
                    return True, "Нечего удалять"

                deleted_bytes = {
                    s.encode() if isinstance(s, str) else s
                    for s in shas_to_delete
                }

                kept = [
                    c for c in all_commits
                    if c.id not in deleted_bytes
                ]

                if not kept:
                    return False, (
                        "Нельзя удалить всю историю целиком"
                    )

                if len(kept) == len(all_commits):
                    # Ничего не удаляется — очищаем пометки
                    # и выходим без пересборки.
                    self._save_deleted(set())
                    return True, "Нечего удалять"

                # Идём от старых к новым, пересобирая цепочку
                new_parent: bytes | None = None

                for old_commit in reversed(kept):
                    new_commit = Commit()
                    new_commit.tree = old_commit.tree
                    new_commit.parents = (
                        [new_parent] if new_parent else []
                    )
                    new_commit.author = old_commit.author
                    new_commit.committer = old_commit.committer
                    new_commit.commit_time = old_commit.commit_time
                    new_commit.author_time = old_commit.author_time
                    new_commit.commit_timezone = (
                        old_commit.commit_timezone
                    )
                    new_commit.author_timezone = (
                        old_commit.author_timezone
                    )
                    new_commit.message = old_commit.message
                    new_commit.encoding = old_commit.encoding

                    repo.object_store.add_object(new_commit)
                    new_parent = new_commit.id

                repo.refs[b"refs/heads/master"] = new_parent

                removed = len(all_commits) - len(kept)

                # Пометки больше не нужны
                self._save_deleted(set())

                logger.info(
                    "git[%s]: rebuild — удалено %d из %d",
                    self.repo_path.name,
                    removed,
                    len(all_commits),
                )

                return True, f"Удалено коммитов: {removed}"

            except Exception as exc:
                logger.error(
                    "git[%s]: ошибка rebuild_history: %s",
                    self.repo_path.name,
                    exc,
                    exc_info=True,
                )
                return False, f"Ошибка пересборки: {exc}"
            finally:
                repo.close()

    def gc(self) -> tuple[bool, str]:
        """
        Освобождает место через git gc.

        Требует установленного git в PATH. Если git нет —
        возвращает (False, ...) без падения. Это не ошибка:
        старые объекты продолжат лежать в .git/objects,
        просто занимая место, но не мешая работе.
        """
        import shutil
        import subprocess

        git_bin = shutil.which("git")
        if git_bin is None:
            return False, "git не найден в PATH"

        try:
            result = subprocess.run(
                [
                    git_bin,
                    "-C", str(self.repo_path),
                    "gc",
                    "--prune=now",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=60,
            )
        except subprocess.TimeoutExpired:
            return False, "git gc превысил таймаут 60 с"
        except Exception as exc:
            return False, f"git gc не запустился: {exc}"

        if result.returncode != 0:
            err = (result.stderr or result.stdout or "").strip()
            return False, err or "git gc вернул ошибку"

        return True, "git gc выполнен"