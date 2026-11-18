# =========================================================
# core/monitoring.py — данные для внешнего мониторинга
# =========================================================
#
#   GET /api/health   без пароля: жива ли программа (Docker
#                     healthcheck, простая проверка доступности);
#   GET /api/status   по токену или логину/паролю: сводка для
#                     Zabbix и аналогов (устройства, планировщик,
#                     последний прогон, проблемы).
#
# Токен мониторинга — отдельный от пароля администратора и
# даёт только чтение состояния. Хранится его SHA-256
# (data/monitoring.json): сам токен показывается один раз
# при создании. NBM_API_TOKEN из окружения (Docker) действует
# наравне с созданным в интерфейсе.
#
# =========================================================

from __future__ import annotations

import hashlib
import secrets
import time
from datetime import datetime, timedelta

from core import backup_state, context, device_status, events, settings
from core.version import APP_NAME, __version__

# Плановый бэкап не начался через столько после срока —
# планировщик «просрочен» (сломан или всё время занят).
SCHEDULER_GRACE = timedelta(hours=1)

# Числовая тяжесть статуса устройства — для графиков и
# триггеров: 0 — норма, 1 — внимание, 2 — проблема.
SEVERITY = {"ok": 0, "disabled": 0, "warn": 1, "none": 1, "fail": 2}

_started_monotonic = time.monotonic()
_started_at = datetime.now()


def mark_started() -> None:
    global _started_monotonic, _started_at
    _started_monotonic = time.monotonic()
    _started_at = datetime.now()


def uptime_seconds() -> int:
    return int(time.monotonic() - _started_monotonic)


# =========================================================
# Токен
# =========================================================

def _token_file():
    return context.DATA_DIR / "monitoring.json"


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def token_info() -> dict:
    data = context.load_json(_token_file(), {})
    return {
        "configured": bool(data.get("token_sha256")),
        "created": data.get("created") or None,
        "from_env": bool(settings.env("NBM_API_TOKEN")),
    }


def generate_token() -> str:
    """Новый токен (прежний перестаёт действовать). Вернуть пользователю один раз."""
    token = secrets.token_urlsafe(32)
    context.save_json(_token_file(), {
        "token_sha256": _sha256(token),
        "created": device_status.now_str(),
    })
    return token


def revoke_token() -> None:
    context.save_json(_token_file(), {})


def check_token(value: str) -> bool:
    if not value:
        return False
    env_token = settings.env("NBM_API_TOKEN")
    if env_token and secrets.compare_digest(
        value.encode("utf-8"), env_token.encode("utf-8"),
    ):
        return True
    stored = context.load_json(_token_file(), {}).get("token_sha256") or ""
    return bool(stored) and secrets.compare_digest(_sha256(value), stored)


# =========================================================
# Здоровье
# =========================================================

def health(scheduler=None) -> tuple[bool, dict]:
    """
    (жива ли, ответ). Не жива — поток планировщика упал
    или в папку данных нельзя писать.
    """
    problems = []
    if scheduler is not None and not scheduler.is_alive():
        problems.append("Планировщик не работает")
    try:
        context.DATA_DIR.mkdir(parents=True, exist_ok=True)
        probe = context.DATA_DIR / ".health"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        problems.append(f"Папка данных недоступна для записи: {exc}")

    return not problems, {
        "status": "ok" if not problems else "error",
        "version": __version__,
        "uptime_s": uptime_seconds(),
        "problems": problems,
    }


# =========================================================
# Сводка /api/status
# =========================================================

def _fmt(dt: datetime | None) -> str | None:
    return dt.strftime(device_status.TIME_FORMAT) if dt else None


