# =========================================================
# tests/test_managed_service.py — служба, установленная install.sh
# =========================================================
#
# NBM_MANAGED_BY=systemd: программа работает не от root —
# остановка и удаление службы из интерфейса не предлагаются,
# перезапуск — выходом с кодом 1 (systemd поднимает снова).
#
# =========================================================

import pytest

from core import service_installer as si
from core import settings


@pytest.fixture
def systemd(monkeypatch):
    monkeypatch.setenv("NBM_MANAGED_BY", "systemd")
    monkeypatch.setenv("NBM_CONTAINER", "0")
    monkeypatch.setattr(si, "_get_service_state_snapshot", lambda: (True, True, "active"))
    si.invalidate_status_cache()
    yield
    si.invalidate_status_cache()


def test_service_manager(monkeypatch):
    monkeypatch.setenv("NBM_CONTAINER", "0")
    monkeypatch.delenv("NBM_MANAGED_BY", raising=False)
    assert settings.service_manager() == ""
    monkeypatch.setenv("NBM_MANAGED_BY", "SystemD")
    assert settings.service_manager() == "systemd"
    monkeypatch.setenv("NBM_CONTAINER", "1")
    assert settings.service_manager() == "docker"


def test_status_systemd(systemd):
    st = si.get_status(use_cache=False)
    assert st["managed_by"] == "systemd" and st["registered"] and st["os_running"]
    assert st["can_restart"] and st["service_mode"]
    assert not (st["can_stop"] or st["can_start"] or st["can_uninstall"] or st["can_install"])
    assert st["service_name"] == si.LINUX_UNIT


@pytest.mark.parametrize("action", [
    si.install_service, si.start_service, si.stop_service,
    si.uninstall_service, si.schedule_stop, si.schedule_uninstall,
])
def test_actions_refused(systemd, action):
    ok, message = action()
    assert not ok and "install.sh" in message and "systemctl" in message


def test_restart_exits_with_code_1(systemd, monkeypatch):
    codes = []

    class SyncThread:
        def __init__(self, target, daemon=None):
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr(si.threading, "Thread", SyncThread)
    monkeypatch.setattr(si.time, "sleep", lambda s: None)
    monkeypatch.setattr(si.os, "_exit", codes.append)
    monkeypatch.setattr(si.logging, "shutdown", lambda: None)
    ok, message = si.schedule_restart()
    assert ok and "перезапускается" in message and codes == [1]


def test_docker_refuses_restart(monkeypatch):
    monkeypatch.setenv("NBM_CONTAINER", "1")
    ok, message = si.schedule_restart()
    assert not ok and "Docker" in message


def test_service_tab_systemd(systemd):
    s = pytest.importorskip("web.server")
    pytest.importorskip("httpx2", reason="нужен httpx2 для TestClient")
    from fastapi.testclient import TestClient

    s.app.dependency_overrides[s.require_auth] = lambda: "test"
    try:
        page = TestClient(s.app).get("/settings?tab=service").text
    finally:
        s.app.dependency_overrides.clear()
    assert "install.sh" in page and "journalctl -u networkbackupmanager.service" in page
    assert 'action="/service/restart"' in page
    assert 'action="/service/uninstall"' not in page and 'action="/service/stop"' not in page
    assert 'action="/service/install"' not in page
