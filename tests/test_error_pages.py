# =========================================================
# tests/test_error_pages.py — страницы ошибок
# =========================================================
#
# Браузер (Accept: text/html) получает страницу ошибки,
# API и fetch — JSON, как раньше.
#
# =========================================================

import json

import pytest

pytest.importorskip("httpx2", reason="нужен httpx2 для TestClient")
s = pytest.importorskip("web.server")

from fastapi.testclient import TestClient  # noqa: E402

from tests.test_auth_and_download import _hash  # noqa: E402

HTML = {"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}
BOOM = "/__error_probe_boom"


@pytest.fixture
def client():
    def boom():
        raise RuntimeError("секрет C:\\путь\\к\\данным")

    s.app.add_api_route(BOOM, boom, methods=["GET"])
    s.app.dependency_overrides[s.require_auth] = lambda: "test"
    try:
        yield TestClient(s.app, raise_server_exceptions=False)
    finally:
        s.app.dependency_overrides.clear()
        s.app.router.routes[:] = [r for r in s.app.router.routes
                                  if getattr(r, "path", None) != BOOM]


def test_missing_device_is_html_page_for_browser(client):
    r = client.get("/device/000000000000/git", headers=HTML)
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("text/html")
    assert "Не найдено" in r.text and "Устройство не найдено" in r.text
    assert 'href="/"' in r.text and "/logs/main" not in r.text


def test_missing_device_is_json_for_fetch_and_api(client):
    r = client.get("/device/000000000000/git")              # Accept: */*
    assert r.status_code == 404 and r.json() == {"detail": "Устройство не найдено"}
    r = client.get("/api/__nope", headers=HTML)              # API — всегда JSON
    assert r.status_code == 404 and r.json() == {"detail": "Not Found"}


def test_unknown_page_hides_english_phrase(client):
    r = client.get("/no/such/page", headers=HTML)
    assert r.status_code == 404 and "Не найдено" in r.text
    assert "Not Found" not in r.text


def test_method_not_allowed(client):
    r = client.get("/settings/general/save", headers=HTML)
    assert r.status_code == 405 and "не открывается напрямую" in r.text
    assert "allow" in {k.lower() for k in r.headers}


def test_unhandled_error_page_hides_exception_and_is_logged(client, caplog):
    r = client.get(BOOM, headers=HTML)
    assert r.status_code == 500 and "Внутренняя ошибка" in r.text
    assert "секрет" not in r.text and "/logs/main" in r.text
    assert any("секрет" in rec.getMessage() and rec.exc_info for rec in caplog.records)

    r = client.get(BOOM)
    assert r.status_code == 500 and "секрет" not in r.text
    assert "журнале" in r.json()["detail"]


def test_401_keeps_basic_challenge(tmp_path, monkeypatch):
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"username": "admin", "password_hash": _hash("pw")}),
                    encoding="utf-8")
    monkeypatch.setattr(s, "AUTH_FILE", auth)
    c = TestClient(s.app)
    r = c.get("/", headers=HTML)
    assert r.status_code == 401 and "Basic" in r.headers["www-authenticate"]
    assert "Нужен вход" in r.text
    assert s.__version__ not in r.text                       # версию до входа не показываем
    r = c.get("/api/backup-status")
    assert r.status_code == 401 and "detail" in r.json()


def test_csrf_rejection_page(client):
    r = client.post("/settings/general/save",
                    headers={**HTML, "Origin": "http://evil.example"})
    assert r.status_code == 403 and "Доступ запрещён" in r.text
    assert "с другого сайта" in r.text
