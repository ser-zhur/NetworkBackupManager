# =========================================================
# core/events.py — журнал событий
# =========================================================
#
# Значимые события программы (не строки лога): бэкап устройства
# не удался или восстановился, хранилище с ошибкой, прогон
# завершён, программа запущена.
#
#     events.emit(events.BACKUP_FAILED, "R1: timed out",
#                 level="error", device_id="7f3a9c2e41b0",
#                 name="R1", host="192.0.2.1")
#
# • последние события — в памяти (recent) и в
#   data/events.jsonl (переживают перезапуск; файл
#   подрезается до последних MAX_KEEP строк);
# • подписчики (subscribe) получают каждое событие сразу —
#   на них строятся уведомления. Ошибка подписчика не
#   мешает ни остальным, ни коду, вызвавшему emit().
#
# Формат события:
#     {"id": 17, "at": "2026-11-18 03:00:12", "kind": "backup.failed",
#      "level": "error", "message": "...", "device_id": "...",
#      "data": {...}}
#
# =========================================================

from __future__ import annotations

import json
import logging
import threading
from collections import deque
from typing import Callable

from core import context
from core.device_status import now_str

logger = logging.getLogger(__name__)

# ---- Виды событий ----
APP_STARTED = "app.started"
BACKUP_FAILED = "backup.failed"            # попытка бэкапа устройства не удалась
BACKUP_RECOVERED = "backup.recovered"      # удалась после неудач
STORAGE_FAILED = "storage.failed"          # выгрузка в хранилище с ошибкой
STORAGE_RECOVERED = "storage.recovered"    # хранилище снова в порядке
RUN_FINISHED = "run.finished"              # массовый прогон завершён
SCHEDULER_ERROR = "scheduler.error"        # сбой планировщика

LEVELS = ("info", "warning", "error")

MAX_KEEP = 500          # в памяти и после подрезки файла
MAX_FILE_LINES = 2000   # дальше файл подрезается до MAX_KEEP

_lock = threading.Lock()
_buffer: deque | None = None
_seq = 0
_file_lines = 0
_subscribers: list[Callable[[dict], None]] = []


def _events_file():
    return context.DATA_DIR / "events.jsonl"


def _load() -> None:
    """Последние события из файла (один раз, под _lock)."""
    global _buffer, _seq, _file_lines
    if _buffer is not None:
        return
    _buffer = deque(maxlen=MAX_KEEP)
    lines: list[str] = []
    try:
        lines = _events_file().read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.warning("События: не прочитан %s: %s", _events_file(), exc)
    _file_lines = len(lines)
    for line in lines[-MAX_KEEP:]:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and isinstance(event.get("id"), int):
            _buffer.append(event)
            _seq = max(_seq, event["id"])


def _append_to_file(event: dict) -> None:
    global _file_lines
    path = _events_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if _file_lines >= MAX_FILE_LINES:
            text = "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in _buffer)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(path)
            _file_lines = len(_buffer)
        else:
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")
            _file_lines += 1
    except OSError as exc:
        logger.warning("События: не записан %s: %s", path, exc)


def emit(
    kind: str,
    message: str,
    level: str = "info",
    device_id: str | None = None,
    **data,
) -> dict:
    """Записать событие и разослать подписчикам. Не бросает исключений."""
    global _seq
    if level not in LEVELS:
        level = "info"
    with _lock:
        _load()
        _seq += 1
        event = {
            "id": _seq,
            "at": now_str(),
            "kind": kind,
            "level": level,
            "message": message,
            "device_id": device_id,
            "data": data,
        }
        _buffer.append(event)
        _append_to_file(event)
        subscribers = list(_subscribers)

    for fn in subscribers:
        try:
            fn(event)
        except Exception as exc:
            logger.error("События: ошибка подписчика %r: %s", fn, exc, exc_info=True)
    return event


def recent(
    limit: int = 50,
    kinds=None,
    device_id: str | None = None,
    after_id: int | None = None,
) -> list[dict]:
    """Последние события, новые первыми."""
    with _lock:
        _load()
        items = list(_buffer)
    out = []
    for event in reversed(items):
        if after_id is not None and event["id"] <= after_id:
            break
        if kinds and event.get("kind") not in kinds:
            continue
        if device_id and event.get("device_id") != device_id:
            continue
        out.append(event)
        if len(out) >= limit:
            break
    return out


def last(kind: str) -> dict | None:
    found = recent(limit=1, kinds=(kind,))
    return found[0] if found else None


def subscribe(fn: Callable[[dict], None]) -> None:
    with _lock:
        if fn not in _subscribers:
            _subscribers.append(fn)


def unsubscribe(fn: Callable[[dict], None]) -> None:
    with _lock:
        if fn in _subscribers:
            _subscribers.remove(fn)


def reset() -> None:
    """Забыть загруженное (тесты, смена NBM_HOME). Файл не трогает."""
    global _buffer, _seq, _file_lines
    with _lock:
        _buffer = None
        _seq = 0
        _file_lines = 0
