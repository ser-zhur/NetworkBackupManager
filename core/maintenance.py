# =========================================================
# core/maintenance.py
# =========================================================
#
# Операции обслуживания истории: пометка удаления,
# пересборка, retention.
#
# Разделение с core.git_storage:
#   • git_storage знает, КАК работать с одним репо;
#   • maintenance знает, ЧТО делать (правила, обход
#     всех устройств, применение config.json).
#
# =========================================================

import logging
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)


# =========================================================
# MARK / UNMARK
# =========================================================

def mark_deleted(repo_path, shas) -> int:
    """
    Помечает sha к удалению.

    Возвращает количество добавленных пометок.
    """
    from core.git_storage import GitStorage

    gs = GitStorage(repo_path)
    return gs.mark_deleted(set(shas))


def unmark_deleted(repo_path, shas=None) -> int:
    """
    Снимает пометки.

    shas=None — очищает весь список.
    """
    from core.git_storage import GitStorage

    gs = GitStorage(repo_path)
    return gs.unmark_deleted(shas)


def list_marked(repo_path) -> list[str]:
    """Возвращает отсортированный список помеченных sha."""
    from core.git_storage import GitStorage

    gs = GitStorage(repo_path)
    return sorted(gs.get_deleted_shas())


# =========================================================
# SWEEP (пересборка)
# =========================================================

def sweep_repo(repo_path) -> tuple[bool, str]:
    """
    Пересобирает историю, физически удаляя помеченные коммиты.

    Возвращает (ok, message).
    Если помеченных нет — no-op (True, "Нечего очищать").
    """
    from core.git_storage import GitStorage

    gs = GitStorage(repo_path)

    if not gs.is_initialized():
        return True, "Репозиторий не инициализирован"

    marked = gs.get_deleted_shas()
    if not marked:
        return True, "Нечего очищать"

    ok, msg = gs.rebuild_history(marked)
    if not ok:
        return False, msg

    # Пытаемся освободить место. Не критично, если git нет.
    gc_ok, gc_msg = gs.gc()
    if gc_ok:
        msg = f"{msg}; {gc_msg}"
    else:
        logger.debug(
            "git[%s]: gc не выполнен: %s",
            Path(repo_path).name,
            gc_msg,
        )

    return True, msg


def sweep_all_devices(plugin_manager=None) -> dict:
    """
    Обходит все устройства и вызывает sweep_repo для каждого.

    Возвращает {device_id: (ok, message)}.
    """
    if plugin_manager is None:
        try:
            from core.plugin_manager import PluginManager
            pm = PluginManager()
            pm.discover_and_load_plugins()
            plugin_manager = pm
        except Exception as exc:
            logger.error(
                "maintenance: не удалось загрузить PluginManager: %s",
                exc,
                exc_info=True,
            )
            return {}

    results: dict = {}

    for device_type, plugin_data in plugin_manager._plugins.items():
        routers = plugin_data.get("routers", [])
        for idx, router in enumerate(routers):
            device_id = f"{device_type}/{idx}"

            raw_path = router.get("backup_path", "")
            if not raw_path:
                continue

            backup_path = Path(raw_path)
            if not backup_path.is_absolute():
                from core.context import BASE_DIR
                backup_path = (BASE_DIR / backup_path).resolve()

            if not backup_path.exists():
                continue

            try:
                ok, msg = sweep_repo(backup_path)
                results[device_id] = (ok, msg)
                if not ok:
                    logger.warning(
                        "maintenance: %s — %s",
                        device_id, msg,
                    )
            except Exception as exc:
                logger.error(
                    "maintenance: %s — исключение: %s",
                    device_id, exc,
                    exc_info=True,
                )
                results[device_id] = (False, str(exc))

    return results


# =========================================================
# RETENTION
# =========================================================

def apply_retention(
    repo_path,
    config: dict,
    now: datetime | None = None,
) -> list[str]:
    """
    Применяет правило retention к одной репе.

    Помечает sha к удалению и возвращает их список.
    Физического удаления НЕ делает — это ответственность
    sweep_repo().

    Правила (все три применяются, результат — пересечение
    «не защищён» И «старше даты»):

        keep_last        — последние N версий защищены
        keep_with_notes  — версии с заметками защищены
        max_age_days     — удалять старше N дней

    Если у коммита одновременно срабатывают keep_last и
    max_age_days — приоритет у keep_last: не помечается.

    now — для тестов: позволяет сдвинуть «текущее время»
    вперёд, чтобы коммиты «постарели» относительно cutoff.
    В продакшене всегда None (берётся datetime.now()).
    """
    from core.git_storage import GitStorage

    gs = GitStorage(repo_path)
    if not gs.is_initialized():
        return []

    keep_last = max(0, int(config.get("keep_last", 20)))
    max_age_days = max(1, int(config.get("max_age_days", 365)))
    keep_with_notes = bool(config.get("keep_with_notes", True))

    # Все коммиты без фильтрации — иначе защита keep_last
    # не увидит помеченные ранее.
    history = gs.get_history(limit=1_000_000)
    if not history:
        return []

    notes = gs.get_notes() if keep_with_notes else {}

    reference = now or datetime.now()
    cutoff_ts = int(
        (reference - timedelta(days=max_age_days)).timestamp()
    )

    # history[0] — самый свежий, history[:keep_last] — свежие N
    protected = {c["sha"] for c in history[:keep_last]}

    marked: list[str] = []

    for c in history:
        sha = c["sha"]

        if sha in protected:
            continue
        if keep_with_notes and sha in notes:
            continue
        if c.get("ts", 0) >= cutoff_ts:
            continue

        marked.append(sha)

    if marked:
        gs.mark_deleted(set(marked))
        logger.info(
            "retention[%s]: помечено %d из %d "
            "(max_age=%d, keep_last=%d, keep_with_notes=%s)",
            Path(repo_path).name,
            len(marked),
            len(history),
            max_age_days,
            keep_last,
            keep_with_notes,
        )

    return marked