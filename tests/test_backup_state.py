# =========================================================
# tests/test_backup_state.py
# =========================================================
#
# Взаимоисключение бэкапов: массовый прогон (ручной или
# плановый) и одиночные бэкапы не должны пересекаться.
#
# =========================================================

import threading

import pytest

from core import backup_state


def _reset():
    backup_state.finish_mass()
    for d in backup_state.get_backup_status()["devices"]:
        backup_state.finish_device(d)
    for d in backup_state.get_sync_status()["devices"]:
        backup_state.finish_sync(d)


@pytest.fixture(autouse=True)
def clean_state():
    """Каждый тест начинает и заканчивает со свободным состоянием."""
    _reset()
    yield
    _reset()


# =========================================================
# backup_state
# =========================================================

def test_mass_blocks_second_mass_and_devices():
    assert backup_state.try_start_mass() is True
    assert backup_state.try_start_mass() is False
    assert backup_state.try_start_device("mikrotik/0") is False
    assert backup_state.is_backup_running_for("mikrotik/0") is True

    backup_state.finish_mass()
    assert backup_state.try_start_device("mikrotik/0") is True


def test_device_blocks_mass_and_same_device_only():
    assert backup_state.try_start_device("mikrotik/0") is True
    assert backup_state.try_start_device("mikrotik/0") is False
    assert backup_state.try_start_device("mikrotik/1") is True
    assert backup_state.try_start_mass() is False

    backup_state.finish_device("mikrotik/0")
    backup_state.finish_device("mikrotik/1")
    assert backup_state.try_start_mass() is True


def test_concurrent_try_start_mass_only_one_wins():
    barrier = threading.Barrier(20)
    wins = []

    def worker():
        barrier.wait()
        if backup_state.try_start_mass():
            wins.append(1)

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(wins) == 1


# =========================================================
# Планировщик
# =========================================================

class _FakePM:
    def __init__(self):
        self.runs = 0
        self.mass_seen_during_run = None

    def run_all_backups(self):
        self.runs += 1
        # Во время планового прогона слот должен быть занят
        self.mass_seen_during_run = backup_state.get_backup_status()["mass"]


class _OneShotEvent:
    """Даёт _run_loop пройти ровно одну итерацию."""

    def __init__(self):
        self._calls = 0

    def is_set(self):
        self._calls += 1
        return self._calls > 1

    def wait(self, timeout=None):
        return None

    def set(self):
        pass

    def clear(self):
        pass


def _make_scheduler(monkeypatch, pm):
    from core import maintenance
    from core.scheduler import BackupScheduler

    # Не трогаем реальные data/scheduler.json и репозитории
    monkeypatch.setattr(maintenance, "post_backup_cleanup", lambda pm: {})
    cfg = {
        "enabled": True,
        "mode": "interval",
        "interval_hours": 1,
        "last_run": None,
    }
    monkeypatch.setattr(BackupScheduler, "load_config", lambda self: dict(cfg))
    monkeypatch.setattr(BackupScheduler, "save_config", lambda self, c=None: None)

    sched = BackupScheduler(pm)
    sched._stop_event = _OneShotEvent()
    return sched


def test_scheduler_occupies_mass_slot_and_releases(monkeypatch):
    pm = _FakePM()
    sched = _make_scheduler(monkeypatch, pm)

    sched._run_loop()

    assert pm.runs == 1
    assert pm.mass_seen_during_run is True
    assert backup_state.is_backup_running() is False
    assert sched.config["last_run"] is not None


def test_scheduler_waits_while_manual_backup_running(monkeypatch):
    pm = _FakePM()
    sched = _make_scheduler(monkeypatch, pm)

    assert backup_state.try_start_device("mikrotik/0") is True
    sched._run_loop()

    # Ручной бэкап идёт — плановый не стартовал и не
    # считается выполненным (повторится позже)
    assert pm.runs == 0
    assert not sched.config.get("last_run")
    assert backup_state.get_backup_status()["devices"] == ["mikrotik/0"]


# =========================================================
# Синхронизация хранилищ
# =========================================================

def test_sync_blocks_mass_and_same_device_backup():
    assert backup_state.try_start_sync("mikrotik/0") is True
    assert backup_state.try_start_mass() is False
    assert backup_state.try_start_device("mikrotik/0") is False
    assert backup_state.try_start_sync("mikrotik/0") is False
    # Другое устройство — можно
    assert backup_state.try_start_device("mikrotik/1") is True
    assert backup_state.try_start_sync("mikrotik/2") is True

    backup_state.finish_sync("mikrotik/0")
    backup_state.finish_sync("mikrotik/2")
    backup_state.finish_device("mikrotik/1")
    assert backup_state.try_start_mass() is True


def test_mass_and_backup_block_sync():
    assert backup_state.try_start_mass() is True
    assert backup_state.try_start_sync("mikrotik/0") is False
    backup_state.finish_mass()

    assert backup_state.try_start_device("mikrotik/0") is True
    assert backup_state.try_start_sync("mikrotik/0") is False
    assert backup_state.is_sync_running_for("mikrotik/0") is False


# =========================================================
# Зависший поток после общего таймаута прогона
# =========================================================

def test_hung_worker_keeps_device_busy_after_timeout(tmp_path, monkeypatch):
    import core.plugin_manager as pm_mod
    from concurrent.futures import TimeoutError as FuturesTimeoutError
    from core.plugin_manager import PluginManager

    started = threading.Event()
    release = threading.Event()

    def hanging_plugin(router):
        started.set()
        release.wait(10)
        return []

    def instant_timeout(futures, timeout=None):
        # Дождаться старта потока и сразу «истечь»
        started.wait(5)
        raise FuturesTimeoutError()
        yield  # pragma: no cover

    monkeypatch.setattr(pm_mod, "as_completed", instant_timeout)

    pm = PluginManager()
    pm._plugins["fake"] = {
        "func": hanging_plugin,
        "routers": [{"host": "10.0.0.1", "backup_path": str(tmp_path)}],
        "dir": str(tmp_path),
    }

    assert backup_state.try_start_mass() is True
    try:
        pm.run_all_backups()          # вернулся по «таймауту»
    finally:
        backup_state.finish_mass()

    try:
        # Поток ещё работает — устройство занято, новый прогон
        # и бэкап этого устройства не стартуют
        assert backup_state.is_backup_running_for("fake/0") is True
        assert backup_state.try_start_mass() is False
        assert backup_state.try_start_device("fake/0") is False
    finally:
        release.set()

    # Поток завершился — устройство освобождено
    for _ in range(100):
        if not backup_state.is_backup_running():
            break
        threading.Event().wait(0.05)
    assert backup_state.is_backup_running() is False
    assert backup_state.try_start_mass() is True
