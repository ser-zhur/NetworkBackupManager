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
import threading
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)


# Глобальный lock: sweep'ы по всем репам сериализуются.
# Даже разные устройства не будут пересобираться одновременно —
# это щадит диск и CPU при параллельных бэкапах.
_SWEEP_LOCK = threading.Lock()

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
    Пересобирает историю одного репозитория, физически
    удаляя помеченные коммиты.

    Атомарность обеспечивается GitStorage.sweep() (per-repo
    лок). Глобальный _SWEEP_LOCK дополнительно сериализует
    sweep'ы по РАЗНЫМ репам — чтобы параллельные бэкапы не
    нагружали диск одновременными пересборками.

    Возвращает (ok, message).
    Если помеченных нет — no-op (True, "Нечего очищать").
    """
    from core.git_storage import GitStorage

    gs = GitStorage(repo_path)

    if not gs.is_initialized():
        return True, "Репозиторий не инициализирован"

    with _SWEEP_LOCK:
        return gs.sweep()

def _iter_devices(plugin_manager):
    """
    Итератор по всем устройствам с заполненным backup_path.

    yield (device_id, device_type, index, router, backup_path).

    backup_path — абсолютный Path. Молча пропускает
    устройства без backup_path и с неразрешимым путём.
    """
    from core.context import resolve_backup_path

    if plugin_manager is None:
        return

    for device_type, plugin_data in plugin_manager._plugins.items():
        routers = plugin_data.get("routers", [])
        for idx, router in enumerate(routers):
            raw = router.get("backup_path", "")
            if not raw:
                continue

            backup_path = resolve_backup_path(raw)

            yield (
                f"{device_type}/{idx}",
                device_type,
                idx,
                router,
                backup_path,
            )


def _ensure_plugin_manager(plugin_manager=None):
    """
    Возвращает переданный plugin_manager или загружает
    новый. None в случае неудачи.
    """
    if plugin_manager is not None:
        return plugin_manager

    try:
        from core.plugin_manager import PluginManager
        pm = PluginManager()
        pm.discover_and_load_plugins()
        return pm
    except Exception as exc:
        logger.error(
            "maintenance: не удалось загрузить PluginManager: %s",
            exc,
            exc_info=True,
        )
        return None


def sweep_all_devices(plugin_manager=None) -> dict:
    """
    Обходит все устройства и вызывает sweep_repo для каждого.

    Возвращает {device_id: (ok, message)}.
    """
    plugin_manager = _ensure_plugin_manager(plugin_manager)
    if plugin_manager is None:
        return {}

    results: dict = {}

    for device_id, _dt, _idx, _router, backup_path in (
        _iter_devices(plugin_manager)
    ):
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

def post_backup_cleanup(plugin_manager=None) -> dict:
    """
    Вызывается после завершения массового бэкапа
    (планового или ручного).

    Последовательность:
        1. Если retention.enabled — применяет политику
           ко всем устройствам (только пометки).
        2. Если retention.auto_sweep — физически удаляет
           помеченные версии через sweep_all_devices.

    Возвращает словарь со статистикой:
        {"marked": int, "removed": int, "errors": int}

    Все исключения логируются, но не поднимаются наружу:
    операция обслуживания не должна ломать основной сценарий.
    """
    from core.context import CONFIG_FILE, load_json

    stats = {"marked": 0, "removed": 0, "errors": 0}

    try:
        config = load_json(CONFIG_FILE, {})
        retention = config.get("retention") or {}
    except Exception as exc:
        logger.error(
            "cleanup: не удалось прочитать config.json: %s",
            exc, exc_info=True,
        )
        stats["errors"] += 1
        return stats

    if not retention.get("enabled"):
        logger.debug(
            "cleanup: retention выключен, пропуск"
        )
        return stats

    plugin_manager = _ensure_plugin_manager(plugin_manager)
    if plugin_manager is None:
        stats["errors"] += 1
        return stats

    # ---- Шаг 1: пометки ----

    logger.info("🧹 Автоочистка: пометка старых версий...")

    from core.plugin_manager import is_device_disabled

    for device_id, _dt, _idx, router, backup_path in (
        _iter_devices(plugin_manager)
    ):
        if not backup_path.exists():
            continue
        # Отключённое устройство: история хранится как есть
        if is_device_disabled(router):
            continue

        try:
            marked = apply_retention(backup_path, retention)
            stats["marked"] += len(marked)
        except Exception as exc:
            stats["errors"] += 1
            logger.error(
                "cleanup[%s]: пометка упала: %s",
                device_id, exc, exc_info=True,
            )

    # ---- Шаг 2: физическое удаление ----

    if retention.get("auto_sweep"):
        logger.info(
            "🧹 Автоочистка: удаление помеченных версий..."
        )

        try:
            results = sweep_all_devices(plugin_manager)

            for device_id, (ok, msg) in results.items():
                if not ok:
                    stats["errors"] += 1
                    logger.warning(
                        "cleanup[%s]: sweep — %s",
                        device_id, msg,
                    )
                    continue

                if "Удалено коммитов:" in msg:
                    try:
                        n = int(
                            msg.split("Удалено коммитов:")[1]
                            .split(";")[0]
                            .strip()
                        )
                        stats["removed"] += n
                    except (IndexError, ValueError):
                        pass
        except Exception as exc:
            stats["errors"] += 1
            logger.error(
                "cleanup: sweep упал: %s",
                exc, exc_info=True,
            )

    if stats["marked"] or stats["removed"] or stats["errors"]:
        logger.info(
            "🧹 Автоочистка завершена: "
            "помечено %d, удалено %d, ошибок %d",
            stats["marked"], stats["removed"], stats["errors"],
        )

    return stats


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