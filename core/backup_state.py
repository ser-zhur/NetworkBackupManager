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
# try_start_* атомарно проверяют И занимают слот под одним
# локом — между проверкой и пометкой нет окна, в которое
# мог бы проскочить второй запуск.
#
# =========================================================

import threading


_lock = threading.Lock()

_mass_running: bool = False
_active_devices: set[str] = set()


# ---------------------------------------------------------
# ЗАХВАТ / ОСВОБОЖДЕНИЕ
# ---------------------------------------------------------

def try_start_mass() -> bool:
    """
    Занять слот массового прогона.

    True — слот занят, вызывающий ОБЯЗАН вызвать
    finish_mass(). False — уже идёт какой-то бэкап.
    """
    global _mass_running
    with _lock:
        if _mass_running or _active_devices:
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
    прогон или бэкап этого устройства.
    """
    with _lock:
        if _mass_running or device_id in _active_devices:
            return False
        _active_devices.add(device_id)
        return True


def finish_device(device_id: str) -> None:
    with _lock:
        _active_devices.discard(device_id)


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


def get_backup_status() -> dict:
    """Снимок состояния для /api/backup-status."""
    with _lock:
        return {
            "running": _mass_running or bool(_active_devices),
            "mass": _mass_running,
            "devices": sorted(_active_devices),
        }