def _scheduler_block(scheduler, now: datetime) -> dict:
    if scheduler is None:
        return {"enabled": False, "alive": False, "overdue": False}
    cfg = scheduler.config or {}
    next_run = scheduler.next_run(now)
    running = scheduler.is_job_running() or backup_state.is_backup_running()
    overdue = bool(
        next_run is not None
        and not running
        and now - next_run > SCHEDULER_GRACE
    )
    return {
        "enabled": bool(cfg.get("enabled")),
        "alive": scheduler.is_alive(),
        "mode": cfg.get("mode", "daily"),
        "daily_time": cfg.get("daily_time"),
        "interval_hours": cfg.get("interval_hours"),
        "last_run": cfg.get("last_run"),
        "next_run": _fmt(next_run),
        "running": scheduler.is_job_running(),
        "overdue": overdue,
    }


def _devices(plugin_manager, now: datetime) -> list[dict]:
    if plugin_manager is None:
        return []
    from core.plugin_manager import is_device_disabled

    state = context.load_json(context.STATE_FILE, {})
    if not isinstance(state, dict):
        state = {}

    out = []
    with plugin_manager.routers_lock:
        items = [
            (device_type, router)
            for device_type, pd in plugin_manager._plugins.items()
            for router in pd.get("routers", [])
        ]
    for device_type, router in items:
        device_id = plugin_manager.router_id(router)
        disabled = is_device_disabled(router)
        h = device_status.summarize(state.get(device_id), disabled=disabled, now=now)
        out.append({
            "id": device_id,
            "name": router.get("name") or "",
            "type": device_type,
            "host": router.get("host") or router.get("ip") or "",
            "disabled": disabled,
            "status": h["status"],
            "severity": SEVERITY.get(h["status"], 1),
            "last_success": h["last_success"],
            "success_age_days": h["success_age_days"],
            "last_attempt": h["last_attempt"],
            "last_attempt_ok": h["last_attempt_ok"],
            "failures": h["failures"],
            "error": h["last_error"],
            "running": backup_state.is_backup_running_for(device_id),
        })
    return out


def status(plugin_manager=None, scheduler=None, now: datetime | None = None) -> dict:
    now = now or datetime.now()
    devices = _devices(plugin_manager, now)
    counts = {s: 0 for s in device_status.STATUSES}
    for d in devices:
        counts[d["status"]] = counts.get(d["status"], 0) + 1

    sched = _scheduler_block(scheduler, now)
    run = events.last(events.RUN_FINISHED)
    last_run = dict(run["data"], message=run["message"]) if run else None
    backup = backup_state.get_backup_status()

    # ---- Проблемы и общий итог ----
    problems: list[str] = []
    warnings: list[str] = []
    if scheduler is not None and not sched["alive"]:
        problems.append("Планировщик не работает")
    if sched.get("overdue"):
        problems.append(f"Плановый бэкап не начался (срок {sched['next_run']})")
    for d in devices:
        label = d["name"] or d["host"] or d["id"]
        if d["status"] == "fail":
            problems.append(f"{label}: {d['error'] or 'давно нет успешного бэкапа'}")
        elif d["status"] == "warn":
            warnings.append(f"{label}: бэкап {d['success_age_days']} дн. назад")
        elif d["status"] == "none":
            warnings.append(f"{label}: бэкапов ещё не было")

    overall = "error" if problems else ("warning" if warnings else "ok")

    return {
        "app": {
            "name": APP_NAME,
            "version": __version__,
            "started": _fmt(_started_at),
            "uptime_s": uptime_seconds(),
            "container": settings.in_container(),
        },
        "status": overall,
        "severity": {"ok": 0, "warning": 1, "error": 2}[overall],
        "problems": problems,
        "warnings": warnings,
        "scheduler": sched,
        "running": {
            "backup": backup["running"],
            "mass": backup["mass"],
            "devices": backup["devices"],
            "syncs": backup_state.get_sync_status()["devices"],
        },
        "last_run": last_run,
        "counts": {
            "total": len(devices),
            "enabled": len(devices) - counts["disabled"],
            **counts,
        },
        "devices": devices,
        "generated": _fmt(now),
    }
