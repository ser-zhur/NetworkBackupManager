# =========================================================
# core/backup_state.py
# =========================================================
#
# Общее для всего процесса состояние «какие бэкапы идут».
#
# Используется и веб-интерфейсом (web/server.py), и
# планировщиком (core/scheduler.py) — чтобы плановый
# прогон и ручные запуски видели друг друга и не писали
# одновременно в одни и те же файлы устройства.
#
# Правила:
#   • массовый прогон (mass) — заняты все устройства;
#     стартует, только если не идёт НИ ОДИН бэкап;
#   • одиночный бэкап — занято одно устройство;
#     стартует, если не идёт массовый и это устройство
#     свободно.
#
#   • синхронизация хранилищ устройства — стартует, если
#     не идёт массовый прогон и устройство свободно (ни
#     бэкапа, ни другой синхронизации); массовый прогон
#     не стартует, пока идёт хоть одна синхронизация.
#
# try_start_* атомарно проверяют И занимают слот под одним
# локом — между проверкой и пометкой нет окна, в которое
# мог бы проскочить второй запуск.
#
# Потоки массового прогона дополнительно помечают своё
# устройство (mark_device_running / finish_device). Если
# прогон оборвался по общему таймауту, зависшие потоки
# продолжают держать свои устройства — новый прогон и
# бэкап этих устройств не стартуют, пока они не завершатся.
#
# =========================================================

import threading


_lock = threading.Lock()

_mass_running: bool = False
_active_devices: set[str] = set()
_active_syncs: set[str] = set()


# ---------------------------------------------------------
# ЗАХВАТ / ОСВОБОЖДЕНИЕ
# ---------------------------------------------------------

def try_start_mass() -> bool:
    """
    Занять слот массового прогона.

    True — слот занят, вызывающий ОБЯЗАН вызвать
    finish_mass(). False — уже идёт бэкап (в т.ч. зависшие
    потоки прошлого прогона) или синхронизация.
    """
    global _mass_running
    with _lock:
        if _mass_running or _active_devices or _active_syncs:
            return False
        _mass_running = True
        return True


def finish_mass() -> None:
    global _mass_running
    with _lock:
        _mass_running = False


def try_start_device(device_id: str) -> bool:
    """
    Занять устройство для одиночного бэкапа.

    True — занято, вызывающий ОБЯЗАН вызвать
    finish_device(device_id). False — идёт массовый
    прогон, бэкап или синхронизация этого устройства.
    """
    with _lock:
        if (
            _mass_running
            or device_id in _active_devices
            or device_id in _active_syncs
        ):
            return False
        _active_devices.add(device_id)
        return True


def mark_device_running(device_id: str) -> None:
    """
    Пометить устройство занятым ВНУТРИ массового прогона.

    Без проверок: слот массового прогона уже занят.
    Вызывающий поток ОБЯЗАН вызвать finish_device() в
    finally — даже если сам прогон уже завершился по
    таймауту.
    """
    with _lock:
        _active_devices.add(device_id)


def finish_device(device_id: str) -> None:
    with _lock:
        _active_devices.discard(device_id)


def try_start_sync(device_id: str) -> bool:
    """
    Занять устройство для синхронизации хранилищ.

    True — занято, вызывающий ОБЯЗАН вызвать
    finish_sync(device_id). False — идёт массовый прогон,
    бэкап или синхронизация этого устройства.
    """
    with _lock:
        if (
            _mass_running
            or device_id in _active_devices
            or device_id in _active_syncs
        ):
            return False
        _active_syncs.add(device_id)
        return True


def finish_sync(device_id: str) -> None:
    with _lock:
        _active_syncs.discard(device_id)


# ---------------------------------------------------------
# ЧТЕНИЕ
# ---------------------------------------------------------

def is_backup_running() -> bool:
    """True, если идёт хоть один бэкап (одиночный или массовый)."""
    with _lock:
        return _mass_running or bool(_active_devices)


def is_backup_running_for(device_id: str) -> bool:
    """True, если занято устройство или идёт массовый бэкап."""
    with _lock:
        return _mass_running or device_id in _active_devices


def is_sync_running_for(device_id: str) -> bool:
    with _lock:
        return device_id in _active_syncs


def get_backup_status() -> dict:
    """Снимок состояния для /api/backup-status."""
    with _lock:
        return {
            "running": _mass_running or bool(_active_devices),
            "mass": _mass_running,
            "devices": sorted(_active_devices),
        }


def get_sync_status() -> dict:
    with _lock:
        return {"devices": sorted(_active_syncs)}
