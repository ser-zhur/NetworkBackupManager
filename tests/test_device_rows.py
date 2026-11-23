# =========================================================
# tests/test_device_rows.py — статус в списке без перезагрузки
# =========================================================
#
# Главная отрисовывает строки устройств один раз. После бэкапа
# /api/backup-status сообщает новую state_version, и страница
# берёт свежие строки из /api/device-rows.
#
# =========================================================

import json

import pytest

from core import device_status


@pytest.fixture
def web(tmp_path, monkeypatch):
    s = pytest.importorskip("web.server")
    pytest.importorskip("httpx2", reason="нужен httpx2 для TestClient")
    from fastapi.testclient import TestClient

    state_file = tmp_path / "state.json"
    monkeypatch.setattr(s, "STATE_FILE", state_file)
    router = {"id": "0123456789ab", "host": "192.0.2.10", "name": "R1",
              "backup_path": str(tmp_path / "backups" / "R1")}
    for pd in s.plugin_manager._plugins.values():
        monkeypatch.setitem(pd, "routers", [])
    monkeypatch.setitem(s.plugin_manager._plugins["ssh"], "routers", [router])
    s.app.dependency_overrides[s.require_auth] = lambda: "test"
    yield s, TestClient(s.app), state_file
    s.app.dependency_overrides.clear()


def write_state(path, ok, error=""):
    entry = device_status.record_attempt({}, ok=ok, error=error)
    if ok:
        entry["last_run"] = entry["attempt"]["at"]
        entry["backends"] = {"git": {"ok": True, "message": "", "skipped": True}}
    path.write_text(json.dumps({"0123456789ab": entry}), encoding="utf-8")


def row(client):
    (r,) = client.get("/api/device-rows").json()["rows"]
    return r


def test_status_follows_state_json(web):
    s, client, state = web

    write_state(state, ok=False, error="SSH: нет связи")
    v1 = client.get("/api/backup-status").json()["state_version"]
    bad = row(client)
    assert bad["status"] == "fail" and "Ошибка" in bad["html"] and "✗" in bad["html"]
    assert "SSH: нет связи" in bad["html"]

    # Ошибку устранили — бэкап прошёл
    write_state(state, ok=True)
    v2 = client.get("/api/backup-status").json()["state_version"]
    good = row(client)
    assert v2 != v1
    assert good["status"] == "ok" and "Актуальный" in good["html"] and "✓" in good["html"]
    assert good["sig"] != bad["sig"]
    assert good["last_attempt_age"] == 0 and good["overdue"] is False


def test_main_page_row_matches_api(web):
    s, client, state = web
    write_state(state, ok=True)
    page = client.get("/").text
    r = row(client)
    # Строка на странице — из того же шаблона, с тем же отпечатком
    assert f'data-sig="{r["sig"]}"' in page
    assert 'data-status="ok"' in page
    assert "refreshDeviceRows" in page


def test_state_version_without_file(web):
    s, client, state = web
    assert client.get("/api/backup-status").json()["state_version"] == "0"
    assert row(client)["status"] == "none"
