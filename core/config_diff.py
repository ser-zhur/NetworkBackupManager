# =========================================================
# core/config_diff.py — что изменилось в конфигурации
# =========================================================
#
# После нового коммита в git-истории устройства сравнивает
# его с предыдущим: какие файлы изменились, сколько строк
# добавлено и удалено, текст изменений (unified diff).
#
# Сравнение — с нормализацией плагина (у MikroTik — без
# строки с датой выгрузки), как и решение «есть ли
# изменения»: иначе каждая выгрузка выглядела бы изменённой.
#
# Текст изменений может содержать секреты конфигурации
# (хэши паролей, ключи) — в журнал событий он не пишется,
# а уходит только подписчикам в памяти (events.emit(_private=…)).
#
# =========================================================

from __future__ import annotations

import difflib
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

MAX_DIFF_CHARS = 3500


def _identity(filename: str, data: bytes) -> bytes:
    return data


def summarize_change(backup_path, normalize=None, max_chars: int = MAX_DIFF_CHARS) -> dict | None:
    """
    Последний коммит против предыдущего. None — версия первая
    (сравнивать не с чем) или история недоступна.

    {"sha", "prev_sha", "files", "added", "removed", "diff"}
    """
    from core.git_storage import GitStorage, is_available

    if not is_available():
        return None
    normalize = normalize or _identity

    try:
        gs = GitStorage(Path(backup_path))
        history = gs.get_history(limit=2)
    except Exception as exc:
        logger.warning("config_diff: история %s недоступна: %s", backup_path, exc)
        return None
    if len(history) < 2:
        return None

    new, old = history[0], history[1]
    names = sorted(set(new.get("files") or []) | set(old.get("files") or []))

    files: list[str] = []
    added = removed = 0
    parts: list[str] = []

    for name in names:
        before = gs.get_file_at(old["sha"], name) or b""
        after = gs.get_file_at(new["sha"], name) or b""
        if before == after:
            continue

        if b"\x00" in before or b"\x00" in after:
            files.append(name)
            parts.append(f"{name}: двоичный файл изменён ({len(before)} → {len(after)} байт)")
            continue

        try:
            before_n = normalize(name, before)
            after_n = normalize(name, after)
        except Exception:
            before_n, after_n = before, after
        if before_n == after_n:
            continue

        diff = list(difflib.unified_diff(
            before_n.decode("utf-8", errors="replace").splitlines(),
            after_n.decode("utf-8", errors="replace").splitlines(),
            fromfile=name, tofile=name, n=1, lineterm="",
        ))
        plus = sum(1 for x in diff if x.startswith("+") and not x.startswith("+++"))
        minus = sum(1 for x in diff if x.startswith("-") and not x.startswith("---"))
        if not plus and not minus:
            continue
        files.append(name)
        added += plus
        removed += minus
        parts.append("\n".join(diff[2:]))           # без заголовков ---/+++

    if not files:
        return None

    text = ""
    for name, part in zip(files, parts):
        block = f"── {name}\n{part}\n"
        if len(text) + len(block) > max_chars:
            text += "…(изменения сокращены)\n"
            break
        text += block

    return {
        "sha": new["sha"],
        "prev_sha": old["sha"],
        "files": files,
        "added": added,
        "removed": removed,
        "diff": text.rstrip(),
    }
