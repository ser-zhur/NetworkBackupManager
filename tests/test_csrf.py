# =========================================================
# tests/test_csrf.py
# =========================================================
#
# Middleware csrf_protect: изменяющие запросы с чужого
# Origin/Referer отклоняются ДО роутинга и авторизации.
#
# Используется несуществующий путь /__csrf_probe:
#   403 — заблокировано middleware;
#   404/405/401 — middleware пропустил запрос дальше.
# Никакие реальные обработчики не вызываются.
#
# =========================================================

import pytest

pytest.importorskip("httpx2", reason="нужен httpx2 для TestClient")

PROBE = "/__csrf_probe"


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient
    import web.server as s

    # TestClient шлёт Host: testserver
    return TestClient(s.app, raise_server_exceptions=False)


def test_post_without_origin_and_referer_passes(client):
    # curl / скрипты: заголовков нет — не браузер
    assert client.post(PROBE).status_code != 403


def test_post_same_origin_passes(client):
    r = client.post(PROBE, headers={"Origin": "http://testserver"})
    assert r.status_code != 403


def test_post_same_referer_passes(client):
    r = client.post(PROBE, headers={"Referer": "http://testserver/settings"})
    assert r.status_code != 403


@pytest.mark.parametrize("origin", [
    "http://evil.example",
    "http://testserver.evil.example",
    "http://testserver:9999",
    "null",
])
def test_post_foreign_origin_blocked(client, origin):
    r = client.post(PROBE, headers={"Origin": origin})
    assert r.status_code == 403


def test_post_foreign_referer_blocked(client):
    r = client.post(PROBE, headers={"Referer": "http://evil.example/page"})
    assert r.status_code == 403


def test_origin_takes_priority_over_referer(client):
    r = client.post(PROBE, headers={
        "Origin": "http://evil.example",
        "Referer": "http://testserver/",
    })
    assert r.status_code == 403


def test_behind_proxy_forwarded_host_accepted(client):
    # Прокси подменил Host на адрес upstream, исходный — в X-Forwarded-Host
    r = client.post(PROBE, headers={
        "Origin": "https://nbm.example.com",
        "X-Forwarded-Host": "nbm.example.com",
    })
    assert r.status_code != 403


def test_forwarded_host_does_not_whitelist_other_sites(client):
    r = client.post(PROBE, headers={
        "Origin": "https://evil.example",
        "X-Forwarded-Host": "nbm.example.com",
    })
    assert r.status_code == 403


def test_get_with_foreign_origin_not_blocked(client):
    # GET не изменяет состояние — не блокируем
    r = client.get(PROBE, headers={"Origin": "http://evil.example"})
    assert r.status_code != 403


def test_debug_disabled():
    import web.server as s
    assert s.app.debug is False
