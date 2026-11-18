# =========================================================
# core/device_status.py — состояние устройства по state.json
# =========================================================
#
# Запись устройства в data/state.json (ключ — id устройства):
#
#     {
#       "last_run": "2026-11-18 03:00:12",   последний УСПЕШНЫЙ опрос
#       "backends": {"git": {"ok": true, "message": "", "skipped": false}},
#       "total_commits": 42,
#       "attempt": {                          последняя ПОПЫТКА
#         "at": "2026-11-19 03:00:05",
#         "ok": false,
#         "error": "timed out",
#         "failures": 1,                      неудач подряд
#         "duration_s": 30.2
#       }
#     }
#
# last_run и backends пишет выгрузка в хранилища (только
# после успешного опроса), attempt — каждый запуск бэкапа,
# удачный или нет. Без attempt упавший бэкап не оставлял
# следа, и устройство ещё 14 дней числилось «Актуальным».
#
# Один источник правды для веб-интерфейса и /api/status.
#
# =========================================================

from __future__ import annotations

from datetime import datetime

TIME_FORMAT = "%Y-%m-%d %H:%M:%S"

# Порог «Актуально» / «Давно» (дней с последнего успешного опроса).
OK_DAYS = 14
WARN_DAYS = 30

STATUSES = ("ok", "warn", "fail", "none", "disabled")


def now_str() -> str:
    return datetime.now().strftime(TIME_FORMAT)


def parse_time(value) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.strptime(value, TIME_FORMAT)
    except ValueError:
        return None


def age_days(value, now: datetime | None = None) -> int | None:
    dt = parse_time(value)
    if dt is None:
        return None
    return ((now or datetime.now()) - dt).days


def classify(success_age_days, last_attempt_ok) -> str:
    """
    ok / warn / fail / none.

    success_age_days — давность последнего успешного опроса
    (а не git-коммита: если изменений нет, коммита нет, но
    устройство исправно опрашивается).
    """
    if last_attempt_ok is False:
        return "fail"
    if success_age_days is None:
        return "none"
    if success_age_days <= OK_DAYS:
        return "ok"
    if success_age_days <= WARN_DAYS:
        return "warn"
    return "fail"


def summarize(entry, disabled: bool = False, now: datetime | None = None) -> dict:
    """
    Запись state.json → поля для интерфейса и мониторинга:

      last_success        время последнего успешного опроса или None
      success_age_days    его давность в днях
      last_attempt        время последней попытки (успешной или нет)
      last_attempt_ok     None — попыток не было; False — бэкап или
                          хоть одно хранилище с ошибкой
      backup_ok           None / исход самого опроса устройства
      backends_ok         None / все хранилища без ошибок
      last_error          текст ошибки последней попытки ('' — нет)
      failures            неудачных попыток подряд
      status              ok / warn / fail / none / disabled
    """
    st = entry if isinstance(entry, dict) else {}
    attempt = st.get("attempt") if isinstance(st.get("attempt"), dict) else {}
    backends = st.get("backends") if isinstance(st.get("backends"), dict) else {}

    last_success = st.get("last_run") or None
    success_age = age_days(last_success, now)

    backends_ok = (
        all(bool(b.get("ok")) for b in backends.values() if isinstance(b, dict))
        if backends else (True if last_success else None)
    )

    if attempt:
        backup_ok = bool(attempt.get("ok"))
        last_attempt = attempt.get("at") or last_success
    else:
        # Записи до появления attempt: была только успешная выгрузка.
        backup_ok = True if last_success else None
        last_attempt = last_success

    if backup_ok is None:
        last_attempt_ok = None
    else:
        last_attempt_ok = backup_ok and backends_ok is not False

    failures = attempt.get("failures") if not backup_ok else 0
    if not isinstance(failures, int) or failures < 0:
        failures = 0

    last_error = ""
    if backup_ok is False:
        last_error = str(attempt.get("error") or "Ошибка бэкапа")
    elif backends_ok is False:
        errors = [
            f"{bid}: {b.get('message') or 'ошибка'}"
            for bid, b in backends.items()
            if isinstance(b, dict) and not b.get("ok")
        ]
        last_error = "; ".join(errors)

    return {
        "last_success": last_success,
        "success_age_days": success_age,
        "last_attempt": last_attempt,
        "last_attempt_ok": last_attempt_ok,
        "backup_ok": backup_ok,
        "backends_ok": backends_ok if backends or last_success else None,
        "last_error": last_error,
        "failures": failures,
        "status": "disabled" if disabled else classify(success_age, last_attempt_ok),
    }


def record_attempt(
    entry: dict | None,
    ok: bool,
    error: str = "",
    duration_s: float | None = None,
    at: str | None = None,
) -> dict:
    """Запись устройства с обновлённым attempt (entry не меняется)."""
    entry = dict(entry) if isinstance(entry, dict) else {}
    prev = entry.get("attempt") if isinstance(entry.get("attempt"), dict) else {}
    prev_failures = prev.get("failures") if isinstance(prev.get("failures"), int) else 0

    attempt = {
        "at": at or now_str(),
        "ok": bool(ok),
        "error": "" if ok else (str(error or "Ошибка бэкапа")[:500]),
        "failures": 0 if ok else prev_failures + 1,
    }
    if duration_s is not None:
        attempt["duration_s"] = round(float(duration_s), 1)
    entry["attempt"] = attempt
    return entry
